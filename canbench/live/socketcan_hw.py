"""A SocketCAN bus that keeps each frame's hardware timestamp as well as the host's.

python-can's ``socketcan`` backend sets only ``SO_TIMESTAMPNS``, so a frame's
timestamp is the host kernel's receive time. Some SocketCAN drivers also stamp
each frame with the adapter's own clock: measured 2026-11-18 on the Kvaser Leaf
under the in-tree ``kvaser_usb`` driver (reverse-it
``projects/vtrux/notes/artifacts/gen-inhibit/runs/kvaser_usb_measure_20261118T154231``),
200 of 200 frames carried a nonzero raw hardware stamp on the Leaf's clock. On
2026-11-13 the same kernel gave zeros on every frame, cause unknown, so each
open checks the stamps before trusting them.

:class:`SocketcanHwBus` opens a raw CAN socket with ``SO_TIMESTAMPING``
(``RX_HARDWARE | RAW_HARDWARE | RX_SOFTWARE | SOFTWARE``) and every error class
enabled (``CAN_RAW_ERR_FILTER`` = ``CAN_ERR_MASK``). Each received message has:

* ``timestamp`` -- the host receive time (``ts[0]``), the same clock python-can's
  ``socketcan`` gives, so :class:`canbench.live.clock.FrameClock` treats it as
  host time unchanged;
* ``host_timestamp`` -- the same value, named;
* ``hw_timestamp`` -- the raw hardware stamp (``ts[2]``) in seconds on the
  adapter's clock, or None: when the driver gave 0, or when this open's stamp
  check failed (then every frame of the open has None);
* ``timestamp_source`` -- ``"host"``: what ``timestamp`` carries.

Error frames come through as python-can error messages (``is_error_frame``,
``arbitration_id`` = the class bits, data as the kernel gave it), the layout
:func:`canbench.errors.decode` reads. On the Kvaser under kvaser_usb, error
frames carry no hardware stamp (0 of 835 in the 2026-11-18 bench check, reverse-it
``tools/artifacts/can-tools-bench/runs/socketcan_hw_20261118``): their
``hw_timestamp`` is None and only host time places them.

**The stamp check** (:class:`StampCheck`, result in ``bus.stamp_check``) runs on
the first ``check_frames`` data frames after the open (error frames are counted
but not judged). It passes when every one has a nonzero hardware stamp, each is
later than the one before, and each is more than ``host_gap_s`` from its host
stamp (closer than that, the "hardware" stamp is host time). Until it has
seen enough frames its state is ``"pending"`` and ``hw_timestamp`` is passed
through as read. On a failure the state is ``"host_only"`` with the reason,
and ``hw_timestamp`` is None for the rest of the open. Fitting the adapter's
clock to host time is the caller's job; this class gives both stamps and the
check's result. After the check, zero or backward hardware stamps are counted
in ``bus.stamp_stats()``.

Opt-in: nothing else in canbench uses this class, and python-can's ``socketcan``
interface is left as it is. The link's bitrate and state are set outside
(``canctl up``); the class does not configure the interface. Classic CAN only.
Linux only.
"""
from __future__ import annotations

import select
import socket
import struct
import time

import can

# linux/net_tstamp.h, asm-generic/socket.h
SO_TIMESTAMPING = 37
SCM_TIMESTAMPING = SO_TIMESTAMPING
SOF_TIMESTAMPING_RX_HARDWARE = 1 << 2
SOF_TIMESTAMPING_RX_SOFTWARE = 1 << 3
SOF_TIMESTAMPING_SOFTWARE = 1 << 4
SOF_TIMESTAMPING_RAW_HARDWARE = 1 << 6
TIMESTAMPING_FLAGS = (SOF_TIMESTAMPING_RX_HARDWARE | SOF_TIMESTAMPING_RAW_HARDWARE
                      | SOF_TIMESTAMPING_RX_SOFTWARE | SOF_TIMESTAMPING_SOFTWARE)

