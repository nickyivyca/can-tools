# canbench.parsers Reference

Log readers for the formats canbench reads itself: BUSMASTER (the main capture format of
the reverse-engineering project this came from), candump / SocketCAN, PEAK TRC 1.0-2.1,
Vector ASC and SavvyCAN CSV, with format detection **by content**, not extension. Pure
standard library; nothing here imports python-can or anything outside canbench.

Moved here from the reverse-it project's `canre.parsers` (2026-10-30), which is to become a re-export of
this package. Paths in the notes below such as `android-app/...`, `projects/...` and
`tools/tools.md` refer to that project and are kept as the record of why each behaviour
exists.

Most callers want `canbench.logio.read_messages(path)`, which picks between these parsers
and python-can and yields `can.Message`. Use this package directly for `CANFrame` access:

```python
from canbench.parsers import parse_file

for frame in parse_file("capture.log"):          # CANFrame objects
    print(frame.timestamp, frame.bus, frame.arbitration_id, frame.data)
```

Non-frame lines (error frames, controller status, error counters, user events) come back as
`CANEvent` records only when asked for, `parse_file(path, include_events=True)`, interleaved
with the frames in file order; test with `isinstance(rec, CANFrame)`.

> **Staleness check:** Each section shows a `Source last modified` timestamp. If the `.py` file's mtime is newer than the timestamp shown here, re-read the source before relying on this documentation. Timestamps MUST include both date and time (`YYYY-MM-DD HH:MM:SS`).

## COMMON MISTAKES

| Wrong (do NOT use) | Correct |
|--------------------|---------|
| `parse_log(...)` | `parse_file(...)` |
| `frame.arb_id`, `frame.can_id`, `frame.id`, `frame.msg_id` | `frame.arbitration_id` |
| `frame.channel` | `frame.bus` |

`frame.bus` is the log's channel number. It is **not** a bus identity: channel numbers follow
dongle insertion order. Identify buses by content.

### `parse_file` is a generator — it can be iterated once

Every `parse_file` yields frames; none returns a list. A second pass over the same result sees **nothing**, with no error. The usual trap is a bus-identification pass followed by an analysis pass: the second one comes back empty and reads as "the ID is absent". If you need more than one pass, materialise it first: `frames = list(parse_file(path))`. Found 2026-10-26, when an HVAC scratchpad reported every ID, its bus anchors included, as missing from all 20 captures.

## base.py — Core Data Types
*Source last modified: 2026-10-31 15:33:37*

### `CANFrame` dataclass

```python
@dataclass(slots=True)
class CANFrame:
    timestamp: float       # seconds (relative to capture start or absolute)
    arbitration_id: int    # 11-bit or 29-bit CAN ID
    data: bytes            # payload, 0–8 bytes
    bus: int               # channel/bus number (a bus-name str after merge_files(bus_map=...))
    is_extended: bool      # True = 29-bit extended ID
    dlc: int               # data length code
    is_tx: bool = False    # True = transmitted frame
    is_error: bool = False # True = adapter error frame; arbitration_id is the error-class mask
    inferred_error: bool = False  # True = a data frame marked by inferred_errors.py
```

`inferred_error` is set only by `parse_file(..., mark_inferred_errors=True)` (see
`inferred_errors.py` below) and never on a line the log wrote as an error.

All parsers produce `CANFrame` objects. Timestamps are always in **seconds**. The BUSMASTER parser converts from `HH:MM:SS:TTTT` (TTTT = 10 kHz ticks, 0.1 ms resolution).

### `BaseParser` ABC

```python
class BaseParser(ABC):
    @staticmethod
    @abstractmethod
    def detect(lines: list[str]) -> bool: ...

    @abstractmethod
    def parse_file(path, *, absolute: bool = False) -> Iterator[CANFrame]: ...
```

