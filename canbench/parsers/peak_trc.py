"""Parser for PEAK PCAN TRC (trace) log files.

Supports both major versions:

Version 2.0:
    ;$FILEVERSION=2.0
    ;   Start time: 01/28/2025 20:06:09
    ;
    ;   Message Number) Time Offset   Type  ID    Data Length  Data Bytes
         1)      0.0000 Rx  0123  8  01 02 03 04 05 06 07 08

Version 1.1:
    ;$FILEVERSION=1.1
    ;
    ;   Start time: ...
    ;    Message Number) Time   Type  ID     Data Length  Data
          1     0.0000   Rx     0123  8  01 02 03 04 05 06 07 08

Key details:
    - Lines starting with ; are comments/headers
    - $FILEVERSION in header indicates format version
    - IDs are plain hex (no 0x prefix)
    - Time offset is in seconds from start of capture
    - Data bytes are space-separated hex
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Iterator

from .base import BaseParser, CANFrame

# Version 2.0 data line: "   1)      0.0000 Rx  0123  8  01 02 ..."
_V2_DATA_RE = re.compile(
    r"^\s*(\d+)\)\s+"            # message number with closing paren
    r"([\d.]+)\s+"               # time offset (seconds)
    r"(Tx|Rx)\s+"                # direction
    r"([0-9A-Fa-f]+)\s+"         # ID (hex, no 0x prefix)
    r"(\d+)\s+"                  # DLC
    r"(.*)",                     # data bytes
    re.IGNORECASE,
)

# Version 1.1 data line: "    1     0.0000   Rx     0123  8  01 02 ..."
_V1_DATA_RE = re.compile(
    r"^\s*(\d+)\s+"              # message number (no closing paren)
    r"([\d.]+)\s+"               # time offset (seconds)
    r"(Tx|Rx)\s+"                # direction
    r"([0-9A-Fa-f]+)\s+"         # ID (hex, no 0x prefix)
    r"(\d+)\s+"                  # DLC
    r"(.*)",                     # data bytes
    re.IGNORECASE,
)

_VERSION_RE = re.compile(r";\$FILEVERSION\s*=\s*([\d.]+)", re.IGNORECASE)


class PeakTrcParser(BaseParser):
    """Parser for PEAK PCAN TRC log files."""

    @staticmethod
    def detect(lines: list[str]) -> bool:
        """Return True if sample lines look like a PEAK TRC file.

        Primary indicator is the ;$FILEVERSION= line. Secondary is the
        characteristic comment/data pattern.
        """
        for line in lines:
            stripped = line.strip()
            if _VERSION_RE.match(stripped):
                return True
        # Fallback: look for comment headers + numbered data lines
        has_comment_header = False
        has_numbered_data = False
        for line in lines:
            stripped = line.strip()
            if stripped.startswith(";"):
                has_comment_header = True
            elif re.match(r"^\s*\d+[\s)]", stripped):
                has_numbered_data = True
        return has_comment_header and has_numbered_data

    def parse_file(self, path: Path | str, *, absolute: bool = False) -> Iterator[CANFrame]:
        """Parse a PEAK TRC file, yielding CANFrame objects.

        Auto-detects version 1.1 vs 2.0 from the header. Time offsets
        are used directly (they are already relative to capture start).
        """
        path = Path(path)
        version = "2.0"  # default assumption
        t_origin: float | None = None

        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for raw_line in fh:
                line = raw_line.strip()
                if not line:
                    continue

                # --- Comment / header lines ---
                if line.startswith(";"):
                    vm = _VERSION_RE.match(line)
                    if vm:
                        version = vm.group(1)
                    continue

                # --- Data lines ---
                frame = self._parse_data_line(line, version)
                if frame is None:
                    continue

                # Make timestamps relative to the first frame
                if t_origin is None:
                    t_origin = frame.timestamp
                frame = CANFrame(
                    timestamp=frame.timestamp - t_origin,
                    arbitration_id=frame.arbitration_id,
                    data=frame.data,
                    bus=frame.bus,
                    is_extended=frame.is_extended,
                    dlc=frame.dlc,
                    is_tx=frame.is_tx,
                )

                yield frame

    @staticmethod
    def _parse_data_line(line: str, version: str) -> CANFrame | None:
        """Attempt to parse a single data line. Returns CANFrame or None."""
        # Try version-specific pattern first, then fall back
        if version.startswith("2"):
            m = _V2_DATA_RE.match(line)
            if not m:
                m = _V1_DATA_RE.match(line)
        else:
            m = _V1_DATA_RE.match(line)
            if not m:
                m = _V2_DATA_RE.match(line)

        if not m:
            return None

        _msg_num, time_str, direction, id_str, dlc_str, data_str = m.groups()

        try:
            timestamp = float(time_str)
            arb_id = int(id_str, 16)
            dlc = int(dlc_str)
        except ValueError:
            return None

        is_tx = direction.upper() == "TX"
        is_extended = arb_id > 0x7FF

        # Parse data bytes (space-separated hex)
        data_tokens = data_str.split()
        try:
            data_bytes = bytes(int(b, 16) for b in data_tokens[:dlc])
        except ValueError:
            data_bytes = b""

        return CANFrame(
            timestamp=timestamp,
            arbitration_id=arb_id,
            data=data_bytes,
            bus=0,  # TRC files don't typically indicate bus/channel
            is_extended=is_extended,
            dlc=dlc,
            is_tx=is_tx,
        )
