"""Parser for PEAK PCAN TRC (trace) log files, versions 1.0 to 2.1.

Written against PEAK-System's "PEAK CAN TRC File Format" (edition 2025-08-04).
Version 3.0 files are read for their CAN and CAN FD lines; CAN XL lines are
skipped and counted.

    1.0   "N)  O  ID  DLC  data"                ('ERROR' / 'RTR' in the data
                                                 field, ID FFFFFFFF = warning)
    1.1   "N)  O  Type  ID  DLC  data"          Type Rx, Tx, Warng, Error
    1.2   "N)  O  Bus  Type  ID  DLC  data"
    1.3   "N)  O  Bus  Type  ID  -  DLC  data"
    2.0+  columns named by the ;$COLUMNS= header, e.g. 2.0 "N,O,T,I,d,l,D",
          2.1 "N,O,T,B,I,d,R,L,D"; message types DT, FD, FB, FE, BI, RR and
          the non-frame types ST, ER, EC (2.0+) and EV (2.1+)

Key details:
    - The time offset is in MILLISECONDS in every version (0.1 ms resolution
      in 1.1, 1 us from 1.2 on).
    - ;$STARTTIME= is days since 1899-12-30 (fraction = time of day).
    - Lines starting with ; are comments, except the ;$ keywords.
    - Files without a Bus column are bus 1.
    - Non-frame lines become CANEvent records, yielded only with
      include_events=True; kinds are the log's own tags (see base.CANEvent).
    - Remote requests (RR, 'RTR') and CAN XL frames are not data frames and
      are skipped; parse_file() counts every skipped line in self.skipped.

Before 2026-10-30 this parser read the offset as seconds (every interval
1000x too long) and matched no 2.0 data line at all; no TRC file had been
read through it on the corpus.
"""
from __future__ import annotations

import re
from collections import Counter
from pathlib import Path
from typing import Iterator

from .base import BaseParser, CANEvent, CANFrame

_VERSION_RE = re.compile(r";\$FILEVERSION\s*=\s*([\d.]+)", re.IGNORECASE)
_STARTTIME_RE = re.compile(r";\$STARTTIME\s*=\s*([\d.]+)", re.IGNORECASE)
_COLUMNS_RE = re.compile(r";\$COLUMNS\s*=\s*([A-Za-z,\s]+)")
_HEX_BYTE_RE = re.compile(r"^[0-9A-Fa-f]{1,2}$")

# Spec defaults, used only if a 2.x file lacks its $COLUMNS line.
_DEFAULT_COLUMNS = {"2.0": "N,O,T,I,d,l,D", "2.1": "N,O,T,B,I,d,R,L,D"}

_FRAME_TYPES = {"DT", "FD", "FB", "FE", "BI"}
_EVENT_TYPES = {"ST", "ER", "EC", "EV"}

# Days from 1899-12-30 (the TRC/OLE epoch) to 1970-01-01.
_OLE_TO_UNIX_DAYS = 25569


def start_time(path: Path | str) -> float | None:
    """Unix time of the trace start from ;$STARTTIME, or None if absent.

    The value is taken as UTC. That is right for the files canbench and the
    Android loggers write (owner ruling 2026-11-13: $STARTTIME is UTC; such a
    file also has a "Start time (local)" comment, see :func:`start_local`).
    PEAK's recorders write local time there (their spec), so for a PEAK file the
    absolute value is off by the recorder's UTC offset; deltas are exact either way.
    """
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            s = line.strip()
            if s and not s.startswith(";"):
                return None
            m = _STARTTIME_RE.match(s)
            if m:
                return (float(m.group(1)) - _OLE_TO_UNIX_DAYS) * 86400.0
    return None


_START_LOCAL_RE = re.compile(r"^;\s*Start time \(local\):\s*(\S+)(?:\s+(\S+))?\s*$")


