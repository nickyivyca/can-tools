"""Send failures during a replay (canbench.replay.SendStats, can_replayer.replay_once):
counted per bus, logged once per kind and then rate-limited, and summarised; and the
wait on a full SocketCAN transmit queue (ENOBUFS, canbench.replay.send_waiting).

Fake buses and a fake clock only; no adapter is opened.

Run standalone:   python tests/test_replay_send.py
Or under pytest:  python -m pytest tests/test_replay_send.py
"""
import errno
import importlib.util
import logging
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import can  # noqa: E402

from canbench.replay import SendStats, queue_full, send_waiting  # noqa: E402


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


ENOBUFS_TEXT = "Failed to transmit: No buffer space available"


class _FullBus:
    """Raises ENOBUFS (python-can's socketcan form) on the sends whose 0-based attempt
    number is in ``full_on`` (or on every send when ``full_on`` is None)."""

    def __init__(self, full_on=(), clock=None):
        self.full_on, self.clock, self.attempts, self.sent = full_on, clock, 0, []

    def send(self, msg):
        n = self.attempts
        self.attempts += 1
        if self.full_on is None or n in self.full_on:
            if self.clock is not None:
                self.clock.t += 0.001
            raise can.CanOperationError(ENOBUFS_TEXT, errno.ENOBUFS)
        self.sent.append(msg)


def _msg():
    return can.Message(arbitration_id=0x7A0, data=b"\x01", is_extended_id=False)


def test_queue_full_matches_enobufs_only():
    assert queue_full(can.CanOperationError(ENOBUFS_TEXT, errno.ENOBUFS))
    assert not queue_full(can.CanOperationError("A hardware error was detected", -15))
    assert not queue_full(can.CanOperationError("no code"))


def test_send_waiting_retries_with_backoff_and_returns_the_wait():
    clock, pauses = _Clock(), []
    bus = _FullBus(full_on={0, 1, 2}, clock=clock)
    w = send_waiting(bus, _msg(), 0.1, sleep=lambda s: (pauses.append(s), setattr(clock, "t", clock.t + s)),
                     clock=clock)
    assert len(bus.sent) == 1 and bus.attempts == 4
    assert pauses == [0.0005, 0.001, 0.002], pauses
    assert abs(w - (0.0005 + 0.001 + 0.001 + 0.002 + 0.001)) < 1e-9, w


def test_send_waiting_gives_up_after_the_wait_and_raises_other_errors_at_once():
    clock = _Clock()
    bus = _FullBus(full_on=None, clock=clock)
    try:
        send_waiting(bus, _msg(), 0.05, sleep=lambda s: setattr(clock, "t", clock.t + s), clock=clock)
        raise AssertionError("expected ENOBUFS")
    except can.CanOperationError as e:
        assert queue_full(e)
    assert 0.05 <= clock.t < 0.065 and bus.attempts > 5, (clock.t, bus.attempts)
    hw, slept = _Bus(fail_from=0), []
    try:
        send_waiting(hw, _msg(), 0.1, sleep=slept.append)
        raise AssertionError("expected the hardware error")
    except can.CanOperationError:
        pass
    assert slept == []
    nowait = _FullBus(full_on={0})
    try:
        send_waiting(nowait, _msg(), 0, sleep=slept.append)
        raise AssertionError("expected ENOBUFS with no wait")
    except can.CanOperationError:
        pass
    assert slept == [] and nowait.attempts == 1


def test_replay_once_waits_on_a_full_queue_and_sends_late():
    rp = _replayer()
    log = _Log()
    stats = SendStats(log=log)
    bus = _FullBus(full_on={3, 4})
    frames = _frames([(i * 0.001, "d_can") for i in range(10)])
    tx = rp.replay_once(frames, {"d_can": bus}, 0, threading.Event(), stats, 0.1)
    assert tx == 10 and len(bus.sent) == 10 and stats.total_failed == 0 and log.lines == []
    assert stats.waited == {"d_can": 1}
    assert stats.wait_lines()[0].startswith("d_can: 1 frames waited, longest "), stats.wait_lines()


def test_replay_once_with_no_wait_keeps_the_old_count():
    rp = _replayer()
    stats = SendStats(log=_Log())
    bus = _FullBus(full_on={3})
    frames = _frames([(i * 0.001, "d_can") for i in range(10)])
    assert rp.replay_once(frames, {"d_can": bus}, 0, threading.Event(), stats, 0) == 9
    assert stats.failed == {"d_can": 1} and not stats.waited


def test_replay_once_stops_waiting_on_a_stuck_bus_until_a_send_goes_out():
    rp = _replayer()
    stats = SendStats(log=_Log())
    bus = _FullBus(full_on=set(range(0, 10_000)))
    frames = _frames([(i * 0.001, "b_can") for i in range(50)])
    tx = rp.replay_once(frames, {"b_can": bus}, 0, threading.Event(), stats, 0.02)
    assert tx == 0 and stats.failed == {"b_can": 50}
    first_wait = bus.attempts - 49                  # every later frame is tried once
    assert 3 < first_wait < 20, bus.attempts        # only the first frame waited (~7 tries in 20 ms)


class _PerFrameBus:
    """ENOBUFS by frame (data[0] = frame index): ``full[i]`` = how many tries of frame i
    are refused (None: all of them)."""

    def __init__(self, full, hw=()):
        self.full, self.hw, self.tries, self.sent = full, hw, {}, []

    def send(self, msg):
        i = msg.data[0]
        n = self.tries[i] = self.tries.get(i, 0) + 1
        if i in self.hw:
            raise can.CanOperationError("A hardware error was detected", -15)
        limit = self.full.get(i, 0)
        if limit is None or n <= limit:
            raise can.CanOperationError(ENOBUFS_TEXT, errno.ENOBUFS)
        self.sent.append(i)


def test_replay_once_waits_again_once_a_stuck_bus_sends():
    rp = _replayer()
    stats = SendStats(log=_Log())
    bus = _PerFrameBus({0: None, 1: None, 2: None, 4: 2})
    frames = [can.Message(timestamp=i * 0.001, arbitration_id=0x7A0, data=bytes([i]), channel="b_can",
                          is_extended_id=False) for i in range(6)]
    assert rp.replay_once(frames, {"b_can": bus}, 0, threading.Event(), stats, 0.02) == 3
    assert bus.sent == [3, 4, 5] and stats.failed == {"b_can": 3} and stats.waited == {"b_can": 1}
    assert bus.tries[1] == 1 and bus.tries[2] == 1 and bus.tries[0] > 3, bus.tries


def test_another_error_does_not_stop_the_wait():
    rp = _replayer()
    stats = SendStats(log=_Log())
    bus = _PerFrameBus({1: 1}, hw={0})
    frames = [can.Message(timestamp=i * 0.001, arbitration_id=0x7A0, data=bytes([i]), channel="b_can",
                          is_extended_id=False) for i in range(2)]
    assert rp.replay_once(frames, {"b_can": bus}, 0, threading.Event(), stats, 0.02) == 1
    assert bus.sent == [1] and stats.failed == {"b_can": 1} and stats.waited == {"b_can": 1}


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
