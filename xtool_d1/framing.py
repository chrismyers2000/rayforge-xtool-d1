"""Framing with the D1's crosshair (framing laser) on.

Rayforge's frame job starts with ``M106 S0`` (framing laser off) and no
``M3``. The D1 shows the crosshair when ``M106 S1`` is sent, so a frame
job turns the first ``M106 S0`` into ``M106 S1``; the last stays.
"""


def crosshair_on(gcode: str) -> str:
    """Turn the first ``M106 S0`` into ``M106 S1``. The line count stays
    the same, so line numbers in the job still match."""
    lines = gcode.splitlines()
    for i, line in enumerate(lines):
        if line.strip().startswith("M106 S0"):
            lines[i] = "M106 S1 ;Framing laser on"
            break
    return "\n".join(lines) + "\n"
