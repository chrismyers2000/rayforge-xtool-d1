"""
Rewrite a job so firmware 40.30.010.01 runs it without stopping at joins.

On 40.30.010.01 the head hitches or stops between moves that 40.30.002.01
ran smoothly (seen on rasters, 2026-10-26):

* The planner's junction speed defaults to ``M204 ... U300`` (reported as
  ``U``, set with ``M204 F``), so the head nearly stops at every join.
* Each ``M3`` makes the D1 wait until the head stops, so a power change
  written as ``M3 S..`` between moves stops it, and the first join after
  an ``M3`` hitches.

The fix, verified on one D1: set ``M204 F6000`` and ``M205 X1800`` up
front, send one ``M3 S0``, then carry the power as ``S`` on each move,
with ``S0`` on travel moves instead of ``M5``. The job ends by restoring
the D1's defaults (``M204 F300``, ``M205 X450``).
"""

import re

from .firmware import Firmware

#: First version that needs the rewrite. 40.30.002.01 has no ``M204``.
SMOOTH_SINCE = Firmware(40, 30, 10, 1)

SETUP = ["M204 F6000 ;Junction speed", "M205 X1800 ;X jerk"]
RESTORE = ["M204 F300 ;Restore junction speed", "M205 X450 ;Restore X jerk"]

_word_re = re.compile(r"([A-Z])\s*(-?\d+(?:\.\d*)?)", re.I)


def needs_smoothing(firmware: "Firmware | None") -> bool:
    """True when *firmware* stops at joins unless the job is rewritten."""
    return firmware is not None and firmware[:4] >= SMOOTH_SINCE[:4]


def _words(code: str) -> dict[str, str]:
    return {k.upper(): v for k, v in _word_re.findall(code)}


def _fmt(power: float) -> str:
    return f"{power:.0f}"


def smooth_lines(gcode: str) -> tuple[list[str], list[int | None]]:
    """Move laser power from ``M3``/``M5`` lines onto the moves.

    Returns the new lines and, for each, the index of the line it came
    from in *gcode* (None for lines this adds), so that a caller that
    streams the job can still tell which operation a line belongs to."""
    lines = gcode.splitlines()
    codes = [line.split(";", 1)[0].strip().upper() for line in lines]
    last_move = max(
        (i for i, c in enumerate(codes) if c.startswith(("G0", "G1"))),
        default=-1,
    )

    out: list[tuple[str, int | None]] = [(line, None) for line in SETUP]
    laser_mode = False  # one M3 S0 sent
    power = 0.0  # what the job asks for now
    sent = None  # the S value the D1 holds, if known
    for i, (line, code) in enumerate(zip(lines, codes)):
        if i > last_move or not code:
            out.append((line, i))
            continue
        words = _words(code)
        if code.startswith("M3") or code.startswith("M4"):
            if not laser_mode:
                out.append(("M3 S0", i))
                laser_mode, sent = True, 0.0
            power = float(words.get("S", power))
            continue
        if code.startswith("M5"):
            power = 0.0
            continue
        if code.startswith(("G0", "G1")):
            if "S" in words:
                sent = float(words["S"])
            else:
                want = 0.0 if code.startswith("G0") else power
                if sent != want:
                    line = f"{line.split(';', 1)[0].rstrip()} S{_fmt(want)}"
                    sent = want
        out.append((line, i))

    end = len(out)
    while end > 0 and not out[end - 1][0].strip():
        end -= 1
    out[end:end] = [(line, None) for line in RESTORE]
    if laser_mode and not any(
        c.startswith("M5") for c in codes[last_move + 1:]
    ):
        out.insert(end, ("M5", None))
    return [line for line, _ in out], [src for _, src in out]


def smooth_moves(gcode: str) -> str:
    """:func:`smooth_lines` as one string."""
    return "\n".join(smooth_lines(gcode)[0]) + "\n"
