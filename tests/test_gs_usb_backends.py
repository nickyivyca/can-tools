"""Innomakers on both Linux backends at once: libusb detection beside SocketCAN.

An Innomaker on Linux is either held by the kernel's gs_usb driver (a SocketCAN
interface) or left to libusb (python-can's ``gs_usb``). ``detect_all_can_interfaces``
lists every Innomaker no kernel driver holds through libusb, opened by USB bus and
address (the libusb scan also lists kernel-held units, and its order is not plug
order), and skips the kernel-held ones there. Every adapter with a serial carries a
backend-qualified id ``<interface>:serial:<s>``; the replayer's ``assign_adapters``
refuses a pin whose backend is not the one the adapter was found on, saying why.

Uses fake libusb scan results, an empty sysfs and a patched python-can Bus, so no
hardware is opened. Serials are made up.

Run standalone:   python tests/test_gs_usb_backends.py
Or under pytest:  python -m pytest tests/test_gs_usb_backends.py
"""
import sys
import tempfile
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import can
from canbench import replay
from canbench.live import receiver

SN_A = "AAAA0000111122223333AAAA"
SN_B = "BBBB4444555566667777BBBB"


class _Usb:
    def __init__(self, bus, address, bound):
        self.bus, self.address, self._bound = bus, address, bound

    def is_kernel_driver_active(self, intf):
        if self._bound is None:
            raise NotImplementedError("not on this platform")
        return self._bound


def _scan(*devs):
    return [types.SimpleNamespace(serial_number=sn, gs_usb=_Usb(b, a, k)) for sn, b, a, k in devs]


def _gs_adapters(devs):
    """detect_all_can_interfaces(brands={'gs_usb'}) over a fake libusb scan, no sysfs."""
    from gs_usb import gs_usb as g
    real_scan, real_ready = g.GsUsb.scan, receiver._ensure_gs_usb_libusb
    g.GsUsb.scan = classmethod(lambda cls: _scan(*devs))
    receiver._ensure_gs_usb_libusb = lambda: True
    try:
        empty = Path(tempfile.mkdtemp(prefix="canbench-sysfs-")) / "net"
        empty.mkdir()
        return receiver.detect_all_can_interfaces(500000, brands={"gs_usb"}, _sysfs=str(empty))
    finally:
        g.GsUsb.scan, receiver._ensure_gs_usb_libusb = real_scan, real_ready


def test_mixed_bench_lists_the_libusb_unit():
    # scan order: the libusb unit first, the kernel-held one second (as seen on a real bench)
    got = _gs_adapters([(SN_A, 1, 38, False), (SN_B, 1, 34, True)])
    assert len(got) == 1
    a = got[0]
    assert a.interface == "gs_usb"
    assert a.channel == {"channel": f"gs_usb:{SN_A}", "bus": 1, "address": 38}
    assert a.ids == (f"serial:{SN_A}", f"gs_usb:serial:{SN_A}", "gs_usb:0")


def test_kernel_held_unit_between_keeps_bus_address():
    # the kernel-held unit sits first: an index-based open of "the first libusb unit" would hit it
    got = _gs_adapters([(SN_B, 1, 34, True), (SN_A, 1, 38, False)])
    assert [(a.channel["bus"], a.channel["address"]) for a in got] == [(1, 38)]
    assert "index" not in got[0].channel


def test_both_on_libusb():
    got = _gs_adapters([(SN_A, 1, 38, False), (SN_B, 1, 34, False)])
    assert [a.channel["address"] for a in got] == [38, 34]


def test_platform_that_cannot_say_lists_everything():
    # Windows: is_kernel_driver_active is not implemented; nothing is a kernel driver there
    got = _gs_adapters([(SN_A, 1, 38, None), (SN_B, 1, 34, None)])
    assert len(got) == 2


def test_all_kernel_held_lists_none_through_libusb():
    assert _gs_adapters([(SN_A, 1, 38, True), (SN_B, 1, 34, True)]) == []


def _refused(fn):
    try:
        fn()
    except replay.ReplayInputError as e:
        return str(e)
    raise AssertionError("not refused")


def _sockcan(sn):
    ids = (f"serial:{sn}", f"socketcan:serial:{sn}", "usb:1-3.3", "socketcan:canchg")
    return receiver.Adapter("socketcan", "canchg", "socketcan canchg (gs_usb)", ids)


def test_backend_qualified_pins():
    lib = _gs_adapters([(SN_A, 1, 38, False)])[0]
    sock = _sockcan(SN_B)
    both = [lib, sock]
    # the backend-qualified id picks the backend; the bare serial still matches either
    assert replay.assign_adapters(["d_can", "b_can"], both,
                                  [f"d_can=gs_usb:serial:{SN_A}", f"b_can=socketcan:serial:{SN_B}"]) == \
        {"d_can": 0, "b_can": 1}
    assert replay.assign_adapters(["d_can", "b_can"], both, [f"d_can={SN_A}", f"b_can=serial:{SN_B}"]) == \
        {"d_can": 0, "b_can": 1}
    # asked for libusb, found on SocketCAN: refused with the reason
    msg = _refused(lambda: replay.assign_adapters(["b_can"], [sock], [f"b_can=gs_usb:serial:{SN_B}"]))
    assert f"adapter {SN_B.lower()} is on socketcan, not gs_usb" in msg
    assert "unbind the kernel gs_usb driver" in msg
    msg = _refused(lambda: replay.assign_adapters(["d_can"], [lib], [f"d_can=socketcan:serial:{SN_A}"]))
    assert "is on gs_usb, not socketcan" in msg and "bind the kernel gs_usb driver" in msg
    # an unknown serial is still "no adapter has"
    assert "no adapter has" in _refused(
        lambda: replay.assign_adapters(["d_can"], both, ["d_can=gs_usb:serial:NOPE"]))


def test_backend_mismatch_ignores_other_ids():
    sock = _sockcan(SN_B)
    assert receiver.backend_mismatch([sock], "usb:1-3.3") is None
    assert receiver.backend_mismatch([sock], f"serial:{SN_B}") is None
    assert receiver.backend_mismatch([sock], f"socketcan:serial:{SN_B}") is None
    assert receiver.backend_mismatch([("pcan", "PCAN_USBBUS1", "pcan")], "gs_usb:serial:X") is None


if __name__ == "__main__":
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except Exception as e:
                failed += 1
                print(f"FAIL {name}: {e!r}")
    sys.exit(1 if failed else 0)
