"""Base types and interfaces for CAN log parsers."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator


@dataclass(slots=True)
class CANFrame:
    """A single CAN bus frame."""
    timestamp: float          # seconds (relative to capture start)
    arbitration_id: int       # 11 or 29-bit ID
    data: bytes               # payload (0-8 bytes)
    bus: int                  # channel/bus number (bus-name str after merge_files(bus_map=...))
    is_extended: bool         # True if 29-bit ID
    dlc: int                  # data length code
    is_tx: bool = False       # True if transmitted (vs received)
    # True for an adapter error frame (BUSMASTER `ERR` line); arbitration_id is
    # then the SocketCAN error-class mask, not a message ID.  Only yielded when
    # a parser is asked for them (BusmasterParser include_errors=True).
    is_error: bool = False
    # True for a data frame that is, by the rule in inferred_errors.py, an
    # adapter error report written as an ordinary frame (Android logs from
    # before ERR lines existed; desktop BUSMASTER Kvaser id-0x000 status
    # frames).  Set only by parse_file(...,
    # mark_inferred_errors=True); never set on a frame the log wrote as an error.
    inferred_error: bool = False

    @property
    def id_hex(self) -> str:
        """Arbitration ID as hex string (e.g. '0x123')."""
        return f"0x{self.arbitration_id:03X}" if not self.is_extended else f"0x{self.arbitration_id:08X}"

    def __repr__(self) -> str:
        data_hex = " ".join(f"{b:02X}" for b in self.data)
        kind = " ERR" if self.is_error else (" inferred-ERR" if self.inferred_error else "")
        return f"CANFrame(t={self.timestamp:.4f} bus={self.bus}{kind} id={self.id_hex} [{self.dlc}] {data_hex})"


@dataclass(slots=True)
class CANEvent:
    """A log line that is not a CAN frame: an error frame, a controller status
    change, an error-counter change, or a user event.

    Reported as written, not translated: *kind* is the log's own tag and *data*
    its own bytes, so a BUSMASTER ``ERR`` (SocketCAN error-frame layout) and a
    TRC ``ER`` (PEAK layout) stay distinguishable.  Only yielded when a parser
    is asked for them (``include_events=True``).

    kind     source            data
    -------  ----------------  -------------------------------------------------
    ERR      BUSMASTER         SocketCAN error frame: class mask in
                               arbitration_id, 8 bytes per linux/can/error.h
    ER       TRC 2.x           5 bytes: type, direction, bit position, RX, TX counter
    ST       TRC 2.x           4-byte hardware status, MSB first (0x02 OVERRUN,
                               0x08 BUSWARNING, 0x40000 BUSPASSIVE, 0x10 BUSOFF)
    EC       TRC 2.x           2 bytes: RX, TX error counter (after a decrease),
                               per the spec; PCAN-Basic 5.0 writes 4: direction,
                               bit position, RX, TX (see parsers.md)
    EV       TRC 2.1+          none; the user text is in *text*
    Error    TRC 1.x           4 bytes: direction, bit position, RX, TX counter;
                               the error type (1 bit, 2 form, 4 stuff, 8 other)
                               in arbitration_id; PCAN-Basic writes type 0 for a
                               counter change
    Warng    TRC 1.x           4-byte error-warning code, MSB first
    """
    timestamp: float          # same time base as the file's frames
    bus: int | None           # None for a TRC event written for all buses ('-')
    kind: str
    data: bytes = b""
    is_tx: bool = False
    arbitration_id: int | None = None
    text: str = ""

    def __repr__(self) -> str:
        data_hex = " ".join(f"{b:02X}" for b in self.data)
        aid = f" id=0x{self.arbitration_id:03X}" if self.arbitration_id is not None else ""
        txt = f" {self.text!r}" if self.text else ""
        return f"CANEvent(t={self.timestamp:.4f} bus={self.bus} {self.kind}{aid} {data_hex}{txt})"


class BaseParser(ABC):
    """Abstract base class for CAN log file parsers."""

    @staticmethod
    @abstractmethod
    def detect(lines: list[str]) -> bool:
        """Return True if the given sample lines match this format."""
        ...

    @abstractmethod
    def parse_file(self, path: Path | str, *, absolute: bool = False) -> Iterator[CANFrame]:
        """Parse a log file, yielding CANFrame objects. Must be streaming/generator-based.

        If *absolute* is True, timestamps are absolute (e.g. seconds from
        midnight for BUSMASTER) rather than relative to the first frame.
        This is needed for chaining T-chunk files that share a timeline.
        """
        ...


def id_filter_sets(ids, clock_ids):
    """Validate a parser's ``ids`` / ``clock_ids`` arguments.

    Returns ``(id_set, clock_set)`` as frozensets of ints, or ``(None, None)``
    when no id filter was asked for. ``clock_ids`` defaults to ``ids`` and must
    be a subset of it.

    Semantics, for every parser that accepts them (BUSMASTER, PEAK TRC):

    - ``ids``: only frames with these arbitration ids exist for the call. Every
      other line -- other ids, and non-frame lines (BUSMASTER ``ERR``, TRC
      ``ER``/``ST``/``EC``/``EV``) -- is skipped before it can take part in the
      time origin or the BUSMASTER midnight rollover, exactly as if the file had
      first been filtered down to those frames. No events are yielded.
    - ``clock_ids``: of those, the ids whose frames define the time base: the
      origin (first such frame) and, for BUSMASTER, the rollover check (against
      the previous such frame). A frame of another kept id is stamped with the
      time base in force at its line, and dropped if it comes before the first
      clock frame. (BUSMASTER: such a frame also applies the rollover test
      to itself against the last clock frame, without changing the time
      base, so one written just after midnight before the next clock frame is
      not stamped a day early.) In absolute mode there is no origin, so it
      changes nothing.

    A format that does not implement this raises rather than giving the
    origin a different meaning.
    """
    if ids is None:
        if clock_ids is not None:
            raise ValueError("clock_ids needs ids")
        return None, None
    id_set = frozenset(int(i) for i in ids)
    clock_set = id_set if clock_ids is None else frozenset(int(i) for i in clock_ids)
    if not clock_set:
        raise ValueError("clock_ids is empty")
    if not clock_set <= id_set:
        raise ValueError("clock_ids must be a subset of ids")
    return id_set, clock_set
