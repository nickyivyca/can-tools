"""canbench.live.socketcan_hw: frames, both stamps, error frames, the per-open stamp check.

Synthetic: a fake socket returns crafted struct can_frame bytes and SCM_TIMESTAMPING
data, so no CAN interface is needed. Run standalone:  python tests/test_socketcan_hw.py
"""
import socket
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import can

from canbench import errors
from canbench.live import socketcan_hw as H

HOST0 = 1_795_000_000.0
DEV0 = 41.25          # the adapter's clock: far from host time, like the Leaf's (about -1.795e9 s)


def ts(sec):
    s = int(sec)
    return s, int(round((sec - s) * 1e9))


def anc(host, hw):
    h = ts(host) if host else (0, 0)
    w = ts(hw) if hw else (0, 0)
    return [(socket.SOL_SOCKET, H.SCM_TIMESTAMPING, H.TIMESPEC3.pack(*h, 0, 0, *w))]


class FakeSock:
    def __init__(self, items):
        self.items = list(items)
        self.sent = []

    def recvmsg(self, n, ancsize):
        data, a = self.items.pop(0)
        return data, a, 0, None

    def send(self, b):
        self.sent.append(b)

    def fileno(self):
        return -1

    def close(self):
        pass


def frame(can_id, data):
    return H.CAN_FRAME.pack(can_id, len(data), bytes(data).ljust(8, b"\0"))


def data_items(n, hw_of=lambda i: DEV0 + i * 0.005, host_of=lambda i: HOST0 + i * 0.005 + 0.0003, aid=0x6F1):
    return [(frame(aid, [i & 0xFF]), anc(host_of(i), hw_of(i))) for i in range(n)]


_OPEN = []


def bus(items, **kw):
    b = H.SocketcanHwBus("kvaser", _sock=FakeSock(items), **kw)
    _OPEN.append(b)
    return b


def teardown_function(fn=None):
    while _OPEN:
        _OPEN.pop().shutdown()


def read_all(b, n):
    return [b.recv(timeout=None) for _ in range(n)]


def test_frame_fields_and_both_stamps():
    b = bus([(frame(0x18FF1234 | H.CAN_EFF_FLAG, b"\x01\x02\x03"), anc(HOST0 + 1.5, DEV0 + 0.25))])
    m = b.recv(timeout=None)
    assert m.arbitration_id == 0x18FF1234 and m.is_extended_id and not m.is_error_frame
    assert bytes(m.data) == b"\x01\x02\x03" and m.dlc == 3
    assert abs(m.timestamp - (HOST0 + 1.5)) < 1e-6 and m.host_timestamp == m.timestamp
    assert abs(m.hw_timestamp - (DEV0 + 0.25)) < 1e-9
    assert m.timestamp_source == "host"
    assert b.stamp_check["state"] == "pending"


def test_check_passes_on_adapter_clock():
    b = bus(data_items(25), check_frames=20)
    ms = read_all(b, 25)
    assert b.stamp_check["state"] == "hardware", b.stamp_check
    assert all(m.hw_timestamp is not None for m in ms)
    assert b.stamp_stats()["after_check"] == {"frames": 5, "hw_zero": 0, "hw_backward": 0, "error_frames": 0}


def test_all_zero_hardware_stamps_fail():
    """The 2026-11-13 case: ts[2] = 0 on every frame."""
    b = bus(data_items(5, hw_of=lambda i: 0))
    ms = read_all(b, 5)
    c = b.stamp_check
    assert c["state"] == "host_only" and "no hardware stamp" in c["reason"], c
    assert all(m.hw_timestamp is None for m in ms)
    assert all(m.timestamp > HOST0 for m in ms)


def test_host_time_posing_as_hardware_fails():
    b = bus(data_items(5, hw_of=lambda i: HOST0 + i * 0.005 + 0.5))
    read_all(b, 5)
    assert b.stamp_check["state"] == "host_only" and "host time" in b.stamp_check["reason"]


def test_stamps_that_do_not_advance_fail_and_stay_failed():
    hw = [DEV0, DEV0 + 0.005, DEV0 + 0.005, DEV0 + 0.02] + [DEV0 + 1 + i for i in range(30)]
    b = bus(data_items(34, hw_of=lambda i: hw[i]))
    ms = read_all(b, 34)
    assert b.stamp_check["state"] == "host_only" and "does not advance" in b.stamp_check["reason"]
    assert ms[0].hw_timestamp is not None and ms[1].hw_timestamp is not None
    assert all(m.hw_timestamp is None for m in ms[2:])


def test_error_frames_pass_whole_and_are_not_judged():
    """kvaser_usb at the wrong bitrate, 2026-11-18: id 0x20000288, data 0000020800000000."""
    err = (frame(0x20000288, bytes.fromhex("0000020800000000")), anc(HOST0, 0))
    b = bus([err] + data_items(3), check_frames=3)
    m = b.recv(timeout=None)
    assert m.is_error_frame and m.arbitration_id == 0x288 and not m.is_extended_id
    assert bytes(m.data) == bytes.fromhex("0000020800000000")
    r = errors.decode(m)
    assert r is not None
    assert m.hw_timestamp is None
    read_all(b, 3)
    c = b.stamp_check
    assert c["state"] == "hardware" and c["error_frames_seen"] == 1, c


def test_after_check_zero_and_backward_counted():
    hw = [DEV0 + i * 0.005 for i in range(20)] + [0, DEV0 + 0.5, DEV0 + 0.4]
    b = bus(data_items(23, hw_of=lambda i: hw[i]), check_frames=20)
    ms = read_all(b, 23)
    assert b.stamp_check["state"] == "hardware"
    assert ms[20].hw_timestamp is None
    assert b.stamp_stats()["after_check"] == {"frames": 3, "hw_zero": 1, "hw_backward": 1, "error_frames": 0}


def test_remote_and_standard_frames_and_send():
    b = bus([(frame(0x123 | H.CAN_RTR_FLAG, b""), anc(HOST0, DEV0))])
    m = b.recv(timeout=None)
    assert m.is_remote_frame and m.arbitration_id == 0x123 and not m.is_extended_id
    b.send(can.Message(arbitration_id=0x1ABCDE, is_extended_id=True, data=[1, 2]))
    can_id, dlc, data = H.CAN_FRAME.unpack(b._sock.sent[0])
    assert can_id == 0x1ABCDE | H.CAN_EFF_FLAG and dlc == 2 and data[:2] == b"\x01\x02"


def test_missing_host_stamp_falls_back_to_time():
    b = bus([(frame(0x10, b"\x00"), [])])
    m = b.recv(timeout=None)
    assert m.host_timestamp > HOST0 - 1e8 and m.hw_timestamp is None
    assert b.stamp_check["state"] == "host_only"


if __name__ == "__main__":
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
                teardown_function()
            except Exception as e:
                failed += 1
                print(f"FAIL {name}: {type(e).__name__}: {e}")
    sys.exit(1 if failed else 0)