# linux/can.h, linux/can/raw.h, linux/can/error.h
CAN_EFF_FLAG = 0x80000000
CAN_RTR_FLAG = 0x40000000
CAN_ERR_FLAG = 0x20000000
CAN_EFF_MASK = 0x1FFFFFFF
CAN_SFF_MASK = 0x000007FF
CAN_ERR_MASK = 0x1FFFFFFF
SOL_CAN_RAW = 101
CAN_RAW_ERR_FILTER = 2

CAN_FRAME = struct.Struct("=IB3x8s")          # struct can_frame, 16 bytes
TIMESPEC3 = struct.Struct("@qqqqqq")          # struct scm_timestamping: 3 x struct timespec (64-bit)
ANC_SPACE = socket.CMSG_SPACE(TIMESPEC3.size) if hasattr(socket, "CMSG_SPACE") else 64


class HwMessage(can.Message):
    """A received frame with both stamps (module doc). ``can.Message`` has ``__slots__``; this adds a dict."""

    def __init__(self, *, host_timestamp=None, hw_timestamp=None, timestamp_source="host", **kwargs):
        super().__init__(**kwargs)
        self.host_timestamp = host_timestamp
        self.hw_timestamp = hw_timestamp
        self.timestamp_source = timestamp_source


def decode_frame(data: bytes, timestamp: float = 0.0, channel=None) -> HwMessage:
    """A message from a ``struct can_frame``, in python-can socketcan's layout."""
    can_id, dlc, payload = CAN_FRAME.unpack_from(data)
    err = bool(can_id & CAN_ERR_FLAG)
    ext = bool(can_id & CAN_EFF_FLAG)
    rtr = bool(can_id & CAN_RTR_FLAG)
    if err:
        aid = can_id & CAN_ERR_MASK
    else:
        aid = can_id & (CAN_EFF_MASK if ext else CAN_SFF_MASK)
    dlc = min(dlc, 8)
    return HwMessage(timestamp=timestamp, arbitration_id=aid, is_extended_id=ext and not err,
                       is_remote_frame=rtr and not err, is_error_frame=err, dlc=dlc,
                       data=b"" if rtr else payload[:dlc], channel=channel)


def encode_frame(msg: can.Message) -> bytes:
    """A ``struct can_frame`` for a python-can data or remote frame."""
    can_id = msg.arbitration_id
    if msg.is_extended_id:
        can_id |= CAN_EFF_FLAG
    if msg.is_remote_frame:
        can_id |= CAN_RTR_FLAG
    data = bytes(msg.data)
    return CAN_FRAME.pack(can_id, msg.dlc if msg.is_remote_frame else len(data), data.ljust(8, b"\0"))


def decode_stamps(ancdata) -> tuple[float | None, float | None]:
    """(host, hardware) seconds from ``recvmsg`` ancillary data; a zero timespec is None."""
    for level, kind, data in ancdata:
        if level == socket.SOL_SOCKET and kind == SCM_TIMESTAMPING and len(data) >= TIMESPEC3.size:
            s0, n0, _s1, _n1, s2, n2 = TIMESPEC3.unpack_from(data)
            host = s0 + n0 / 1e9 if (s0 or n0) else None
            hw = s2 + n2 / 1e9 if (s2 or n2) else None
            return host, hw
    return None, None


class StampCheck:
    """The per-open hardware stamp check (module doc)."""

    def __init__(self, frames: int = 20, host_gap_s: float = 2.0):
        self.frames = frames
        self.host_gap_s = host_gap_s
        self.state = "pending"
        self.reason = ""
        self.judged = 0
        self.error_frames = 0
        self._last_hw = None

    def feed(self, hw: float | None, host: float | None, is_error: bool = False) -> str:
        """Judge one frame while pending; returns the state."""
        if self.state != "pending":
            return self.state
        if is_error:
            self.error_frames += 1
            return self.state
        n = self.judged + 1
        if hw is None:
            return self._fail(f"data frame {n} after the open has no hardware stamp (0)")
        if host is None:
            return self._fail(f"data frame {n} after the open has no host stamp to compare with")
        if abs(hw - host) <= self.host_gap_s:
            return self._fail(f"data frame {n}: hardware stamp {hw:.6f} is within {self.host_gap_s:g} s of "
                              f"host time {host:.6f}, so it is host time, not the adapter's clock")
        if self._last_hw is not None and hw <= self._last_hw:
            return self._fail(f"data frame {n}: hardware stamp {hw:.6f} does not advance "
                              f"(previous {self._last_hw:.6f})")
        self._last_hw = hw
        self.judged = n
        if n >= self.frames:
            self.state = "hardware"
            self.reason = (f"{n} data frames: hardware stamps nonzero, advancing, on the adapter's clock "
                           f"(hardware - host {hw - host:+.3f} s at the last)")
        return self.state

    def _fail(self, why: str) -> str:
        self.state = "host_only"
        self.reason = why
        return self.state

    def result(self) -> dict:
        return {"state": self.state, "reason": self.reason, "data_frames_judged": self.judged,
                "error_frames_seen": self.error_frames, "frames_needed": self.frames,
                "host_gap_s": self.host_gap_s}


