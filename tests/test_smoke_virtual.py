"""Hardware-free smoke test for the canbench runtime.

Exercises the migrated code end-to-end using python-can's in-process ``virtual``
bus, so it runs anywhere with no dongles attached:

  - ``detect_all_can_interfaces`` executes cleanly (socketcan branch; no real
    hardware is opened)
  - ``CANBusLogger``'s real receive thread pulls frames off a live virtual bus
    into its queue
  - ``logio`` writes those frames to a candump ``.log`` and reads them back with
    arbitration ids / payloads intact

Run standalone:   py -3.12 tests/test_smoke_virtual.py
Or under pytest:  py -3.12 -m pytest tests/test_smoke_virtual.py
"""
import os
import sys
import time
import tempfile
from pathlib import Path
from queue import Queue, Empty

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import can
from canbench.live.receiver import detect_all_can_interfaces, CANBusLogger
from canbench import logio


def test_detect_runs_without_hardware():
    """Detection executes and returns a list (socketcan is absent on Windows)."""
    ifaces = detect_all_can_interfaces(500000, brands={"socketcan"})
    assert isinstance(ifaces, list)


def test_canbuslogger_receives_over_virtual_bus():
    """The migrated receive loop queues frames sent on a shared virtual channel."""
    ch = "cbtest_recv"
    q = Queue()
    rx = can.Bus(interface="virtual", channel=ch)
    tx = can.Bus(interface="virtual", channel=ch)
    blog = CANBusLogger(0, rx, q, "virtual", ch, 500000)
    blog.start()
    try:
        n = 6
        for i in range(n):
            tx.send(can.Message(arbitration_id=0x100 + i,
                                data=bytes([i, (i * 2) & 0xFF]),
                                is_extended_id=False))
            time.sleep(0.02)
        time.sleep(0.3)
    finally:
        blog.stop()      # also shuts down rx
        tx.shutdown()

    got = _drain(q)
    assert len(got) == n
    ids = sorted(m.arbitration_id for (_, _, m, _) in got)
    assert ids == [0x100 + i for i in range(n)]


def test_logio_candump_roundtrip():
    """A capture written by logio reads back with ids/payloads/bus preserved."""
    ch = "cbtest_rt"
    q = Queue()
    rx = can.Bus(interface="virtual", channel=ch)
    tx = can.Bus(interface="virtual", channel=ch)
    blog = CANBusLogger(2, rx, q, "virtual", ch, 500000)
    blog.start()
    try:
        for i in range(4):
            tx.send(can.Message(arbitration_id=0x2A0 + i, data=bytes([0xF0, i]),
                                is_extended_id=False))
            time.sleep(0.02)
        time.sleep(0.3)
    finally:
        blog.stop()
        tx.shutdown()

    got = _drain(q)
    out = os.path.join(tempfile.gettempdir(), "canbench_smoke_rt.log")
    with logio.open_writer(out) as w:
        for (ts, bid, msg, d) in got:
            msg.channel = logio.bus_channel(bid)
            w(msg)

    back = list(logio.read_messages(out))
    assert len(back) == len(got)
    assert sorted(m.arbitration_id for m in back) == sorted(m.arbitration_id for (_, _, m, _) in got)
    # bus id survives via the canN channel field
    assert all(logio.channel_bus_id(m.channel) == 2 for m in back)


def _drain(q):
    out = []
    while True:
        try:
            out.append(q.get_nowait())
        except Empty:
            return out


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS  {name}")
            except AssertionError as e:
                failures += 1
                print(f"FAIL  {name}: {e}")
            except Exception as e:
                failures += 1
                print(f"ERROR {name}: {e!r}")
    print(f"\n{'ALL PASS' if failures == 0 else str(failures) + ' FAILED'}")
    sys.exit(1 if failures else 0)
