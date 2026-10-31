"""Parser for BUSMASTER CAN log files.

BUSMASTER log format (v3.x):
    - Header lines start with ***
    - Data lines: HH:MM:SS:TTTT Tx/Rx Channel 0xID s/x DLC D0 D1 ... D7
    - Timestamp TTTT is 10 kHz ticks (0.1 ms resolution)
    - Type 's' = standard (11-bit), 'x' = extended (29-bit)
    - Type 'ERR' = error frame (Android logger, 2026-10-28 on); yielded only
      with parse_file(..., include_errors=True)
    - Supports mid-file session restarts
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Iterator

from .base import BaseParser, CANFrame

# Matches a BUSMASTER data line (not a header/metadata line).
# Groups: timestamp, tx/rx, channel, id, type, dlc, remaining data bytes
# A real midnight crossing steps the time of day back by nearly 86400 s.
# Anything smaller is an out-of-order write, not a new day. See the rollover
# comment in parse() -- testing against the first frame instead of the previous
# one smeared 36 Coda captures across 24 hours.
_ROLLOVER_BACKSTEP = 43200.0  # 12 h

# Each timestamp component may be NEGATIVE.  The Android logger's
# BusmasterWriter formats its offset with Kotlin's truncating division and
# sign-preserving remainder, so a frame stamped before the file's origin comes
# out as e.g. `00:00:-2:-3130` (= -2.3130 s) or `00:-1:-26:-150` (= -86.015 s).
# The shipped pattern could not match a minus sign, so `parse_file` fell through
# its `if not m: continue` and dropped the line -- silently, with no counter.
# Measured 2026-09-27 over 3,674 files / 2.379 bn data lines: 4,827,795 lines
# (0.203 %) discarded across 1,715 files, worst single file 447,123 lines = 44 %
# of the capture.  See projects/coda/notes/android-log-timestamp-defects.md.
#
# Field widths vary with the sign because `%02d`/`%04d` widen rather than
# truncate, so the groups are `-?\d{1,2}` and `-?\d{1,4}` rather than fixed.
# `_parse_timestamp` needs no change: summing the signed components reproduces
# the original offset exactly (verified by round-tripping 4,010 values through a
# reimplementation of the Kotlin formatter, 0 mismatches).
#
# The hour field stays at two digits deliberately.  BUSMASTER does not wrap at
# midnight -- `charge_M0_T52.log` contains `24:01:29:4480` -- so two digits
# covers any plausible capture, and the one corpus line that exceeds it,
# `50224:01:46:2870` (5.7 years), is corruption that should stay rejected.
_DATA_RE = re.compile(
    r"^(-?\d{1,2}:-?\d{1,2}:-?\d{1,2}:-?\d{1,4})\s+"  # HH:MM:SS:TTTT, components may be signed
    r"(Tx|Rx)\s+"                          # direction
    r"(\d+)\s+"                            # channel
    r"(0x[0-9A-Fa-f]+)\s+"                # arbitration ID
    r"([sx])\s+"                           # s=standard, x=extended
    r"(\d+)\s+"                            # DLC
    r"(.*)",                               # data bytes (space-separated hex)
    re.IGNORECASE,
)

# Error-frame line, written by the Android logger since 2026-10-28 (plan
# android-app/notes/plans/gs-usb-error-frames.md):
#     HH:MM:SS:TTTT Rx <ch> 0x<class mask> ERR <dlc> <data bytes>
# `ERR` is the token Wireshark's BUSMASTER reader takes as an error frame
# (`<time> <dir> <ch> <id> ERR <free text>`); the DLC and payload after it are
# this project's convention, so both are optional here.  _DATA_RE cannot match
# such a line (its type group is [sx]), which is what kept older copies of this
# parser from counting error reports as messages.
_ERR_RE = re.compile(
    r"^(-?\d{1,2}:-?\d{1,2}:-?\d{1,2}:-?\d{1,4})\s+"
    r"(Tx|Rx)\s+"
    r"(\d+)\s+"
    r"(0x[0-9A-Fa-f]+|\d+)\s+"
    r"ERR\b\s*"
    r"(\d+)?\s*"
    r"(.*)",
    re.IGNORECASE,
)

_HEADER_RE = re.compile(r"^\*\*\*")
_SESSION_START_RE = re.compile(r"^\*\*\*\[START LOGGING SESSION\]\*\*\*", re.IGNORECASE)
_SESSION_STOP_RE = re.compile(r"^\*\*\*\[STOP LOGGING SESSION\]\*\*\*", re.IGNORECASE)
_HEX_MODE_RE = re.compile(r"^\*\*\*HEX\*\*\*", re.IGNORECASE)
_DEC_MODE_RE = re.compile(r"^\*\*\*DEC\*\*\*", re.IGNORECASE)
# Parses: ***START DATE AND TIME DD:MM:YYYY HH:MM:SS:TTT***
# We use only HH:MM:SS as the wall-clock epoch (sub-second field omitted to
# avoid ambiguity between ms and 10 kHz ticks across log sources).
_START_TIME_RE = re.compile(
    r"^\*\*\*START DATE AND TIME\s+\d{2}:\d{2}:\d{4}\s+"
    r"(?P<time>\d{1,2}:\d{2}:\d{2}):\d+\*\*\*",
    re.IGNORECASE,
)


def _parse_timestamp(ts_str: str) -> float:
    """Convert HH:MM:SS:TTTT to session-relative seconds.

    BUSMASTER timestamps are session-relative (reset to 0 at logging start),
    not wall-clock time.  TTTT is in 10 kHz ticks (0.1 ms per tick).
    """
    parts = ts_str.split(":")
    hours = int(parts[0])
    minutes = int(parts[1])
    seconds = int(parts[2])
    ticks = int(parts[3])
    return hours * 3600.0 + minutes * 60.0 + seconds + ticks * 0.0001


def _error_frame(m: re.Match, timestamp: float, hex_mode: bool) -> CANFrame:
    """CANFrame for an _ERR_RE match.  A tail that is not DLC + bytes (free
    text, as BUSMASTER itself may write) gives an empty payload, not an error."""
    _, direction, channel, id_str, dlc_str, data_str = m.groups()
    arb_id = int(id_str, 16) if id_str.lower().startswith("0x") else int(id_str)
    data_bytes = b""
    dlc = 0
    if dlc_str is not None:
        try:
            dlc = int(dlc_str)
            tokens = data_str.split()[:dlc]
            data_bytes = bytes(int(b, 16 if hex_mode else 10) for b in tokens)
        except ValueError:
            dlc, data_bytes = 0, b""
    return CANFrame(
        timestamp=timestamp,
        arbitration_id=arb_id,
        data=data_bytes,
        bus=int(channel),
        is_extended=False,
        dlc=dlc,
        is_tx=direction.upper() == "TX",
        is_error=True,
    )


class BusmasterParser(BaseParser):
    """Parser for BUSMASTER CAN log files."""

    @staticmethod
    def detect(lines: list[str]) -> bool:
        """Return True if sample lines look like a BUSMASTER log.

        We look for the characteristic ***BUSMASTER*** or ***PROTOCOL CAN***
        header lines, or the ***HEX***/***DEC*** marker.
        Also matches truncated-header BUSMASTER files that start with
        ***START DATE AND TIME or ***END CHANNEL BAUD RATE***.
        """
        for line in lines:
            stripped = line.strip()
            if re.match(r"^\*\*\*BUSMASTER", stripped, re.IGNORECASE):
                return True
            if re.match(r"^\*\*\*PROTOCOL CAN\*\*\*", stripped, re.IGNORECASE):
                return True
            if re.match(r"^\*\*\*START DATE AND TIME", stripped, re.IGNORECASE):
                return True
            if re.match(r"^\*\*\*END CHANNEL BAUD RATE\*\*\*", stripped, re.IGNORECASE):
                return True
            if _DATA_RE.match(stripped):
                return True
        return False

    @staticmethod
    def scan_ids(path: Path | str) -> tuple[dict[int, set], dict[int, int], float]:
        """Fast scan path for the influx_upload.scan_log() dual-pass.

        Returns ``(channel_ids, channel_counts, last_ts)`` -- the same shape
        scan_log builds via the full parser, but skips work that scan_log
        does not consume:
          * `_parse_timestamp` per line (~36s tottime in the 535MB benchmark
            across both passes) -- only the last line's timestamp is
            converted, since scan_log only uses last_ts as the
            derive_start_epoch mtime fallback.
          * `bytes(int(b,16) for b in tokens)` data-byte decoding (~63s
            tottime) -- not needed for ID-only counting.
          * `CANFrame` dataclass allocation per line.
          * Tx/Rx, frame_type, dlc, is_extended.

        Hex/Dec mode and session-restart markers are honoured by virtue of
        skipping every ``***`` line; mode flips don't matter because we
        never decode data bytes here.

        Other parsers (CandumpParser, PeakTrcParser, SavvyCANParser,
        VectorAscParser) don't override this -- influx_upload.scan_log
        falls back to the full ``parse_file`` for them. That fallback is
        slower but those formats are rare in this project, so correctness
        beats speed.
        """
        path = Path(path)
        channel_ids: dict[int, set] = {}
        channel_counts: dict[int, int] = {}
        last_ts_str = ""
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for raw_line in fh:
                if not raw_line or raw_line[0] == "*":
                    continue
                line = raw_line.strip()
                if not line:
                    continue
                m = _DATA_RE.match(line)
                if not m:
                    continue
                last_ts_str = m.group(1)
                channel = int(m.group(3))
                arb_id = int(m.group(4), 16)
                ids = channel_ids.get(channel)
                if ids is None:
                    ids = set()
                    channel_ids[channel] = ids
                    channel_counts[channel] = 0
                ids.add(arb_id)
                channel_counts[channel] += 1

        last_ts = 0.0
        if last_ts_str:
            parts = last_ts_str.split(":")
            last_ts = (int(parts[0]) * 3600 + int(parts[1]) * 60
                       + int(parts[2]) + int(parts[3]) * 0.0001)
        return channel_ids, channel_counts, last_ts

    def parse_file(self, path: Path | str, *, absolute: bool = False,
                   include_errors: bool = False) -> Iterator[CANFrame]:
        """Parse a BUSMASTER log file, yielding CANFrame objects.

        By default the first frame's timestamp becomes t=0 and all
        subsequent timestamps are relative to that origin.  "First frame"
        includes an error-frame (ERR) line, so the origin is the same whether
        or not error frames are yielded.

        Error frames (``ERR`` lines, written by the Android logger since
        2026-10-28) are skipped unless *include_errors* is True, so every
        existing caller sees exactly the data frames it saw before.  When
        yielded they carry ``is_error=True`` and ``arbitration_id`` is the
        SocketCAN error-class mask, which collides with real IDs.  Logs from
        before the fix carry the same reports as ordinary data frames
        ``0x004`` / ``0x00C``; see android-app/notes/outstanding-changes.md
        Bug 11.

        If *absolute* is True, each frame's session-relative timestamp is
        offset by the wall-clock HH:MM:SS from the file's
        ``***START DATE AND TIME***`` header.  This gives every T-chunk a
        distinct, non-overlapping time base so that merge_files() can chain
        them correctly with heapq.merge.  Files without the header fall back
        to session-relative time (epoch=0), which is the pre-fix behaviour.
        """
        path = Path(path)
        hex_mode = True  # default; toggled by ***HEX*** / ***DEC***
        t_origin: float | None = None
        prev_rel: float | None = None
        day_offset: float = 0.0
        session_epoch: float = 0.0  # set from ***START DATE AND TIME*** header

        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for raw_line in fh:
                line = raw_line.strip()
                if not line:
                    continue

                # ---- Header / metadata lines ----
                if _HEADER_RE.match(line):
                    if _HEX_MODE_RE.match(line):
                        hex_mode = True
                    elif _DEC_MODE_RE.match(line):
                        hex_mode = False
                    elif absolute:
                        m_st = _START_TIME_RE.match(line)
                        if m_st:
                            h, mn, s = (int(x) for x in m_st.group("time").split(":"))
                            session_epoch = h * 3600.0 + mn * 60.0 + s
                    # Session start/stop are noted but don't reset origin
                    continue

                # ---- Data lines, and error-frame (ERR) lines ----
                # An ERR line takes part in the time origin and the rollover
                # check whether or not it is yielded: the writer's origin is
                # the first frame it wrote, of either kind, and
                # canre/utils/timebase.py counts every Rx/Tx line the same way.
                m = _DATA_RE.match(line)
                is_error = False
                if not m:
                    m = _ERR_RE.match(line)
                    if not m:
                        continue
                    is_error = True

                ts_str = m.group(1)

                rel_time = _parse_timestamp(ts_str)

                if absolute:
                    timestamp = session_epoch + rel_time
                else:
                    if t_origin is None:
                        t_origin = rel_time
                        prev_rel = rel_time
                    # Midnight rollover, measured against the PREVIOUS frame.
                    #
                    # This used to test `rel_time - t_origin < -1.0`, i.e. against
                    # the FIRST frame in the file, and add a day to anything more
                    # than a second before it. Captures are not always written in
                    # timestamp order -- the Android logger emits a few seconds of
                    # frames out of order at the start of a chunk -- so a benign
                    # 4-second backwards write was promoted to a 24-hour jump.
                    # 36 of 1069 Coda captures parsed to a span of -1.0..86399.0 s
                    # against headers only minutes long, which manufactured phantom
                    # multi-module dropouts and was the real cause of the "T0 spans
                    # 24 h with later chunks nested inside it" overlap that
                    # influx_sync's overlapping_files() was added to work around.
                    # A genuine rollover steps the clock back by nearly a full day;
                    # an out-of-order write steps it back by seconds. Found
                    # 2026-09-26.
                    if rel_time < prev_rel - _ROLLOVER_BACKSTEP:
                        day_offset += 86400.0
                    prev_rel = rel_time
                    timestamp = rel_time + day_offset - t_origin

                if is_error:
                    if include_errors:
                        yield _error_frame(m, timestamp, hex_mode)
                    continue

                _, direction, channel, arb_id_str, frame_type, dlc_str, data_str = m.groups()
                is_tx = direction.upper() == "TX"
                bus = int(channel)
                arb_id = int(arb_id_str, 16)
                is_extended = frame_type.lower() == "x"
                dlc = int(dlc_str)

                # Parse data bytes
                data_tokens = data_str.split()
                if hex_mode:
                    data_bytes = bytes(int(b, 16) for b in data_tokens[:dlc])
                else:
                    data_bytes = bytes(int(b) for b in data_tokens[:dlc])

                yield CANFrame(
                    timestamp=timestamp,
                    arbitration_id=arb_id,
                    data=data_bytes,
                    bus=bus,
                    is_extended=is_extended,
                    dlc=dlc,
                    is_tx=is_tx,
                )
