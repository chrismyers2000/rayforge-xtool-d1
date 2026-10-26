import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "xtool_d1"))

from pacing import DEFAULT_FEED, MovePacer  # noqa: E402


class FakeClock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


def make(pos=(0.0, 0.0), lookahead=0.5):
    clock = FakeClock()
    return MovePacer(pos, clock=clock, lookahead=lookahead), clock


class MovePacerTest(unittest.TestCase):
    def test_non_motion_commands_do_not_wait(self):
        pacer, _ = make()
        for cmd in ("G21", "G90", "M3 S100", "M5", "M106 S0", "G54", ";x"):
            self.assertEqual(pacer.before_send(cmd), 0.0)
        self.assertEqual(pacer.remaining(), 0.0)

    def test_first_move_is_sent_at_once(self):
        pacer, _ = make()
        self.assertEqual(pacer.before_send("G1 X10 F600"), 0.0)
        self.assertAlmostEqual(pacer.remaining(), 1.0)  # 10 mm at 10 mm/s

    def test_queue_is_held_to_the_lookahead(self):
        pacer, _ = make()
        pacer.before_send("G1 X10 F600")  # queued, 1.0 s long
        # Second move may only be sent once 0.5 s of the first remain.
        self.assertAlmostEqual(pacer.before_send("G1 X20"), 0.5)
        self.assertAlmostEqual(pacer.remaining(), 2.0 - 0.0)

    def test_many_short_moves_stay_bounded(self):
        pacer, clock = make()
        for i in range(1, 200):
            clock.now += pacer.before_send(f"G1 X{i * 0.5} F600")
            # Never more than the lookahead plus the move just queued.
            self.assertLessEqual(pacer.remaining(), 0.5 + 0.05 + 1e-9)

    def test_relative_moves_and_modal_state(self):
        pacer, _ = make()
        pacer.before_send("G91")
        pacer.before_send("G0 F600 X10")
        self.assertAlmostEqual(pacer.remaining(), 1.0)
        pacer.before_send("G90")
        pacer.before_send("G0 X0")  # back 10 mm, absolute
        self.assertAlmostEqual(pacer.x, 0.0)
        self.assertAlmostEqual(pacer.remaining(), 2.0)

    def test_zero_length_move_adds_nothing(self):
        pacer, _ = make(pos=(50.0, 50.0))
        self.assertEqual(pacer.before_send("G0 X50 Y50"), 0.0)
        self.assertEqual(pacer.remaining(), 0.0)

    def test_unknown_feed_falls_back_to_slow(self):
        pacer, _ = make()
        pacer.before_send("G1 X10")
        self.assertAlmostEqual(pacer.remaining(), 10 / (DEFAULT_FEED / 60))

    def test_diagonal_distance(self):
        pacer, _ = make()
        pacer.before_send("G1 X30 Y40 F3000")  # 50 mm at 50 mm/s
        self.assertAlmostEqual(pacer.remaining(), 1.0)

    def test_sync_only_applies_when_idle(self):
        pacer, clock = make()
        pacer.before_send("G1 X10 F600")
        pacer.sync((99.0, 99.0))  # busy: ignored
        self.assertAlmostEqual(pacer.x, 10.0)
        clock.now += 2.0
        pacer.sync((99.0, 99.0))
        self.assertAlmostEqual(pacer.x, 99.0)

    def test_separate_one_line_calls_share_the_queue(self):
        pacer, _ = make()
        waits = [pacer.before_send(c) for c in (
            "G1 X50 F1200", "G1 X150", "G1 X250", "G1 X50")]
        self.assertEqual(waits[0], 0.0)
        self.assertEqual(waits, sorted(waits))
        self.assertGreater(waits[-1], 5.0)

    def test_short_move_is_not_split(self):
        pacer, _ = make()
        self.assertEqual(pacer.split("G1 X10 F600"), ["G1 X10 F600"])

    def test_long_absolute_move_is_split_under_the_limit(self):
        pacer, _ = make()
        segs = pacer.split("G1 X200 F1200")  # 10 s -> 10 segments
        self.assertEqual(len(segs), 10)
        self.assertEqual(segs[0], "G1 X20.000 F1200")
        self.assertEqual(segs[-1], "G1 X200.000")
        check, _ = make()
        for seg in segs:
            check.before_send(seg)
        self.assertAlmostEqual(check.x, 200.0)

    def test_split_relative_move(self):
        pacer, _ = make()
        pacer.before_send("G91")
        segs = pacer.split("G0 X-60 F1200")  # 3 s
        self.assertEqual(len(segs), 3)
        self.assertEqual(segs[1], "G0 X-20.000")

    def test_split_diagonal_keeps_both_axes(self):
        pacer, _ = make()
        segs = pacer.split("G1 X90 Y120 F3000")  # 150 mm at 50 mm/s = 3 s
        self.assertEqual(len(segs), 3)
        self.assertEqual(segs[0], "G1 X30.000 Y40.000 F3000")
        self.assertEqual(segs[-1], "G1 X90.000 Y120.000")

    def test_split_leaves_other_commands_alone(self):
        pacer, _ = make()
        for cmd in ("M3 S100", "G1 X200 S50 F1200", "G1 Z5", "G90"):
            self.assertEqual(pacer.split(cmd), [cmd])

    def test_split_does_not_change_state(self):
        pacer, _ = make()
        pacer.split("G1 X200 F1200")
        self.assertEqual((pacer.x, pacer.feed, pacer.remaining()),
                         (0.0, DEFAULT_FEED, 0.0))

    def test_queue_drains_as_time_passes(self):
        pacer, clock = make()
        pacer.before_send("G1 X10 F600")
        clock.now += 2.0
        self.assertEqual(pacer.remaining(), 0.0)
        self.assertEqual(pacer.before_send("G1 X20"), 0.0)


if __name__ == "__main__":
    unittest.main()
