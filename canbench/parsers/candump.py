"""Parser for candump / SocketCAN log files.

candump format:
    (1492883377.245934) can0 0C800040#006D00000000
    (1492883377.254733) can0 10250040#0003

Key details:
    - Timestamp in parentheses is Unix epoch seconds with microsecond precision
    - Interface name (e.g. can0, vcan1) maps to bus number
    - ID is hex; extended if > 0x7FF (or if preceded by an extended flag)
    - '#' separates ID from data payload
    - Data is a contiguous hex string (pairs of hex chars, no spaces)
    - Some variants use '##' for FD frames with flags byte
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Iterator

from .base import BaseParser, CANFrame

# Main data line pattern
# Groups: (timestamp) interface ID#DATA
_LINE_RE = re.compile(
    r"^\s*\((\d+\.\d+)\)\s+"       # (epoch.usec)
    r"(\S+)\s+"                      # interface (can0, vcan1, etc.)
    r"([0-9A-Fa-f]+)"               # arbitration ID (hex)
    r"#"                              # separator
    r"#?([0-9A-Fa-f]*)"             # data hex string (## = FD flag byte prefix)
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


class CandumpParser(BaseParser):
    """Parser for candump / SocketCAN log files."""

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

    def parse_file(self, path: Path | str, *, absolute: bool = False) -> Iterator[CANFrame]:
        """Parse a candump log file, yielding CANFrame objects.

        The first frame's timestamp becomes t=0. All subsequent timestamps
        are relative to that origin.
        """
        path = Path(path)
        t_origin: float | None = None

        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for raw_line in fh:
                line = raw_line.strip()
                if not line or line.startswith("#"):
                    continue

                m = _LINE_RE.match(line)
                if not m:
                    continue

                ts_str, iface, id_str, data_hex = m.groups()

                abs_time = float(ts_str)
                if t_origin is None:
                    t_origin = abs_time
                rel_time = abs_time - t_origin

                bus = _interface_to_bus(iface)
                arb_id = int(id_str, 16)
                is_extended = arb_id > 0x7FF
                data = _hex_string_to_bytes(data_hex)
                dlc = len(data)

                yield CANFrame(
                    timestamp=rel_time,
                    arbitration_id=arb_id,
                    data=data,
                    bus=bus,
                    is_extended=is_extended,
                    dlc=dlc,
                )
