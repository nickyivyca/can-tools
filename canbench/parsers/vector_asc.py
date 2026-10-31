"""Parser for Vector CANalyzer / CANdb++ ASC log files.

Vector ASC format (base hex, timestamps absolute):
    date Sat Mar 08 04:14:24 PM 2025
    base hex  timestamps absolute
    no internal events logged
    // version 7.1.1
    3824324055.7855 1      215                Rx  d 7 00 00 00 57 00 50 8F

Key details:
    - Header lines: 'date ...', 'base hex ...', 'no internal events logged',
      '// version ...'
    - Data lines: <float_ts> <channel> <id_hex> <dir> d <dlc> <data bytes...>
    - Timestamps are absolute (large epoch-like floats); made relative to first frame
    - Channel is an integer (1-based in practice)
    - ID is hex (no 0x prefix)
    - Direction is 'Rx' or 'Tx'
    - 'd' signals a data frame; DLC follows, then data bytes in hex
    - Extended ID inferred from value > 0x7FF (no explicit flag in standard ASC)
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Iterator

from .base import BaseParser, CANFrame

# Data line: <timestamp> <channel> <id> <dir> d <dlc> [bytes...]
# Handles both standard and CODACAN-style ASC.
# Example: 3824324055.7855 1      215                Rx  d 7 00 00 00 57 00 50 8F
_LINE_RE = re.compile(
    r"^\s*(\d+\.\d+)"          # timestamp (large float)
    r"\s+(\d+)"                 # channel
    r"\s+([0-9A-Fa-f]+)"       # arbitration ID (hex, no prefix)
    r"\s+\S+"                   # direction (Rx/Tx) or extended flag
    r"\s+d"                     # frame type 'd' = data
    r"\s+(\d+)"                 # DLC
    r"((?:\s+[0-9A-Fa-f]{1,2})*)"  # data bytes (space-separated hex)
)

# Header markers used for format detection
_HEADER_MARKERS = ("base hex", "base dec", "date ", "no internal events")


class VectorAscParser(BaseParser):
    """Parser for Vector CANalyzer ASC log files."""

    @staticmethod
    def detect(lines: list[str]) -> bool:
        """Return True if sample lines look like a Vector ASC file.

        Looks for the characteristic 'base hex' or 'base dec' header line,
        or the 'date ...' line typical of ASC files.
        """
        for line in lines:
            lower = line.lower()
            for marker in _HEADER_MARKERS:
                if lower.startswith(marker):
                    return True
        return False

    def parse_file(self, path: Path | str, *, absolute: bool = False) -> Iterator[CANFrame]:
        """Parse a Vector ASC log file, yielding CANFrame objects.

        The first data frame's timestamp becomes t=0 unless absolute=True.
        """
        path = Path(path)
        t_origin: float | None = None

        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for raw_line in fh:
                line = raw_line.strip()
                if not line:
                    continue
                # Skip header / comment lines
                lower = line.lower()
                if (lower.startswith("date ")
                        or lower.startswith("base ")
                        or lower.startswith("no internal")
                        or lower.startswith("//")
                        or lower.startswith("begin")
                        or lower.startswith("end")):
                    continue

                m = _LINE_RE.match(line)
                if not m:
                    continue

                ts_str, ch_str, id_str, dlc_str, data_part = m.groups()

                abs_time = float(ts_str)
                if t_origin is None:
                    t_origin = abs_time
                timestamp = abs_time if absolute else (abs_time - t_origin)

                bus = int(ch_str)
                arb_id = int(id_str, 16)
                is_extended = arb_id > 0x7FF
                dlc = int(dlc_str)

                # data_part is the remainder like " 00 00 57 00 50 8F"
                raw_bytes = data_part.split()
                data = bytes(int(b, 16) for b in raw_bytes[:dlc])

                yield CANFrame(
                    timestamp=timestamp,
                    arbitration_id=arb_id,
                    data=data,
                    bus=bus,
                    is_extended=is_extended,
                    dlc=dlc,
                )
