"""canbench.errors, the TRC writer, and the PCAN / Kvaser bus classes.

Every fixture is synthetic: error frames are built from linux/can/error.h and
PEAK's published record layouts, never copied from a capture.

Run standalone:   python tests/test_errors.py
Or under pytest:  python -m pytest tests/test_errors.py
"""
import ctypes
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import can

from canbench import errors as E
from canbench import logio
from canbench.parsers.peak_trc import PeakTrcParser
from canbench.parsers.base import CANEvent, CANFrame
from canbench.trcwriter import TRCWriter


def linux(cls, data, **kw):
    return can.Message(arbitration_id=cls, is_error_frame=True, data=bytes(data), dlc=len(data),
                       is_extended_id=False, **kw)


# ---- decode: Linux error frames ---------------------------------------------

def test_protocol_error_with_buserror():
    # peak_usb / sja1000 style: one frame per bus error, counters flagged
    m = linux(E.CAN_ERR_PROT | E.CAN_ERR_BUSERROR | E.CAN_ERR_CNT,
              [0, 0, E.CAN_ERR_PROT_STUFF | E.CAN_ERR_PROT_TX, 0x0A, 0, 0, 12, 3])
    r = E.decode(m)
    assert (r.error_type, r.direction, r.position, r.tx_count, r.rx_count) == (4, "tx", 10, 12, 3)
    assert r.state is None
    assert E.trc_records(r) == [("ER", "Tx", bytes([4, 0, 10, 3, 12]))]


def test_bit0_bit1_are_bit_errors_and_form_is_form():
    for prot, want in ((E.CAN_ERR_PROT_BIT0, 1), (E.CAN_ERR_PROT_BIT1, 1),
                       (E.CAN_ERR_PROT_FORM, 2), (0, 8), (E.CAN_ERR_PROT_OVERLOAD, 8)):
        r = E.decode(linux(E.CAN_ERR_PROT | E.CAN_ERR_BUSERROR, [0, 0, prot, 0, 0, 0, 0, 0]))
        assert r.error_type == want, (prot, r)
        assert r.direction == "rx"
        assert r.position is None and r.rx_count is None


def test_gs_usb_state_frame_is_a_state_change_not_a_bus_error():
    # candleLight: CRTL|PROT at each state change, last error code in data[2],
    # counters in data[6]/[7] without CAN_ERR_CNT
    r = E.decode(linux(E.CAN_ERR_CRTL | E.CAN_ERR_PROT,
                       [0, E.CAN_ERR_CRTL_RX_PASSIVE | E.CAN_ERR_CRTL_TX_PASSIVE,
                        E.CAN_ERR_PROT_STUFF, 0, 0, 0, 0, 0x90]))
    assert r.error_type is None and r.last_error_type == 4
    assert r.state == "passive" and (r.tx_count, r.rx_count) == (0, 0x90)
    assert E.trc_records(r) == [("ST", "Rx", bytes.fromhex("00040000")),
                                ("EC", "Rx", bytes([0x90, 0]))]


def test_counters_zero_without_flag_are_unknown():
    r = E.decode(linux(E.CAN_ERR_CRTL | E.CAN_ERR_PROT, [0, 0, E.CAN_ERR_PROT_STUFF, 0, 0, 0, 0, 0]))
    assert r.rx_count is None and r.tx_count is None and r.state is None
    assert E.trc_records(r) == []          # nothing a TRC record can say; EV keeps the frame


def test_states_and_overflow():
    cases = [
        (E.CAN_ERR_CRTL, [0, E.CAN_ERR_CRTL_ACTIVE], "active", 0),
        (E.CAN_ERR_CRTL, [0, E.CAN_ERR_CRTL_RX_WARNING], "warning", 0x08),
        (E.CAN_ERR_CRTL, [0, E.CAN_ERR_CRTL_TX_PASSIVE], "passive", 0x40000),
        (E.CAN_ERR_BUSOFF, [], "busoff", 0x10),
        (E.CAN_ERR_RESTARTED, [], "restarted", 0),
    ]
    for cls, data, state, st in cases:
        r = E.decode(linux(cls, data + [0] * (8 - len(data))))
        assert r.state == state
        assert E.trc_records(r) == [("ST", "Rx", st.to_bytes(4, "big"))]
    r = E.decode(linux(E.CAN_ERR_CRTL, [0, E.CAN_ERR_CRTL_RX_OVERFLOW, 0, 0, 0, 0, 0, 0]))
    assert r.rx_overflow and r.state is None
    assert E.trc_records(r) == [("ST", "Rx", bytes.fromhex("00000002"))]


