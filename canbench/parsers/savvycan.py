"""Parser for SavvyCAN CSV log files.

SavvyCAN CSV format (typical):
    Time Stamp,ID,Extended,Bus,LEN,D1,D2,D3,D4,D5,D6,D7,D8
    0.000000,0x123,false,0,8,0x01,0x02,0x03,...

Also handles the numeric-only variant (no header row, no 0x prefixes)
and variations in column naming.
"""
from __future__ import annotations

import csv
import re
from pathlib import Path
from typing import Iterator

from .base import BaseParser, CANFrame

# Expected header tokens (case-insensitive substring matches)
_HEADER_TOKENS = {"time", "stamp", "id", "bus", "len", "d1"}

# Quick regex to check if a line looks like SavvyCAN CSV data
_CSV_DATA_RE = re.compile(
    r"^\s*[\d.]+\s*,"   # timestamp (float), comma
    r"\s*0?x?[0-9A-Fa-f]+\s*,"  # ID (with or without 0x), comma
)


def _parse_int(value: str) -> int:
    """Parse an integer that may have a 0x prefix."""
    value = value.strip()
    if value.lower().startswith("0x"):
        return int(value, 16)
    return int(value)


def _parse_bool(value: str) -> bool:
    """Parse a boolean from common string representations."""
    return value.strip().lower() in ("true", "1", "yes")


