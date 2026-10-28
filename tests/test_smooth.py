import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from xtool_d1.firmware import Firmware  # noqa: E402
from xtool_d1.smooth import (  # noqa: E402
    RESTORE,
    SETUP,
    needs_smoothing,
    smooth_lines,
    smooth_moves,
)

RASTER = """\
G21 ;Set units to mm
G90 ;Absolute positioning
M106 S0 ;Framing laser off
G0 X4.775
G1 X5.055 F1800
M3 S51
G1 X10.007
M3 S71
G1 X10.047
M5
G1 X10.3
G0 Y0.08
G1 X10.047
M3 S71
G1 X5
M5 ;Ensure laser is off
M106 S0 ;Framing laser off
"""


class SmoothTest(unittest.TestCase):
    def test_version_gate(self):
        self.assertFalse(needs_smoothing(None))
        self.assertFalse(needs_smoothing(Firmware(40, 30, 2, 1, "B1")))
        self.assertTrue(needs_smoothing(Firmware(40, 30, 10, 1, "B2")))

    def test_raster_rewrite(self):
        out = smooth_moves(RASTER).splitlines()
        self.assertEqual(out[:2], SETUP)
        self.assertEqual(out[-2:], RESTORE)
        body = out[2:-2]
        self.assertEqual(
            body,
            [
                "G21 ;Set units to mm",
                "G90 ;Absolute positioning",
                "M106 S0 ;Framing laser off",
                "G0 X4.775 S0",
                "G1 X5.055 F1800",
                "M3 S0",
                "G1 X10.007 S51",
                "G1 X10.047 S71",
                "G1 X10.3 S0",
                "G0 Y0.08",
                "G1 X10.047",
                "G1 X5 S71",
                "M5 ;Ensure laser is off",
                "M106 S0 ;Framing laser off",
            ],
        )

    def test_only_one_m3(self):
        out = smooth_moves(RASTER)
        self.assertEqual(
            [l for l in out.splitlines() if l.startswith(("M3", "M4"))],
            ["M3 S0"],
        )

    def test_adds_m5_when_job_has_none(self):
        out = smooth_moves("M3 S100\nG1 X1 F600\n").splitlines()
        self.assertEqual(out[-3:], ["M5"] + RESTORE)

    def test_job_without_laser(self):
        out = smooth_moves("G0 X1\nG0 Y1\n").splitlines()
        self.assertNotIn("M3 S0", out)
        self.assertNotIn("M5", out)


if __name__ == "__main__":
    unittest.main()


class SmoothLinesTest(unittest.TestCase):
    JOB = "G21\nG90\nM3 S100\nG1 X10 F600\nM5\nG0 X0\nM106 S0\n"

    def test_same_text_as_smooth_moves(self):
        lines, _ = smooth_lines(self.JOB)
        self.assertEqual("\n".join(lines) + "\n", smooth_moves(self.JOB))

    def test_each_line_knows_where_it_came_from(self):
        lines, origin = smooth_lines(self.JOB)
        self.assertEqual(len(lines), len(origin))
        by_source = {src: line for line, src in zip(lines, origin) if src is not None}
        self.assertEqual(by_source[0], "G21")
        self.assertEqual(by_source[2], "M3 S0")  # the first M3
        self.assertTrue(by_source[3].startswith("G1 X10 F600"))
        self.assertTrue(by_source[5].startswith("G0 X0"))
        self.assertEqual(by_source[6], "M106 S0")
        self.assertNotIn(4, by_source)  # the M5 between moves is dropped

    def test_added_lines_have_no_source(self):
        lines, origin = smooth_lines(self.JOB)
        for added in SETUP + RESTORE:
            self.assertIsNone(origin[lines.index(added)])
        self.assertIsNone(origin[0])