- `detect(lines)` — given the first few lines of a file, returns True if this parser can handle it
- `parse_file(path, absolute=False)` — parse the file, yielding CANFrame objects (a generator, not a list). When `absolute=True`, timestamps are not zeroed to the first frame (needed for stitching T-chunk files that share a timeline)

### `CANEvent` dataclass

```python
@dataclass(slots=True)
class CANEvent:
    timestamp: float              # same time base as the file's frames
    bus: int | None               # None for a TRC event written for all buses ('-')
    kind: str                     # the log's own tag, see below
    data: bytes = b""
    is_tx: bool = False
    arbitration_id: int | None = None
    text: str = ""                # EV user text
```

A log line that is not a CAN frame, **reported as written**: `kind` is the log's own tag and
`data` its own bytes, so a BUSMASTER `ERR` (SocketCAN error-frame layout) and a TRC `ER`
(PEAK layout) stay distinguishable. Yielded only with `include_events=True`.

| kind | Source | data |
|---|---|---|
| `ERR` | BUSMASTER | SocketCAN error frame (linux/can/error.h); class mask in `arbitration_id` |
| `ER` | TRC 2.x | 5 bytes: error type (1 bit, 2 form, 4 stuff, 8 other), direction (0 tx, 1 rx), bit position, RX counter, TX counter |
| `ST` | TRC 2.x | 4-byte hardware status, MSB first: `0x02` OVERRUN, `0x08` BUSWARNING, `0x40000` BUSPASSIVE, `0x10` BUSOFF |
| `EC` | TRC 2.x | 2 bytes: RX, TX error counter, written when a counter decreases (spec); PCAN-Basic 5.0 writes 4, see below |
| `EV` | TRC 2.1+ | none; user text in `text` |
| `Error` | TRC 1.x | 4 bytes: direction, bit position, RX, TX counter; error type in `arbitration_id` |
| `Warng` | TRC 1.x | 4-byte error-warning code, MSB first |

