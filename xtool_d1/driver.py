"""
xTool D1 serial driver.

The D1 speaks a GRBL-flavoured line protocol at 230400 baud, but is not
GRBL. What is verified on a D1 10W (firmware 40.30.002.01 B1; the ``$I``
reply's identity line is ``[XTool`` there and ``[xTool`` on 40.30.010.01):

* Opening the port resets the board. It boots into "app mode": it
  streams ``ok:512`` / ``ok:IDLE`` heartbeats and does not acknowledge
  commands. Sending ``$I`` switches it to "ack mode" (``ok`` per line).
* ``?`` replies ``<MPos:x,y,z,w>`` with no state word.
* There is no ``$J`` jog and no homing.
* ``M3 S<n>`` fires the cutting laser, ``M4`` does not.
* ``M106 S0`` / ``M106 S1`` switch the framing crosshair off / on.
* A 3 s idle timer in the firmware turns the motors off (``M18``).

On 40.30.010.01 and newer the 3 s cutoff of a single move and the stalls
between queued moves are gone (laser-off tests), so moves are not split
and the port is polled at the idle rate during a job, and a job is
rewritten like the Wi-Fi driver does (``smooth.py``) so it runs without
stopping at every join. Cancel therefore lets the move in progress finish:
the ``M5`` it sends is queued behind that move (a 40 mm laser-on line at
10 mm/s burned to its end). The power switch is the stop.

This driver sends one line and waits for ``ok``, but the D1 acknowledges a
command when it *queues* it, not when the move finishes. A whole job is
therefore queued on the board within milliseconds and runs on its own.
Cancel cannot recall queued moves: the power switch is the only
verified way to stop the machine.
"""

import asyncio
import logging
import re
from dataclasses import replace
from collections.abc import Awaitable, Callable
from gettext import gettext as _
from typing import TYPE_CHECKING, Any

from rayforge.core.varset import BaudrateVar, SerialPortVar, VarSet
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
from rayforge.machine.transport import SerialTransport, TransportStatus
from rayforge.machine.transport.serial import SerialPortPermissionError
from rayforge.pipeline.encoder.base import (
    EncodedOutput,
    MachineCodeOpMap,
    OpsEncoder,
)
from rayforge.pipeline.encoder.gcode import GcodeEncoder

from .firmware import (
    Firmware,
    has_usb_quirks,
    is_identity_line,
    parse_version,
)
from .framing import crosshair_on
from .jogging import JogTracker
from .pacing import MAX_SEGMENT_S, MovePacer
from .smooth import needs_smoothing, smooth_lines

if TYPE_CHECKING:
    from raygeo.ops import Ops

    from rayforge.context import RayforgeContext
    from rayforge.core.doc import Doc
    from rayforge.machine.models.laser import Laser
    from rayforge.machine.models.machine import Machine

logger = logging.getLogger(__name__)

DEFAULT_BAUDRATE = 230400
# The board resets when the port is opened; commands sent while it
# boots are lost.
BOOT_DELAY_S = 5.0
HANDSHAKE_TIMEOUT_S = 3.0
RECONNECT_DELAY_S = 5.0
POLL_INTERVAL_S = 1.0
JOB_POLL_INTERVAL_S = 0.2
# Position is "stopped" once two reads this far apart differ by less
# than the tolerance.
SETTLE_POLL_S = 0.15
SETTLE_TOLERANCE_MM = 0.01
SETTLE_MAX_POLLS = 40

_mpos_re = re.compile(r"MPos:([-\d.]+),([-\d.]+),([-\d.]+)(?:,([-\d.]+))?")


class _Pending:
    """A sent command awaiting its ``ok`` response."""

    __slots__ = ("command", "error", "event", "response_lines")

    def __init__(self, command: str):
        self.command = command
        self.event = asyncio.Event()
        self.response_lines: list[str] = []
        self.error: str | None = None

    def set_result(self, error: str | None = None):
        self.error = error
        self.event.set()

    async def wait(self, timeout: float) -> list[str]:
        await asyncio.wait_for(self.event.wait(), timeout=timeout)
        if self.error is not None:
            raise DeviceConnectionError(self.error)
        return self.response_lines


