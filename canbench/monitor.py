"""Per-bus traffic statistics for the live logger GUI (GUI-agnostic).

Ingests the ``(timestamp, bus_id, msg, direction)`` items produced by
``canbench.live.receiver.CANBusLogger`` and maintains per-bus counters. The GUI
feeds every drained frame via :meth:`TrafficMonitor.ingest` and calls
:meth:`TrafficMonitor.poll` on a fixed timer to get a fresh snapshot (total
frames, rolling frames/sec, last-seen). Kept free of any Qt dependency so it can
be unit-tested headless.
"""
from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass
from typing import Dict, Optional


@dataclass
class BusStat:
    bus_id: int
    total: int
    rate_hz: float
    last_seen: Optional[float]      # wall-clock epoch of most recent frame, or None


class TrafficMonitor:
    """Accumulates per-bus frame counts; computes frames/sec between polls."""

    def __init__(self):
        self._total: Dict[int, int] = defaultdict(int)
        self._last_seen: Dict[int, float] = {}
        self._prev_total: Dict[int, int] = {}
        self._prev_t: Optional[float] = None

    def ingest(self, bus_id: int, when: Optional[float] = None) -> None:
        """Record one frame on ``bus_id`` (wall-clock ``when`` defaults to now)."""
        self._total[bus_id] += 1
        self._last_seen[bus_id] = time.time() if when is None else when

    def ingest_queue_item(self, item) -> None:
        """Ingest a receiver queue 4-tuple ``(rel_ts, bus_id, msg, direction)``."""
        self.ingest(item[1])

    def known_buses(self):
        return sorted(self._total)

    def reset(self) -> None:
        self._total.clear()
        self._last_seen.clear()
        self._prev_total.clear()
        self._prev_t = None

    def poll(self, now: Optional[float] = None) -> Dict[int, BusStat]:
        """Return per-bus stats and advance the rate window.

        ``rate_hz`` is (frames since last poll) / (seconds since last poll); it is
        0.0 on the first poll (no interval yet). Pass ``now`` in tests for
        deterministic timing.
        """
        now = time.time() if now is None else now
        dt = None if self._prev_t is None else (now - self._prev_t)

        stats: Dict[int, BusStat] = {}
        for bus_id, total in list(self._total.items()):   # snapshot: ingest runs on another thread
            prev = self._prev_total.get(bus_id, 0)
            rate = (total - prev) / dt if (dt and dt > 0) else 0.0
            stats[bus_id] = BusStat(bus_id, total, rate, self._last_seen.get(bus_id))

        self._prev_total = dict(self._total)
        self._prev_t = now
        return stats