def test_no_ack_is_other_at_ack_slot_on_transmit():
    r = E.decode(linux(E.CAN_ERR_ACK, [0] * 8))
    assert (r.error_type, r.direction, r.position) == (8, "tx", 25)


def test_error_frame_with_no_known_class_is_an_unknown_bus_error():
    r = E.decode(linux(0, []))
    assert (r.error_type, r.direction, r.position) == (8, None, None)
    assert E.trc_records(r) == [("ER", "Rx", bytes([8, 1, 0, 0, 0]))]


def test_decode_ignores_data_frames():
    assert E.decode(can.Message(arbitration_id=0x123, data=b"\x01")) is None


# ---- translations ------------------------------------------------------------

def test_pcan_error_round_trips_to_the_same_er_bytes():
    # PCAN-Basic error message: id = type, data = direction, position, RX, TX
    for typ in (1, 2, 4, 8):
        for direction in (0, 1):
            data = bytes([direction, 0x0F, 0x22, 0x07])
            m = E.from_pcan_error(typ, data)
            assert m.raw_backend == "pcan" and m.raw_id == typ and m.raw_data == data
            assert E.trc_records(E.decode(m)) == [
                ("ER", "Tx" if direction == 0 else "Rx", bytes([typ, direction, 0x0F, 0x22, 0x07]))]


def test_pcan_counter_only_message_becomes_ec():
    m = E.from_pcan_error(0, bytes([1, 0, 0x7F, 0x00]))
    assert E.trc_records(E.decode(m)) == [("EC", "Rx", bytes([0x7F, 0x00]))]


def test_pcan_status_words_match_peak_st_records():
    for word in (0x00000, 0x00008, 0x40000, 0x00010, 0x00002):
        recs = E.trc_records(E.decode(E.from_pcan_status(word)))
        assert recs == [("ST", "Rx", word.to_bytes(4, "big"))], (hex(word), recs)
    # passive with heavy and light set (0x4000C) is passive
    assert E.decode(E.from_pcan_status(0x4000C)).state == "passive"
    # BUSLIGHT alone has no equivalent
    assert E.decode(E.from_pcan_status(0x00004)).state is None


def test_kvaser_completed_frame():
    m = E.from_kvaser(E.canSTAT_ERROR_PASSIVE, 9, 130, raw_id=0, raw_data=bytes.fromhex("20480000"))
    r = E.decode(m)
    assert (r.error_type, r.direction, r.state, r.tx_count, r.rx_count) == (8, None, "passive", 9, 130)
    assert r.raw_data == bytes.fromhex("20480000")
    assert E.trc_records(r) == [("ER", "Rx", bytes([8, 1, 0, 130, 9])),
                                ("ST", "Rx", bytes.fromhex("00040000"))]
    # nothing readable: still a bus error
    assert E.decode(E.from_kvaser(None, None, None)).error_type == 8


# ---- TRC writer --------------------------------------------------------------

def _write(msgs):
    d = Path(tempfile.mkdtemp())
    p = d / "w.trc"
    w = TRCWriter(str(p))
    for m in msgs:
        w.on_message_received(m)
    w.stop()
    return p


def _sample():
    t0 = 1_800_000_000.0
    return [
        can.Message(timestamp=t0, arbitration_id=0x123, data=b"\x01\x02", channel="can0"),
        can.Message(timestamp=t0 + 0.001, arbitration_id=0x18EFC034, is_extended_id=True,
                    data=bytes(range(8)), channel="can1", is_rx=False),
        can.Message(timestamp=t0 + 0.002, arbitration_id=0x100, is_remote_frame=True, dlc=3,
                    channel="can0"),
        can.Message(timestamp=t0 + 0.003, arbitration_id=0x200, is_fd=True, bitrate_switch=True,
                    data=bytes(range(12)), channel="can0"),
        E.from_pcan_error(4, bytes([1, 0x0A, 0x05, 0x00]), timestamp=t0 + 0.004, channel="can0"),
        linux(E.CAN_ERR_CRTL | E.CAN_ERR_PROT, [0, E.CAN_ERR_CRTL_RX_PASSIVE, 4, 0, 0, 0, 0, 0x88],
              timestamp=t0 + 0.005, channel="can1"),
    ]