def start_local(path: Path | str):
    """``(datetime with its UTC offset, IANA zone name or None)`` from the header's
    ``;   Start time (local): 2026-11-13T08:54:10.697-08:00 America/Los_Angeles`` line,
    which canbench's TRC writer and the Android loggers write (2026-11-13); None if
    the file has none (a PEAK-written file, or an older canbench one) or it does not parse."""
    from datetime import datetime
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            s = line.strip()
            if s and not s.startswith(";"):
                return None
            m = _START_LOCAL_RE.match(s)
            if m:
                try:
                    when = datetime.fromisoformat(m.group(1))
                except ValueError:
                    return None
                if when.utcoffset() is None:
                    return None
                return when, m.group(2)
    return None


def _hex_bytes(tokens: list[str], limit: int | None = None) -> bytes:
    """Leading 2-digit hex tokens as bytes; stops at the first other token
    (a '--' filler, a flag name such as BUSLIGHT, or 'RTR')."""
    out = []
    for t in tokens:
        if limit is not None and len(out) >= limit:
            break
        if not _HEX_BYTE_RE.match(t):
            break
        out.append(int(t, 16))
    return bytes(out)


def _is_extended(id_tok: str, arb_id: int) -> bool:
    # PEAK writes 4 digits for an 11-bit ID (3 in 3.0) and 8 for a 29-bit one, even
    # when its value is small. python-can 4.6.1's TRCWriter writes 7 for a 29-bit ID,
    # so anything wider than 4 digits is extended.
    return len(id_tok) > 4 or arb_id > 0x7FF


