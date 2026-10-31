"""SocketCAN detection picks adapters by driver, never by canN name.

canN follows plug order, so ``detect_all_can_interfaces`` reads each CAN
interface's driver from sysfs: ``--brands ixxat`` selects the ix_usb_can
interface, ``innomaker`` / ``gs_usb`` the gs_usb one, ``socketcan`` all of
them; an interface that is down is skipped. Uses a fake sysfs tree and a
patched python-can Bus, so no hardware is opened.

Run standalone:   python tests/test_socketcan_detect.py
Or under pytest:  python -m pytest tests/test_socketcan_detect.py
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import can
from canbench.live import receiver


def _sysfs(ifaces):
    """ifaces: [(name, type, driver or None, flags)] -> fake /sys/class/net root."""
    root = Path(tempfile.mkdtemp(prefix="canbench-sysfs-"))
    drivers = root / "_drivers"
    for name, typ, driver, flags in ifaces:
        d = root / "net" / name
        d.mkdir(parents=True)
        (d / "type").write_text(f"{typ}\n")
        (d / "flags").write_text(f"{flags}\n")
        if driver:
            (drivers / driver).mkdir(parents=True, exist_ok=True)
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


def _detect(brands, rig=RIG):
    real = can.interface.Bus
    can.interface.Bus = lambda **kw: _FakeBus(**kw)
    try:
        return [(i, ch) for i, ch, _ in
                receiver.detect_all_can_interfaces(500000, brands=brands, _sysfs=_sysfs(rig))
                if i == "socketcan"]
    finally:
        can.interface.Bus = real


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
