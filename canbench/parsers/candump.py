"""Parser for candump / SocketCAN log files.

candump format:
    (1492883377.245934) can0 0C800040#006D00000000
    (1492883377.254733) can0 10250040#0003

Key details:
    - Timestamp in parentheses is Unix epoch seconds with microsecond precision
    - Interface name (e.g. can0, vcan1) maps to bus number
    - ID is hex, 3 digits for an 11-bit ID and 8 for a 29-bit one (can-utils
      lib.c), so the width says which: 8 digits is extended even when the value
      is small. A value above 0x7FF is extended whatever the width.
    - '#' separates ID from data payload
    - Data is a contiguous hex string (pairs of hex chars, no spaces)
    - '##<flags>' marks a CAN FD frame: one hex digit of flags, then the data
    - '#R' (optionally '#R<len>') is a remote request: not a data frame, so it
      is skipped and counted in ``self.skipped['remote']``
    - An ID with CAN_ERR_FLAG (0x20000000) set is a SocketCAN error frame
      (python-can writes ``20000080#...``). It is skipped unless asked for:
      ``include_events=True`` yields a CANEvent ``ERR``, ``include_errors=True``
      a CANFrame with ``is_error`` set, both with the error-class mask
      (ID & 0x1FFFFFFF) in ``arbitration_id`` and the 8 data bytes as written --
      the same layout as a BUSMASTER ``ERR`` line.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from pathlib import Path
from collections import Counter
from typing import Iterator

from .base import UTC, BaseParser, CANEvent, CANFrame, CaptureStart

CAN_ERR_FLAG = 0x20000000
CAN_EFF_MASK = 0x1FFFFFFF

# Main data line pattern
# Groups: (timestamp) interface ID#DATA
_LINE_RE = re.compile(
    r"^\s*\((\d+\.\d+)\)\s+"       # (epoch.usec)
    r"(\S+)\s+"                      # interface (can0, vcan1, etc.)
    r"([0-9A-Fa-f]+)"               # arbitration ID (hex)
    r"#"                              # separator
    r"(#[0-9A-Fa-f]|R\d*)?"          # '##<flags>' (CAN FD) or 'R[len]' (remote)
    r"([0-9A-Fa-f]*)"               # data hex string
)

# Extract bus number from interface name like "can0", "vcan1"
_BUS_RE = re.compile(r"(\d+)$")


def _interface_to_bus(iface: str) -> int:
    """Extract bus number from interface name (e.g. 'can0' -> 0)."""
    m = _BUS_RE.search(iface)
    return int(m.group(1)) if m else 0


def _hex_string_to_bytes(hex_str: str) -> bytes:
    """Convert a contiguous hex string to bytes (e.g. '006D0000' -> b'\\x00\\x6d...')."""
    # Ensure even length
    if len(hex_str) % 2 != 0:
        hex_str = "0" + hex_str
    return bytes.fromhex(hex_str)


_START_LINES = 64


def first_frame_start(path: Path | str) -> CaptureStart | None:
    """The first line's ``(epoch)`` as a ``utc`` :class:`CaptureStart` (searched in the
    first 64 lines), or None. ``seconds`` is the timestamp as written, parsed as a float;
    ``wall`` is exact to the microsecond (from the digits, not the float)."""
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for i, line in enumerate(fh):
            if i >= _START_LINES:
                break
            m = _LINE_RE.match(line.strip())
            if m:
                ts = m.group(1)
                whole, frac = ts.split(".")
                wall = (datetime(1970, 1, 1) + timedelta(seconds=int(whole),
                                                         microseconds=int(frac[:6].ljust(6, "0"))))
                return CaptureStart(float(ts), UTC, wall, None, "candump first frame epoch")
    return None


class CandumpParser(BaseParser):
    """Parser for candump / SocketCAN log files."""

    def start(self, path: Path | str) -> CaptureStart | None:
        """See :func:`first_frame_start`."""
        return first_frame_start(path)

    def __init__(self) -> None:
        self.skipped: Counter = Counter()

    @staticmethod
    def detect(lines: list[str]) -> bool:
        """Return True if sample lines look like candump output.

        We look for the characteristic (timestamp) canX ID#DATA pattern.
        """
        matches = 0
        for line in lines:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if _LINE_RE.match(stripped):
                matches += 1
        return matches >= 2

    def parse_file(self, path: Path | str, *, absolute: bool = False,
                   include_errors: bool = False,
                   include_events: bool = False) -> Iterator[CANFrame | CANEvent]:
        """Parse a candump log file, yielding CANFrame objects.

        The first line's timestamp becomes t=0. All subsequent timestamps
        are relative to that origin.

        Error frames are skipped unless *include_events* (a CANEvent ``ERR``)
        or *include_errors* (a CANFrame with ``is_error``) asks for them;
        *include_events* takes precedence. Remote requests are skipped and
        counted in ``self.skipped``.
        """
        path = Path(path)
        t_origin: float | None = None
        self.skipped = Counter()

        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for raw_line in fh:
                line = raw_line.strip()
                if not line or line.startswith("#"):
                    continue

                m = _LINE_RE.match(line)
                if not m:
                    continue

                ts_str, iface, id_str, marker, data_hex = m.groups()

                abs_time = float(ts_str)
                if t_origin is None:
                    t_origin = abs_time
                rel_time = abs_time - t_origin

                bus = _interface_to_bus(iface)
                arb_id = int(id_str, 16)
                data = _hex_string_to_bytes(data_hex)
                dlc = len(data)

                if arb_id & CAN_ERR_FLAG:
                    mask = arb_id & CAN_EFF_MASK
                    if include_events:
                        yield CANEvent(rel_time, bus, "ERR", data, False, mask)
                    elif include_errors:
                        yield CANFrame(timestamp=rel_time, arbitration_id=mask, data=data,
                                       bus=bus, is_extended=False, dlc=dlc, is_error=True)
                    continue
                if marker and marker[0] in "Rr":
                    self.skipped["remote"] += 1
                    continue
                is_extended = len(id_str) > 3 or arb_id > 0x7FF

                yield CANFrame(
                    timestamp=rel_time,
                    arbitration_id=arb_id,
                    data=data,
                    bus=bus,
                    is_extended=is_extended,
                    dlc=dlc,
                )
