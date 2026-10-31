"""Marking adapter error reports that a log wrote as ordinary data frames.

Android loggers built before the Bug 11 fix (2026-10-28) wrote each gs_usb
error report from an Innomaker adapter as a plain standard frame: the ID is
the SocketCAN error-class mask, the 8 data bytes follow linux/can/error.h.
Those IDs are also real message IDs (Coda C CAN 0x004 / 0x00C carry cell
voltages), so the ID alone cannot tell them apart.

The rule, validated on 3,643 captures and approved by the owner 2026-10-30
(reverse-it tools/artifacts/error-report-sweep/README.md).  A frame is marked
only when all of these hold; anything else stays data:

1. Capture gate: the BUSMASTER header names an Innomaker adapter and the
   file has no ERR lines (it was written before the fix).
2. ID gate: a standard ID in INFERRED_ERROR_IDS, the masks the Innomaker was
   measured to emit.  Add others only from a measurement.
3. Payload gate: the 8 bytes fit the error.h layout (layout_ok), or they are
   all zero and no frame of that ID on that channel in that capture fails
   layout_ok.

The marks are a property of the whole capture, so marking takes a first pass
over the file before frames are yielded.
"""
from __future__ import annotations

from pathlib import Path

from .busmaster import BusmasterParser, channel_adapters

INFERRED_ERROR_IDS = frozenset({0x004, 0x00C, 0x024})


def layout_ok(arb_id: int, data: bytes) -> bool:
    """True if *data* can be a SocketCAN error frame of class mask *arb_id*:
    a byte whose class bit is clear is zero, and the controller and location
    bytes carry only codes error.h defines."""
    d = data
    if len(d) != 8 or arb_id == 0 or arb_id >= 0x400:
        return False
    if d[0] and not arb_id & 0x002:          # lost arbitration bit
        return False
    if d[1] and not arb_id & 0x004:          # controller status
        return False
    if d[1] & 0x80:                          # no controller code above 0x40
        return False
    if (d[2] or d[3]) and not arb_id & 0x008:  # protocol type, location
        return False
    if d[3] > 0x1F:                          # no location code above 0x1F
        return False
    if d[4] and not arb_id & 0x010:          # transceiver status
        return False
    return True


def _outcome(arb_id: int, data: bytes) -> str:
    if not any(data):
        return "allzero"
    return "pass" if layout_ok(arb_id, data) else "fail"


def capture_gate(path: Path | str) -> bool:
    """Rule 1 without the ERR-line half: the header names an Innomaker."""
    return any("innomaker" in name.lower() for name in channel_adapters(path).values())


def inferred_error_marker(path: Path | str):
    """A function frame -> bool for the frames of *path* that the rule marks,
    or None if the capture gate fails (then no frame is marked).

    Reads the whole file once.  Only BUSMASTER captures can pass the gate:
    no other format here names the adapter.
    """
    if not capture_gate(path):
        return None
    failing: set[tuple[int, int]] = set()
    for rec in BusmasterParser().parse_file(path, include_errors=True):
        if rec.is_error:
            return None                      # written after the fix
        if (not rec.is_extended and rec.arbitration_id in INFERRED_ERROR_IDS
                and _outcome(rec.arbitration_id, rec.data) == "fail"):
            failing.add((rec.bus, rec.arbitration_id))

    def marked(frame) -> bool:
        if frame.is_extended or frame.arbitration_id not in INFERRED_ERROR_IDS:
            return False
        out = _outcome(frame.arbitration_id, frame.data)
        if out == "pass":
            return True
        return out == "allzero" and len(frame.data) == 8 \
            and (frame.bus, frame.arbitration_id) not in failing

    return marked
