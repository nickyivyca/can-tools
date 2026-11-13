"""canbench's PCAN bus: error and status reports in the Linux layout, and its
own bus-off recovery.

python-can 4.6.1's ``PcanBus`` has three gaps this class closes:

1. **Status messages come out as data frames.** PCAN-Basic delivers controller
   state changes as ``PCAN_MESSAGE_STATUS`` messages; python-can never tests
   that flag, so each becomes an ordinary frame (id = the driver's state
   number, 4 data bytes) that a logger writes and a replayer replays as bus
   traffic (2026-11-12 bench, reverse-it ``tools/plans/madhouse-can-tools-bench.md``).
   Here a status message becomes a Linux controller-state error frame.
2. **A bus state returned by Read is dropped.** When Read returns
   BUSLIGHT/BUSHEAVY python-can logs a warning and returns nothing; other states
   raise. Here the state becomes a status report like (1).
3. **Bus-off is not recovered on Linux.** PCAN-Basic for Linux 9.3.3's
   ``PCAN_BUSOFF_AUTORESET`` never fires on a failed send: ``pcbcore.c`` tests
   the write status against the driver's ``PCANFD_ERROR_BUSOFF`` (4) where the
   status is PCAN-Basic's ``PCAN_ERROR_BUSOFF`` (0x10); only its read path
   resets, and (below) only with the hard reset on. So when a
   send fails, or a read reports bus-off, this class asks the channel's status
   and, if it is bus-off, resets the channel itself (``PCANBasic.Reset``), at most
   once per ``busoff_reset_interval`` seconds, and reports it as a
   ``CAN_ERR_RESTARTED`` error frame. ``PCANBasic.Reset`` only flushes the
   queues unless the channel's ``PCAN_HARD_RESET_STATUS`` (0x2E) is on, when it
   resets the controller as well (``pcanbasic_reset``: ``PCANFD_RESET_ALL``); with
   it off, 124 resets on the 2026-11-12 bench left the PCAN bus-off. So this
   class turns it on at open. Pass ``canbench_busoff_reset=False`` to leave
   bus-off and that parameter alone. The driver and library are not modified.

PCAN error messages (``PCAN_MESSAGE_ERRFRAME``) become Linux protocol-error
frames (``canbench.errors.from_pcan_error``). Every translated frame keeps the
adapter's own id and bytes (``canbench.errors.ErrorMessage``).

Selected for ``interface="pcan"`` by ``canbench.live.backends.install()``.
"""
from __future__ import annotations

import logging
import time
from collections import deque

from can.interfaces.pcan.basic import (
    PCAN_ALLOW_STATUS_FRAMES, PCAN_ERROR_ILLDATA, PCAN_ERROR_OK, PCAN_ERROR_QRCVEMPTY,
    PCAN_MESSAGE_ERRFRAME, PCAN_MESSAGE_STATUS, PCAN_PARAMETER_ON, TPCANParameter,
)
from can.interfaces.pcan.pcan import PcanBus, PcanCanOperationError

from canbench import errors

logger = logging.getLogger(__name__)

# PCANBasic.h: "Activation status of hard reset processing via CAN_Reset calls";
# python-can 4.6.1 does not name it.
PCAN_HARD_RESET_STATUS = TPCANParameter(0x2E)

_STATE_RESULT = errors.PCAN_BUS_STATE_BITS | errors.PCAN_ERROR_OVERRUN | errors.PCAN_ERROR_QOVERRUN


def _v(x):
    return getattr(x, "value", x)


