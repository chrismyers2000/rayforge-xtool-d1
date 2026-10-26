"""Runs the Wi-Fi driver against a fake D1 web server (needs Rayforge)."""

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    from aiohttp import web

    from rayforge.machine.driver.driver import DeviceConnectionError
    from xtool_d1.wifi_driver import XToolD1WifiDriver
except ImportError:  # run with Rayforge's environment
    XToolD1WifiDriver = None


class FakeD1:
    def __init__(self, upload_reply='{"result":"ok"}'):
        self.requests = []
        self.stored = None
        self.upload_reply = upload_reply

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


def make_driver(port, auto_start=False):
    drv = object.__new__(XToolD1WifiDriver)
    drv.host, drv.port, drv.auto_start = "127.0.0.1", port, auto_start
    drv.keep_running = False
    drv.state = SimpleNamespace(status=None, error=None)
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
        await drv.run_raw("M106 S0 ; crosshair off\n\nG4 P1\n")
        self.assertEqual(
            self.d1.requests,
            [("GET", "/cmd?cmd=M106%20S0"), ("GET", "/cmd?cmd=G4%20P1")],
        )

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


if __name__ == "__main__":
    unittest.main()
