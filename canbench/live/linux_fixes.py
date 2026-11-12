"""Workarounds for two python-can / gs_usb defects that stop real adapters opening on Linux.

Found on a Debian 13 bench (kernel 6.12, python-can 4.6.1, gs_usb 0.3, Kvaser linuxcan / CANlib 5.53),
2026-11-11. Both are no-ops on other platforms.

1. Kvaser on linuxcan. python-can's ``KvaserBus.__init__`` calls
   ``canIoCtl(canIOCTL_SET_LOCAL_TXACK)`` with a 1-byte buffer. linuxcan's CANlib checks that the buffer
   is exactly ``sizeof(uint32_t)`` (``canlib/VCanFunctions.c``, case ``canIOCTL_SET_LOCAL_TXACK``) and
   returns canERR_PARAM, so no Kvaser channel opens ("Function canIoCtl failed - Error in parameter").
   The fix passes that one ioctl the same value as a 4-byte unsigned int; every other call is untouched.

2. gs_usb (Innomaker / candleLight) over libusb. ``gs_usb.GsUsb.start()`` begins with a USB
   ``reset()``. On Linux the device re-enumerates on that reset (a disconnect and a new device number),
   so the handle the library holds is gone and the next transfer fails (``USBError(2, 'Entity not
   found')``). python-can's ``GsUsbBus`` calls ``start()``, so every libusb open on Linux hits it. The
   fix replaces ``start()`` with the same steps minus the reset: detach a bound kernel driver, stop the
   channel (MODE RESET), then MODE START with the requested flags masked to the device's features.

``apply()`` installs both (idempotent); ``canbench.live.receiver`` calls it on import, and a harness that
opens adapters through python-can without importing the receiver calls it itself.
"""
import ctypes
import logging
import sys

logger = logging.getLogger(__name__)

_kvaser_done = False
_gs_usb_done = False


def _linux():
    return sys.platform.startswith("linux")


def _kvaser_wrap(orig, txack_code):
    def wrapped(handle, func, buf, buflen):
        if func == txack_code and buflen == 1:
            val = ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte)).contents.value
            return orig(handle, func, ctypes.byref(ctypes.c_uint32(val)), 4)
        return orig(handle, func, buf, buflen)
    wrapped.canbench_linux_fix = True
    return wrapped


def apply_kvaser():
    """Pass canIOCTL_SET_LOCAL_TXACK a 4-byte buffer. True if installed (or already), False if not needed."""
    global _kvaser_done
    if _kvaser_done:
        return True
    if not _linux():
        return False
    try:
        from can.interfaces.kvaser import canlib as kv
        from can.interfaces.kvaser import constants as kc
    except Exception as e:      # no CANlib on this machine: python-can cannot load the backend at all
        logger.debug("kvaser fix not installed: %s", e)
        return False
    if getattr(kv.canIoCtlInit, "canbench_linux_fix", False):
        _kvaser_done = True
        return True
    kv.canIoCtlInit = _kvaser_wrap(kv.canIoCtlInit, kc.canIOCTL_SET_LOCAL_TXACK)
    _kvaser_done = True
    return True


def _start_no_reset(self, flags=None):
    """gs_usb.GsUsb.start() without the USB reset (see the module docstring)."""
    import platform
    import usb.core
    from gs_usb import gs_usb as g
    if flags is None:
        flags = g.GS_CAN_MODE_NORMAL | g.GS_CAN_MODE_HW_TIMESTAMP
    dev = self.gs_usb
    if "windows" not in platform.system().lower():
        try:
            if dev.is_kernel_driver_active(0):
                dev.detach_kernel_driver(0)
        except usb.core.USBError:
            pass
    dev.ctrl_transfer(0x41, g._GS_USB_BREQ_MODE, 0, 0, g.DeviceMode(g.GS_CAN_MODE_RESET, 0).pack())
    flags &= self.device_capability.feature
    flags &= (g.GS_CAN_MODE_LISTEN_ONLY | g.GS_CAN_MODE_LOOP_BACK | g.GS_CAN_MODE_ONE_SHOT
              | g.GS_CAN_MODE_HW_TIMESTAMP)
    self.device_flags = flags
    dev.ctrl_transfer(0x41, g._GS_USB_BREQ_MODE, 0, 0, g.DeviceMode(g.GS_CAN_MODE_START, flags).pack())


_start_no_reset.canbench_linux_fix = True


def apply_gs_usb():
    """Replace gs_usb.GsUsb.start with the no-reset start. True if installed (or already)."""
    global _gs_usb_done
    if _gs_usb_done:
        return True
    if not _linux():
        return False
    try:
        from gs_usb import gs_usb as g
    except Exception as e:      # gs_usb not installed: nothing to fix
        logger.debug("gs_usb fix not installed: %s", e)
        return False
    if not getattr(g.GsUsb.start, "canbench_linux_fix", False):
        g.GsUsb.start = _start_no_reset
    _gs_usb_done = True
    return True


def apply():
    """Install both fixes where they apply. Returns {'kvaser': bool, 'gs_usb': bool}."""
    return {"kvaser": apply_kvaser(), "gs_usb": apply_gs_usb()}