class SavvyCANParser(BaseParser):
    """Parser for SavvyCAN CSV log files."""

    @staticmethod
    def detect(lines: list[str]) -> bool:
        """Return True if sample lines look like a SavvyCAN CSV.

        Detection heuristics:
        1. A header row containing 'Time Stamp' and 'ID' (case-insensitive).
        2. Or multiple CSV data rows matching the timestamp,id,... pattern.
        """
        header_found = False
        data_rows = 0

        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue

            # Check for header
            lower = stripped.lower()
            if "time" in lower and "id" in lower and "," in lower:
                # Count how many expected tokens appear
                matches = sum(1 for tok in _HEADER_TOKENS if tok in lower)
                if matches >= 3:
                    header_found = True
                    continue

            # Check for data rows
            if _CSV_DATA_RE.match(stripped):
                data_rows += 1

        return header_found or data_rows >= 3

    def parse_file(self, path: Path | str, *, absolute: bool = False) -> Iterator[CANFrame]:
        """Parse a SavvyCAN CSV file, yielding CANFrame objects.

        Supports both header and headerless variants. Timestamps are used
        as-is (SavvyCAN typically provides seconds from start).
        """
        path = Path(path)
        t_origin: float | None = None

        with open(path, "r", encoding="utf-8", errors="replace", newline="") as fh:
            # Peek at the first non-empty line to decide if there's a header
            first_line = ""
            for first_line in fh:
                first_line = first_line.strip()
                if first_line:
                    break

            has_header = bool(
                first_line
                and not first_line[0].isdigit()
                and "," in first_line
            )

            # Rewind and use csv reader
            fh.seek(0)
            reader = csv.reader(fh)

            if has_header:
                # Read and normalise header
                raw_header = next(reader)
                header = [h.strip().lower().replace(" ", "") for h in raw_header]

                # Build column index map
                col_map = self._build_column_map(header)
                if col_map is None:
                    return  # unrecognised header layout

                for row in reader:
                    if not row or not row[0].strip():
                        continue
                    frame = self._parse_row_mapped(row, col_map, t_origin)
                    if frame is None:
                        continue
                    if t_origin is None:
                        t_origin = frame.timestamp
                        frame = CANFrame(
                            timestamp=0.0,
                            arbitration_id=frame.arbitration_id,
                            data=frame.data,
                            bus=frame.bus,
                            is_extended=frame.is_extended,
                            dlc=frame.dlc,
                            is_tx=frame.is_tx,
                        )
                    else:
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
            else:
                # Headerless: assume positional columns
                # timestamp, id, extended, bus, len, d1, d2, ...
                for row in reader:
                    if not row or not row[0].strip():
                        continue
                    frame = self._parse_row_positional(row, t_origin)
                    if frame is None:
                        continue
                    if t_origin is None:
                        t_origin = frame.timestamp
                        frame = CANFrame(
                            timestamp=0.0,
                            arbitration_id=frame.arbitration_id,
                            data=frame.data,
                            bus=frame.bus,
                            is_extended=frame.is_extended,
                            dlc=frame.dlc,
                            is_tx=frame.is_tx,
                        )
                    else:
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

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _build_column_map(header: list[str]) -> dict[str, int] | None:
        """Map logical field names to column indices from a normalised header."""
        col = {}

        # Timestamp
        for i, h in enumerate(header):
            if "timestamp" in h or "time" in h:
                col["time"] = i
                break
        else:
            return None

        # ID
        for i, h in enumerate(header):
            if h == "id" or "canid" in h:
                col["id"] = i
                break
        else:
            return None

        # Extended (optional)
        for i, h in enumerate(header):
            if "extended" in h or "ext" == h:
                col["extended"] = i
                break

        # Bus (optional)
        for i, h in enumerate(header):
            if "bus" in h or "channel" in h:
                col["bus"] = i
                break

        # Length
        for i, h in enumerate(header):
            if h in ("len", "length", "dlc"):
                col["len"] = i
                break
        else:
            return None

        # Data bytes: D1..D8 or data columns
        data_cols = []
        for i, h in enumerate(header):
            if re.match(r"^d\d+$", h):
                data_cols.append(i)
        if data_cols:
            col["data_start"] = min(data_cols)
        else:
            # Assume data follows length column
            col["data_start"] = col["len"] + 1

        return col

    @staticmethod
    def _parse_row_mapped(
        row: list[str], col: dict[str, int], t_origin: float | None
    ) -> CANFrame | None:
        """Parse a CSV row using a column map. Returns raw (non-relative) timestamp."""
        try:
            timestamp = float(row[col["time"]].strip())
            arb_id = _parse_int(row[col["id"]])
            dlc = int(row[col["len"]].strip())

            is_extended = False
            if "extended" in col:
                is_extended = _parse_bool(row[col["extended"]])
            elif arb_id > 0x7FF:
                is_extended = True

            bus = 0
            if "bus" in col:
                bus = int(row[col["bus"]].strip())

            data_start = col["data_start"]
            data_bytes = bytes(
                _parse_int(row[data_start + i])
                for i in range(dlc)
                if (data_start + i) < len(row) and row[data_start + i].strip()
            )

            return CANFrame(
                timestamp=timestamp,
                arbitration_id=arb_id,
                data=data_bytes,
                bus=bus,
                is_extended=is_extended,
                dlc=dlc,
            )
        except (ValueError, IndexError):
            return None

    @staticmethod
    def _parse_row_positional(row: list[str], t_origin: float | None) -> CANFrame | None:
        """Parse a CSV row by positional columns (headerless variant)."""
        try:
            timestamp = float(row[0].strip())
            arb_id = _parse_int(row[1])

            # Column 2 might be 'extended' (bool) or 'bus' (int)
            is_extended = False
            bus = 0
            idx = 2

            if len(row) > 2:
                val = row[2].strip().lower()
                if val in ("true", "false", "1", "0", "yes", "no"):
                    is_extended = _parse_bool(val)
                    idx = 3

            if len(row) > idx:
                try:
                    bus = int(row[idx].strip())
                    idx += 1
                except ValueError:
                    pass

            dlc = int(row[idx].strip())
            idx += 1

            if arb_id > 0x7FF:
                is_extended = True

            data_bytes = bytes(
                _parse_int(row[idx + i])
                for i in range(dlc)
                if (idx + i) < len(row) and row[idx + i].strip()
            )

            return CANFrame(
                timestamp=timestamp,
                arbitration_id=arb_id,
                data=data_bytes,
                bus=bus,
                is_extended=is_extended,
                dlc=dlc,
            )
        except (ValueError, IndexError):
            return None
