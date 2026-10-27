"""Runs the Wi-Fi driver against a fake D1 web server (needs Rayforge)."""

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    from aiohttp import web

    from rayforge.machine.driver.driver import DeviceConnectionError
    from rayforge.machine.driver.driver import DeviceState, DeviceStatus
    from xtool_d1.firmware import Firmware
    from xtool_d1.wifi_driver import XToolD1WifiDriver
except ImportError:  # run with Rayforge's environment
    XToolD1WifiDriver = None


OLD_FW = '{"result":"ok","sn":"X","version":"V40.30.002.01 B1"}'
NEW_FW = '{"result":"ok","sn":"X","version":"V40.30.010.01 B2"}'


class FakeD1:
    def __init__(self, upload_reply='{"result":"ok"}'):
        self.requests = []
        self.stored = None
        self.upload_reply = upload_reply
        self.version_reply = NEW_FW
        self.working = "0"
        self.action_reply = '{"result":"ok"}'

    async def system(self, request):
        self.requests.append(("GET", request.path_qs))
        action = request.query.get("action")
        if action == "version":
            return web.Response(text=self.version_reply)
        if action == "get_working_sta":
            return web.Response(
                text=f'{{"result":"ok","working":"{self.working}"}}'
            )
        return web.Response(text='{"result":"fail"}')

    async def job_action(self, request):
        self.requests.append(("GET", request.path_qs))
        return web.Response(text=self.action_reply)

    async def ping(self, request):
        self.requests.append(("GET", request.path_qs))
        return web.Response(text='{"result":"ok"}')

    async def upload(self, request):
        self.requests.append(("POST", request.path_qs))
        form = await request.post()
        self.stored = form["file"].file.read().decode()
        return web.Response(text=self.upload_reply)

    async def other(self, request):
        self.requests.append(("GET", request.path_qs))
        return web.Response(text='{"result":"ok"}')


class _Signal:
    def __init__(self):
        self.sent = 0

    def send(self, *a, **k):
        self.sent += 1


def make_driver(port, auto_start=False, firmware=None, return_home=False):
    drv = object.__new__(XToolD1WifiDriver)
    drv.host, drv.port, drv.auto_start = "127.0.0.1", port, auto_start
    drv.return_home = return_home
    drv.keep_running = False
    drv._framing = False
    drv.firmware = firmware
    drv._is_paused = False
    drv._toast = lambda message: None
    drv._keep_copy = lambda gcode: None  # keep the user's real copy
    drv.state = DeviceState()
    for name in ("state_changed", "job_finished", "connection_status_changed"):
        setattr(drv, name, _Signal())
    return drv


