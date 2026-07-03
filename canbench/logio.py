"""python-can-native candump log I/O for canbench.

candump / SocketCAN ``.log`` is canbench's default capture format. python-can
reads and writes it natively (``CanutilsLogWriter`` / ``CanutilsLogReader``), so
canbench carries no custom log parser and stays independent of the reverse-it
``canre`` framework.

Key properties of the format:
    * absolute epoch timestamps (no filename-anchoring needed for real time)
    * one interface field per line -> multi-bus captures round-trip via the
      per-message ``channel`` label
    * plain text, greppable, canplayer/SocketCAN compatible

``can.Logger`` / ``can.LogReader`` dispatch by file extension, so ``.asc`` and
``.blf`` are transparently supported for read/replay too; only the default
*writer* is candump.
"""
from pathlib import Path

import can

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


def read_messages(path):
    """Yield ``can.Message`` objects from a candump/asc/blf log (by extension).

    Timestamps are absolute epoch seconds; ``msg.channel`` carries the bus label.
    """
    with can.LogReader(str(path)) as reader:
        for msg in reader:
            yield msg


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
