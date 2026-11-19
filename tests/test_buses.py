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
    # list_interfaces appends software entries after hardware. Detection stubbed: the real one opens every UP
    # SocketCAN netdev on the host (on the madhouse bench, the Kvaser's while its link is up; 2026-11-19).
    real = buses.detect_all_can_interfaces
    buses.detect_all_can_interfaces = lambda bitrate, brands=None: [("socketcan", "hw0", "stub")]
    try:
        full = buses.list_interfaces(500000, brands={"socketcan"})
    finally:
        buses.detect_all_can_interfaces = real
    assert full[0] == ("socketcan", "hw0", "stub") and full[-2:] == sw


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


def _ttl(bus):
    import socket
    return bus._multicast._socket.getsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL)


def test_udp_multicast_hop_limit_is_zero():
    # python-can's default is 1, which puts every frame on the LAN (simulators.md, 2026-09-01)
    bus = buses.open_bus("udp_multicast", "239.99.0.201", 500000)
    try:
        assert _ttl(bus) == 0
    finally:
        bus.shutdown()


def test_udp_multicast_nonzero_hop_limit_refused():
    try:
        buses.open_bus("udp_multicast", "239.99.0.202", 500000, hop_limit=1)
    except ValueError as e:
        assert "hop_limit" in str(e)
    else:
        raise AssertionError("hop_limit=1 was accepted")


def test_confine_to_host_refuses_without_socket():
    try:
        buses.confine_to_host(object())
    except RuntimeError as e:
        assert "refusing" in str(e)
    else:
        raise AssertionError("a bus with no reachable socket was accepted")


def test_udp_adapters_pin_by_group():
    from canbench import replay
    ads = buses.udp_adapters(["239.99.0.11", "239.99.0.12:47812"])
    assert [tuple(a)[:2] for a in ads] == [("udp_multicast", {"channel": "239.99.0.11", "port": 43113}),
                                           ("udp_multicast", {"channel": "239.99.0.12", "port": 47812})]
    assert ads[1].ids == ("udp:239.99.0.12:47812", "udp:239.99.0.12")
    got = replay.assign_adapters([1, 2], ads, ["1=udp:239.99.0.12", "2=udp:239.99.0.11"])
    assert got == {1: 1, 2: 0}, got


def test_replayer_and_reconnect_open_through_open_bus():
    # can_replayer's open_buses and the receiver's reconnect must confine a udp bus too
    import importlib.util
    root = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location("can_replayer_t", root / "tools" / "can_replayer.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    opened = mod.open_buses(buses.udp_adapters(["239.99.0.203:47903"]), 500000)
    try:
        assert _ttl(opened[0][0]) == 0
    finally:
        for b, _ in opened:
            b.shutdown()
    from canbench.live.receiver import CANBusLogger
    from queue import Queue
    first = buses.open_bus("udp_multicast", "239.99.0.204", 500000)
    blog = CANBusLogger(0, first, Queue(), "udp_multicast", "239.99.0.204", 500000)
    blog._reconnect(max_open_attempts=1)
    try:
        assert blog.bus is not first and _ttl(blog.bus) == 0
    finally:
        blog.bus.shutdown()


def test_open_bus_asks_python_can_for_hop_limit_zero():
    # the first guard on its own: the bus is opened with hop_limit=0, not fixed up afterwards
    seen = {}
    real = can.interface.Bus

    def spy(**kw):
        seen.update(kw)
        return real(**kw)
    buses.can.interface.Bus = spy
    try:
        bus = buses.open_bus("udp_multicast", "239.99.0.205", 500000)
        bus.shutdown()
    finally:
        buses.can.interface.Bus = real
    assert seen.get("hop_limit") == 0, seen


def test_confine_to_host_fixes_a_default_bus():
    # the second guard on its own: a bus python-can opened with its default (1) reads 0 after
    bus = can.interface.Bus(interface="udp_multicast", channel="239.99.0.206")
    try:
        assert _ttl(bus) == 1
        assert buses.confine_to_host(bus) == 0 and _ttl(bus) == 0
    finally:
        bus.shutdown()


def test_open_bus_confines_even_if_python_can_ignores_hop_limit():
    # open_bus must call confine_to_host: a backend that drops hop_limit still ends at 0
    real = can.interface.Bus

    def drops(**kw):
        kw.pop("hop_limit", None)
        return real(**kw)
    buses.can.interface.Bus = drops
    try:
        bus = buses.open_bus("udp_multicast", "239.99.0.207", 500000)
    finally:
        buses.can.interface.Bus = real
    try:
        assert _ttl(bus) == 0
    finally:
        bus.shutdown()


def test_confine_to_host_refuses_a_socket_that_stays_nonzero():
    import socket
    import types

    class Stuck:
        family = socket.AF_INET

        def setsockopt(self, *a):
            pass

        def getsockopt(self, *a):
            return 1
    bus = types.SimpleNamespace(_multicast=types.SimpleNamespace(_socket=Stuck()))
    try:
        buses.confine_to_host(bus)
    except RuntimeError as e:
        assert "reads 1" in str(e)
    else:
        raise AssertionError("a socket reading TTL 1 was accepted")


def test_udp_adapters_refuse_a_shared_port():
    for specs in (["239.99.0.11", "239.99.0.12"], ["239.99.0.11:47811", "239.99.0.12:47811"]):
        try:
            buses.udp_adapters(specs)
        except ValueError as e:
            assert "own port" in str(e)
        else:
            raise AssertionError(f"{specs} share a port and were accepted")


def test_udp_buses_on_their_own_ports_do_not_cross():
    # the reason for the rule above, checked live: one port per bus keeps the buses apart
    a, b = buses.udp_adapters(["239.99.0.211:47921", "239.99.0.212:47922"])
    ta = buses.open_bus(a.interface, a.channel, 0)
    rb = buses.open_bus(b.interface, b.channel, 0)
    time.sleep(0.2)
    try:
        ta.send(can.Message(arbitration_id=0x123, data=b"\x01", is_extended_id=False))
        assert rb.recv(timeout=0.5) is None
    finally:
        ta.shutdown(); rb.shutdown()


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