class CanbenchPcanBus(PcanBus):
    """``PcanBus`` with Linux-layout error reports and bus-off recovery (module doc)."""

    def __init__(self, channel="PCAN_USBBUS1", *args, canbench_busoff_reset=True,
                 busoff_reset_interval=0.1, **kwargs):
        super().__init__(channel, *args, **kwargs)
        self._pending: deque = deque()
        self._last_type = 0
        self._last_raw = (0, b"")
        self._synth_status: int | None = None
        self._last_ts = 0.0
        self.busoff_reset = canbench_busoff_reset
        self.busoff_reset_interval = busoff_reset_interval
        self.busoff_resets = 0
        self._last_reset = float("-inf")
        try:
            self.m_objPCANBasic.SetValue(self.m_PcanHandle, PCAN_ALLOW_STATUS_FRAMES,
                                         PCAN_PARAMETER_ON)
        except Exception as e:   # older libraries
            logger.debug("PCAN_ALLOW_STATUS_FRAMES: %s", e)
        self.hard_reset_status = None
        if self.busoff_reset:
            try:
                self.hard_reset_status = _v(self.m_objPCANBasic.SetValue(
                    self.m_PcanHandle, PCAN_HARD_RESET_STATUS, PCAN_PARAMETER_ON))
            except Exception as e:
                logger.warning("PCAN_HARD_RESET_STATUS: %s", e)
        self._wrap_read("Read")
        self._wrap_read("ReadFD")

    # -- receive -------------------------------------------------------------

    def _wrap_read(self, name):
        pcb = self.m_objPCANBasic
        original = getattr(pcb, name, None)
        if original is None:
            return

        def read(handle):
            result, m, ts = original(handle)
            r = _v(result)
            if r == _v(PCAN_ERROR_OK):
                self._last_type = m.MSGTYPE
                if m.MSGTYPE & (_v(PCAN_MESSAGE_STATUS) | _v(PCAN_MESSAGE_ERRFRAME)):
                    self._last_raw = (m.ID, bytes(m.DATA[:4]))
                return result, m, ts
            if r in (_v(PCAN_ERROR_QRCVEMPTY), _v(PCAN_ERROR_ILLDATA)):
                return result, m, ts
            if r & _STATE_RESULT and not r & ~_STATE_RESULT:
                # A bus state in place of a message: hand it up as a status message.
                self._synth_status = r
                self._last_type = _v(PCAN_MESSAGE_STATUS)
                self._last_raw = (0, r.to_bytes(4, "big"))
                m.MSGTYPE = _v(PCAN_MESSAGE_STATUS)
                m.ID = 0
                if hasattr(m, "LEN"):
                    m.LEN = 4
                else:
                    m.DLC = 4
                for i, b in enumerate(r.to_bytes(4, "big")):
                    m.DATA[i] = b
                return PCAN_ERROR_OK, m, ts
            return result, m, ts

        setattr(pcb, name, read)

    def _recv_internal(self, timeout):
        if self._pending:
            return self._pending.popleft(), False
        self._last_type = 0
        self._synth_status = None
        msg, filtered = super()._recv_internal(timeout)
        if msg is None:
            if self._pending:
                return self._pending.popleft(), False
            return None, filtered
        if self._last_type & _v(PCAN_MESSAGE_STATUS):
            raw_id, raw_data = self._last_raw
            if self._synth_status is not None:
                status = self._synth_status
                note = f"status=0x{status:05X} (returned by Read)"
                ts = self._last_ts
            else:
                status = int.from_bytes(raw_data[:4], "big")
                note = f"status=0x{status:05X} (status message)"
                ts = msg.timestamp
            msg = errors.from_pcan_status(status, timestamp=ts, channel=msg.channel,
                                          raw_id=raw_id, raw_data=raw_data, raw_note=note)
            if status & errors.PCAN_ERROR_BUSOFF:
                self._recover("read")
        elif msg.is_error_frame:
            msg = errors.from_pcan_error(msg.arbitration_id, bytes(msg.data),
                                         timestamp=msg.timestamp, channel=msg.channel)
        else:
            self._last_ts = msg.timestamp
        return msg, filtered

    # -- transmit ------------------------------------------------------------

    def send(self, msg, timeout=None):
        try:
            return super().send(msg, timeout)
        except PcanCanOperationError:
            self._recover("send")
            raise

    # -- bus-off -------------------------------------------------------------

    def _recover(self, where):
        if not self.busoff_reset:
            return
        now = time.monotonic()
        if now - self._last_reset < self.busoff_reset_interval:
            return
        try:
            status = _v(self.m_objPCANBasic.GetStatus(self.m_PcanHandle))
        except Exception:
            status = errors.PCAN_ERROR_BUSOFF
        if not status & errors.PCAN_ERROR_BUSOFF:
            return
        self._last_reset = now
        try:
            ok = _v(self.m_objPCANBasic.Reset(self.m_PcanHandle)) == _v(PCAN_ERROR_OK)
        except Exception as e:
            logger.warning("PCAN reset after bus-off failed: %s", e)
            ok = False
        self.busoff_resets += 1
        self._pending.append(errors.error_frame(
            errors.CAN_ERR_RESTARTED, b"", timestamp=self._last_ts, channel=self.channel_info,
            raw_backend="canbench",
            raw_note=f"PCAN reset after bus-off on {where}: {'ok' if ok else 'failed'}"))
