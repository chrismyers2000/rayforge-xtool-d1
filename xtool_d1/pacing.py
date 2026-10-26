"""
Keeps the amount of motion queued on the D1 small.

The D1 acknowledges a command when it queues it, so streaming "one line,
wait for ok" would queue a whole job at once. This tracks how long the
queued moves should take (distance / feed) and tells the driver how long
to wait before sending the next one, so a cancel can only be late by
about ``lookahead`` seconds of motion.

The estimate ignores acceleration, so the real queue can run a little
longer than estimated. Unknown feeds fall back to a slow one, which
over-estimates durations and so errs on the side of a shorter queue.
"""

import math
import re
import time
from collections.abc import Callable

LOOKAHEAD_S = 0.5
# The D1 abandons a move after about 3 s, so no single move may take
# longer than this.
MAX_SEGMENT_S = 1.0
# mm/min assumed until a command sets F.
DEFAULT_FEED = 600.0

_word_re = re.compile(r"([A-Z])\s*(-?\d*\.?\d+)")


class MovePacer:
    def __init__(
        self,
        pos: tuple[float, float],
        clock: Callable[[], float] = time.monotonic,
        lookahead: float = LOOKAHEAD_S,
    ):
        self.x, self.y = pos
        self.absolute = True
        self.feed = DEFAULT_FEED
        self._clock = clock
        self._lookahead = lookahead
        self._queue_end = 0.0

    def split(self, command: str) -> list[str]:
        """
        Splits a long G0/G1 move into segments of at most MAX_SEGMENT_S,
        because the D1 cuts off any single move after about 3 s. Other
        commands, and moves it cannot split safely, come back unchanged.
        Does not change the pacer's state.
        """
        words = [(w, float(v)) for w, v in _word_re.findall(command.upper())]
        gcodes = [v for w, v in words if w == "G"]
        values = {w: v for w, v in words if w != "G"}
        if len(gcodes) != 1 or gcodes[0] not in (0, 1):
            return [command]
        if set(values) - {"X", "Y", "F"} or not set(values) & {"X", "Y"}:
            return [command]

        feed = values.get("F", self.feed)
        absolute = self.absolute
        dx = values["X"] - self.x if absolute and "X" in values else 0.0
        dy = values["Y"] - self.y if absolute and "Y" in values else 0.0
        if not absolute:
            dx = values.get("X", 0.0)
            dy = values.get("Y", 0.0)
        duration = math.hypot(dx, dy) / (feed / 60.0)
        count = math.ceil(duration / MAX_SEGMENT_S - 1e-9)
        if count <= 1:
            return [command]

        g = f"G{int(gcodes[0])}"
        segments = []
        for i in range(1, count + 1):
            parts = [g]
            for axis, start, delta in (("X", self.x, dx), ("Y", self.y, dy)):
                if axis not in values:
                    continue
                if absolute:
                    target = values[axis] if i == count else (
                        start + delta * i / count)
                    parts.append(f"{axis}{target:.3f}")
                else:
                    parts.append(f"{axis}{delta / count:.3f}")
            if i == 1 and "F" in values:
                parts.append(f"F{values['F']:g}")
            segments.append(" ".join(parts))
        return segments

    def sync(self, pos: tuple[float, float]) -> None:
        """Re-seeds the position from the machine while nothing is queued."""
        if self.remaining() == 0.0:
            self.x, self.y = pos

    def remaining(self) -> float:
        """Seconds until the queued moves should have finished."""
        return max(0.0, self._queue_end - self._clock())

    def before_send(self, command: str) -> float:
        """
        Registers *command* as sent after the returned delay (seconds).
        Non-motion commands need no delay.
        """
        words = [(w, float(v)) for w, v in _word_re.findall(command.upper())]
        gcodes = [v for w, v in words if w == "G"]
        values = {w: v for w, v in words if w != "G"}

        if 90 in gcodes:
            self.absolute = True
        if 91 in gcodes:
            self.absolute = False
        if "F" in values and values["F"] > 0:
            self.feed = values["F"]
        if not any(g in (0, 1) for g in gcodes):
            return 0.0

        x, y = self.x, self.y
        if self.absolute:
            x = values.get("X", x)
            y = values.get("Y", y)
        else:
            x += values.get("X", 0.0)
            y += values.get("Y", 0.0)
        duration = math.hypot(x - self.x, y - self.y) / (self.feed / 60.0)
        self.x, self.y = x, y

        now = self._clock()
        wait = max(0.0, self._queue_end - now - self._lookahead)
        start = max(now + wait, self._queue_end)
        self._queue_end = start + duration
        return wait
