"""python-can-native candump log I/O for canbench.

candump / SocketCAN ``.log`` is canbench's default capture format. python-can
reads and writes it natively (``CanutilsLogWriter`` / ``CanutilsLogReader``), so
it is the default writer. canbench never imports the reverse-it ``canre``
framework; its own readers live in ``canbench.parsers``.

Key properties of the format:
    * absolute epoch timestamps (no filename-anchoring needed for real time)
    * one interface field per line -> multi-bus captures round-trip via the
      per-message ``channel`` label
    * plain text, greppable, canplayer/SocketCAN compatible

``can.Logger`` / ``can.LogReader`` dispatch by file extension, so ``.asc`` and
``.blf`` are transparently supported for read/replay too; only the default
*writer* is candump.

Reading is by content first: a file whose first lines identify it as BUSMASTER
or SavvyCAN CSV -- formats python-can has no reader for -- is read through
``canbench.parsers``. Everything else goes to python-can by extension, as
before. This matters for ``.log``, which BUSMASTER and candump share: python-can
alone would parse a BUSMASTER ``.log`` as candump and fail.
"""
import calendar
import re
from pathlib import Path

import can

from canbench import parsers
from canbench.parsers.busmaster import BusmasterParser
from canbench.parsers.savvycan import SavvyCANParser

DEFAULT_SUFFIX = ".log"


def bus_channel(bus_id):
    """Interface label written to the candump line for a given logical bus id.

    can_logger assigns bus ids 0..N-1 in dongle-detection order; we render them
    as ``can0``, ``can1``, ... so the bus identity survives a write->read
    round-trip via ``can.Message.channel``.
    """
    return f"can{bus_id}"


def channel_bus_id(channel):
    """Inverse of :func:`bus_channel`: parse a channel label back to an int id.

    Accepts ``"can3"`` -> ``3`` and bare ints/int-strings. Returns ``0`` for an
    unrecognised or missing channel so replay/analysis has a stable default.
    """
    if channel is None:
        return 0
    if isinstance(channel, int):
        return channel
    s = str(channel)
    digits = "".join(c for c in s if c.isdigit())
    return int(digits) if digits else 0


def open_writer(path, append=False):
    """Open a candump log writer for ``path``.

    Returns a ``can.Logger`` (a python-can ``Listener`` + context manager).
    Feed it messages with ``writer(msg)`` or ``writer.on_message_received(msg)``
    and close it with ``writer.stop()`` (or use it as a context manager). Dispatch
    is by extension, so a ``.log`` path yields the candump/SocketCAN writer.
    """
    return can.Logger(str(path), append=append)


# Content-detected formats read through canbench.parsers instead of python-can.
_OWN_FORMATS = (BusmasterParser, SavvyCANParser)

# ***START DATE AND TIME DD:MM:YYYY HH:MM:SS:mmm*** -- the trailing field is
# MILLISECONDS, while BUSMASTER data-line timestamps are 0.1 ms ticks.
_BM_START_RE = re.compile(
    r"^\*\*\*START DATE AND TIME\s+(\d{2}):(\d{2}):(\d{4})\s+"
    r"(\d{1,2}):(\d{2}):(\d{2}):(\d+)\*\*\*", re.IGNORECASE)


def busmaster_start(path):
    """Header start time of a BUSMASTER file as seconds, or None if absent.

    The header is the logger's local wall-clock time with no zone, so it is
    converted as if it were UTC: consistent across machines and chunks, but not
    a true epoch. Use ``canre.utils.timebase`` where the real instant matters.
    """
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for i, line in enumerate(fh):
            m = _BM_START_RE.match(line.strip())
            if m:
                dd, mo, yy, hh, mi, ss, ms = (int(g) for g in m.groups())
                return calendar.timegm((yy, mo, dd, hh, mi, ss)) + ms / 1000.0
            if i > 40:
                return None
    return None


def detect_reader(path):
    """The ``canbench.parsers`` parser for ``path`` if canbench reads it itself,
    else None (python-can reads it by extension)."""
    try:
        parser = parsers.detect_format(path)
    except ValueError:
        return None
    return parser if isinstance(parser, _OWN_FORMATS) else None


def read_messages(path, include_errors=False):
    """Yield ``can.Message`` objects from a capture, detecting the format.

    BUSMASTER and SavvyCAN CSV are read by ``canbench.parsers``; candump, ASC,
    BLF, CSV and TRC by python-can (by extension). ``msg.channel`` carries the
    bus label (``canN``), never a bus identity: identify buses by content.

    Timestamps: python-can formats give absolute epoch seconds. BUSMASTER gives
    the header start (see :func:`busmaster_start`) plus each frame's offset from
    the first frame, so differences between frames are exact; without a header
    they start at 0.

    With ``include_errors`` a BUSMASTER ``ERR`` line is yielded as a message with
    ``is_error_frame=True`` whose ``arbitration_id`` is the SocketCAN error-class
    mask. Otherwise error lines are skipped. python-can formats yield whatever
    python-can yields.
    """
    parser = detect_reader(path)
    if parser is None:
        with can.LogReader(str(path)) as reader:
            for msg in reader:
                yield msg
        return
    base = 0.0
    if isinstance(parser, BusmasterParser):
        base = busmaster_start(path) or 0.0
        frames = parser.parse_file(path, include_errors=include_errors)
    else:
        frames = parser.parse_file(path)
    for fr in frames:
        yield can.Message(
            timestamp=base + fr.timestamp,
            arbitration_id=fr.arbitration_id,
            is_extended_id=fr.is_extended,
            is_error_frame=fr.is_error,
            dlc=fr.dlc,
            data=fr.data,
            channel=bus_channel(fr.bus),
            is_rx=not fr.is_tx,
        )


def write_messages(path, messages, append=False):
    """Convenience: write an iterable of ``can.Message`` to a candump log.

    Mainly for tests / one-shot conversions. Returns the number written.
    """
    count = 0
    with open_writer(path, append=append) as writer:
        for msg in messages:
            writer(msg)
            count += 1
    return count
