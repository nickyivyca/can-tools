"""Capture engine: multi-bus acquisition feeding a TrafficMonitor + candump log.

GUI-agnostic (no Qt) so it can be unit-tested headless and reused by the CLI.
The GUI creates one CaptureEngine, calls :meth:`start` with the selected
interfaces (and an optional log path), polls ``engine.monitor`` on a timer to
refresh its table, and calls :meth:`stop` to end the session.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from queue import Queue, Empty
from typing import Dict, List, Optional, Tuple

from . import logio, buses
from .live.receiver import CANBusLogger
from .monitor import TrafficMonitor

logger = logging.getLogger(__name__)


@dataclass
class OpenResult:
    opened: List[Tuple[int, str]] = field(default_factory=list)   # (bus_id, desc)
    failed: List[Tuple[str, str]] = field(default_factory=list)   # (desc, error)


class CaptureEngine:
    """Opens selected interfaces, counts traffic, optionally writes candump."""

    def __init__(self, bitrate: int = 500000):
        self.bitrate = bitrate
        self.monitor = TrafficMonitor()
        self.bus_labels: Dict[int, str] = {}
        self.total = 0
        self.log_path: Optional[str] = None
        self._queue: Queue = Queue()
        self._loggers: List[CANBusLogger] = []
        self._writer = None
        self._drain: Optional[threading.Thread] = None
        self._running = False

    @property
    def running(self) -> bool:
        return self._running

    @property
    def logging_enabled(self) -> bool:
        return self._writer is not None

    def start(self, interfaces, log_path: Optional[str] = None) -> OpenResult:
        """Open ``interfaces`` (list of (iface, ch, desc)); begin capture.

        If ``log_path`` is given, frames are also written to a candump log. Buses
        that fail to open are reported in the result and skipped; capture still
        starts on the ones that opened.
        """
        if self._running:
            raise RuntimeError("engine already running")

        self.monitor.reset()
        self.bus_labels.clear()
        self.total = 0
        self.log_path = log_path
        result = OpenResult()

        for bus_id, (iface, ch, desc) in enumerate(interfaces):
            try:
                bus = buses.open_bus(iface, ch, self.bitrate)
            except Exception as e:
                logger.error("Failed to open %s: %s", desc, e)
                result.failed.append((desc, str(e)))
                continue
            blog = CANBusLogger(bus_id, bus, self._queue, iface, ch, self.bitrate)
            self._loggers.append(blog)
            self.bus_labels[bus_id] = desc
            result.opened.append((bus_id, desc))

        if not self._loggers:
            return result

        if log_path:
            self._writer = logio.open_writer(log_path)

        self._running = True
        for blog in self._loggers:
            blog.start()
        self._drain = threading.Thread(target=self._drain_loop, daemon=True)
        self._drain.start()
        return result

    def _drain_loop(self):
        while self._running:
            try:
                item = self._queue.get(timeout=0.2)
            except Empty:
                continue
            self.monitor.ingest_queue_item(item)
            if self._writer is not None:
                _ts, bus_id, msg, _dir = item
                msg.channel = logio.bus_channel(bus_id)
                msg.timestamp = time.time()
                try:
                    self._writer(msg)
                except Exception as e:
                    logger.error("write error: %s", e)
            self.total += 1

    def stop(self):
        """Stop capture, close the log, and shut down all buses."""
        if not self._running:
            return
        self._running = False
        if self._drain is not None:
            self._drain.join(timeout=2.0)
            self._drain = None
        for blog in self._loggers:
            try:
                blog.stop()
            except Exception:
                pass
        self._loggers.clear()
        if self._writer is not None:
            try:
                self._writer.stop()
            except Exception:
                pass
            self._writer = None