class XToolD1Driver(Driver):
    label = _("xTool D1")
    subtitle = _("xTool D1 / D1 10W diode laser over USB serial")
    supports_settings = False
    reports_granular_progress = True
    maturity = DriverMaturity.EXPERIMENTAL

    def __init__(self, context: "RayforgeContext", machine: "Machine"):
        super().__init__(context, machine)
        self._transport: SerialTransport | None = None
        self.keep_running = False
        self._connection_task: asyncio.Task | None = None
        self._poll_task: asyncio.Task | None = None
        self._cmd_lock = asyncio.Lock()
        self._is_cancelled = False
        self._job_running = False
        self._active_streams = 0
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ack_mode = asyncio.Event()
        self._on_command_done: (
            Callable[[int], None | Awaitable[None]] | None
        ) = None
        self._pending: _Pending | None = None
        self._line_buffer = bytearray()
        self._status_seen = asyncio.Event()
        self._pacer = MovePacer((0.0, 0.0))
        self._jogs = JogTracker()
        self._framing = False
        self._current_op_index = -1
        self.firmware: Firmware | None = None

    @property
    def machine_space_wcs(self) -> str:
        return "G53"

    @property
    def machine_space_wcs_display_name(self) -> str:
        return _("Machine Coordinates")

    @property
    def resource_uri(self) -> str | None:
        if self._transport and self._transport.port:
            return f"serial://{self._transport.port}"
        return None

    @classmethod
    def precheck(cls, **kwargs: Any) -> None:
        try:
            SerialTransport.check_serial_permissions_globally()
        except SerialPortPermissionError as e:
            raise DriverPrecheckError(str(e)) from e

    @classmethod
    def get_setup_vars(cls) -> VarSet:
        return VarSet(
            vars=[
                SerialPortVar(
                    key="port",
                    label=_("Port"),
                    description=_("Serial port of the D1 (CH340 USB)"),
                ),
                BaudrateVar(
                    "baudrate",
                    choices=SerialTransport.list_baud_rates(),
                    default=DEFAULT_BAUDRATE,
                ),
            ]
        )

    @classmethod
    def create_encoder(cls, machine: "Machine") -> OpsEncoder:
        assert machine.dialect is not None
        return GcodeEncoder(machine.dialect)

    def _setup_implementation(self, **kwargs: Any) -> None:
        port = kwargs.get("port", "")
        baudrate = kwargs.get("baudrate", DEFAULT_BAUDRATE)
        if not port:
            raise DriverSetupError(_("Port must be configured."))
        self._transport = SerialTransport(str(port), int(baudrate))
        self._transport.received.connect(self._on_serial_data)

    async def cleanup(self) -> None:
        self.keep_running = False
        self._is_cancelled = False
        self._job_running = False
        self._active_streams = 0
        for task in (self._connection_task, self._poll_task):
            if task and not task.done():
                task.cancel()
        self._connection_task = None
        self._poll_task = None
        if self._transport:
            self._transport.received.disconnect(self._on_serial_data)
            if self._transport.is_connected:
                await self._transport.disconnect()
        await super().cleanup()

    # -- connection ----------------------------------------------------

    async def _connect_implementation(self) -> None:
        if self._connection_task and not self._connection_task.done():
            self._connection_task.cancel()
        self.keep_running = True
        self._loop = asyncio.get_running_loop()
        self._connection_task = asyncio.ensure_future(self._connection_loop())

    async def _reenter_ack(self) -> None:
        transport = self._transport
        if transport and transport.is_connected:
            try:
                await transport.send(b"$I\n")
            except ConnectionError as e:
                logger.warning(f"Could not re-send $I: {e}")

    async def _enter_ack_mode(self) -> bool:
        """
        Waits out the boot caused by opening the port, then sends
        ``$I`` to switch the board from app mode to ack mode. Returns
        True once the board identifies itself.
        """
        self._ack_mode.clear()
        await asyncio.sleep(BOOT_DELAY_S)
        assert self._transport
        await self._transport.send(b"$I\n")
        try:
            await asyncio.wait_for(
                self._ack_mode.wait(), timeout=HANDSHAKE_TIMEOUT_S
            )
        except asyncio.TimeoutError:
            return False
        return True

    async def _connection_loop(self) -> None:
        transport = self._transport
        if not transport:
            return

        while self.keep_running:
            try:
                await transport.connect()
                self._update_connection_status(
                    TransportStatus.CONNECTING, "Waiting for D1 to boot"
                )
                if not await self._enter_ack_mode():
                    logger.warning("D1 did not answer $I. Retrying.")
                    await transport.disconnect()
                    self._update_connection_status(
                        TransportStatus.SLEEPING, "No answer, retrying..."
                    )
                    await asyncio.sleep(RECONNECT_DELAY_S)
                    continue

                self._update_connection_status(
                    TransportStatus.CONNECTED, "Connected"
                )
                self._poll_task = asyncio.ensure_future(self._poll_loop())
                while transport.is_connected and self.keep_running:
                    await asyncio.sleep(0.5)

            except asyncio.CancelledError:
                break
            except Exception as e:  # noqa: BLE001 - connection loop boundary
                logger.error(f"Connection error: {e}")
                self._update_connection_status(TransportStatus.ERROR, str(e))

            if self.keep_running:
                await transport.disconnect()
                self._update_connection_status(
                    TransportStatus.SLEEPING, "Reconnecting..."
                )
                await asyncio.sleep(RECONNECT_DELAY_S)

    async def _poll_loop(self) -> None:
        transport = self._transport
        if not transport:
            return
        try:
            while transport.is_connected and self.keep_running:
                # Polls during a job too, and faster on 40.30.002.01: it
                # stalled between queued moves while the port was quiet.
                async with self._cmd_lock:
                    if transport.is_connected:
                        try:
                            await self._ping_pong("?", timeout=2.0)
                        except (DeviceConnectionError, ConnectionError):
                            pass
                await asyncio.sleep(
                    JOB_POLL_INTERVAL_S
                    if self._job_running and has_usb_quirks(self.firmware)
                    else POLL_INTERVAL_S
                )
        except asyncio.CancelledError:
            pass

    def _update_connection_status(
        self, status: TransportStatus, message: str = ""
    ) -> None:
        if status == TransportStatus.CONNECTED:
            self._publish_state(status=DeviceStatus.IDLE)
        elif status in (TransportStatus.ERROR, TransportStatus.SLEEPING):
            self._publish_state(status=DeviceStatus.UNKNOWN)
        self.connection_status_changed.send(
            self, status=status, message=message
        )

    def _publish_state(self, **changes: Any) -> None:
        """Hand Rayforge a new DeviceState with *changes*. Rayforge only
        passes a state on when it differs from the object it already
        holds, so a state edited in place is never seen."""
        self.state = replace(self.state, **changes)
        self.state_changed.send(self, state=self.state)

    # -- serial I/O ----------------------------------------------------

    async def _ping_pong(
        self, command: str, timeout: float = 30.0
    ) -> list[str]:
        """Sends *command* and waits for ``ok`` / ``error:``."""
        if not self._transport or not self._transport.is_connected:
            raise ConnectionError("Serial transport not connected")

        pending = _Pending(command)
        self._pending = pending
        logger.info(command.strip(), extra=self._log_extra("USER_COMMAND"))
        await self._transport.send((command.strip() + "\n").encode("utf-8"))
        try:
            return await pending.wait(timeout=timeout)
        except asyncio.TimeoutError:
            raise DeviceConnectionError(
                f"Device did not respond to '{command}' within {timeout}s"
            )
        finally:
            self._pending = None

    def _on_serial_data(self, sender, data: bytes) -> None:
        self._line_buffer.extend(data)
        while b"\n" in self._line_buffer:
            idx = self._line_buffer.index(b"\n") + 1
            raw = bytes(self._line_buffer[:idx])
            del self._line_buffer[:idx]
            for line in raw.decode("utf-8", errors="replace").splitlines():
                line = line.strip()
                if line:
                    self._handle_line(line)

    def _handle_line(self, line: str) -> None:
        # App-mode heartbeat (ok:512, ok:IDLE, ...): never an ack. It
        # only runs outside ack mode, so seeing it while we believe we
        # are in ack mode means the board dropped out of it (the
        # machine's own button stopping a stored job does this).
        if line.startswith("ok:"):
            if self._ack_mode.is_set():
                self._ack_mode.clear()
                logger.warning("D1 left ack mode. Sending $I again.")
                if self._loop:
                    self._loop.call_soon_threadsafe(
                        lambda: asyncio.ensure_future(self._reenter_ack())
                    )
            return

        if is_identity_line(line):
            firmware = parse_version(line)
            if firmware != self.firmware:
                self.firmware = firmware
                logger.info(f"D1 firmware: {firmware or 'unknown'} ({line})")
            self._ack_mode.set()
            if self._pending:
                self._pending.response_lines.append(line)
            return

        if line == "ok":
            if self._pending:
                self._pending.set_result()
            return

        if line.startswith("error"):
            logger.warning(line, extra=self._log_extra("MACHINE_EVENT"))
            if self._pending:
                self._pending.set_result(error=line)
            return

        if line.startswith("<") and line.endswith(">"):
            self._handle_status_report(line)
            return

        if self._pending:
            self._pending.response_lines.append(line)

    def _handle_status_report(self, report: str) -> None:
        """Parses ``<MPos:x,y,z,w>``. The D1 reports no state word."""
        match = _mpos_re.search(report)
        if not match:
            return
        x, y, z = (float(match.group(i)) for i in (1, 2, 3))
        self._status_seen.set()
        status = self.state.status
        if status in (DeviceStatus.UNKNOWN, DeviceStatus.IDLE):
            status = (
                DeviceStatus.RUN if self._job_running else DeviceStatus.IDLE
            )
        self._publish_state(
            machine_pos=(x, y, z), work_pos=(x, y, z), status=status
        )

    # -- jobs ----------------------------------------------------------

    def _start_job(
        self,
        on_command_done: Callable[[int], None | Awaitable[None]] | None = None,
    ) -> None:
        self._is_cancelled = False
        self._jogs.reset()
        # Rayforge's console submits one line per call, so several
        # streams can overlap: the job is over when the last one ends.
        self._active_streams += 1
        self._job_running = True
        self._on_command_done = on_command_done
        self._current_op_index = -1
        self._publish_state(status=DeviceStatus.RUN)

    async def _stream(
        self,
        lines: list[str],
        op_map: MachineCodeOpMap | None = None,
        origin: list[int | None] | None = None,
    ) -> None:
        """Sends *lines*. *origin*, when the lines were rewritten, gives
        each one's line number in the text *op_map* was made for."""
        total = len(lines)
        sent = 0
        completed = False
        # The queue lives on the D1, so the pacer is shared by every
        # call: Rayforge's G-code console submits one line per call.
        x, y, _z = self.state.machine_pos
        pacer = self._pacer
        pacer.max_segment_s = (
            MAX_SEGMENT_S if has_usb_quirks(self.firmware) else None
        )
        pacer.sync((x or 0.0, y or 0.0))
        try:
            for idx, line in enumerate(lines):
                if self._is_cancelled:
                    break
                command = line.split(";", 1)[0].strip()
                if not command:
                    sent += 1
                    continue

                source = origin[idx] if origin is not None else idx
                new_op = (
                    op_map.op_for_line(source)
                    if op_map and source is not None
                    else None
                )
                if new_op is not None and new_op != self._current_op_index:
                    self._current_op_index = new_op
                    if self._on_command_done:
                        result = self._on_command_done(new_op)
                        if asyncio.iscoroutine(result):
                            await result

                # The D1 cuts any single move off after ~3 s, so long
                # moves go out as short segments. It also acks on
                # queueing, so hold each one back until the queue is
                # short enough for cancel to matter.
                cancelled = False
                for segment in pacer.split(command):
                    if not await self._sleep_unless_cancelled(
                        pacer.before_send(segment)
                    ):
                        cancelled = True
                        break
                    async with self._cmd_lock:
                        await self._ping_pong(segment)
                if cancelled:
                    break
                sent += 1
            completed = sent == total and not self._is_cancelled
            if completed:
                await self._settle(pacer)
        except (DeviceConnectionError, ConnectionError) as e:
            logger.warning(f"Job interrupted: {e}")
            await self._laser_off()
        except Exception:
            logger.exception("Unexpected streaming error")
            await self._laser_off()
        finally:
            self._active_streams = max(0, self._active_streams - 1)
            self._job_running = self._active_streams > 0
            self._on_command_done = None
            if completed or not self._is_cancelled:
                self.job_finished.send(self)
            self._publish_state(status=DeviceStatus.IDLE)

    async def _sleep_unless_cancelled(self, seconds: float) -> bool:
        """Sleeps in short steps. Returns False if the job was cancelled."""
        deadline = asyncio.get_running_loop().time() + seconds
        while not self._is_cancelled:
            left = deadline - asyncio.get_running_loop().time()
            if left <= 0:
                return True
            await asyncio.sleep(min(left, 0.05))
        return False

    async def _read_position(self) -> tuple[float, float] | None:
        """Polls ``?`` and returns the fresh (x, y), or None on timeout."""
        self._status_seen.clear()
        async with self._cmd_lock:
            await self._ping_pong("?", timeout=2.0)
        # The position line can arrive just after the ``ok``.
        try:
            await asyncio.wait_for(self._status_seen.wait(), timeout=0.5)
        except asyncio.TimeoutError:
            return None
        x, y, _z = self.state.machine_pos
        return (x, y) if x is not None and y is not None else None

    async def _settle(self, pacer: MovePacer) -> None:
        """Waits for the queued motion to finish before the job ends."""
        if not await self._sleep_unless_cancelled(pacer.remaining()):
            return
        last = None
        stable = 0
        for _poll in range(SETTLE_MAX_POLLS):
            pos = await self._read_position()
            if pos is None:
                return
            if last is not None and (
                abs(pos[0] - last[0]) < SETTLE_TOLERANCE_MM
                and abs(pos[1] - last[1]) < SETTLE_TOLERANCE_MM
            ):
                stable += 1
                if stable >= 2:
                    return
            else:
                stable = 0
            last = pos
            if not await self._sleep_unless_cancelled(SETTLE_POLL_S):
                return

    async def run(
        self,
        encoded: EncodedOutput,
        doc: "Doc",
        ops: "Ops",
        on_command_done: Callable[[int], None | Awaitable[None]] | None = None,
    ) -> None:
        self._start_job(on_command_done)
        text = crosshair_on(encoded.text) if self._framing else encoded.text
        if needs_smoothing(self.firmware):
            lines, origin = smooth_lines(text)
            await self._stream(lines, encoded.op_map, origin)
        else:
            await self._stream(text.splitlines(), encoded.op_map)

    async def frame(self, *args: Any, **kwargs: Any) -> None:
        """Frame with the D1's crosshair light on while the head moves."""
        self._framing = True
        try:
            await super().frame(*args, **kwargs)
        finally:
            self._framing = False

    async def run_raw(self, machine_code: str) -> None:
        lines = [x.strip() for x in machine_code.splitlines() if x.strip()]
        if not lines:
            return
        self._start_job()
        await self._stream(lines)

    async def _laser_off(self) -> None:
        """Best effort: cutting laser and framing laser off."""
        transport = self._transport
        if not transport or not transport.is_connected:
            return
        for command in ("M5", "M106 S0"):
            try:
                await transport.send((command + "\n").encode("utf-8"))
            except ConnectionError as e:
                logger.warning(f"Safety command '{command}' failed: {e}")

    async def cancel(self, emergency: bool = False) -> None:
        # The D1 has no known software stop, and it acks commands when
        # it queues them (a whole job is queued within milliseconds), so
        # this only stops sending lines the job has not yet sent and
        # asks for M5. Moves already queued on the board still run.
        self._is_cancelled = True
        self._job_running = False
        self._on_command_done = None
        if self._pending:
            self._pending.set_result()
            self._pending = None
        await self._laser_off()
        self.job_finished.send(self)

    async def set_hold(self, hold: bool = True) -> None:
        # No pause/resume exists on the D1 firmware.
        logger.warning("Hold is not supported by the xTool D1.")

    # -- interactive commands -----------------------------------------

    async def _execute_command(self, command: str) -> list[str]:
        self._is_cancelled = False
        async with self._cmd_lock:
            return await self._ping_pong(command)

    def can_home(self, axis: Axis | None = None) -> bool:
        return False

    async def home(self, axes: Axis | None = None) -> None:
        raise NotImplementedError("The xTool D1 cannot home.")

    async def move_to(self, pos_x, pos_y, pos_z=None, speed=None) -> None:
        cmd = self._format_move_to(float(pos_x), float(pos_y), None, speed)
        await self._execute_command(cmd)

    def can_jog(self, axis: Axis | None = None) -> bool:
        return True

    async def jog(self, speed: int, **deltas: float) -> None:
        # No $J on the D1: relative G0 between G91 and G90.
        deltas = {k.lower(): v for k, v in deltas.items()}
        deltas = {k: v for k, v in deltas.items() if k in ("x", "y")}
        if self._machine.soft_limits_enabled:
            x, y, _z = self.state.machine_pos
            deltas = self._jogs.clamp(
                {"x": x, "y": y},
                deltas,
                self._machine.get_soft_limits(),
                speed,
            )
        moves = "".join(
            f" {name.upper()}{self._to_machine_length(dist)}"
            for name, dist in deltas.items()
            if dist
        )
        if not moves:
            return
        feed = self._to_machine_speed(speed)
        await self._execute_command("G91")
        try:
            await self._execute_command(f"G0 F{feed}{moves}")
        finally:
            await self._execute_command("G90")

    async def select_tool(self, tool_number: int) -> None:
        pass

    async def clear_alarm(self) -> None:
        self._publish_state(error=None)

    async def set_power(self, head: "Laser", percent: float) -> None:
        if percent <= 0:
            await self._execute_command(self.dialect.laser_off)
        else:
            power = percent * head.max_power
            await self._execute_command(
                self.dialect.laser_on.format(power=power)
            )

    async def set_focus_power(self, head: "Laser", percent: float) -> None:
        await self.set_power(head, percent)

    # -- settings / WCS: not supported --------------------------------

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
