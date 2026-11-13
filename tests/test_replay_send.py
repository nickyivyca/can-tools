"""Send failures during a replay (canbench.replay.SendStats, can_replayer.replay_once):
counted per bus, logged once per kind and then rate-limited, and summarised.

Fake buses and a fake clock only; no adapter is opened.

Run standalone:   python tests/test_replay_send.py
Or under pytest:  python -m pytest tests/test_replay_send.py
"""
import importlib.util
import logging
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import can  # noqa: E402

from canbench.replay import SendStats  # noqa: E402


def _replayer():
    spec = importlib.util.spec_from_file_location("can_replayer", ROOT / "tools" / "can_replayer.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Log:
    def __init__(self):
        self.lines = []

    def warning(self, fmt, *a):
        self.lines.append(fmt % a)


class _Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


class _Bus:
    """Sends ok until ``fail_from`` sends have been made, then raises ``err`` on every send."""

    def __init__(self, fail_from=None, err="Function canWrite failed - A hardware error was detected [Error Code -15]"):
        self.fail_from, self.err, self.sent = fail_from, err, []

    def send(self, msg):
        if self.fail_from is not None and len(self.sent) >= self.fail_from:
            raise can.CanOperationError(self.err)
        self.sent.append(msg)


def _frames(spec):
    """[(t, channel)] -> messages."""
    return [can.Message(timestamp=t, arbitration_id=0x7A0, data=b"\x01", channel=ch, is_extended_id=False)
            for t, ch in spec]


def test_first_failure_per_kind_is_logged_then_rate_limited():
    log, clock = _Log(), _Clock()
    s = SendStats(report_every_s=10.0, clock=clock, log=log)
    s.fail("b_can", 99, 1000, 82.6, "E1")
    assert len(log.lines) == 1 and "frame 100 of 1000" in log.lines[0] and "t=82.600" in log.lines[0]
    for i in range(500):
        clock.t = i * 0.01                       # 5 s of failures
        s.fail("b_can", 100 + i, 1000, 83.0 + i * 0.01, "E1")
    assert len(log.lines) == 1, log.lines
    clock.t = 10.0
    s.fail("b_can", 600, 1000, 90.0, "E1")
    assert len(log.lines) == 2 and "501 more send failures (502 in all" in log.lines[1], log.lines
    s.fail("b_can", 601, 1000, 90.1, "E2")       # a new kind is logged at once
    assert len(log.lines) == 3 and "frame 602" in log.lines[2]


def test_flush_reports_what_is_left_and_summary_per_bus():
    log, clock = _Log(), _Clock()
    s = SendStats(clock=clock, log=log)
    s.ok("b_can")
    s.ok("d_can")
    s.fail("d_can", 5, 10, 1.5, "ENOBUFS")
    s.fail("d_can", 6, 10, 1.6, "ENOBUFS")
    s.fail("d_can", 7, 10, 1.7, "ENOBUFS")
    s.flush()
    assert "2 more send failures (3 in all" in log.lines[-1]
    s.flush()                                    # nothing pending: no new line
    assert len(log.lines) == 2
    assert s.total_failed == 3
    lines = s.summary_lines()
    assert lines == ["d_can: 1 sent, 3 failed (t=1.500-1.700 s in the log): 3 x ENOBUFS"], lines


def test_replay_once_counts_per_bus_and_keeps_going():
    rp = _replayer()
    log = _Log()
    stats = SendStats(log=log)
    b, d = _Bus(fail_from=3), _Bus()
    frames = _frames([(i * 0.001, "b_can" if i % 2 == 0 else "d_can") for i in range(20)])
    tx = rp.replay_once(frames, {"b_can": b, "d_can": d}, 0, threading.Event(), stats)
    assert tx == 13 and len(b.sent) == 3 and len(d.sent) == 10
    assert stats.sent == {"b_can": 3, "d_can": 10} and stats.failed == {"b_can": 7}
    # the first failure is frame 7 (index 6), the fourth b_can frame
    assert "bus b_can: send failed at frame 7 of 20" in log.lines[0], log.lines
    assert "6 more send failures (7 in all" in log.lines[-1]


def test_replay_once_without_stats_still_returns_count():
    rp = _replayer()
    logging.getLogger("canbench.replay").setLevel(logging.ERROR)
    frames = _frames([(0.0, 0), (0.001, 0)])
    assert rp.replay_once(frames, {0: _Bus(fail_from=1)}, 0, threading.Event()) == 1


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
