"""Unit tests for canbench.monitor.TrafficMonitor (no hardware)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from canbench.monitor import TrafficMonitor


def test_counts_and_rate():
    m = TrafficMonitor()
    # first poll establishes the baseline time; rate 0
    s0 = m.poll(now=100.0)
    assert s0 == {}

    for _ in range(10):
        m.ingest(0, when=100.5)
    for _ in range(4):
        m.ingest(1, when=100.5)

    s1 = m.poll(now=101.0)          # 1.0 s between polls (100.0 -> 101.0)
    assert s1[0].total == 10
    assert s1[1].total == 4
    assert abs(s1[0].rate_hz - 10.0) < 1e-6   # 10 frames / 1.0 s poll interval
    assert abs(s1[1].rate_hz - 4.0) < 1e-6
    assert s1[0].last_seen == 100.5           # frame wall-clock, independent of poll timing


def test_rate_uses_delta_between_polls():
    m = TrafficMonitor()
    m.poll(now=0.0)
    for _ in range(100):
        m.ingest(0)
    m.poll(now=1.0)                 # consumes the 100
    for _ in range(50):
        m.ingest(0)
    s = m.poll(now=2.0)            # only the new 50 count toward this window
    assert s[0].total == 150
    assert abs(s[0].rate_hz - 50.0) < 1e-6


def test_ingest_queue_item_and_reset():
    m = TrafficMonitor()
    m.ingest_queue_item((0.0, 3, object(), "Rx"))
    assert m.known_buses() == [3]
    m.reset()
    assert m.known_buses() == []


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn(); print(f"PASS  {name}")
            except AssertionError as e:
                failures += 1; print(f"FAIL  {name}: {e}")
    print(f"\n{'ALL PASS' if failures == 0 else str(failures) + ' FAILED'}")
    sys.exit(1 if failures else 0)