def test_writer_lines_and_canbench_reader():
    p = _write(_sample())
    text = p.read_text()
    assert ";$FILEVERSION=2.1" in text and ";$COLUMNS=N,O,T,B,I,d,R,L,D" in text
    recs = list(PeakTrcParser().parse_file(p, include_events=True))
    frames = [r for r in recs if isinstance(r, CANFrame)]
    events = [r for r in recs if isinstance(r, CANEvent)]
    assert [(f.arbitration_id, f.bus, f.is_tx) for f in frames] == [
        (0x123, 1, False), (0x18EFC034, 2, True), (0x200, 1, False)]
    assert frames[1].is_extended and frames[2].data == bytes(range(12))
    kinds = [(e.kind, e.bus, e.data) for e in events]
    assert kinds[0] == ("ER", 1, bytes([4, 1, 0x0A, 0x05, 0x00]))
    assert kinds[1][0] == "EV" and "canbench-raw pcan id=00000004 data=01 0A 05 00" in events[1].text
    assert kinds[2] == ("ST", 2, bytes.fromhex("00040000"))
    assert kinds[3] == ("EC", 2, bytes([0x88, 0]))
    assert events[4].kind == "EV" and "canbench-raw linux id=0000000C" in events[4].text
    # offsets in ms from the first message
    first = [ln for ln in text.splitlines() if ln and not ln.startswith(";")]
    assert first[1].split()[1] == "1.000"


def test_writer_through_python_can_reader_and_logio_channels():
    p = _write(_sample())
    msgs = list(logio.read_messages(p))
    # python-can skips ER/ST/EC/EV and keeps the frames; logio maps bus N back to can{N-1}
    assert [(m.arbitration_id, m.channel) for m in msgs] == [
        (0x123, "can0"), (0x18EFC034, "can1"), (0x100, "can0"), (0x200, "can0")]
    assert msgs[2].is_remote_frame and msgs[3].is_fd and msgs[3].bitrate_switch
    assert not any(m.is_error_frame for m in msgs)


def test_open_writer_picks_trc_writer():
    d = Path(tempfile.mkdtemp())
    w = logio.open_writer(d / "x.trc")
    assert isinstance(w, TRCWriter)
    w.stop()
    try:
        logio.open_writer(d / "y.trc", append=True)
        raise AssertionError("append accepted")
    except ValueError:
        pass


# ---- PCAN bus class (fake PCAN-Basic) ----------------------------------------

class _Msg(ctypes.Structure):
    _fields_ = [("ID", ctypes.c_uint), ("MSGTYPE", ctypes.c_ubyte), ("LEN", ctypes.c_ubyte),
                ("DATA", ctypes.c_ubyte * 8)]


class _TS(ctypes.Structure):
    _fields_ = [("millis", ctypes.c_uint), ("millis_overflow", ctypes.c_ushort),
                ("micros", ctypes.c_ushort)]


class FakePCANBasic:
    """Just enough of PCANBasic for CanbenchPcanBus's own code paths."""

    def __init__(self, reads, status=0):
        self.reads = list(reads)
        self.status = status
        self.resets = 0
        self.values = {}

    def Read(self, handle):
        if not self.reads:
            return 0x20, _Msg(), _TS()           # QRCVEMPTY
        result, mid, mtype, data = self.reads.pop(0)
        m = _Msg(ID=mid, MSGTYPE=mtype, LEN=len(data))
        for i, b in enumerate(data):
            m.DATA[i] = b
        return result, m, _TS(millis=5)

    def GetStatus(self, handle):
        return self.status

    def Reset(self, handle):
        self.resets += 1
        self.status = 0
        return 0

    def SetValue(self, handle, param, value):
        self.values[param] = value
        return 0


def _pcan_bus(fake):
    from can.interfaces.pcan.pcan import PcanBus
    from canbench.live.pcan_bus import CanbenchPcanBus

    bus = CanbenchPcanBus.__new__(CanbenchPcanBus)
    # the parts of PcanBus.__init__ the receive path uses
    from can.bus import CanProtocol
    bus.m_objPCANBasic = fake
    bus.m_PcanHandle = 0x51
    bus._can_protocol = CanProtocol.CAN_20
    bus.channel_info = "PCAN_USBBUS1"
    bus._recv_event = None
    import collections, time
    bus._pending = collections.deque()
    bus._last_type, bus._last_raw, bus._synth_status, bus._last_ts = 0, (0, b""), None, 0.0
    bus.busoff_reset, bus.busoff_reset_interval, bus.busoff_resets = True, 0.0, 0
    bus._last_reset = float("-inf")
    bus._wrap_read("Read")
    return bus


