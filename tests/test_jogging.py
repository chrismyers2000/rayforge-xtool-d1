import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "xtool_d1"))

from jogging import JogTracker, clamp_jog  # noqa: E402

LIMITS = (0.0, 0.0, 432.0, 406.0)


class FakeClock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


def make():
    clock = FakeClock()
    return JogTracker(clock=clock, lag_s=1.5), clock


class ClampJogTest(unittest.TestCase):
    def test_stops_at_the_limit(self):
        self.assertEqual(clamp_jog(3.0, -10.0, 0.0, 406.0), -3.0)
        self.assertEqual(clamp_jog(400.0, 10.0, 0.0, 406.0), 6.0)

    def test_inside_the_range_is_unchanged(self):
        self.assertEqual(clamp_jog(50.0, -10.0, 0.0, 406.0), -10.0)

    def test_beyond_a_limit_only_inward_jogs_move(self):
        self.assertEqual(clamp_jog(-5.0, -10.0, 0.0, 406.0), 0.0)
        self.assertEqual(clamp_jog(-5.0, 10.0, 0.0, 406.0), 10.0)


class JogTrackerTest(unittest.TestCase):
    def test_single_jog_is_clamped_at_the_limit(self):
        tracker, _ = make()
        out = tracker.clamp({"x": 5.0, "y": 5.0}, {"x": -10.0}, LIMITS, 1000)
        self.assertEqual(out, {"x": -5.0})

    def test_rapid_presses_stop_at_the_limit(self):
        # The reported position stays at Y21.92 while the presses queue.
        tracker, clock = make()
        reported = {"x": 106.11, "y": 21.92}
        total = 0.0
        for _ in range(5):
            out = tracker.clamp(reported, {"y": -10.0}, LIMITS, 1000)
            total += out["y"]
            clock.now += 0.3
        self.assertAlmostEqual(21.92 + total, 0.0)

    def test_presses_beyond_the_limit_send_nothing(self):
        tracker, clock = make()
        reported = {"x": 0.0, "y": 5.0}
        first = tracker.clamp(reported, {"y": -10.0}, LIMITS, 1000)
        second = tracker.clamp(reported, {"y": -10.0}, LIMITS, 1000)
        self.assertEqual(first, {"y": -5.0})
        self.assertEqual(second, {"y": 0.0})

    def test_reported_position_is_used_once_the_head_has_settled(self):
        tracker, clock = make()
        tracker.clamp({"x": 50.0, "y": 50.0}, {"x": -10.0}, LIMITS, 1000)
        clock.now += 60.0
        # The head was moved by hand since: the report wins.
        out = tracker.clamp({"x": 200.0, "y": 50.0}, {"x": -10.0}, LIMITS, 1000)
        self.assertEqual(out, {"x": -10.0})

    def test_unknown_position_or_no_limits_leave_the_jog_alone(self):
        tracker, _ = make()
        self.assertEqual(
            tracker.clamp({"x": None, "y": None}, {"x": -10.0}, LIMITS, 1000),
            {"x": -10.0},
        )
        self.assertEqual(
            tracker.clamp({"x": 0.0, "y": 0.0}, {"x": -10.0}, None, 1000),
            {"x": -10.0},
        )

    def test_reset_forgets_queued_jogs(self):
        tracker, _ = make()
        tracker.clamp({"x": 50.0, "y": 50.0}, {"x": -10.0}, LIMITS, 1000)
        tracker.reset()
        out = tracker.clamp({"x": 200.0, "y": 50.0}, {"x": -10.0}, LIMITS, 1000)
        self.assertEqual(out, {"x": -10.0})

    def test_axes_are_tracked_independently(self):
        tracker, _ = make()
        out = tracker.clamp(
            {"x": 3.0, "y": 50.0}, {"x": -10.0, "y": -10.0}, LIMITS, 1000
        )
        self.assertEqual(out, {"x": -3.0, "y": -10.0})


if __name__ == "__main__":
    unittest.main()
