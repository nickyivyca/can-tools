"""Tests for canbench.buses software-bus support (no hardware)."""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import can
from canbench import buses


def test_software_interfaces_listed():
    sw = buses.software_interfaces()
    ifaces = {i[0] for i in sw}
    assert ifaces == {"virtual", "udp_multicast"}
    # list_interfaces appends software entries after hardware
    full = buses.list_interfaces(500000, brands={"socketcan"})
    assert full[-2:] == sw


def test_open_virtual_roundtrip():
    tx = buses.open_bus("virtual", buses.VIRTUAL_CHANNEL, 500000)
    rx = buses.open_bus("virtual", buses.VIRTUAL_CHANNEL, 500000)
    try:
        tx.send(can.Message(arbitration_id=0x55, data=b"\x01", is_extended_id=False))
        m = rx.recv(timeout=1.0)
        assert m is not None and m.arbitration_id == 0x55
    finally:
        tx.shutdown(); rx.shutdown()


def test_open_udp_multicast_roundtrip():
    tx = buses.open_bus("udp_multicast", buses.UDP_MULTICAST_GROUP, 500000,
                        receive_own_messages=False)
    rx = buses.open_bus("udp_multicast", buses.UDP_MULTICAST_GROUP, 500000)
    time.sleep(0.2)
    try:
        tx.send(can.Message(arbitration_id=0x321, data=b"\xAB", is_extended_id=False))
        m = rx.recv(timeout=1.5)
        assert m is not None and m.arbitration_id == 0x321
    finally:
        tx.shutdown(); rx.shutdown()


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn(); print(f"PASS  {name}")
            except AssertionError as e:
                failures += 1; print(f"FAIL  {name}: {e}")
            except Exception as e:
                failures += 1; print(f"ERROR {name}: {e!r}")
    print(f"\n{'ALL PASS' if failures == 0 else str(failures) + ' FAILED'}")
    sys.exit(1 if failures else 0)
