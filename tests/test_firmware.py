import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "xtool_d1"))

from firmware import (  # noqa: E402
    Firmware,
    has_job_control,
    is_identity_line,
    parse_version,
)


class FirmwareTest(unittest.TestCase):
    def test_old_serial_identity(self):
        line = "[XTool D1:ver 40.30.002.01 B1]"
        self.assertTrue(is_identity_line(line))
        self.assertEqual(parse_version(line), Firmware(40, 30, 2, 1, "B1"))

    def test_new_serial_identity(self):
        line = "[xTool D1:ver 40.30.010.01 B2]"
        self.assertTrue(is_identity_line(line))
        self.assertEqual(parse_version(line), Firmware(40, 30, 10, 1, "B2"))

    def test_wifi_version(self):
        reply = '{"result":"ok","sn":"X","version":"V40.30.010.01 B2"}'
        self.assertEqual(parse_version(reply), Firmware(40, 30, 10, 1, "B2"))

    def test_other_lines_are_not_identity(self):
        for line in ("ok", "ok:IDLE", "[MSG:Pgm End]", "<MPos:0,0,0,0>"):
            self.assertFalse(is_identity_line(line))

    def test_no_version(self):
        self.assertIsNone(parse_version("[xTool D1]"))

    def test_versions_compare_in_order(self):
        old = parse_version("40.30.002.01 B1")
        new = parse_version("40.30.010.01 B2")
        self.assertLess(old, new)
        self.assertEqual(str(new), "40.30.010.01 B2")

    def test_job_control_by_version(self):
        self.assertFalse(has_job_control(None))
        self.assertFalse(has_job_control(parse_version("40.30.002.01 B1")))
        self.assertTrue(has_job_control(parse_version("40.30.010.01 B2")))
        self.assertTrue(has_job_control(parse_version("40.30.010.01 B1")))
        self.assertTrue(has_job_control(parse_version("40.31.006.01 B2")))


if __name__ == "__main__":
    unittest.main()
