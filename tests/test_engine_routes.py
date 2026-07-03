"""Engine capture_ids + passthrough routes over two virtual segments (no hardware).

Segments segA (bus 0) and segB (bus 1). Route 0->1 forwards A's traffic to B.
capture_ids={0} logs only bus-0-sourced frames. A node on segB also sends; those
frames must be counted but neither forwarded nor logged.
"""
import os
import sys
import time
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import can
from canbench.engine import CaptureEngine
from canbench import logio


def test_routes_and_capture_ids():
    out = os.path.join(tempfile.gettempdir(), "canbench_routes.log")
    if os.path.exists(out):
        os.remove(out)

    node_a = can.Bus(interface="virtual", channel="segA")
    node_b = can.Bus(interface="virtual", channel="segB")

    eng = CaptureEngine(bitrate=500000)
    res = eng.start(
        [("virtual", "segA", "A"), ("virtual", "segB", "B")],
        log_path=out,
        capture_ids={0},          # log only bus 0 (segA)
        routes={0: 1},            # forward segA -> segB
    )
    assert len(res.opened) == 2
    try:
        for i in range(3):
            node_a.send(can.Message(arbitration_id=0xA0 + i, data=b"\x01", is_extended_id=False))
            time.sleep(0.02)
        for i in range(2):
            node_b.send(can.Message(arbitration_id=0xB0 + i, data=b"\x02", is_extended_id=False))
            time.sleep(0.02)
        time.sleep(0.4)

        # segA frames were forwarded onto segB and seen by node_b
        fwd = []
        while True:
            m = node_b.recv(timeout=0.2)
            if m is None:
                break
            fwd.append(m.arbitration_id)
        assert sorted(fwd) == [0xA0, 0xA1, 0xA2], fwd

        stats = eng.monitor.poll()
        assert stats[0].total == 3 and stats[1].total == 2   # both buses counted
    finally:
        eng.stop()
        node_a.shutdown()
        node_b.shutdown()

    # log holds ONLY bus-0-sourced frames (capture_ids={0}), tagged can0
    logged = list(logio.read_messages(out))
    assert sorted(m.arbitration_id for m in logged) == [0xA0, 0xA1, 0xA2]
    assert all(logio.channel_bus_id(m.channel) == 0 for m in logged)


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn(); print(f"PASS  {name}")
            except AssertionError as e:
                failures += 1; print(f"FAIL  {name}: {e}")
            except Exception as e:
                failures += 1; print(f"ERROR {name}: {e!r}")
    print(f"\n{'ALL PASS' if failures == 0 else str(failures) + ' FAILED'}")
    sys.exit(1 if failures else 0)
