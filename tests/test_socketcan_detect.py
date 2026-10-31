"""SocketCAN detection picks adapters by driver, never by canN name.

canN follows plug order, so ``detect_all_can_interfaces`` reads each CAN
interface's driver from sysfs: ``--brands ixxat`` selects the ix_usb_can
interface, ``innomaker`` / ``gs_usb`` the gs_usb one, ``socketcan`` all of
them; an interface that is down is skipped. Each adapter carries its USB
serial and port as ids, and the replayer's ``assign_adapters`` pins buses by
them and refuses to guess between two or more adapters. Uses a fake sysfs
tree and a patched python-can Bus, so no hardware is opened.

Run standalone:   python tests/test_socketcan_detect.py
Or under pytest:  python -m pytest tests/test_socketcan_detect.py
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import can
from canbench import replay
from canbench.live import receiver


def _sysfs(ifaces):
    """ifaces: [(name, type, driver or None, flags[, usb port, serial])] -> fake /sys/class/net.

    With a USB port, ``device`` links to the USB interface directory under the
    port's device directory, which holds ``busnum`` and (if given) ``serial``,
    as in real sysfs.
    """
    root = Path(tempfile.mkdtemp(prefix="canbench-sysfs-"))
    drivers = root / "_drivers"
    for name, typ, driver, flags, *usb in ifaces:
        d = root / "net" / name
        d.mkdir(parents=True)
        (d / "type").write_text(f"{typ}\n")
        (d / "flags").write_text(f"{flags}\n")
        if driver:
            (drivers / driver).mkdir(parents=True, exist_ok=True)
            if usb:
                port, serial = usb
                dev = root / "usb" / port
                (dev / f"{port}:1.0").mkdir(parents=True)
                (dev / "busnum").write_text("1\n")
                if serial:
                    (dev / "serial").write_text(f"{serial}\n")
                os.symlink(dev / f"{port}:1.0", d / "device")
            else:
                (d / "device").mkdir()
            os.symlink(drivers / driver, d / "device" / "driver")
    return str(root / "net")


# Plug order reversed on purpose: the Innomaker is can0, the IXXAT can1.
RIG = [("can0", 280, "gs_usb", "0x40081"), ("can1", 280, "ix_usb_can", "0x40081"),
       ("vcan0", 280, None, "0x40081"), ("eno1", 1, "e1000e", "0x1003")]


class _FakeBus:
    def __init__(self, **kw):
        pass

    def shutdown(self):
        pass


def _adapters(brands, rig=RIG):
    real = can.interface.Bus
    can.interface.Bus = lambda **kw: _FakeBus(**kw)
    try:
        return [a for a in receiver.detect_all_can_interfaces(500000, brands=brands, _sysfs=_sysfs(rig))
                if a.interface == "socketcan"]
    finally:
        can.interface.Bus = real


def _detect(brands, rig=RIG):
    return [(i, ch) for i, ch, _ in _adapters(brands, rig)]


def test_interfaces_listed_with_driver():
    got = receiver.socketcan_interfaces(_sysfs(RIG))
    assert got == [("can0", "gs_usb", True), ("can1", "ix_usb_can", True), ("vcan0", None, True)]


def test_brand_selects_by_driver_not_name():
    assert _detect({"ixxat"}) == [("socketcan", "can1")]
    assert _detect({"innomaker"}) == [("socketcan", "can0")]
    assert _detect({"gs_usb"}) == [("socketcan", "can0")]
    assert _detect({"socketcan"}) == [("socketcan", "can0"), ("socketcan", "can1"), ("socketcan", "vcan0")]


def test_down_interface_skipped():
    rig = [("can0", 280, "gs_usb", "0x40080"), ("can1", 280, "ix_usb_can", "0x40081")]
    assert _detect({"socketcan"}, rig) == [("socketcan", "can1")]
    assert _detect({"innomaker"}, rig) == []


# Two Innomakers (same driver) and an IXXAT, with USB ports and serials.
TWO_INNOMAKERS = [("can0", 280, "gs_usb", "0x40081", "1-10.2", "0011223344556677AABBCCDD"),
                  ("can1", 280, "ix_usb_can", "0x40081", "1-10.1", "HW123456"),
                  ("can2", 280, "gs_usb", "0x40081", "1-10.3", None)]


def test_adapter_ids_from_sysfs():
    got = {a.channel: a.ids for a in _adapters({"socketcan"}, TWO_INNOMAKERS)}
    assert got == {
        "can0": ("serial:0011223344556677AABBCCDD", "usb:1-10.2", "socketcan:can0"),
        "can1": ("serial:HW123456", "usb:1-10.1", "socketcan:can1"),
        "can2": ("usb:1-10.3", "socketcan:can2"),       # no serial string: port only
    }
    a = _adapters({"ixxat"}, TWO_INNOMAKERS)[0]
    assert a.desc == "socketcan can1 (ix_usb_can) [serial:HW123456, usb:1-10.1]"
    iface, ch, desc = a                                  # still unpacks as before
    assert (iface, ch) == ("socketcan", "can1")
    assert receiver.adapter_matches(a, "HW123456")       # a bare serial
    assert receiver.adapter_matches(a, "USB:1-10.1")
    assert not receiver.adapter_matches(a, "1-10.1")


def _refused(fn):
    try:
        fn()
    except replay.ReplayInputError as e:
        return str(e)
    raise AssertionError("not refused")


def test_assign_refuses_to_guess_between_adapters():
    inno = _adapters({"innomaker"}, TWO_INNOMAKERS)
    assert len(inno) == 2
    # one bus, two Innomakers: the old code sent it on the first; now refused
    msg = _refused(lambda: replay.assign_adapters(["d_can"], inno))
    assert "2 adapters detected" in msg and "usb:1-10.3" in msg
    # as many buses as adapters: the old code paired them by position; refused too
    _refused(lambda: replay.assign_adapters(["b_can", "d_can"], inno))
    # pinned by serial and by port
    assert replay.assign_adapters(["b_can", "d_can"], inno,
                                  ["d_can=0011223344556677AABBCCDD", "B_CAN=usb:1-10.3"]) == \
        {"d_can": 0, "b_can": 1}
    # every bus must be pinned once any is
    assert "not pinned: b_can" in _refused(
        lambda: replay.assign_adapters(["b_can", "d_can"], inno, ["d_can=usb:1-10.2"]))
    # an id that names no adapter, or names two
    assert "no adapter has" in _refused(lambda: replay.assign_adapters(["d_can"], inno, ["d_can=serial:X"]))
    assert "2 adapters have" in _refused(lambda: replay.assign_adapters(
        ["d_can"], inno, ["d_can=gs_usb"], matches=lambda a, k: True))
    assert "no replayed bus" in _refused(lambda: replay.assign_adapters(["d_can"], inno, ["c_can=usb:1-10.2"]))
    # one adapter: every bus goes out on it, as before
    ix = _adapters({"ixxat"}, TWO_INNOMAKERS)
    assert replay.assign_adapters([0, 1], ix) == {0: 0, 1: 0}
    assert replay.assign_adapters([1], ix, ["1=HW123456"]) == {1: 0}


if __name__ == "__main__":
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except Exception as e:
                failed += 1
                print(f"FAIL {name}: {type(e).__name__}: {e}")
    sys.exit(1 if failed else 0)