@unittest.skipIf(XToolD1WifiDriver is None, "needs Rayforge's environment")
class WifiDriverTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.d1 = FakeD1()
        app = web.Application()
        app.router.add_get("/ping", self.d1.ping)
        app.router.add_post("/cnc/data", self.d1.upload)
        app.router.add_get("/read", self.d1.other)
        app.router.add_get("/cmd", self.d1.other)
        app.router.add_get("/system", self.d1.system)
        app.router.add_get("/cnc/data", self.d1.job_action)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.port = site._server.sockets[0].getsockname()[1]

    async def asyncTearDown(self):
        await self.runner.cleanup()

    def encoded(self, text):
        return SimpleNamespace(text=text, op_map=None)

    async def test_ping(self):
        drv = make_driver(self.port)
        self.assertIn('"ok"', await drv._get("/ping"))

    async def test_run_uploads_and_does_not_start(self):
        drv = make_driver(self.port)
        await drv.run(self.encoded("G21\nG1 X10 F600\nM5\n"), None, None)
        self.assertEqual(self.d1.stored, "G21\nG1 X10 F600\nM5\n")
        self.assertEqual(
            self.d1.requests,
            [("POST", "/cnc/data?filetype=1"), ("GET", "/cmd?cmd=$I")],
        )
        self.assertEqual(drv.job_finished.sent, 1)

    async def test_run_smooths_moves_on_new_firmware(self):
        drv = make_driver(self.port, firmware=Firmware(40, 30, 10, 1, "B2"))
        await drv.run(self.encoded("M3 S100\nG1 X10 F600\nM5\n"), None, None)
        self.assertIn("M204 F6000", self.d1.stored)
        self.assertIn("G1 X10 F600 S100", self.d1.stored)

    async def test_return_home_is_off_by_default(self):
        drv = make_driver(self.port)
        await drv.run(self.encoded("G1 X10 F600\nM5\n"), None, None)
        self.assertNotIn("X0 Y0", self.d1.stored)

    async def test_return_home_after_last_move(self):
        drv = make_driver(self.port, return_home=True)
        await drv.run(
            self.encoded("M3 S100\nG1 X10 F600\nM5 ;off\nM106 S0\n"),
            None, None,
        )
        self.assertEqual(
            self.d1.stored.splitlines(),
            ["M3 S100", "G1 X10 F600", "M5", "G0 X0 Y0 ;Return to origin",
             "M5 ;off", "M106 S0"],
        )

    async def test_return_home_with_smoothing(self):
        drv = make_driver(
            self.port, firmware=Firmware(40, 30, 10, 1, "B2"), return_home=True
        )
        await drv.run(self.encoded("M3 S100\nG1 X10 F600\nM5\n"), None, None)
        self.assertIn("G0 X0 Y0 S0", self.d1.stored)

    async def test_frame_does_not_return_home(self):
        drv = make_driver(self.port, return_home=True)
        drv._framing = True
        await drv.run(self.encoded("M106 S0\nG0 X1\n"), None, None)
        self.assertNotIn("X0 Y0", self.d1.stored)

    async def test_auto_start_requests_the_stored_file(self):
        drv = make_driver(self.port, auto_start=True)
        await drv.run(self.encoded("G21\n"), None, None)
        self.assertIn(("GET", "/read?file=/tmp.gcode"), self.d1.requests)

    async def test_refused_upload_raises(self):
        self.d1.upload_reply = '{"result":"fail"}'
        drv = make_driver(self.port)
        with self.assertRaises(DeviceConnectionError):
            await drv.run(self.encoded("G21\n"), None, None)
        self.assertEqual(drv.job_finished.sent, 1)

    async def test_unreachable_raises(self):
        drv = make_driver(1)  # nothing listens here
        with self.assertRaises(DeviceConnectionError):
            await drv._get("/ping")

    async def test_console_lines_use_cmd(self):
        drv = make_driver(self.port)
        await drv.run_raw("M106 S1 ; crosshair\n\nG4 P1\n")
        self.assertEqual(
            self.d1.requests,
            [("GET", "/cmd?cmd=M106%20S1"), ("GET", "/cmd?cmd=G4%20P1")],
        )

    def test_crosshair_on_only_replaces_the_first_off(self):
        text = "G21\nM106 S0 ;off\nG0 X1\nM5\nM106 S0 ;off\n"
        out = XToolD1WifiDriver._crosshair_on(text)
        self.assertEqual(
            out.splitlines(),
            [
                "G21",
                "M106 S1 ;Framing laser on",
                "G0 X1",
                "M5",
                "M106 S0 ;off",
            ],
        )

    async def test_frame_job_turns_the_crosshair_on(self):
        drv = make_driver(self.port)
        drv._framing = True
        await drv.run(
            self.encoded("G21\nM106 S0 ;off\nG0 X1\nM106 S0 ;off\n"),
            None, None,
        )
        self.assertIn("M106 S1", self.d1.stored)
        self.assertTrue(self.d1.stored.rstrip().endswith("M106 S0 ;off"))

    async def test_normal_job_is_not_changed(self):
        drv = make_driver(self.port)
        drv._framing = False
        await drv.run(self.encoded("M106 S0\nG0 X1\n"), None, None)
        self.assertEqual(self.d1.stored, "M106 S0\nG0 X1\n")

    async def test_connect_sends_the_ack_mode_handshake(self):
        drv = make_driver(self.port)
        await drv._enter_ack_mode()
        self.assertEqual(self.d1.requests, [("GET", "/cmd?cmd=$I")])

    async def test_failed_handshake_does_not_raise(self):
        drv = make_driver(1)  # nothing listens here
        await drv._enter_ack_mode()

    def test_nothing_is_jogged_or_homed(self):
        drv = make_driver(self.port)
        self.assertFalse(drv.can_jog())
        self.assertFalse(drv.can_home())

    # -- firmware versions -------------------------------------------------

    async def test_reads_new_firmware(self):
        drv = make_driver(self.port)
        await drv._read_firmware()
        self.assertEqual(drv.firmware, Firmware(40, 30, 10, 1, "B2"))

    async def test_reads_old_firmware(self):
        self.d1.version_reply = OLD_FW
        drv = make_driver(self.port)
        await drv._read_firmware()
        self.assertEqual(drv.firmware, Firmware(40, 30, 2, 1, "B1"))

    async def test_unreadable_version_does_not_raise(self):
        drv = make_driver(1)  # nothing listens here
        await drv._read_firmware()
        self.assertIsNone(drv.firmware)

    async def test_old_firmware_polls_ping(self):
        drv = make_driver(self.port, firmware=Firmware(40, 30, 2, 1, "B1"))
        self.assertEqual(await drv._poll_once(), DeviceStatus.IDLE)
        self.assertEqual(self.d1.requests, [("GET", "/ping")])

    async def test_new_firmware_reports_running(self):
        drv = make_driver(self.port, firmware=Firmware(40, 30, 10, 1, "B2"))
        self.assertEqual(await drv._poll_once(), DeviceStatus.IDLE)
        for working in ("1", "2"):
            self.d1.working = working
            self.assertEqual(await drv._poll_once(), DeviceStatus.RUN)

    async def test_new_firmware_stop(self):
        drv = make_driver(self.port, firmware=Firmware(40, 30, 10, 1, "B2"))
        await drv.cancel()
        self.assertEqual(
            self.d1.requests,
            [("GET", "/cnc/data?action=stop"), ("GET", "/cmd?cmd=M5")],
        )
        self.assertEqual(drv.job_finished.sent, 1)

    async def test_old_firmware_cancel_sends_nothing(self):
        drv = make_driver(self.port, firmware=Firmware(40, 30, 2, 1, "B1"))
        await drv.cancel()
        self.assertEqual(self.d1.requests, [])
        self.assertEqual(drv.job_finished.sent, 1)

    async def test_unknown_firmware_cancel_sends_nothing(self):
        drv = make_driver(self.port)
        await drv.cancel()
        self.assertEqual(self.d1.requests, [])

    async def test_pause_and_resume(self):
        drv = make_driver(self.port, firmware=Firmware(40, 30, 10, 1, "B2"))
        self.d1.working = "2"
        await drv.set_hold(True)
        self.assertEqual(await drv._poll_once(), DeviceStatus.HOLD)
        await drv.set_hold(False)
        self.assertEqual(await drv._poll_once(), DeviceStatus.RUN)
        self.assertIn(("GET", "/cnc/data?action=pause"), self.d1.requests)
        self.assertIn(("GET", "/cnc/data?action=resume"), self.d1.requests)

    async def test_refused_pause_keeps_running(self):
        drv = make_driver(self.port, firmware=Firmware(40, 30, 10, 1, "B2"))
        self.d1.working = "2"
        self.d1.action_reply = '{"result":"fail"}'
        await drv.set_hold(True)
        self.assertEqual(await drv._poll_once(), DeviceStatus.RUN)

    async def test_status_change_hands_rayforge_a_new_state(self):
        # Rayforge ignores a state object it already holds.
        drv = make_driver(self.port, firmware=Firmware(40, 30, 10, 1, "B2"))
        before = drv.state
        self.d1.working = "2"
        drv._set_device_status(await drv._poll_once())
        self.assertIsNot(drv.state, before)
        self.assertEqual(drv.state.status, DeviceStatus.RUN)
        self.assertNotEqual(drv.state, before)
        self.assertEqual(drv.state_changed.sent, 1)

    async def test_old_firmware_pause_sends_nothing(self):
        drv = make_driver(self.port, firmware=Firmware(40, 30, 2, 1, "B1"))
        await drv.set_hold(True)
        self.assertEqual(self.d1.requests, [])


if __name__ == "__main__":
    unittest.main()
