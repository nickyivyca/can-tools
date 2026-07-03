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

import can

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
        self._bus_by_id: Dict[int, object] = {}
        self._capture_ids: Optional[set] = None      # None = log every opened bus
        self._routes: Dict[int, int] = {}            # source bus_id -> dest bus_id (passthrough)
        self._writer = None
        self._drain: Optional[threading.Thread] = None
        self._running = False

    @property
    def running(self) -> bool:
        return self._running

    @property
    def logging_enabled(self) -> bool:
        return self._writer is not None

    def start(self, interfaces, log_path: Optional[str] = None,
              capture_ids=None, routes=None) -> OpenResult:
        """Open ``interfaces`` (list of (iface, ch, desc)); begin capture.

        Only the interfaces passed here are opened/owned -- callers pass just the
        buses that are enabled or are passthrough endpoints, so an unlisted or
        unchecked dongle stays free for other programs (e.g. loopback testing).

        ``capture_ids``: bus_ids whose frames are written to ``log_path`` (default
        None = every opened bus). ``routes``: ``{src_bus_id: dest_bus_id}`` to
        forward each source bus's frames onto another opened bus (passthrough).
        Buses that fail to open are reported and skipped.
        """
        if self._running:
            raise RuntimeError("engine already running")

        self.monitor.reset()
        self.bus_labels.clear()
        self._bus_by_id.clear()
        self.total = 0
        self.log_path = log_path
        self._capture_ids = set(capture_ids) if capture_ids is not None else None
        self._routes = dict(routes) if routes else {}
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
            self._bus_by_id[bus_id] = bus
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
            _ts, bus_id, msg, _dir = item
            self.monitor.ingest_queue_item(item)

            # passthrough: forward this source bus's frame onto its dest bus
            dest = self._routes.get(bus_id)
            if dest is not None:
                dest_bus = self._bus_by_id.get(dest)
                if dest_bus is not None:
                    fwd = can.Message(
                        arbitration_id=msg.arbitration_id, data=msg.data,
                        is_extended_id=msg.is_extended_id,
                        is_remote_frame=msg.is_remote_frame, dlc=msg.dlc,
                    )
                    try:
                        dest_bus.send(fwd)
                    except Exception as e:
                        logger.debug("forward error onto bus %s: %s", dest, e)

            # log only frames sourced on a captured bus
            if self._writer is not None and (self._capture_ids is None or bus_id in self._capture_ids):
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
