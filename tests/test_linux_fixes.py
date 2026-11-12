"""canbench.live.linux_fixes: the two Linux adapter workarounds, with fakes (no hardware, no CANlib needed).

1. Kvaser: a canIoCtl(canIOCTL_SET_LOCAL_TXACK) call made with a 1-byte buffer reaches CANlib as a 4-byte
   unsigned int holding the same value; every other call passes through unchanged.
2. gs_usb: GsUsb.start() issues MODE RESET then MODE START (flags masked to the device's features and to
   the modes the driver supports) and never calls the USB reset() that makes an Innomaker re-enumerate.

Run standalone:   python tests/test_linux_fixes.py
Or under pytest:  python -m pytest tests/test_linux_fixes.py
"""
import ctypes
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from canbench.live import linux_fixes  # noqa: E402

TXACK, OTHER = 46, 11


def test_kvaser_txack_passed_as_uint32():
    seen = []

    def fake(handle, func, buf, buflen):
        if buflen == 4:
            seen.append((handle, func, ctypes.cast(buf, ctypes.POINTER(ctypes.c_uint32)).contents.value, buflen))
        else:
            seen.append((handle, func, ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte)).contents.value, buflen))
        return 0
    w = linux_fixes._kvaser_wrap(fake, TXACK)
    w(7, TXACK, ctypes.byref(ctypes.c_byte(1)), 1)
    w(7, TXACK, ctypes.byref(ctypes.c_byte(0)), 1)
    w(7, OTHER, ctypes.byref(ctypes.c_byte(1)), 1)
    w(7, TXACK, ctypes.byref(ctypes.c_uint32(1)), 4)
    assert seen == [(7, TXACK, 1, 4), (7, TXACK, 0, 4), (7, OTHER, 1, 1), (7, TXACK, 1, 4)], seen


class _Cap:
    feature = 0xF3            # what the Innomaker USB2CAN V3.4.3 reports: no ONE_SHOT


class _Usb:
    def __init__(self, kernel_bound):
        self.calls, self.kernel_bound = [], kernel_bound

    def reset(self):
        self.calls.append(("reset",))

    def is_kernel_driver_active(self, intf):
        return self.kernel_bound

    def detach_kernel_driver(self, intf):
        self.calls.append(("detach", intf))

    def ctrl_transfer(self, req_type, req, value, index, data):
        self.calls.append(("ctrl", req_type, req, bytes(data)))


class _Dev:
    def __init__(self, kernel_bound=False):
        self.gs_usb = _Usb(kernel_bound)
        self.device_capability = _Cap()
        self.device_flags = None


def _modes(calls):
    return [struct.unpack("<II", c[3]) for c in calls if c[0] == "ctrl"]


def test_gs_usb_start_without_reset():
    from gs_usb import gs_usb as g
    d = _Dev(kernel_bound=True)
    flags = g.GS_CAN_MODE_LISTEN_ONLY | g.GS_CAN_MODE_HW_TIMESTAMP | g.GS_CAN_MODE_ONE_SHOT
    linux_fixes._start_no_reset(d, flags)
    assert ("reset",) not in d.gs_usb.calls, d.gs_usb.calls
    assert d.gs_usb.calls[0] == ("detach", 0), d.gs_usb.calls
    assert _modes(d.gs_usb.calls) == [(g.GS_CAN_MODE_RESET, 0),
                                      (g.GS_CAN_MODE_START, g.GS_CAN_MODE_LISTEN_ONLY | g.GS_CAN_MODE_HW_TIMESTAMP)]
    assert d.device_flags == g.GS_CAN_MODE_LISTEN_ONLY | g.GS_CAN_MODE_HW_TIMESTAMP


def test_gs_usb_start_default_flags_and_no_detach_when_unbound():
    from gs_usb import gs_usb as g
    d = _Dev(kernel_bound=False)
    linux_fixes._start_no_reset(d)
    assert [c[0] for c in d.gs_usb.calls] == ["ctrl", "ctrl"], d.gs_usb.calls
    assert _modes(d.gs_usb.calls)[1] == (g.GS_CAN_MODE_START, g.GS_CAN_MODE_NORMAL | g.GS_CAN_MODE_HW_TIMESTAMP)


def test_apply_installs_once_on_linux():
    r1 = linux_fixes.apply()
    r2 = linux_fixes.apply()
    assert r1 == r2
    if sys.platform.startswith("linux"):
        from gs_usb import gs_usb as g
        assert getattr(g.GsUsb.start, "canbench_linux_fix", False)
        assert r1["gs_usb"] is True


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("ok  ", name)
            except Exception as e:
                fails += 1
                print("FAIL", name, repr(e))
    sys.exit(1 if fails else 0)
