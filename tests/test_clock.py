"""canbench.live.clock.FrameClock: frame timestamps from the adapter's clock, on the wall clock.

Synthetic clocks only. Run standalone:  python tests/test_clock.py
"""
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import can

from canbench.live.clock import FrameClock

T0 = 1_800_000_000.0


def simulate(clock, n=20000, period=0.01, drift_ppm=0.0, dev0=5000.0, wrap=None, delay=(0.0002, 0.004),
             seed=1, late=()):
    """True wall times, the device's stamps (offset dev0, drift, optional wrap) and host read times
    (true + a random delivery delay, a few very late reads); returns max |mapped - true| after 2 s."""
    rnd = random.Random(seed)
    worst = 0.0
    last = None
    for i in range(n):
        true = T0 + i * period
        dev = dev0 + (true - T0) * (1 + drift_ppm * 1e-6)
        if wrap:
            dev %= wrap
        host = true + rnd.uniform(*delay) + (0.5 if i in late else 0.0)
        out = clock.wall(dev, host)
        if last is not None:
            assert out >= last, "not monotonic"
        last = out
        if true - T0 > 2.0:
            worst = max(worst, abs(out - true))
    return worst


def test_device_clock_mapped_within_the_least_delay():
    c = FrameClock("pcan")
    worst = simulate(c)
    assert worst < 0.0005, worst            # the envelope finds the fastest reads (0.2 ms)
    assert c.stats()["clock"] == "device (mapped)"


def test_drift_is_tracked():
    for ppm in (-50.0, 50.0):
        c = FrameClock("kvaser")
        worst = simulate(c, n=60000, drift_ppm=ppm)   # 10 minutes
        assert worst < 0.001, (ppm, worst)
        assert abs(c.stats()["drift_ppm"] + ppm) < 10, c.stats()


def test_late_reads_do_not_move_it():
    c = FrameClock("pcan")
    worst = simulate(c, late=set(range(3000, 3010)))
    assert worst < 0.0005, worst


def test_gs_usb_counter_wrap():
    c = FrameClock("gs_usb")
    wrap = 2 ** 32 / 1e6
    worst = simulate(c, n=3000, period=0.1, dev0=wrap - 100.0, wrap=wrap)   # wraps after 100 s
    assert worst < 0.0005, worst
    assert c.stats()["wraps"] == 1 and "restarts" not in c.stats()


def test_sparse_traffic_is_as_good_as_its_fastest_read():
    # one frame a second: ~10 reads per window, so the error is about the least delay among them
    c = FrameClock("pcan")
    assert simulate(c, n=600, period=1.0) < 0.004


def test_backward_jump_restarts_mapping():
    c = FrameClock("pcan")
    for i in range(500):
        c.wall(1000.0 + i * 0.01, T0 + i * 0.01 + 0.001)
    out = c.wall(3.0, T0 + 5.01 + 0.001)          # adapter reset: its clock starts again
    assert abs(out - (T0 + 5.01)) < 0.002
    assert c.stats()["restarts"] == 1


def test_host_clock_backends_pass_through():
    for b in ("socketcan", "virtual", "udp_multicast"):
        c = FrameClock(b)
        assert c.wall(T0 + 1.234567, T0 + 1.3) == T0 + 1.234567


def test_missing_timestamp_gets_host_time():
    c = FrameClock("pcan")
    assert c.wall(0.0, T0) == T0 and c.wall(None, T0 + 1) == T0 + 1
    assert c.stats()["no_timestamp"] == 2


def test_stamp_sets_message_timestamp():
    c = FrameClock("pcan")
    m = can.Message(timestamp=100.0, arbitration_id=1)
    c.stamp(m, T0)
    assert m.timestamp == T0


if __name__ == "__main__":
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except Exception as e:
                failed += 1
                print(f"FAIL {name}: {type(e).__name__}: {e}")
    sys.exit(1 if failed else 0)