def test_pcan_status_message_becomes_error_frame():
    import can.interfaces.pcan.pcan as pc
    saved = pc.HAS_EVENTS
    pc.HAS_EVENTS = False
    try:
        fake = FakePCANBasic([
            (0, 0x200, 0x00, b"\x01\x02"),                     # data frame
            (0, 0x001, 0x80, b"\x00\x04\x00\x00"),             # status message: passive
            (0, 0x004, 0x40, b"\x01\x0A\x05\x00"),             # error message: stuff, Rx
            (0x00008, 0, 0, b""),                              # BUSHEAVY returned by Read
        ])
        bus = _pcan_bus(fake)
        got = [bus._recv_internal(0.01)[0] for _ in range(5)]
    finally:
        pc.HAS_EVENTS = saved
    assert not got[0].is_error_frame and got[0].arbitration_id == 0x200
    r1 = E.decode(got[1])
    assert r1.state == "passive" and got[1].raw_id == 1 and "status message" in got[1].raw_note
    r2 = E.decode(got[2])
    assert (r2.error_type, r2.direction, r2.position) == (4, "rx", 0x0A)
    r3 = E.decode(got[3])
    assert r3.state == "warning" and "returned by Read" in got[3].raw_note
    assert got[4] is None


def test_pcan_busoff_on_send_resets_once_and_reports():
    from canbench.live.pcan_bus import CanbenchPcanBus
    fake = FakePCANBasic([], status=0x10)
    bus = _pcan_bus(fake)
    bus._recover("send")
    assert fake.resets == 1 and bus.busoff_resets == 1
    restarted = bus._pending.popleft()
    assert E.decode(restarted).state == "restarted"
    assert "PCAN reset after bus-off on send: ok" in restarted.raw_note
    bus._recover("send")                     # status now OK: no second reset
    assert fake.resets == 1


def test_pcan_busoff_interval_limits_resets():
    fake = FakePCANBasic([], status=0x10)
    bus = _pcan_bus(fake)
    bus.busoff_reset_interval = 60.0
    bus._recover("send")
    fake.status = 0x10
    bus._recover("send")
    assert fake.resets == 1


def test_pcan_busoff_reset_can_be_disabled():
    fake = FakePCANBasic([], status=0x10)
    bus = _pcan_bus(fake)
    bus.busoff_reset = False
    bus._recover("send")
    assert fake.resets == 0 and not bus._pending


# ---- Kvaser bus class --------------------------------------------------------

def test_kvaser_state_reported_on_change_only():
    from canbench.live.kvaser_bus import CanbenchKvaserBus

    bus = CanbenchKvaserBus.__new__(CanbenchKvaserBus)
    bus._last_state = None
    states = iter([(E.canSTAT_ERROR_ACTIVE, 8, 0), (E.canSTAT_ERROR_ACTIVE, 16, 0),
                   (E.canSTAT_ERROR_PASSIVE, 128, 0)])
    bus._read_state = lambda: next(states)
    frames = iter([can.Message(arbitration_id=0, is_error_frame=True, data=bytes(4), dlc=4)] * 3)

    from can.interfaces.kvaser.canlib import KvaserBus
    saved = KvaserBus._recv_internal
    KvaserBus._recv_internal = lambda self, timeout: (next(frames), False)
    try:
        out = [E.decode(bus._recv_internal(0)[0]) for _ in range(3)]
    finally:
        KvaserBus._recv_internal = saved
    assert [o.state for o in out] == ["active", None, "passive"]
    assert [o.tx_count for o in out] == [8, 16, 128]
    assert all(o.error_type == 8 for o in out)
    assert "status=0x8" in out[1].raw_note    # the read is kept even when the state is not new


def test_kvaser_zero_counters_are_unknown():
    from canbench.live.kvaser_bus import CanbenchKvaserBus
    from can.interfaces.kvaser.canlib import KvaserBus

    bus = CanbenchKvaserBus.__new__(CanbenchKvaserBus)
    bus._last_state = None
    bus._read_state = lambda: (E.canSTAT_ERROR_PASSIVE, 0, 0)
    saved = KvaserBus._recv_internal
    KvaserBus._recv_internal = lambda self, timeout: (
        can.Message(arbitration_id=0, is_error_frame=True, data=bytes(4), dlc=4), False)
    try:
        m = bus._recv_internal(0)[0]
    finally:
        KvaserBus._recv_internal = saved
    r = E.decode(m)
    assert r.state == "passive" and r.tx_count is None and r.rx_count is None
    assert "tx=0 rx=0" in m.raw_note


def test_backends_install_and_uninstall():
    from can.interfaces import BACKENDS
    from canbench.live import backends
    before = dict(BACKENDS)
    backends.install()
    assert BACKENDS["pcan"] == ("canbench.live.pcan_bus", "CanbenchPcanBus")
    assert backends.installed()
    backends.uninstall()
    assert BACKENDS["pcan"] == before["pcan"] and BACKENDS["kvaser"] == before["kvaser"]


if __name__ == "__main__":
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except Exception as e:
                failed += 1
                import traceback
                traceback.print_exc()
                print(f"FAIL {name}: {type(e).__name__}: {e}")
    sys.exit(1 if failed else 0)
