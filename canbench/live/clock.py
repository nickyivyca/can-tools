"""Frame timestamps on the wall clock, from each adapter's own clock.

Until 2026-11-12 can_logger and the GUI engine overwrote every frame's
timestamp with the host's ``time.time()`` when the frame was written -- for
can_logger, when the writer took it off the queue -- so every adapter's own
timestamp was lost and the host's scheduling showed up as timing. Now each
receive thread passes the frame and the host time at ``recv()`` to a
:class:`FrameClock` for that bus, and the frame keeps its own timestamp, put on
the wall clock:

* **Host-clock backends** (``socketcan``: the kernel's receive time;
  ``virtual``, ``udp_multicast``: ``time.time()`` at send) are already wall
  time and pass through unchanged.
* **Device-clock backends** (``pcan``: the device clock under the RAW library on
  Linux, the Windows tick base on Windows; ``kvaser``: the adapter timer plus an
  offset python-can sets once at open; ``gs_usb``: a 32-bit microsecond counter;
  ``ixxat``) are mapped: wall = device + offset, where the offset follows the
  lower envelope of (host recv time - device time) -- the reads with the least
  delivery delay -- over the last ``window_s`` seconds. A lower envelope takes
  effect at once; a higher one (the device clock running slow against the host)
  is followed at no more than ``slew_ppm``, so drift of a few tens of ppm is
  tracked while one late read moves nothing. The gs_usb counter's wrap (every
  2**32 us, 71.6 min) is unwrapped. A backward jump of more than a second on any
  other backend (a reopened or reset adapter) starts the mapping again.
  Output is kept monotonic per bus. Accuracy is about the least delivery delay
  among the reads in the window: a few hundred microseconds on a busy bus,
  milliseconds at one frame a second.

A frame without a timestamp (None, or 0.0 from a backend that never stamps)
gets the host time. ``stats()`` reports what the
mapping did, for the logger's summary.
"""
from __future__ import annotations

import time
from collections import deque

HOST_CLOCK = frozenset({"socketcan", "virtual", "udp_multicast", "socketcand"})
WRAP_S = {"gs_usb": 2 ** 32 / 1e6}


class FrameClock:
    """Maps one bus's frame timestamps to the wall clock (module doc)."""

    def __init__(self, backend: str, window_s: float = 10.0, slew_ppm: float = 200.0):
        self.backend = backend
        self.passthrough = backend in HOST_CLOCK
        self.wrap = WRAP_S.get(backend)
        self.window_s = window_s
        self.slew = slew_ppm * 1e-6
        self._env: deque = deque()     # (host, host - device), lower envelope candidates
        self.offset: float | None = None
        self._last_raw: float | None = None
        self._wraps = 0
        self._last_host: float | None = None
        self._last_out = float("-inf")
        self.n = 0
        self.restarts = 0
        self.wraps_seen = 0
        self.no_timestamp = 0
        self.first_offset: float | None = None
        self.first_host: float | None = None

    def _restart(self):
        self._env.clear()
        self.offset = None
        self._wraps = 0
        self.restarts += 1

    def wall(self, device_ts, host_ts: float) -> float:
        """The wall-clock time of a frame with timestamp ``device_ts`` read at ``host_ts``."""
        self.n += 1
        # None, or 0.0 when the previous frame had none either (a backend that does not stamp);
        # a single 0.0 after real stamps is a counter that reads zero, e.g. at a wrap
        if device_ts is None or (device_ts == 0 and not self._last_raw):
            self.no_timestamp += 1
            return host_ts
        if self.passthrough:
            return device_ts
        raw = float(device_ts)
        if self._last_raw is not None and raw < self._last_raw - 1.0:
            if self.wrap and raw < self._last_raw - self.wrap / 2:
                self._wraps += 1
                self.wraps_seen += 1
            else:
                self._restart()
        self._last_raw = raw
        dev = raw + self._wraps * (self.wrap or 0.0)
        d = host_ts - dev

        env = self._env
        while env and env[-1][1] >= d:
            env.pop()
        env.append((host_ts, d))
        while env and env[0][0] < host_ts - self.window_s:
            env.popleft()
        target = env[0][1]

        if self.offset is None:
            self.offset = target
            if self.first_offset is None:
                self.first_offset, self.first_host = target, host_ts
        elif target < self.offset:
            self.offset = target
        elif target > self.offset:
            dt = max(host_ts - (self._last_host or host_ts), 0.0)
            self.offset += min(target - self.offset, self.slew * dt)
        self._last_host = host_ts

        out = dev + self.offset
        if out < self._last_out:
            out = self._last_out
        self._last_out = out
        return out

    def stamp(self, msg, host_ts: float | None = None):
        """Set ``msg.timestamp`` to its wall-clock time; returns the message."""
        msg.timestamp = self.wall(msg.timestamp, time.time() if host_ts is None else host_ts)
        return msg

    def stats(self) -> dict:
        out = {"backend": self.backend, "frames": self.n,
               "clock": "host (unchanged)" if self.passthrough else "device (mapped)"}
        if not self.passthrough and self.offset is not None:
            out["offset_s"] = round(self.offset, 6)
            if self.first_offset is not None and self._last_host and self._last_host > self.first_host + 1:
                out["drift_ppm"] = round((self.offset - self.first_offset)
                                         / (self._last_host - self.first_host) * 1e6, 1)
        if self.restarts:
            out["restarts"] = self.restarts
        if self.wraps_seen:
            out["wraps"] = self.wraps_seen
        if self.no_timestamp:
            out["no_timestamp"] = self.no_timestamp
        return out