**PCAN-Basic 5.0 writes `EC` in 4 bytes** (measured 2026-10-31 on 5.0.0.1115, reverse-it
`tools/notes/peak_trc_error_records.md`): `direction, bit position, RX, TX`, the body of its API's
error-frame message with ID 0 ("only updated error counter values", PCAN-Basic docs, "Error
Frames"), where PEAK's trace spec has 2 bytes `RX, TX`. In TRC 1.1 it writes the same counter
change as an `Error` line with type `0000`, which the spec does not list. The bench traces'
counter movement matches bytes 2/3 = RX/TX. To read counters, take bytes 0/1 of a 2-byte `EC`
and bytes 2/3 of a 4-byte one. PEAK-Converter 3.2 keeps only the first 2 bytes of a 4-byte `EC`,
so a 2.1 or 1.1 file it converts from PCAN-Basic's 2.0 holds direction and position there, not
counters; nothing in the file shows this. This reader reports the bytes as written and decodes
no counters. A writer should follow the spec (2 bytes).

---

## busmaster.py — BUSMASTER `.log` Parser
*Source last modified: 2026-10-30 18:17:26*

**Changed 2026-10-28 — error-frame (`ERR`) lines.** The Android logger now writes the
adapter's CAN error frames as `HH:MM:SS:TTTT Rx <ch> 0x<class mask> ERR <dlc> <bytes>`
instead of as standard data frames `0x004` / `0x00C` (Bug 11 in
`android-app/notes/outstanding-changes.md`; plan `android-app/notes/plans/gs-usb-error-frames.md`).
`parse_file` skips them unless `include_errors=True`, so every existing caller sees exactly
the data frames it saw before; when yielded they carry `is_error=True`. An `ERR` line still
sets the time origin if it comes first, so timestamps do not depend on `include_errors`.
**Logs from before the fix carry the same reports as ordinary data lines `0x004` / `0x00C`**,
which no flag can separate. Checked by `android-app/notes/artifacts/err_line_roundtrip.py`.

**Changed 2026-09-26 — midnight rollover was measured against the wrong frame.**
The check was `rel_time - t_origin < -1.0`, i.e. against the **first frame in the
file**, adding a day to anything more than a second earlier. Captures are not always
written in timestamp order — the Android logger emits a few seconds of frames out of
order at the start of a chunk — so a benign 4-second backwards write became a 24-hour
jump. **36 of 1069 Coda captures** parsed to a span of `-1.0..86399.0 s` against headers
only minutes long (`coda_20260508_191742_T0`: 6-minute header, 24-hour parsed span);
1 of 2031 Vtrux captures. The comparison is now against the **previous** frame with a
12-hour threshold (`_ROLLOVER_BACKSTEP`), since a real rollover steps back by nearly a
full day while an out-of-order write steps back by seconds. Verified: the 36 captures
now parse to their true length, and 0 of 33 laptop time-of-day captures regressed.

Two consequences beyond the timestamps. This was the real cause of the
"T0 spans 24 h with later chunks nested inside it" overlap that
`influx_sync.overlapping_files()` was added to work around on 2026-08-26 — the note in
`tools/tools.md` attributing that to the Android logger is wrong. And it manufactured
phantom multi-module dropout events in gap-based analysis.

**Changed 2026-09-27 — `_DATA_RE` could not match a negative timestamp component,
so it was discarding data lines.** The Android logger stamps a frame received before
the file's origin with negative components (`00:00:-2:-3130`, `00:-1:-26:-150`), which
the pattern rejected, and `parse_file` dropped the line with no warning and no counter.
Across 3,674 files and 2.379 bn data lines, **4,827,795 lines (0.203 %) in 1,715 files**
were being lost; the worst capture lost 447,123 lines, 44 % of itself, all from its start.
The groups are now `-?\d{1,2}` and `-?\d{1,4}`, since `%02d`/`%04d` widen rather than
truncate a negative. `_parse_timestamp` needed no change — summing the signed components
reproduces the offset exactly (round-tripped 4,010 values against a reimplementation of
the logger's Kotlin formatter, 0 mismatches). The hour field stays at two digits: the one
corpus line that exceeds it is `50224:01:46:2870`, which is corruption.

**Frames can carry a negative timestamp, and that is correct.** The first frame *in file
order* is still t=0 under `absolute=False`, so a frame stamped before the origin keeps
its true earlier time. The pre-origin block reaches **-7.0 s**, and clock-step-repaired
captures reach **-186 s** — the earlier "observed down to about -4 s" was one file. Do
not clamp or filter negatives.

**A second, separate defect is repaired in the logs, not here.** In 55 captures the
phone's wall clock stepped backwards mid-file by 8–216 s, leaving two blocks whose stamped
ranges overlap. It is independent of the dropped lines — several rewinding captures drop
none — so the regex fix does not touch it. Those files were repaired on disk, with the
originals kept in a `pre-clockstep-repair/` folder in each log root. See
`projects/coda/notes/android-log-timestamp-defects.md`.

Parser for BUSMASTER CAN log files (v3.x). This is the primary log format for the project and the first parser tried during auto-detection.

### `BusmasterParser` class

Inherits from `BaseParser`.

#### `detect(lines: list[str]) -> bool` (static)

Returns `True` if any sample line matches `***BUSMASTER` or `***PROTOCOL CAN***` (case-insensitive).

#### `parse_file(path, *, absolute: bool = False, include_errors: bool = False, include_events: bool = False) -> Iterator[CANFrame | CANEvent]`

Streams through a BUSMASTER log, yielding one `CANFrame` per data line. Skips header/metadata lines (those starting with `***`), and `ERR` error-frame lines unless asked for: `include_events=True` yields them as `CANEvent(kind="ERR")`, the form every parser uses for non-frame lines; `include_errors=True` yields them as `CANFrame(is_error=True)`, the older form kept for existing callers (see the 2026-10-28 note above). `include_events` wins if both are given.

#### `channel_adapters(path) -> dict[int, str]` (module function)

Adapter name per channel from the header, e.g. `{1: "Innomaker"}` for the Android logger's `***CHANNEL 1 - Innomaker - 500000 bps***`. Empty if the header names none.

**Timestamp handling:**
- Frame timestamps in the file are **session-relative** (reset to 0 at logging start), in `HH:MM:SS:TTTT` format where TTTT = 10 kHz ticks (0.1 ms, NOT milliseconds).
- Converted to seconds via: `H*3600 + M*60 + S + TTTT*0.0001`.
- **absolute=False** (default): first frame's timestamp becomes t=0; all others are relative to it. Midnight rollover is detected against the **previous** frame, not the first: a step back of more than `_ROLLOVER_BACKSTEP` (12 h) adds a day. Smaller backward steps are out-of-order writes and are left alone, so they yield slightly negative timestamps — see the 2026-09-26 note above.
- **absolute=True**: each frame's session-relative time is offset by the wall-clock `HH:MM:SS` from the file's `***START DATE AND TIME DD:MM:YYYY HH:MM:SS:TTT***` header (sub-second field ignored to avoid ms/ticks ambiguity). This gives every T-chunk a unique, non-overlapping time base so `merge_files()` can chain them correctly. Files without the header fall back to session-relative time (epoch=0).

**HEX/DEC mode:**
- Tracks `***HEX***` and `***DEC***` markers mid-file. Default assumption is HEX mode.
- Data byte tokens are clipped to `DLC` count (extra trailing tokens ignored).

**Data line fields:** timestamp, direction (Tx/Rx), channel, arb ID (with 0x), frame type (`s`=standard, `x`=extended), DLC, data bytes. An error-frame line has type `ERR`, the error-class mask in the ID column, then DLC and payload; a free-text tail after `ERR` gives an empty payload.

`scan_ids` (influx_upload's fast pass) counts data lines only.

**Caveat:** Session start/stop markers do **not** reset the time origin. Multiple sessions in one file produce monotonically increasing timestamps from the first data frame.

---

## candump.py — candump / SocketCAN Parser
*Source last modified: 2026-10-31 01:04:11*

Parser for `candump` (SocketCAN) log format. Each line has the form `(epoch.usec) canX ID#DATA`.

### `CandumpParser` class

Inherits from `BaseParser`.

#### `detect(lines: list[str]) -> bool` (static)

Returns `True` if at least 2 non-comment lines match the candump pattern `(timestamp) interface ID#data`.

#### `parse_file(path, *, absolute: bool = False, include_errors: bool = False, include_events: bool = False) -> Iterator[CANFrame | CANEvent]`

**Timestamp handling:** Unix epoch seconds with microsecond precision, made relative to the first line. The `absolute` parameter is accepted but **not currently implemented** — timestamps are always relative.

**Bus number:** Extracted from trailing digits of the interface name (e.g. `can0` → bus 0, `vcan1` → bus 1). Defaults to bus 0.

**Extended ID:** from the ID's width: can-utils writes 3 digits for an 11-bit ID and 8 for a 29-bit one, so more than 3 digits is extended even when the value is small; a value above `0x7FF` is extended whatever the width. (Before 2026-10-31 it was inferred from the value alone, so `0000007F#` read back as standard.)

**Data payload:** Contiguous hex after `#` (no spaces). `##<flags>` is a CAN FD frame: one hex digit of flags, then the data. (Before 2026-10-31 the flags digit was read as data.)

**Error frames:** an ID with `CAN_ERR_FLAG` (`0x20000000`) set is a SocketCAN error frame, e.g. python-can's `20000080#0000000000000000`. Skipped by default. `include_events=True` yields a `CANEvent` `ERR`, `include_errors=True` a `CANFrame` with `is_error` set; both carry the error-class mask (ID & `0x1FFFFFFF`) in `arbitration_id` and the 8 bytes as written, the same layout as a BUSMASTER `ERR`. (Before 2026-10-31 an error line was yielded as a data frame with ID `0x20000080`.)

**Remote requests:** `#R` or `#R<len>` is skipped and counted in the parser's `skipped` Counter (key `remote`), like TRC's `RR`. (Before 2026-10-31 it was yielded as a 0-byte data frame.)

**Caveat:** `is_tx` is always `False` — candump does not distinguish Tx from Rx.

---

## peak_trc.py — PEAK PCAN TRC Parser
*Source last modified: 2026-10-31 01:04:19*

Parser for PEAK PCAN Trace (`.trc`) log files, versions 1.0 to 2.1, written against PEAK-System's "PEAK CAN TRC File Format" (edition 2025-08-04). Version 3.0 files are read for their CAN and CAN FD lines. The version comes from `;$FILEVERSION=`; a file without it is 1.0.

**Rewritten 2026-10-30.** The earlier parser read the time offset as seconds, making every interval 1000x too long, and matched no 2.0 data line at all, so a PCAN-View 2.0 trace gave zero frames. No TRC file had been read through it on the corpus.

| Version | Line layout |
|---|---|
| 1.0 | `N) O ID DLC data`; `ERROR` in the data field = error frame, ID `FFFFFFFF` = warning, `RTR` = remote request |
| 1.1 | `N) O Type ID DLC data`, Type `Rx` / `Tx` / `Warng` / `Error` |
| 1.2 | `N) O Bus Type ID DLC data` |
| 1.3 | `N) O Bus Type ID - DLC data` (Reserved column) |
| 2.0, 2.1, 3.0 | the columns named by `;$COLUMNS=` (2.0 e.g. `N,O,T,I,d,l,D`, 2.1 e.g. `N,O,T,B,I,d,R,L,D`); types `DT FD FB FE BI RR ST ER EC EV` (3.0 adds `XL`) |

### `PeakTrcParser` class

Inherits from `BaseParser`.

#### `detect(lines: list[str]) -> bool` (static)

Returns `True` if any line matches `;$FILEVERSION=X.X`. Fallback: returns `True` if the sample has both `;`-prefixed headers and numbered data lines.

#### `parse_file(path, *, absolute: bool = False, include_events: bool = False) -> Iterator[CANFrame | CANEvent]`

**Timestamps:** the time offset is in **milliseconds** in every version. Default: the first data line of any kind (frame or event) is t=0, so times do not depend on `include_events`. `absolute=True`: seconds from midnight of `;$STARTTIME` plus the offset, like BUSMASTER's absolute mode.

**Non-frame lines:** `ER`, `ST`, `EC`, `EV` (1.x `Error`, `Warng`) become `CANEvent` records, yielded only with `include_events=True`. In 2.0 the ID and length columns of `ER` / `ST` / `EC` lines are empty; from 2.1 they hold `-` and a length.

**Extended ID:** more than 4 ID digits, or a value above `0x7FF`. PEAK writes 4 digits for an 11-bit ID (3 in 3.0) and 8 for a 29-bit one; python-can 4.6.1's `TRCWriter` writes 7 for a 29-bit ID, which the earlier 8-digit rule read back as standard (found 2026-10-31).

**Bus number:** from the Bus column (1.2, 1.3, 2.1 `B`); bus 1 when the file has none. An `EV` written for all buses (`-`) has `bus=None`.

**Skipped lines:** remote requests (`RR`, `RTR`), CAN XL frames and lines that do not parse; each is counted in the parser's `skipped` Counter after a full pass.

#### `start_time(path) -> float | None` (module function)

Unix time of `;$STARTTIME` (days since 1899-12-30). The header is the recorder's local time and is treated as UTC, as `canbench.logio.busmaster_start` does, so deltas are exact.

**PCAN-Basic 5.0 writes a wrong `$STARTTIME`** (measured 2026-10-31, reverse-it `tools/plans/can-replay-readers.md` step 6c): the TRC 1.1 traces written by PCAN-Basic 5.0.0.1115's trace feature on Windows, on a machine at UTC-7, carry `$STARTTIME` = local time minus 7 h (UTC minus 14 h), in whole seconds, while the same file's `Start time:` comment line has the right local time to the millisecond. PEAK's spec makes the two the same instant. This reader follows the spec and does not cross-check the comment, so `start_time()` and `absolute=True` are 14 h off for such files; relative times are unaffected. Take the start from the `Start time:` line by hand if one of these traces needs absolute time. The FD-channel TRC 2.0 traces from the same version are off the same way. PCAN-Basic 5.0.1 (2026-03-31) lists the fix "The conversion of the STARTTIME value in generated PCAN trace files did not take time zones into account"; that is likely this bug, unconfirmed until a 5.0.1 trace is read.

---

## savvycan.py — SavvyCAN CSV Parser
*Source last modified: 2026-02-16 00:42:22*

Parser for SavvyCAN CSV log files. Handles both header-row format (`Time Stamp,ID,Extended,Bus,LEN,D1,...,D8`) and a headerless positional variant.

### `SavvyCANParser` class

Inherits from `BaseParser`.

#### `detect(lines: list[str]) -> bool` (static)

Two heuristics (either sufficient):
1. **Header detection:** Line containing "time", "id", and a comma with ≥3 of the tokens `{"time", "stamp", "id", "bus", "len", "d1"}`.
2. **Data-only detection:** At least 3 lines match CSV data pattern (starts with float, comma, hex ID, comma).

#### `parse_file(path, *, absolute: bool = False) -> Iterator[CANFrame]`

Auto-detects header vs headerless by checking if the first non-empty line starts with a digit.

**Header variant:** Normalises header tokens (lowercase, no spaces), maps to column indices. Required columns: `time`, `id`, `len`. Optional: `extended`, `bus`.

**Headerless variant:** Assumed column order: timestamp, id, [extended], [bus], len, d1, d2, ... Uses heuristics to detect whether column 2 is an extended flag or bus number.

**Integer parsing:** All integer fields accept both `0x`-prefixed hex and plain decimal.

**Extended ID:** From `Extended` column if present, or inferred from ID > `0x7FF`.

**Caveat:** `is_tx` is always `False` — SavvyCAN CSV has no direction column.

---

## vector_asc.py — Vector CANalyzer ASC Parser
*Source last modified: 2026-03-05 13:35:56*

Parser for Vector CANalyzer / CANdb++ `.asc` log files. Used by CODACAN and other
Vector-based loggers.

### `VectorAscParser` class

Inherits from `BaseParser`.

#### `detect(lines: list[str]) -> bool` (static)

Returns `True` if any sample line starts with `base hex`, `base dec`, `date `, or
`no internal events` (case-insensitive). These are characteristic ASC header lines.

#### `parse_file(path, *, absolute: bool = False) -> Iterator[CANFrame]`

**Format:** `<float_ts> <channel> <id_hex> <dir> d <dlc> [byte ...]`

Example: `3824324055.7855 1      215                Rx  d 7 00 00 00 57 00 50 8F`

**Timestamp handling:** Absolute large float (epoch-like). Made relative to first frame
unless `absolute=True`.

**Channel:** Integer (typically 1-based). Stored directly as `bus` field.

**ID:** Hex without `0x` prefix.

**Extended ID:** Inferred from value > `0x7FF` (no explicit flag in standard ASC).

**Skipped lines:** Header lines (`date`, `base`, `no internal events`, `//`, `begin`,
`end`) and any line not matching the data line pattern. Remote frames (`r`) and error
frames are silently skipped.

---

## inferred_errors.py — Error Reports Written as Data Frames
*Source last modified: 2026-10-31 00:11:18*

Android loggers built before 2026-10-28 wrote each gs_usb error report from an Innomaker adapter as a plain standard frame: the ID is the SocketCAN error-class mask and the 8 bytes follow linux/can/error.h. The same IDs are real message IDs elsewhere (Coda C CAN `0x004` / `0x00C` are cell voltages), so the ID alone cannot tell them apart.

The rule, validated on 3,643 captures and approved 2026-10-30 (reverse-it `tools/error-report-sweep/README.md`). A frame is marked only when all three hold:

1. **Capture gate:** the BUSMASTER header names an Innomaker and the file has no `ERR` lines.
2. **ID gate:** a standard ID in `INFERRED_ERROR_IDS` (`0x004`, `0x00C`, `0x024`), the masks the Innomaker was measured to emit. Add others only from a measurement.
3. **Payload gate:** `layout_ok(id, data)`, or the payload is all zero and no frame of that ID on that channel in that capture fails `layout_ok`.

- `layout_ok(arb_id, data) -> bool` — 8 bytes; a byte whose class bit is clear is zero; controller code at most `0x40`, location code at most `0x1F`.
**Second rule: desktop BUSMASTER Kvaser id-`0x000` status frames** (step 4c, 2026-10-30; reverse-it `tools/error-report-sweep/README.md`, "Kvaser id-0x000 status frames"). BUSMASTER 3.2.2 with Kvaser adapters writes short standard-id `0x000` frames on any bus, thought to be adapter error-counter and error-state events (a hypothesis). Coda C CAN's real `0x000` is DLC 8 and is never marked. A frame is marked when:

1. **Channel gate:** the header names a Kvaser for the frame's channel (`kvaser_channels(path)`).
2. **Frame gate:** standard ID `KVASER_STATUS_ID` (`0x000`), DLC 4.
3. **Payload gate:** `kvaser_status_form(data)` is `"count"` (`00 00 NN 00`) or `"flags"` (`B0 xx 00 00`, B0 `0x00` or `0x20`, xx non-zero).

On 3,643 captures it marks 68,116 frames, none on C CAN, and canbench's marks equal the sweep's on all 209 Kvaser-header captures.

- `inferred_error_marker(path)` — returns a `frame -> bool` function applying whichever rules' gates pass, or `None` when neither does. The Innomaker rule reads the file once first; the Kvaser rule reads only the header. `parse_file(..., mark_inferred_errors=True)` uses it.
- To build the resolver's input from parsed frames, use `canbench.live.vehicle_configs.observed_ids_per_channel()`, which leaves marked frames out.

Marking never drops a frame; a caller that wants the reports out filters on `frame.inferred_error`.

---

## autodetect.py — Format Auto-Detection & Convenience Entry Point
*Source last modified: 2026-10-31 01:04:19*

Automatic format detection and the primary public entry point for all log parsing.

### `detect_format(path) -> BaseParser`

Reads the first ~20 non-empty lines and tries each parser's `detect()` in priority order:
1. `BusmasterParser` (tried first — highly distinctive `***BUSMASTER` header)
2. `CandumpParser`
3. `PeakTrcParser`
4. `SavvyCANParser`

Returns the first matching parser instance. Raises `ValueError` if none match.

### `parse_file(path, *, absolute: bool = False, include_errors: bool = False, include_events: bool = False, mark_inferred_errors: bool = False) -> Iterator[CANFrame | CANEvent]`

One-step convenience: detects format, then delegates to the matched parser's `parse_file`. `include_errors` is passed only to the BUSMASTER parser. `include_events` is passed to the BUSMASTER and TRC parsers; the other formats have no non-frame lines here. `mark_inferred_errors` applies the `inferred_errors.py` rule (BUSMASTER only; it reads the file twice).

```python
from canbench.parsers import parse_file

for frame in parse_file("capture.log"):
    print(frame)
```

This is the intended entry point — callers never need to choose a parser manually.
