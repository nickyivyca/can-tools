"""One error format for every adapter: the Linux ``can/error.h`` error frame.

Adapters report bus errors in their own styles. SocketCAN drivers (gs_usb,
peak_usb, kvaser_usb, ...) and python-can's gs_usb libusb backend already give
a Linux error frame: the error class in the CAN id, details in 8 data bytes.
PEAK's PCAN-Basic gives its own error and status messages, and Kvaser's canlib
gives an error frame with no documented fields. canbench turns the last two
into Linux error frames at the bus (``canbench.live.backends``), so everything
above the bus -- the candump and TRC writers, the GUI, analysis -- reads one
layout. The adapter's own words are kept on the message (:class:`ErrorMessage`)
and written next to the translation, so nothing the adapter said is lost.

:func:`decode` reads a Linux error frame into an :class:`ErrorReport`, the
canonical record, with ``None`` for anything the frame does not say.
:func:`trc_records` renders a report as PEAK TRC ``ER`` / ``ST`` / ``EC``
records.

Rules worth knowing (each one is a test in tests/test_errors.py):

* A bus error (``ER``) is reported only when the frame says one happened:
  ``CAN_ERR_PROT`` with ``CAN_ERR_BUSERROR`` (sja1000, peak_usb and kvaser_usb set
  both for each error), ``CAN_ERR_PROT`` without a controller-state change,
  ``CAN_ERR_ACK``, ``CAN_ERR_BUSERROR`` alone, or a frame with no known class at
  all. A state-change frame that also carries ``CAN_ERR_PROT`` -- the gs_usb /
  candleLight form, which repeats the controller's last error code at every
  state change -- is a state change, not a new bus error; its error code is
  kept in ``last_error_type``.
* Error counters are known when ``CAN_ERR_CNT`` is set, or when bytes 6 and 7
  are not both zero: gs_usb fills them without setting the flag. Zero counters
  without the flag are unknown, and so are zero counters in a frame whose only
  class is ``CAN_ERR_CNT``: kvaser_usb sends one every 512 ms while the
  controller is not error-active, all data zero although passive means a
  counter of 128 or more (owner, 2026-11-13).
* PEAK's error position codes are the SJA1000 ECC segment codes, and so are
  error.h's ``CAN_ERR_PROT_LOC_*`` values: the position is copied unchanged.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import can

# ---- linux/can/error.h ----------------------------------------------------
CAN_ERR_FLAG = 0x20000000
CAN_ERR_MASK = 0x1FFFFFFF

CAN_ERR_TX_TIMEOUT = 0x001
CAN_ERR_LOSTARB = 0x002
CAN_ERR_CRTL = 0x004
CAN_ERR_PROT = 0x008
CAN_ERR_TRX = 0x010
CAN_ERR_ACK = 0x020
CAN_ERR_BUSOFF = 0x040
CAN_ERR_BUSERROR = 0x080
CAN_ERR_RESTARTED = 0x100
CAN_ERR_CNT = 0x200
_KNOWN_CLASSES = 0x3FF

# data[1]: controller status
CAN_ERR_CRTL_RX_OVERFLOW = 0x01
CAN_ERR_CRTL_TX_OVERFLOW = 0x02
CAN_ERR_CRTL_RX_WARNING = 0x04
CAN_ERR_CRTL_TX_WARNING = 0x08
CAN_ERR_CRTL_RX_PASSIVE = 0x10
CAN_ERR_CRTL_TX_PASSIVE = 0x20
CAN_ERR_CRTL_ACTIVE = 0x40

# data[2]: protocol error type
CAN_ERR_PROT_BIT = 0x01
CAN_ERR_PROT_FORM = 0x02
CAN_ERR_PROT_STUFF = 0x04
CAN_ERR_PROT_BIT0 = 0x08
CAN_ERR_PROT_BIT1 = 0x10
CAN_ERR_PROT_OVERLOAD = 0x20
CAN_ERR_PROT_ACTIVE = 0x40
CAN_ERR_PROT_TX = 0x80

# data[3]: location (= SJA1000 ECC segment = PEAK's "position in bit stream")
CAN_ERR_PROT_LOC_ACK = 0x19

# ---- PEAK TRC / PCAN-Basic --------------------------------------------------
# TRC ER byte 0
PEAK_BIT, PEAK_FORM, PEAK_STUFF, PEAK_OTHER = 1, 2, 4, 8
# PCAN-Basic status bits (TPCANStatus); the TRC ST record uses the same values
PCAN_ERROR_OK = 0x00000
PCAN_ERROR_XMTFULL = 0x00001
PCAN_ERROR_OVERRUN = 0x00002
PCAN_ERROR_BUSLIGHT = 0x00004
PCAN_ERROR_BUSHEAVY = 0x00008      # = PCAN_ERROR_BUSWARNING
PCAN_ERROR_BUSOFF = 0x00010
PCAN_ERROR_QRCVEMPTY = 0x00020
PCAN_ERROR_QOVERRUN = 0x00040
PCAN_ERROR_QXMTFULL = 0x00080
PCAN_ERROR_BUSPASSIVE = 0x40000
PCAN_BUS_STATE_BITS = (PCAN_ERROR_BUSLIGHT | PCAN_ERROR_BUSHEAVY | PCAN_ERROR_BUSOFF
                       | PCAN_ERROR_BUSPASSIVE)

STATES = ("active", "warning", "passive", "busoff", "restarted")
_TRC_STATUS = {"active": PCAN_ERROR_OK, "restarted": PCAN_ERROR_OK,
               "warning": PCAN_ERROR_BUSHEAVY, "passive": PCAN_ERROR_BUSPASSIVE,
               "busoff": PCAN_ERROR_BUSOFF}


class ErrorMessage(can.Message):
    """A Linux-layout error frame that remembers what the adapter said.

    ``raw_backend`` names the source ("pcan", "kvaser", "canbench" for a
    report canbench made itself), ``raw_id`` / ``raw_data`` are the adapter's
    own id and bytes, and ``raw_note`` adds anything else (a PCAN status word,
    a Kvaser counter read). A plain ``can.Message`` error frame from a SocketCAN
    or gs_usb bus has none of these: it is its own raw form.
    """

    def __init__(self, *, raw_backend: str = "", raw_id: int = 0, raw_data: bytes = b"",
                 raw_note: str = "", **kwargs):
        kwargs.setdefault("is_error_frame", True)
        kwargs.setdefault("dlc", 8)
        super().__init__(**kwargs)
        self.raw_backend = raw_backend
        self.raw_id = raw_id
        self.raw_data = bytes(raw_data)
        self.raw_note = raw_note


def error_frame(cls_bits: int, data: bytes | list[int], *, timestamp: float = 0.0,
                channel=None, **raw) -> ErrorMessage:
    """Build a Linux error frame (class bits in the id, 8 data bytes)."""
    payload = bytes(data)[:8].ljust(8, b"\x00")
    return ErrorMessage(timestamp=timestamp, arbitration_id=cls_bits & CAN_ERR_MASK,
                        is_extended_id=False, data=payload, channel=channel, **raw)


@dataclass
class ErrorReport:
    """What one error frame says, in adapter-neutral terms. ``None`` = not said."""
    error_type: int | None = None        # PEAK type of a bus error: 1 bit, 2 form, 4 stuff, 8 other
    direction: str | None = None         # 'tx' / 'rx' for the bus error
    position: int | None = None          # SJA1000 / PEAK segment code
    rx_count: int | None = None
    tx_count: int | None = None
    state: str | None = None             # one of STATES
    rx_overflow: bool = False
    tx_overflow: bool = False
    lost_arbitration_bit: int | None = None
    last_error_type: int | None = None   # protocol code repeated in a state-change frame
    raw_backend: str = ""
    raw_id: int = 0
    raw_data: bytes = field(default=b"")
    raw_note: str = ""


def _peak_type(prot: int) -> int:
    if prot & (CAN_ERR_PROT_BIT | CAN_ERR_PROT_BIT0 | CAN_ERR_PROT_BIT1):
        return PEAK_BIT
    if prot & CAN_ERR_PROT_FORM:
        return PEAK_FORM
    if prot & CAN_ERR_PROT_STUFF:
        return PEAK_STUFF
    return PEAK_OTHER


def decode(msg: can.Message) -> ErrorReport | None:
    """The :class:`ErrorReport` of a Linux error frame, or None for any other frame."""
    if not msg.is_error_frame:
        return None
    cls = msg.arbitration_id & CAN_ERR_MASK
    raw = bytes(msg.data)
    n = len(raw)
    d = raw.ljust(8, b"\x00")
    r = ErrorReport(raw_backend=getattr(msg, "raw_backend", ""),
                    raw_id=getattr(msg, "raw_id", msg.arbitration_id),
                    raw_data=getattr(msg, "raw_data", raw),
                    raw_note=getattr(msg, "raw_note", ""))

    # controller state
    crtl = d[1] if (cls & CAN_ERR_CRTL and n > 1) else 0
    if cls & CAN_ERR_BUSOFF:
        r.state = "busoff"
    elif cls & CAN_ERR_RESTARTED:
        r.state = "restarted"
    elif crtl & (CAN_ERR_CRTL_RX_PASSIVE | CAN_ERR_CRTL_TX_PASSIVE):
        r.state = "passive"
    elif crtl & (CAN_ERR_CRTL_RX_WARNING | CAN_ERR_CRTL_TX_WARNING):
        r.state = "warning"
    elif crtl & CAN_ERR_CRTL_ACTIVE:
        r.state = "active"
    r.rx_overflow = bool(crtl & CAN_ERR_CRTL_RX_OVERFLOW)
    r.tx_overflow = bool(crtl & CAN_ERR_CRTL_TX_OVERFLOW)

    # counters
    cnt_only_zero = (cls & _KNOWN_CLASSES) == CAN_ERR_CNT and not (d[6] or d[7])
    if n >= 8 and (cls & CAN_ERR_CNT or d[6] or d[7]) and not cnt_only_zero:
        r.tx_count, r.rx_count = d[6], d[7]

    if cls & CAN_ERR_LOSTARB:
        r.lost_arbitration_bit = d[0] if n > 0 else None

    # bus error
    prot = d[2] if n > 2 else 0
    loc = d[3] if n > 3 else 0
    state_change = bool(cls & (CAN_ERR_CRTL | CAN_ERR_BUSOFF | CAN_ERR_RESTARTED))
    if cls & CAN_ERR_PROT and (cls & CAN_ERR_BUSERROR or not state_change):
        r.error_type = _peak_type(prot)
        r.direction = "tx" if prot & CAN_ERR_PROT_TX else "rx"
        r.position = loc or None
    elif cls & CAN_ERR_ACK:
        r.error_type = PEAK_OTHER
        r.direction = "tx"
        r.position = loc or CAN_ERR_PROT_LOC_ACK
    elif cls & (CAN_ERR_BUSERROR | CAN_ERR_TX_TIMEOUT):
        r.error_type = PEAK_OTHER
        r.direction = "tx" if cls & CAN_ERR_TX_TIMEOUT else None
    elif not cls & _KNOWN_CLASSES:
        r.error_type = PEAK_OTHER         # an error frame that says nothing else
    if cls & CAN_ERR_PROT and r.error_type is None:
        r.last_error_type = _peak_type(prot)
    return r


def trc_records(r: ErrorReport) -> list[tuple[str, str, bytes]]:
    """``(type, direction, data)`` TRC records for a report: ``ER`` for a bus
    error, ``ST`` for a state or overrun, ``EC`` for counters reported without a
    bus error. Unknown fields are written as 0 (TRC has no "unknown"; the raw
    line kept beside them says what was actually reported)."""
    out = []
    if r.error_type is not None:
        tx = r.direction == "tx"
        out.append(("ER", "Tx" if tx else "Rx",
                    bytes([r.error_type, 0 if tx else 1, r.position or 0,
                           r.rx_count or 0, r.tx_count or 0])))
    if r.state is not None or r.rx_overflow:
        status = _TRC_STATUS.get(r.state, 0) | (PCAN_ERROR_OVERRUN if r.rx_overflow else 0)
        out.append(("ST", "Rx", status.to_bytes(4, "big")))
    if r.error_type is None and r.rx_count is not None:
        out.append(("EC", "Rx", bytes([r.rx_count, r.tx_count or 0])))
    return out


# ---- translations into the Linux layout --------------------------------------

_PEAK_TO_PROT = {PEAK_BIT: CAN_ERR_PROT_BIT, PEAK_FORM: CAN_ERR_PROT_FORM,
                 PEAK_STUFF: CAN_ERR_PROT_STUFF, PEAK_OTHER: 0}


def from_pcan_error(type_id: int, data: bytes, **kw) -> ErrorMessage:
    """A PCAN-Basic error message (``PCAN_MESSAGE_ERRFRAME``: id = PEAK error
    type, 0 for a counter-only update; data = direction, position, RX, TX)."""
    data = bytes(data).ljust(4, b"\x00")
    direction, position, rx, tx = data[0], data[1], data[2], data[3]
    d = bytearray(8)
    d[6], d[7] = tx, rx
    if type_id == 0:
        cls = CAN_ERR_CNT
    else:
        cls = CAN_ERR_PROT | CAN_ERR_BUSERROR | CAN_ERR_CNT
        d[2] = _PEAK_TO_PROT.get(type_id, 0) | (CAN_ERR_PROT_TX if direction == 0 else 0)
        d[3] = position
    kw.setdefault("raw_backend", "pcan")
    kw.setdefault("raw_id", type_id)
    kw.setdefault("raw_data", bytes(data[:4]))
    return error_frame(cls, d, **kw)


def from_pcan_status(status: int, **kw) -> ErrorMessage:
    """A PCAN status word (a status message's 4 data bytes, MSB first, or a bus
    state returned by Read) as a Linux controller-state frame. BUSLIGHT alone
    has no Linux or TRC equivalent and becomes a state-less CRTL frame; the
    word itself is kept in ``raw_note``."""
    d = bytearray(8)
    cls = CAN_ERR_CRTL
    if status & PCAN_ERROR_BUSOFF:
        cls |= CAN_ERR_BUSOFF
    elif status & PCAN_ERROR_BUSPASSIVE:
        d[1] |= CAN_ERR_CRTL_RX_PASSIVE | CAN_ERR_CRTL_TX_PASSIVE
    elif status & PCAN_ERROR_BUSHEAVY:
        d[1] |= CAN_ERR_CRTL_RX_WARNING | CAN_ERR_CRTL_TX_WARNING
    elif status == PCAN_ERROR_OK:
        d[1] |= CAN_ERR_CRTL_ACTIVE
    if status & (PCAN_ERROR_OVERRUN | PCAN_ERROR_QOVERRUN):
        d[1] |= CAN_ERR_CRTL_RX_OVERFLOW
    if status & PCAN_ERROR_QXMTFULL:
        d[1] |= CAN_ERR_CRTL_TX_OVERFLOW
    kw.setdefault("raw_backend", "pcan")
    kw.setdefault("raw_note", f"status=0x{status:05X}")
    return error_frame(cls, d, **kw)


# canlib canReadStatus flags
canSTAT_ERROR_PASSIVE = 0x01
canSTAT_BUS_OFF = 0x02
canSTAT_ERROR_WARNING = 0x04
canSTAT_ERROR_ACTIVE = 0x08
canSTAT_HW_OVERRUN = 0x200
canSTAT_SW_OVERRUN = 0x400


def from_kvaser(status: int | None, tx: int | None, rx: int | None, *, bus_error: bool = True,
                **kw) -> ErrorMessage:
    """A canlib error frame, completed with the channel's status flags and
    error counters read when it arrived (None if the read failed). canlib's
    error frame says a bus error happened and nothing more, so the type is
    "other" with no direction or position. ``bus_error=False`` makes a
    state-only report (a polled state change with no error frame)."""
    d = bytearray(8)
    cls = CAN_ERR_BUSERROR if bus_error else 0
    if status is not None:
        cls |= CAN_ERR_CRTL
        if status & canSTAT_BUS_OFF:
            cls |= CAN_ERR_BUSOFF
        elif status & canSTAT_ERROR_PASSIVE:
            d[1] |= CAN_ERR_CRTL_RX_PASSIVE | CAN_ERR_CRTL_TX_PASSIVE
        elif status & canSTAT_ERROR_WARNING:
            d[1] |= CAN_ERR_CRTL_RX_WARNING | CAN_ERR_CRTL_TX_WARNING
        elif status & canSTAT_ERROR_ACTIVE:
            d[1] |= CAN_ERR_CRTL_ACTIVE
        if status & (canSTAT_HW_OVERRUN | canSTAT_SW_OVERRUN):
            d[1] |= CAN_ERR_CRTL_RX_OVERFLOW
    if tx is not None and rx is not None:
        cls |= CAN_ERR_CNT
        d[6], d[7] = min(tx, 255), min(rx, 255)
    kw.setdefault("raw_backend", "kvaser")
    note = []
    if status is not None:
        note.append(f"status=0x{status:X}")
    if tx is not None:
        note.append(f"tx={tx} rx={rx}")
    kw.setdefault("raw_note", " ".join(note))
    return error_frame(cls, d, **kw)
