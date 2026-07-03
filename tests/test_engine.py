"""CaptureEngine test over a virtual bus, with candump logging (no hardware)."""
import os
import sys
import time
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import can
from canbench.engine import CaptureEngine
from canbench import logio


def test_capture_counts_and_logs():
    ch = "engtest"
    out = os.path.join(tempfile.gettempdir(), "canbench_engine.log")
    if os.path.exists(out):
        os.remove(out)

    eng = CaptureEngine(bitrate=500000)
    res = eng.start([("virtual", ch, "Virtual A")], log_path=out)
    assert len(res.opened) == 1 and not res.failed
    try:
        tx = can.Bus(interface="virtual", channel=ch)
        n = 8
        for i in range(n):
            tx.send(can.Message(arbitration_id=0x200 + i, data=bytes([i]), is_extended_id=False))
            time.sleep(0.02)
        time.sleep(0.4)
        tx.shutdown()

        stats = eng.monitor.poll()
        assert stats[0].total == n
        assert eng.total == n
    finally:
        eng.stop()

    back = list(logio.read_messages(out))
    assert len(back) == n
    assert all(logio.channel_bus_id(m.channel) == 0 for m in back)


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
