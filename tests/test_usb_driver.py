"""Runs the USB driver's job streaming against a recording stub (needs
Rayforge): what it sends on each firmware, and which operations it
reports."""

import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    from rayforge.machine.driver.driver import DeviceState
    from xtool_d1.driver import XToolD1Driver
    from xtool_d1.firmware import Firmware
    from xtool_d1.pacing import MovePacer
except ImportError:  # run with Rayforge's environment
    XToolD1Driver = None


OLD_FW = Firmware(40, 30, 2, 1, "B1")
NEW_FW = Firmware(40, 30, 10, 1, "B2")

JOB = """\
G21 ;units
G90
M106 S0
G0 X0 Y0
M3 S100
G1 X200 F600
M5
"""
#: JOB's first four lines belong to op 0, the rest to op 1.
OP_OF_LINE = [0, 0, 0, 0, 1, 1, 1]


class _Signal:
    def send(self, *a, **k):
        pass


class _OpMap:
    @staticmethod
    def op_for_line(line):
        return OP_OF_LINE[line]


def make_driver(firmware):
    drv = object.__new__(XToolD1Driver)
    drv.firmware = firmware
    drv._is_cancelled = False
    drv._active_streams = 0
    drv._job_running = False
    drv._on_command_done = None
    drv._current_op_index = -1
    drv._pacer = MovePacer((0.0, 0.0))
    drv._cmd_lock = asyncio.Lock()
    drv.state = DeviceState()
    for name in ("state_changed", "job_finished"):
        setattr(drv, name, _Signal())
    drv.sent = []

    async def ping_pong(command, timeout=30.0):
        drv.sent.append(command)
        return []

    async def no_wait(seconds):
        return True

    async def settle(pacer):
        pass

    drv._ping_pong = ping_pong
    drv._sleep_unless_cancelled = no_wait
    drv._settle = settle
    return drv


@unittest.skipIf(XToolD1Driver is None, "needs Rayforge's environment")
class UsbStreamTest(unittest.IsolatedAsyncioTestCase):
    async def run_job(self, firmware):
        drv = make_driver(firmware)
        ops = []
        encoded = SimpleNamespace(text=JOB, op_map=_OpMap())
        await drv.run(encoded, None, None, on_command_done=ops.append)
        return drv, ops

    async def test_new_firmware_sends_the_smoothed_job_unsplit(self):
        drv, _ = await self.run_job(NEW_FW)
        self.assertEqual(
            drv.sent,
            [
                "M204 F6000", "M205 X1800", "G21", "G90", "M106 S0",
                "G0 X0 Y0 S0", "M3 S0", "G1 X200 F600 S100", "M5",
                "M204 F300", "M205 X450",
            ],
        )

    async def test_new_firmware_reports_each_operation_once(self):
        _, ops = await self.run_job(NEW_FW)
        self.assertEqual(ops, [0, 1])

    async def test_old_firmware_sends_the_job_split_and_unchanged(self):
        drv, ops = await self.run_job(OLD_FW)
        self.assertNotIn("M204 F6000", drv.sent)
        self.assertEqual(drv.sent[:5], ["G21", "G90", "M106 S0", "G0 X0 Y0",
                                        "M3 S100"])
        moves = [c for c in drv.sent if c.startswith("G1")]
        self.assertGreater(len(moves), 1)  # 20 s of travel in 1 s pieces
        self.assertEqual(drv.sent[-1], "M5")
        self.assertEqual(ops, [0, 1])

    async def test_unknown_firmware_is_treated_as_old(self):
        drv, _ = await self.run_job(None)
        self.assertNotIn("M204 F6000", drv.sent)
        self.assertGreater(len([c for c in drv.sent if c.startswith("G1")]), 1)


if __name__ == "__main__":
    unittest.main()
