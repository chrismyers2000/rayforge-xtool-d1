"""
xTool D1 Wi-Fi driver: upload a job, run it on the machine.

The D1 serves a small HTTP API on port 8080 and can store a G-code file
and run it with its own streamer. This driver only uses that:

* ``GET /ping`` checks the machine is reachable.
* ``POST /cnc/data?filetype=1`` uploads the job (it replaces the one
  stored file, ``/tmp.gcode``).
* The job is then started from the machine's own button, the only start
  and stop verified on a D1. Optionally the driver can start it with
  ``GET /read?file=/tmp.gcode``, which was seen to start the stored job
  but is not documented anywhere.
* ``GET /cmd?cmd=<line>`` runs one G-code line (used by the console).
* ``GET /system?action=version`` on connect, to see what the firmware has.

Firmware 40.30.010.01 and newer also have ``GET /cnc/data?action=pause|
resume|stop`` and ``GET /system?action=get_working_sta`` (0 idle, 1 running
from ``/read``, 2 running from the button; documented for the D1 Pro by
github.com/1RandomDev/xTool-Connect). On those the driver reports whether
a job runs, and Rayforge's Pause and Cancel use them. On older firmware
there is no status or software stop: use the machine's button or its
power switch.

There is no position, jogging or homing: the D1 only reports position on
its serial port.
"""

import asyncio
import inspect
import logging
import re
from dataclasses import replace
from collections.abc import Awaitable, Callable
from gettext import gettext as _
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

import aiohttp

from rayforge.core.varset import BoolVar, HostnameVar, PortVar, VarSet
from rayforge.core.varset.hostnamevar import is_valid_hostname_or_ip
from rayforge.machine.driver.driver import (
    Axis,
    DeviceConnectionError,
    DeviceStatus,
    Driver,
    DriverMaturity,
    DriverPrecheckError,
    DriverSetupError,
    Pos,
)
from rayforge.machine.transport import TransportStatus
from rayforge.pipeline.encoder.base import EncodedOutput, OpsEncoder
from rayforge.pipeline.encoder.gcode import GcodeEncoder

from .firmware import Firmware, has_job_control, parse_version
from .smooth import needs_smoothing, smooth_moves

if TYPE_CHECKING:
    from raygeo.ops import Ops

    from rayforge.context import RayforgeContext
    from rayforge.core.doc import Doc
    from rayforge.machine.models.laser import Laser
    from rayforge.machine.models.machine import Machine

logger = logging.getLogger(__name__)

DEFAULT_PORT = 8080
PING_INTERVAL_S = 5.0
# With get_working_sta the poll is also the job status, so it runs faster.
STATUS_INTERVAL_S = 1.0
HTTP_TIMEOUT_S = 5.0
UPLOAD_TIMEOUT_S = 60.0
STORED_FILE = "/tmp.gcode"
#: A copy of the last uploaded job, so it can be read before it is run.
LAST_JOB_NAME = "xtool-d1-last-job.gcode"