class SocketcanHwBus(can.BusABC):
    """A SocketCAN channel read with hardware and host timestamps (module doc)."""

    def __init__(self, channel: str, *, check_frames: int = 20, host_gap_s: float = 2.0,
                 receive_own_messages: bool = False, can_filters=None, _sock=None, **kwargs):
        self.channel = channel
        self.channel_info = f"socketcan_hw channel '{channel}'"
        self.check = StampCheck(check_frames, host_gap_s)
        self._after = {"frames": 0, "hw_zero": 0, "hw_backward": 0, "error_frames": 0}
        self._last_hw = None
        if _sock is not None:              # tests
            self._sock = _sock
        else:
            s = socket.socket(socket.AF_CAN, socket.SOCK_RAW, socket.CAN_RAW)
            try:
                s.setsockopt(SOL_CAN_RAW, CAN_RAW_ERR_FILTER, struct.pack("=I", CAN_ERR_MASK))
                if receive_own_messages:
                    s.setsockopt(SOL_CAN_RAW, socket.CAN_RAW_RECV_OWN_MSGS, 1)
                s.setsockopt(socket.SOL_SOCKET, SO_TIMESTAMPING, TIMESTAMPING_FLAGS)
                s.bind((channel,))
            except OSError:
                s.close()
                raise
            self._sock = s
        super().__init__(channel=channel, can_filters=can_filters, **kwargs)

    @property
    def stamp_check(self) -> dict:
        """The open's stamp check: state ``pending`` / ``hardware`` / ``host_only`` and why."""
        return self.check.result()

    def stamp_stats(self) -> dict:
        """The check's result plus what the stamps did after it was decided."""
        return dict(self.check.result(), after_check=dict(self._after))

    def _recv_internal(self, timeout):
        if timeout is not None:
            r, _, _ = select.select([self._sock], [], [], max(0.0, timeout))
            if not r:
                return None, False
        data, anc, _flags, _addr = self._sock.recvmsg(CAN_FRAME.size, ANC_SPACE)
        if len(data) < CAN_FRAME.size:
            return None, False
        host, hw = decode_stamps(anc)
        if host is None:
            host = time.time()
        msg = decode_frame(data, timestamp=host, channel=self.channel)
        was = self.check.state
        state = self.check.feed(hw, host, msg.is_error_frame)
        if state == "host_only":
            hw = None
        elif was != "pending":
            self._watch(hw, msg.is_error_frame)
        msg.host_timestamp = host
        msg.hw_timestamp = hw
        msg.timestamp_source = "host"
        return msg, False

    def _watch(self, hw, is_error):
        a = self._after
        a["frames"] += 1
        if is_error:
            a["error_frames"] += 1
        if hw is None:
            a["hw_zero"] += 1
            return
        if self._last_hw is not None and hw <= self._last_hw:
            a["hw_backward"] += 1
        self._last_hw = hw

    def send(self, msg: can.Message, timeout=None):
        if timeout is not None:
            _, w, _ = select.select([], [self._sock], [], max(0.0, timeout))
            if not w:
                raise can.CanOperationError("socketcan_hw: send timed out")
        try:
            self._sock.send(encode_frame(msg))
        except OSError as e:
            raise can.CanOperationError(f"socketcan_hw send on {self.channel}: {e}") from e

    def fileno(self):
        return self._sock.fileno()

    def shutdown(self):
        super().shutdown()
        self._sock.close()
