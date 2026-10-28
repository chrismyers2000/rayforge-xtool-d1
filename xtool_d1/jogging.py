"""
Keeps jogs inside the machine's limits when presses come faster than the
D1 reports its position.

Rayforge shortens each jog press against the last position the driver
reported, which is at least a second old and lags further while the head
is still moving. Presses made in quick succession were therefore each
checked against a position from before the earlier ones had moved the
head, and overshot the limit (five diagonal presses took Y from 21.9 to
-28.1 with the limit at 0). The D1 has no limit switches.

This remembers where the jogs already sent will leave the head and
shortens new jogs against that, until the reported position has caught up.
"""

import time
from collections.abc import Callable

Limits = tuple[float, float, float, float]  # x_min, y_min, x_max, y_max

#: Seconds after the last jog should have finished before the reported
#: position is trusted again (the driver reads it once a second).
REPORT_LAG_S = 1.5


def clamp_jog(pos: float, distance: float, lower: float, upper: float) -> float:
    """Shorten *distance* so *pos* does not travel past a limit. Never
    reverses or lengthens it. Already beyond a limit, a jog further out
    becomes 0 and a jog back towards the range is allowed."""
    target = pos + distance
    if distance > 0:
        target = min(target, max(upper, pos))
    elif distance < 0:
        target = max(target, min(lower, pos))
    return target - pos


class JogTracker:
    def __init__(
        self,
        clock: Callable[[], float] = time.monotonic,
        lag_s: float = REPORT_LAG_S,
    ):
        self._clock = clock
        self._lag_s = lag_s
        self._target: dict[str, float] = {}
        self._busy_until = 0.0

    def reset(self) -> None:
        """Forget queued jogs (a job starts, or the head was stopped)."""
        self._target = {}
        self._busy_until = 0.0

    def clamp(
        self,
        reported: dict[str, float | None],
        deltas: dict[str, float],
        limits: Limits | None,
        speed: float,
    ) -> dict[str, float]:
        """*deltas* (axis letter -> mm) shortened to stay inside *limits*.
        *reported* is the last reported position by axis letter; an axis
        with no reported position, or no *limits*, is left alone."""
        if limits is None:
            return dict(deltas)
        x_min, y_min, x_max, y_max = limits
        bounds = {"x": (x_min, x_max), "y": (y_min, y_max)}

        now = self._clock()
        queued = now < self._busy_until + self._lag_s
        if not queued:
            self._target = {}

        out: dict[str, float] = {}
        for axis, distance in deltas.items():
            if axis not in bounds:
                out[axis] = distance
                continue
            base = self._target.get(axis) if queued else None
            if base is None:
                base = reported.get(axis)
            if base is None:
                out[axis] = distance  # position unknown: cannot clamp
                continue
            lower, upper = bounds[axis]
            allowed = clamp_jog(base, distance, lower, upper)
            out[axis] = allowed
            self._target[axis] = base + allowed

        travel = max((abs(d) for d in out.values()), default=0.0)
        if travel > 0 and speed > 0:
            self._busy_until = max(self._busy_until, now) + travel / (
                speed / 60.0
            )
        return out
