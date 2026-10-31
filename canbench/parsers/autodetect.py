"""Auto-detection and convenience entry point for CAN log parsing.

Reads the first ~20 non-empty lines of a file, tries each parser's
``detect()`` class method, and returns the appropriate parser instance.
Also provides a convenience ``parse_file()`` function for one-step usage.
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterator

from .base import BaseParser, CANFrame
from .busmaster import BusmasterParser
from .candump import CandumpParser
from .peak_trc import PeakTrcParser
from .savvycan import SavvyCANParser
from .vector_asc import VectorAscParser

# Ordered list of parsers to try. BUSMASTER first since it is the primary
# format for this project, followed by others in rough likelihood order.
_PARSERS: list[type[BaseParser]] = [
    BusmasterParser,
    CandumpParser,
    PeakTrcParser,
    VectorAscParser,
    SavvyCANParser,
]

_SAMPLE_LINES = 20  # number of non-empty lines to read for detection


def _read_sample(path: Path, max_lines: int = _SAMPLE_LINES) -> list[str]:
    """Read up to *max_lines* non-empty lines from the beginning of *path*."""
    lines: list[str] = []
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for raw_line in fh:
            stripped = raw_line.strip()
            if stripped:
                lines.append(stripped)
            if len(lines) >= max_lines:
                break
    return lines


def detect_format(path: Path | str) -> BaseParser:
    """Detect the log format and return an appropriate parser instance.

    Parameters
    ----------
    path : Path or str
        Path to the CAN log file to examine.

    Returns
    -------
    BaseParser
        An instance of the detected parser.

    Raises
    ------
    ValueError
        If the format could not be identified from the sample lines.
    """
    path = Path(path)
    sample = _read_sample(path)

    for parser_cls in _PARSERS:
        if parser_cls.detect(sample):
            return parser_cls()

    raise ValueError(
        f"Unable to detect CAN log format for: {path}\n"
        f"First {len(sample)} non-empty lines did not match any known parser.\n"
        f"Supported formats: BUSMASTER, candump/SocketCAN, PEAK TRC, Vector ASC, SavvyCAN CSV"
    )


def parse_file(path: Path | str, *, absolute: bool = False,
               include_errors: bool = False) -> Iterator[CANFrame]:
    """Auto-detect format and parse a CAN log file, yielding CANFrame objects.

    This is the main convenience entry point. It combines ``detect_format``
    with the detected parser's ``parse_file`` method.

    Parameters
    ----------
    path : Path or str
        Path to the CAN log file to parse.
    absolute : bool
        If True, return absolute timestamps (e.g. seconds from midnight)
        instead of relative to first frame. Useful for chaining T-chunk files.
    include_errors : bool
        If True, also yield error frames (``CANFrame.is_error``). Only the
        BUSMASTER parser has them (``ERR`` lines); other formats ignore this.

    Yields
    ------
    CANFrame
        Parsed CAN frames in chronological order.
    """
    parser = detect_format(path)
    if include_errors and isinstance(parser, BusmasterParser):
        yield from parser.parse_file(path, absolute=absolute, include_errors=True)
    else:
        yield from parser.parse_file(path, absolute=absolute)
