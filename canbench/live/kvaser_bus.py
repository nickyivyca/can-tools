"""canbench's Kvaser bus: canlib error frames completed with state and counters.

canlib (python-can's ``kvaser`` interface) reports a bus error as an error
frame with no documented fields. The channel's status flags and error counters
are kept by the driver, so when an error frame arrives this class reads them
(``canReadStatus``, ``canReadErrorCounters``) and hands up a Linux error frame
(``canbench.errors.from_kvaser``): a bus error of type "other", the counters,
and the controller state when it differs from the last one reported. The
frame's own id and bytes are kept (``canbench.errors.ErrorMessage``). Counters
that read 0/0 are reported as unknown: linuxcan returned 0/0 on every error
frame of a 2026-11-12 jammer run, also while the status read error passive.

A state change with no error frame -- the return to error-active once errors
stop -- is found by polling the status every ``status_poll_s`` (0.5 s) from the
receive call, and reported as a state-only Linux frame (no bus error).
``canReadStatus`` asks the adapter (linuxcan ``leaf_get_chipstate``).

linuxcan copies an error frame's data bytes verbatim from the firmware
(``leafHWIf.c``, ``CMD_RX_STD_MESSAGE``), and neither it nor canlib documents
them, so they are kept raw. The counters come from the firmware too (the same
chip-state query); their 0/0 while error passive is the firmware's answer.

Selected for ``interface="kvaser"`` by ``canbench.live.backends.install()``.
"""
from __future__ import annotations

import ctypes
import logging
import sys
import time

from can.interfaces.kvaser.canlib import KvaserBus

from canbench import errors

logger = logging.getLogger(__name__)

_STATE_FLAGS = (errors.canSTAT_ERROR_PASSIVE | errors.canSTAT_BUS_OFF
                | errors.canSTAT_ERROR_WARNING | errors.canSTAT_ERROR_ACTIVE)


def _canlib():
    return getattr(sys.modules[KvaserBus.__module__], "__canlib", None)


class CanbenchKvaserBus(KvaserBus):
    """``KvaserBus`` whose error frames carry state and counters (module doc)."""

    def __init__(self, channel, *args, status_poll_s=0.5, **kwargs):
        super().__init__(channel, *args, **kwargs)
        self._last_state: int | None = None
        self.status_poll_s = status_poll_s
        self._next_poll = 0.0
        self._pending: list = []

    def _read_state(self):
        """(status flags, tx, rx), each None if canlib could not say."""
        lib = _canlib()
        status = tx = rx = None
        if lib is None:
            return status, tx, rx
        handle = self._read_handle
        try:
            flags = ctypes.c_ulong(0)
            if lib.canReadStatus(handle, ctypes.byref(flags)) >= 0:
                status = flags.value
        except Exception as e:
            logger.debug("canReadStatus: %s", e)
        try:
            t, r, o = ctypes.c_uint(0), ctypes.c_uint(0), ctypes.c_uint(0)
            if lib.canReadErrorCounters(handle, ctypes.byref(t), ctypes.byref(r),
                                        ctypes.byref(o)) >= 0:
                tx, rx = t.value, r.value
        except Exception as e:
            logger.debug("canReadErrorCounters: %s", e)
        return status, tx, rx

    def _poll_state(self):
        """A state-only report if the polled state differs from the last one reported, else None."""
        now = time.monotonic()
        if self.status_poll_s is None or now < self._next_poll:
            return None
        self._next_poll = now + self.status_poll_s
        status, _tx, _rx = self._read_state()
        if status is None:
            return None
        state = status & _STATE_FLAGS
        if self._last_state is None:
            self._last_state = state          # the first read is the baseline, not a change
            return None
        if state == self._last_state:
            return None
        self._last_state = state
        out = errors.from_kvaser(status, None, None, bus_error=False, timestamp=time.time(),
                                 channel=self.channel_info,
                                 raw_backend="kvaser", raw_note=f"status=0x{status:X} (polled)")
        out.host_stamped = True     # host time, not the adapter's clock (canbench/live/clock.py)
        return out

    def _recv_internal(self, timeout):
        if self._pending:
            return self._pending.pop(0), False
        msg, filtered = super()._recv_internal(timeout)
        if msg is not None and msg.is_error_frame and not isinstance(msg, errors.ErrorMessage):
            msg = self._complete(msg)
        polled = self._poll_state()
        if polled is not None:
            if msg is None:
                return polled, False
            self._pending.append(polled)
        return msg, filtered

    def _complete(self, msg):
        """A canlib error frame completed with the state and counters read now."""
        status, tx, rx = self._read_state()
        note = []
        if status is not None:
            note.append(f"status=0x{status:X}")
        if tx is not None:
            note.append(f"tx={tx} rx={rx}")
        if tx == 0 and rx == 0:
            # linuxcan's counters are the firmware's chip-state answer; on the 2026-11-12 bench
            # (Leaf Light v2, linuxcan 5.53) they read 0/0 on every error frame, also while
            # canReadStatus said error passive. Zeros are therefore unknown (raw_note keeps them),
            # as for a Linux error frame without CAN_ERR_CNT.
            tx = rx = None
        state = None if status is None else status & _STATE_FLAGS
        report_status = status if state != self._last_state else None
        if status is not None:
            self._last_state = state
        return errors.from_kvaser(report_status, tx, rx, timestamp=msg.timestamp,
                                  channel=msg.channel, is_rx=msg.is_rx,
                                  raw_id=msg.arbitration_id, raw_data=bytes(msg.data),
                                  raw_note=" ".join(note))
