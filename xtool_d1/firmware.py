"""
Firmware version of an xTool D1, read from what the board reports.

* Serial ``$I`` reply: ``[XTool D1:ver 40.30.002.01 B1]`` on 40.30.002.01,
  ``[xTool D1:ver 40.30.010.01 B2]`` on 40.30.010.01 (the case changed).
* Wi-Fi ``GET /system?action=version``: ``"version":"V40.30.010.01 B2"``.
"""

import re
from typing import NamedTuple

_version_re = re.compile(r"(\d+)\.(\d+)\.(\d+)\.(\d+)(?:\s*(B\d+))?", re.I)


class Firmware(NamedTuple):
    major: int
    minor: int
    patch: int
    build: int
    tag: str = ""

    def __str__(self) -> str:
        text = f"{self.major}.{self.minor}.{self.patch:03d}.{self.build:02d}"
        return f"{text} {self.tag}" if self.tag else text


#: First version known to have the Wi-Fi job API: ``GET /cnc/data?action=
#: pause|resume|stop`` and ``GET /system?action=get_working_sta``. Absent
#: from 40.30.002.01 (no such strings in its flash); a version between the
#: two was never seen.
JOB_CONTROL_SINCE = Firmware(40, 30, 10, 1)


def has_job_control(firmware: "Firmware | None") -> bool:
    """True when *firmware* has the Wi-Fi pause/resume/stop API."""
    return firmware is not None and firmware[:4] >= JOB_CONTROL_SINCE[:4]


#: First version without the USB streaming quirks of 40.30.002.01 (laser-off
#: tests, 2026-10-27): a single move is no longer abandoned after ~3 s, and
#: the D1 no longer stalls between queued moves while the port is quiet.
NO_USB_QUIRKS_SINCE = Firmware(40, 30, 10, 1)


def has_usb_quirks(firmware: "Firmware | None") -> bool:
    """True when streaming needs the 1 s move splitting and fast polling.
    An unknown firmware counts as having them."""
    return firmware is None or firmware[:4] < NO_USB_QUIRKS_SINCE[:4]


def is_identity_line(line: str) -> bool:
    """True for the board's ``$I`` identity line, in either firmware's case."""
    return line.lower().startswith("[xtool")


def parse_version(text: str) -> Firmware | None:
    """The first ``a.b.ccc.dd [Bn]`` version in *text*, or None."""
    match = _version_re.search(text)
    if not match:
        return None
    major, minor, patch, build, tag = match.groups()
    return Firmware(
        int(major), int(minor), int(patch), int(build), (tag or "").upper()
    )