class PeakTrcParser(BaseParser):
    """Parser for PEAK PCAN TRC log files."""

    def __init__(self) -> None:
        self.skipped: Counter = Counter()

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

    def parse_file(self, path: Path | str, *, absolute: bool = False,
                   include_events: bool = False) -> Iterator[CANFrame | CANEvent]:
        """Parse a PEAK TRC file, yielding CANFrame objects (and CANEvent
        objects in file order if *include_events* is True).

        By default the first data line of any kind (frame or event) is t=0,
        so the origin does not depend on *include_events*.  If *absolute* is
        True, timestamps are seconds from midnight of ;$STARTTIME plus the
        line's offset, matching BUSMASTER's absolute mode; a file without
        ;$STARTTIME falls back to the trace offset alone.
        """
        path = Path(path)
        self.skipped = Counter()
        version = "1.0"           # a file without $FILEVERSION is 1.0
        columns: list[str] | None = None
        day_seconds = 0.0
        t_origin: float | None = None

        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for raw_line in fh:
                line = raw_line.strip()
                if not line:
                    continue

                if line.startswith(";"):
                    m = _VERSION_RE.match(line)
                    if m:
                        version = m.group(1)
                        continue
                    m = _STARTTIME_RE.match(line)
                    if m:
                        day_seconds = (float(m.group(1)) % 1.0) * 86400.0
                        continue
                    m = _COLUMNS_RE.match(line)
                    if m:
                        columns = [c.strip() for c in m.group(1).split(",") if c.strip()]
                    continue

                if version.startswith("1"):
                    rec = self._parse_v1(line, version)
                else:
                    if columns is None:
                        columns = _DEFAULT_COLUMNS.get(version, _DEFAULT_COLUMNS["2.1"]).split(",")
                    rec = self._parse_v2(line, version, columns)
                if rec is None:
                    self.skipped["unparsed"] += 1
                    continue
                kind, offset_ms, rec = rec

                offset = offset_ms / 1000.0
                if absolute:
                    timestamp = day_seconds + offset
                else:
                    if t_origin is None:
                        t_origin = offset
                    timestamp = offset - t_origin

                if kind == "frame":
                    rec.timestamp = timestamp
                    yield rec
                elif kind == "event":
                    if include_events:
                        rec.timestamp = timestamp
                        yield rec
                else:
                    self.skipped[kind] += 1

    # ---- version 1.x: fixed columns --------------------------------------

    def _parse_v1(self, line: str, version: str):
        """(kind, offset_ms, record) for a 1.x line, or None if it is not one.
        kind is 'frame', 'event', or the name of a skipped line type."""
        toks = line.split()
        try:
            if not toks[0].rstrip(")").isdigit():
                return None
            offset_ms = float(toks[1])
            i = 2
            bus = 1
            if version >= "1.2":
                bus = int(toks[i])
                i += 1
            typ = "Rx"
            if version >= "1.1":
                typ = toks[i]
                i += 1
            id_tok = toks[i]
            arb_id = int(id_tok, 16)
            i += 1
            if version >= "1.3":
                i += 1                     # Reserved column ('-' on CAN buses)
            dlc = int(toks[i])
            rest = toks[i + 1:]
        except (IndexError, ValueError):
            return None

        if version < "1.1":                # 1.0: kind is in the ID and data fields
            if rest and rest[0].upper() == "ERROR":
                typ, rest = "Error", rest[1:]
            elif id_tok.upper() == "FFFFFFFF":
                typ = "Warng"
        if typ == "Warng":
            data = bytes(int(t, 16) for t in rest if _HEX_BYTE_RE.match(t))[:4]
            return "event", offset_ms, CANEvent(0.0, bus, "Warng", data)
        if typ == "Error":
            return "event", offset_ms, CANEvent(0.0, bus, "Error", _hex_bytes(rest, 4),
                                                arbitration_id=arb_id)
        if typ not in ("Rx", "Tx"):
            return None
        if rest and rest[0].upper() == "RTR":
            return "RTR", offset_ms, None
        return "frame", offset_ms, CANFrame(
            timestamp=0.0, arbitration_id=arb_id, data=_hex_bytes(rest, dlc), bus=bus,
            is_extended=_is_extended(id_tok, arb_id), dlc=dlc, is_tx=typ == "Tx")

    # ---- version 2.0 and later: columns from $COLUMNS --------------------

    def _parse_v2(self, line: str, version: str, columns: list[str]):
        toks = line.split()
        vals: dict[str, str] = {}
        data: list[str] = []
        pos = 0
        try:
            for col in columns:
                typ = vals.get("T")
                if col == "D":
                    data = toks[pos:]
                    break
                if typ == "EV" and col not in ("N", "O", "T", "B"):
                    # The event text starts right after the bus specifier.
                    parts = line.split(None, pos)
                    vals["text"] = parts[pos] if len(parts) > pos else ""
                    break
                if (version == "2.0" and typ in ("ST", "ER", "EC")
                        and col in ("I", "l", "L")):
                    continue               # empty columns in 2.0, no token
                vals[col] = toks[pos]
                pos += 1
            typ = vals["T"]
            offset_ms = float(vals["O"])
        except (IndexError, KeyError, ValueError):
            return None

        b = vals.get("B", "1")
        bus = None if b == "-" else int(b) if b.isdigit() else None
        is_tx = vals.get("d", "Rx") == "Tx"

        if typ in _EVENT_TYPES:
            if typ == "EV":
                return "event", offset_ms, CANEvent(0.0, bus, "EV", text=vals.get("text", ""))
            return "event", offset_ms, CANEvent(0.0, bus, typ, _hex_bytes(data), is_tx=is_tx)
        if typ not in _FRAME_TYPES:
            return typ, offset_ms, None    # RR, XL, or a type this reader does not know
        try:
            id_tok = vals["I"]
            arb_id = int(id_tok, 16)
            length = int(vals["l"]) if "l" in vals else None
            dlc = int(vals["L"]) if "L" in vals else length
        except (KeyError, ValueError):
            return None
        payload = _hex_bytes(data, length)
        return "frame", offset_ms, CANFrame(
            timestamp=0.0, arbitration_id=arb_id, data=payload, bus=bus if bus is not None else 1,
            is_extended=_is_extended(id_tok, arb_id),
            dlc=dlc if dlc is not None else len(payload), is_tx=is_tx)
