"""Two-dongle CAN passthrough bridge.

Forwards frames between two CAN buses (two physical segments) in both directions.
Unlike passive logging, this actively re-transmits each frame onto the *other*
bus, so the two segments behave as one. Each original frame is reported once,
tagged with the bus it was *sourced* on (the forwarded copy is not re-logged
because ``receive_own_messages`` is off), so a passthrough capture has no
duplicates.

Typical use: sit between a module and the rest of a vehicle to observe (and
optionally, later, modify) traffic crossing the gap.
"""
from __future__ import annotations

import logging
import threading

import can

logger = logging.getLogger(__name__)


class PassthroughBridge:
    """Bidirectional forwarder between two buses with a per-original callback.

    ``on_frame(source_bus_id, msg)`` is invoked for every original frame before
    it is forwarded — use it to log the frame under its source bus id.
    """

    def __init__(self, bus_a, bus_b, on_frame=None, bus_ids=(0, 1)):
        self.buses = (bus_a, bus_b)
        self.bus_ids = bus_ids
        self.on_frame = on_frame
        self.counts = [0, 0]            # frames sourced on bus A / bus B
        self._running = False
        self._threads = []

    def start(self):
        self._running = True
        for src in (0, 1):
            t = threading.Thread(target=self._pump, args=(src,), daemon=True)
            t.start()
            self._threads.append(t)
        logger.info("Passthrough bridge started (bus %s <-> bus %s)", *self.bus_ids)

    def _pump(self, src):
        dst = 1 - src
        src_bus, dst_bus = self.buses[src], self.buses[dst]
        src_id = self.bus_ids[src]
        while self._running:
            try:
                msg = src_bus.recv(timeout=0.1)
            except Exception as e:
                if self._running:
                    logger.debug("recv error on bus %s: %s", src_id, e)
                continue
            if msg is None:
                continue
            self.counts[src] += 1
            if self.on_frame is not None:
                try:
                    self.on_frame(src_id, msg)
                except Exception as e:
                    logger.debug("on_frame callback error: %s", e)
            out = can.Message(
                arbitration_id=msg.arbitration_id,
                data=msg.data,
                is_extended_id=msg.is_extended_id,
                is_remote_frame=msg.is_remote_frame,
                dlc=msg.dlc,
            )
            try:
                dst_bus.send(out)
            except can.CanError as e:
                logger.debug("forward send error onto bus %s: %s", self.bus_ids[dst], e)

    def stop(self):
        self._running = False
        for t in self._threads:
            t.join(timeout=1.0)
        logger.info("Passthrough bridge stopped (A=%d, B=%d frames)", *self.counts)
