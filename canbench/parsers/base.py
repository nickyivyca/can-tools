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

    @property
    def id_hex(self) -> str:
        """Arbitration ID as hex string (e.g. '0x123')."""
        return f"0x{self.arbitration_id:03X}" if not self.is_extended else f"0x{self.arbitration_id:08X}"

    def __repr__(self) -> str:
        data_hex = " ".join(f"{b:02X}" for b in self.data)
        kind = " ERR" if self.is_error else ""
        return f"CANFrame(t={self.timestamp:.4f} bus={self.bus}{kind} id={self.id_hex} [{self.dlc}] {data_hex})"


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