class XToolD1WifiDriver(Driver):
    label = _("xTool D1 (Wi-Fi)")
    subtitle = _(
        "Uploads the job to the D1 over Wi-Fi; start it on the machine"
    )
    supports_settings = False
    reports_granular_progress = False
    maturity = DriverMaturity.EXPERIMENTAL

    def __init__(self, context: "RayforgeContext", machine: "Machine"):
        super().__init__(context, machine)
        self.host = ""
        self.port = DEFAULT_PORT
        self.auto_start = False
        self.return_home = False
        self.keep_running = False
        self._ping_task: asyncio.Task | None = None
        self._framing = False
        self.firmware: Firmware | None = None
        self._is_paused = False

    @property
    def machine_space_wcs(self) -> str:
        return "G53"

    @property
    def machine_space_wcs_display_name(self) -> str:
        return _("Machine Coordinates")

    @property
    def resource_uri(self) -> str | None:
        return f"http://{self.host}:{self.port}" if self.host else None

    @property
    def _base(self) -> str:
        return f"http://{self.host}:{self.port}"

    @classmethod
    def precheck(cls, **kwargs: Any) -> None:
        host = str(kwargs.get("host", ""))
        if not is_valid_hostname_or_ip(host):
            raise DriverPrecheckError(
                _("Invalid hostname or IP address: '{host}'").format(host=host)
            )

    @classmethod
    def get_setup_vars(cls) -> VarSet:
        return VarSet(
            vars=[
                HostnameVar(
                    key="host",
                    label=_("Hostname"),
                    description=_("IP address of the D1 on your network"),
                ),
                PortVar(
                    key="port",
                    label=_("HTTP Port"),
                    description=_("The D1 serves its API on port 8080"),
                    default=DEFAULT_PORT,
                ),
                BoolVar(
                    key="auto_start",
                    label=_("Start jobs automatically"),
                    description=_(
                        "WARNING: the cutting laser may turn on during "
                        "framing, even though the job never asks for it. "
                        "Starts the uploaded job from here instead of the "
                        "machine's button. Not documented by xTool, so off "
                        "by default; the button is the safe way to start "
                        "and stop a job."
                    ),
                    default=False,
                ),
                BoolVar(
                    key="return_home",
                    label=_("Return to origin after a job"),
                    description=_(
                        "Moves the head back to X0 Y0, laser off, when a "
                        "job ends. A cancelled job does not return."
                    ),
                    default=False,
                ),
            ]
        )

    @classmethod
    def create_encoder(cls, machine: "Machine") -> OpsEncoder:
        assert machine.dialect is not None
        return GcodeEncoder(machine.dialect)

    def _setup_implementation(self, **kwargs: Any) -> None:
        host = str(kwargs.get("host", "")).strip()
        if not host:
            raise DriverSetupError(_("Hostname must be configured."))
        self.host = host
        self.port = int(kwargs.get("port", DEFAULT_PORT))
        self.auto_start = bool(kwargs.get("auto_start", False))
        self.return_home = bool(kwargs.get("return_home", False))

    async def cleanup(self) -> None:
        self.keep_running = False
        if self._ping_task and not self._ping_task.done():
            self._ping_task.cancel()
        self._ping_task = None
        await super().cleanup()

    # -- connection ----------------------------------------------------

    async def _connect_implementation(self) -> None:
        if self._ping_task and not self._ping_task.done():
            self._ping_task.cancel()
        self.keep_running = True
        await self._read_firmware()
        self._ping_task = asyncio.ensure_future(self._ping_loop())
        await self._enter_ack_mode()

    async def _read_firmware(self) -> None:
        try:
            reply = await self._get("/system?action=version")
        except DeviceConnectionError as e:
            logger.warning("Could not read the D1's firmware version: %s", e)
            return
        self.firmware = parse_version(reply)
        logger.info(
            "D1 firmware: %s; Wi-Fi pause/stop %s",
            self.firmware or "unknown",
            "available" if has_job_control(self.firmware) else "not available",
        )

    def _update_connection_status(
        self, status: TransportStatus, message: str = ""
    ) -> None:
        self._publish_state(
            status=DeviceStatus.IDLE
            if status == TransportStatus.CONNECTED
            else DeviceStatus.UNKNOWN
        )
        self.connection_status_changed.send(
            self, status=status, message=message
        )

    def _set_device_status(self, status: DeviceStatus) -> None:
        if status != self.state.status:
            self._publish_state(status=status)

    def _publish_state(self, **changes: Any) -> None:
        """Hand Rayforge a new DeviceState with *changes*. Rayforge only
        passes a state on when it differs from the object it already
        holds, so a state edited in place is never seen."""
        self.state = replace(self.state, **changes)
        self.state_changed.send(self, state=self.state)

    async def _poll_once(self) -> DeviceStatus:
        """One reachability check; on job-control firmware also the job
        status. Raises DeviceConnectionError when the D1 cannot be reached."""
        if not has_job_control(self.firmware):
            reply = await self._get("/ping")
            if '"ok"' not in reply:
                raise DeviceConnectionError(f"Unexpected: {reply}")
            return DeviceStatus.IDLE
        reply = await self._get("/system?action=get_working_sta")
        match = re.search(r'"working"\s*:\s*"?(\d+)', reply)
        if not match:
            raise DeviceConnectionError(f"Unexpected: {reply}")
        working = match.group(1)
        if working == "0":
            self._is_paused = False
            return DeviceStatus.IDLE
        if working not in ("1", "2"):
            logger.info("D1 reports working state %s", working)
        return DeviceStatus.HOLD if self._is_paused else DeviceStatus.RUN

    async def _ping_loop(self) -> None:
        last = None
        try:
            while self.keep_running:
                if self.firmware is None:
                    await self._read_firmware()
                device_status = None
                try:
                    device_status = await self._poll_once()
                    status, message = TransportStatus.CONNECTED, "Connected"
                except (DeviceConnectionError, asyncio.TimeoutError) as e:
                    status = TransportStatus.SLEEPING
                    message = str(e) or "No answer"
                if (status, message) != last:
                    self._update_connection_status(status, message)
                    last = (status, message)
                if device_status is not None:
                    self._set_device_status(device_status)
                await asyncio.sleep(
                    STATUS_INTERVAL_S
                    if has_job_control(self.firmware)
                    else PING_INTERVAL_S
                )
        except asyncio.CancelledError:
            pass

    # -- HTTP ----------------------------------------------------------

    async def _get(self, path: str) -> str:
        url = f"{self._base}{path}"
        try:
            async with (
                aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=HTTP_TIMEOUT_S)
                ) as session,
                session.get(url) as response,
            ):
                return await response.text()
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            raise DeviceConnectionError(
                f"Cannot reach the D1 at {self.host}: {e}"
            ) from e

    async def _upload(self, gcode: str) -> None:
        form = aiohttp.FormData()
        form.add_field(
            "file",
            gcode.encode("utf-8"),
            filename="rayforge.gcode",
            content_type="application/octet-stream",
        )
        url = f"{self._base}/cnc/data?filetype=1"
        try:
            async with (
                aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=UPLOAD_TIMEOUT_S)
                ) as session,
                session.post(url, data=form) as response,
            ):
                reply = await response.text()
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            raise DeviceConnectionError(
                f"Upload to the D1 at {self.host} failed: {e}"
            ) from e
        if '"ok"' not in reply:
            raise DeviceConnectionError(f"The D1 refused the upload: {reply}")

    def _keep_copy(self, gcode: str) -> None:
        lines = gcode.splitlines()
        laser_on = sum(1 for ln in lines if ln.lstrip().startswith("M3"))
        from rayforge.config import CONFIG_DIR

        copy = CONFIG_DIR / LAST_JOB_NAME
        try:
            copy.write_text(gcode)
        except OSError as e:
            logger.warning("Could not save a copy of the job: %s", e)
        logger.warning(
            "Uploaded %d lines (%d bytes) to the D1; %d laser-on (M3) lines. "
            "Copy: %s",
            len(lines), len(gcode), laser_on, copy,
        )

    async def _enter_ack_mode(self) -> None:
        """Send ``$I``: after a power cycle the D1's button only starts a
        stored job once it has had this (the USB driver sends it too).
        A button stop drops it back, so this is repeated after uploads."""
        try:
            await self._command("$I")
        except DeviceConnectionError as e:
            logger.warning("Could not send $I to the D1: %s", e)

    @staticmethod
    def _toast(message: str) -> None:
        """Show a message in the main window; never raises."""
        try:
            import gi

            gi.require_version("Adw", "1")
            gi.require_version("Gtk", "4.0")
            from gi.repository import Adw, GLib, Gtk

            def show() -> bool:
                app = Gtk.Application.get_default()
                win = app.get_active_window() if app else None
                overlay = getattr(win, "toast_overlay", None)
                if overlay is not None:
                    toast = Adw.Toast.new(message)
                    toast.set_timeout(8)
                    overlay.add_toast(toast)
                return False

            GLib.idle_add(show)
        except Exception:
            logger.debug("Could not show a toast", exc_info=True)

    async def _command(self, line: str) -> None:
        reply = await self._get(f"/cmd?cmd={quote(line, safe='')}")
        if '"ok"' not in reply:
            raise DeviceConnectionError(f"The D1 rejected '{line}': {reply}")

    # -- jobs ----------------------------------------------------------

    async def run(
        self,
        encoded: EncodedOutput,
        doc: "Doc",
        ops: "Ops",
        on_command_done: Callable[[int], None | Awaitable[None]] | None = None,
    ) -> None:
        try:
            if on_command_done is not None and encoded.op_map:
                for op_index in range(encoded.op_map.op_count):
                    result = on_command_done(op_index)
                    if inspect.isawaitable(result):
                        await result
            text = (
                self._crosshair_on(encoded.text)
                if self._framing
                else encoded.text
            )
            if self.return_home and not self._framing:
                text = self._return_home(text)
            if needs_smoothing(self.firmware):
                text = smooth_moves(text)
            await self._upload(text)
            self._keep_copy(text)
            await self._enter_ack_mode()
            if self.auto_start:
                await self._get(f"/read?file={quote(STORED_FILE, safe='/')}")
                self._toast(_("Job uploaded and started on the D1."))
                logger.info("Job uploaded and started on the D1.")
            else:
                self._toast(
                    _("Job uploaded to the D1. Press its button to start.")
                )
                logger.warning(
                    "Job uploaded. Press the button on the D1 to start it. "
                    "Stop it with Rayforge's Cancel (firmware 40.30.010.01 "
                    "or newer) or the D1's power switch."
                )
        except Exception as e:
            self._toast(_("Upload to the D1 failed: {err}").format(err=e))
            self._update_connection_status(TransportStatus.ERROR, str(e))
            raise
        finally:
            self.job_finished.send(self)

    async def frame(self, *args: Any, **kwargs: Any) -> None:
        """Frame with the D1's crosshair light on while the head moves."""
        self._framing = True
        try:
            await super().frame(*args, **kwargs)
        finally:
            self._framing = False

    @staticmethod
    def _crosshair_on(gcode: str) -> str:
        """Turn the first ``M106 S0`` into ``M106 S1``; the last stays."""
        lines = gcode.splitlines()
        for i, line in enumerate(lines):
            if line.strip().startswith("M106 S0"):
                lines[i] = "M106 S1 ;Framing laser on"
                break
        return "\n".join(lines) + "\n"

    @staticmethod
    def _return_home(gcode: str) -> str:
        """Add laser off and ``G0 X0 Y0`` after the job's last move."""
        lines = gcode.splitlines()
        moves = [
            i for i, line in enumerate(lines)
            if line.strip().upper().startswith(("G0", "G1"))
        ]
        at = moves[-1] + 1 if moves else len(lines)
        lines[at:at] = ["M5", "G0 X0 Y0 ;Return to origin"]
        return "\n".join(lines) + "\n"

    async def run_raw(self, machine_code: str) -> None:
        for raw in machine_code.splitlines():
            line = raw.split(";", 1)[0].strip()
            if line:
                await self._command(line)

    async def _job_action(self, action: str) -> bool:
        """``GET /cnc/data?action=<action>``; True when the D1 says ok
        (it answers fail when the action does not fit its state)."""
        reply = await self._get(f"/cnc/data?action={action}")
        ok = '"ok"' in reply
        if not ok:
            logger.warning("The D1 refused '%s': %s", action, reply)
        return ok

    async def cancel(self, emergency: bool = False) -> None:
        if not has_job_control(self.firmware):
            logger.warning(
                "This D1 firmware (%s) cannot be stopped over Wi-Fi: press "
                "the button on the machine, or use its power switch. "
                "Firmware 40.30.010.01 or newer can.",
                self.firmware or "unknown",
            )
            self._toast(_("Stop the D1 with its button or power switch."))
            self.job_finished.send(self)
            return
        try:
            stopped = await self._job_action("stop")
            # The laser state after a stop is unverified: switch it off.
            await self._command("M5")
        except DeviceConnectionError as e:
            self._toast(_("Could not stop the D1: {err}").format(err=e))
            raise
        finally:
            self.job_finished.send(self)
        self._is_paused = False
        if stopped:
            self._toast(_("Stop sent to the D1."))
            self._set_device_status(DeviceStatus.IDLE)

    async def set_hold(self, hold: bool = True) -> None:
        if not has_job_control(self.firmware):
            logger.warning(
                "This D1 firmware (%s) cannot pause over Wi-Fi; firmware "
                "40.30.010.01 or newer can.",
                self.firmware or "unknown",
            )
            return
        if await self._job_action("pause" if hold else "resume"):
            self._is_paused = hold
            self._set_device_status(
                DeviceStatus.HOLD if hold else DeviceStatus.RUN
            )

    # -- not supported over Wi-Fi --------------------------------------

    def can_home(self, axis: Axis | None = None) -> bool:
        return False

    async def home(self, axes: Axis | None = None) -> None:
        raise NotImplementedError("The xTool D1 cannot home.")

    async def move_to(self, pos_x, pos_y, pos_z=None, speed=None) -> None:
        raise NotImplementedError("Moving is not supported over Wi-Fi.")

    def can_jog(self, axis: Axis | None = None) -> bool:
        return False

    async def jog(self, speed: int, **deltas: float) -> None:
        raise NotImplementedError("Jogging is not supported over Wi-Fi.")

    async def select_tool(self, tool_number: int) -> None:
        pass

    async def clear_alarm(self) -> None:
        self._publish_state(error=None)

    async def set_power(self, head: "Laser", percent: float) -> None:
        raise NotImplementedError("Laser control is not supported over Wi-Fi.")

    async def set_focus_power(self, head: "Laser", percent: float) -> None:
        raise NotImplementedError("Laser control is not supported over Wi-Fi.")

    def get_setting_vars(self) -> list[VarSet]:
        return []

    async def read_settings(self) -> None:
        pass

    async def write_setting(self, key: str, value: Any) -> None:
        pass

    async def set_wcs_offset(
        self, wcs_slot: str, x: float, y: float, z: float | None
    ) -> None:
        pass

    async def read_wcs_offsets(self) -> dict[str, Pos]:
        return {}

    async def run_probe_cycle(
        self, axis: Axis, max_travel: float, feed_rate: int
    ) -> Pos | None:
        return None
