"""canbench.bench_roles: a bench adapter by its canctl role (resolve, open_role, refusals, tagging).

Synthetic: ``canctl roles`` text is passed in, python-can's Bus is replaced by a recorder, and a
fake canctl script stands in for the real one. No adapter is opened. Run standalone:
    python tests/test_bench_roles.py
"""
import os
import stat
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import can

from canbench import bench_roles as B
from canbench import replay
from canbench.live.receiver import Adapter

LINES = {
    "canmain": "role=canmain kind=gs_usb mode=socketcan attached=1 usb=1-3.2.3 busnum=1 devnum=41 driver=gs_usb "
               "netdev=canmain link=up tty=none chardev=none devid=none",
    "canchg": "role=canchg kind=gs_usb mode=libusb attached=1 usb=1-3.3 busnum=1 devnum=40 driver=none "
              "netdev=none link=none tty=none chardev=none devid=none",
    "canx2": "role=canx2 kind=slcan mode=- attached=1 usb=1-3.4 busnum=1 devnum=10 driver=cdc_acm "
             "netdev=none link=none tty=/dev/ttyACM1 chardev=none devid=none",
    "kvaser": "role=kvaser kind=kvaser mode=socketcan attached=1 usb=1-3.2.4 busnum=1 devnum=17 driver=kvaser_usb "
              "netdev=kvaser link=down tty=none chardev=none devid=none",
    "pcanfd": "role=pcanfd kind=pcan mode=- attached=1 usb=1-3.2.2 busnum=1 devnum=11 driver=pcan "
              "netdev=none link=none tty=none chardev=pcanusbfd32 devid=3",
}


def text(**swap):
    """The bench's roles text, with lines replaced by keyword (role=line)."""
    d = dict(LINES)
    d.update(swap)
    return "\n".join(d.values()) + "\n"


def refused(fn, *words):
    try:
        fn()
    except B.RoleError as e:
        msg = str(e)
        for w in words:
            assert w in msg, (w, msg)
        return msg
    raise AssertionError("not refused")


class Rec:
    """Stands in for can.Bus: records what it was asked to open."""
    calls = []
    shut = []

    def __init__(self, **kw):
        Rec.calls.append(kw)
        self.gs_usb = None

    def shutdown(self):
        Rec.shut.append(self)


def with_fake_bus(fn):
    real = can.Bus
    can.Bus = Rec
    Rec.calls = []
    Rec.shut = []
    try:
        return fn()
    finally:
        can.Bus = real


def test_parse_roles_fields_and_nones():
    t = B.parse_roles(text())
    assert list(t) == ["canmain", "canchg", "canx2", "kvaser", "pcanfd"]
    assert t["canchg"].busnum == 1 and t["canchg"].devnum == 40 and t["canchg"].netdev is None
    assert t["canx2"].mode is None and t["canx2"].tty == "/dev/ttyACM1"
    assert t["pcanfd"].devid == 3 and t["pcanfd"].chardev == "pcanusbfd32"


def test_parse_roles_refuses_an_old_format():
    refused(lambda: B.parse_roles("role=canmain kind=gs_usb mode=socketcan attached=1\n"), "lacks", "too old")


def test_resolve_each_kind_and_mode():
    r = B.resolve("canmain", text=text())
    assert (r.mode, r.interface, r.kwargs, r.needs_link_up, r.netdev) == ("socketcan", "socketcan",
                                                                           {"channel": "canmain"}, True, "canmain")
    r = B.resolve("canchg", text=text())
    assert (r.mode, r.interface, r.kwargs["bus"], r.kwargs["address"], r.busnum, r.devnum) == \
        ("libusb", "gs_usb", 1, 40, 1, 40)
    r = B.resolve("canx2", text=text())
    assert (r.mode, r.interface, r.kwargs) == ("tty", "slcan", {"channel": "/dev/ttyACM1"})
    r = B.resolve("canx2", text=text(canx2=LINES["canx2"].replace("netdev=none link=none", "netdev=canx2 link=up")))
    assert (r.mode, r.interface, r.kwargs) == ("socketcan", "socketcan", {"channel": "canx2"})
    r = B.resolve("kvaser", text=text())
    assert (r.mode, r.interface, r.kwargs) == ("socketcan", "socketcan", {"channel": "kvaser"})
    r = B.resolve("pcanfd", text=text())
    assert (r.mode, r.interface, r.kwargs) == ("pcan", "pcan", {"device_id": 3})


def test_resolve_kvaser_on_leaf_uses_the_one_canlib_channel():
    leaf = LINES["kvaser"].replace("mode=socketcan", "mode=linuxcan").replace("driver=kvaser_usb", "driver=leaf") \
        .replace("netdev=kvaser link=down", "netdev=none link=none")
    r = B.resolve("kvaser", text=text(kvaser=leaf), _kvaser_channels=lambda: [(2, "Kvaser Leaf Light v2")])
    assert (r.mode, r.interface, r.kwargs) == ("linuxcan", "kvaser", {"channel": 2})
    refused(lambda: B.resolve("kvaser", text=text(kvaser=leaf), _kvaser_channels=lambda: []), "0 non-virtual")
    refused(lambda: B.resolve("kvaser", text=text(kvaser=leaf),
                              _kvaser_channels=lambda: [(0, "Leaf A"), (1, "Leaf B")]), "2 non-virtual")
    wrong = leaf.replace("driver=leaf", "driver=kvaser_usb")
    refused(lambda: B.resolve("kvaser", text=text(kvaser=wrong), _kvaser_channels=lambda: [(0, "L")]),
            "linuxcan mode but its driver is kvaser_usb")


def test_resolve_refusals():
    refused(lambda: B.resolve("can0", text=text()), "not a canctl role", "canmain")
    refused(lambda: B.resolve("canchg", text=text(canchg=LINES["canchg"].replace("attached=1", "attached=0"))),
            "not attached")
    refused(lambda: B.resolve("kvaser", text=text(kvaser=LINES["kvaser"].replace("attached=1", "attached=2"))),
            "2 USB devices match kvaser", "will not pick")
    refused(lambda: B.resolve("canchg", text=text(canchg=LINES["canchg"].replace("driver=none", "driver=gs_usb"))),
            "still bound")
    refused(lambda: B.resolve("canmain", text=text(canmain=LINES["canmain"].replace("netdev=canmain link=up",
                                                                                   "netdev=none link=none"))),
            "no netdev")
    refused(lambda: B.resolve("pcanfd", text=text(pcanfd=LINES["pcanfd"].replace("devid=3", "devid=none"))),
            "no PCAN chardev")
    refused(lambda: B.resolve("canx2", text=text(canx2=LINES["canx2"].replace("tty=/dev/ttyACM1", "tty=none"))),
            "no tty")


def test_open_role_after_canctl_up():
    with_fake_bus(lambda: B.open_role("canmain", text=text()))
    assert Rec.calls == [{"interface": "socketcan", "channel": "canmain"}]
    refused(lambda: B.open_role("kvaser", text=text()), "netdev kvaser is down", "canctl up kvaser")


def test_open_role_modes_and_bitrate():
    refused(lambda: B.open_role("canchg", modes=("socketcan",), text=text()), "mode libusb", "takes socketcan")
    refused(lambda: B.open_role("canchg", text=text()), "give bitrate=")
    with_fake_bus(lambda: B.open_role("canchg", bitrate=500000, modes=("socketcan", "libusb"), text=text()))
    assert Rec.calls == [{"interface": "gs_usb", "bitrate": 500000, "channel": "gs_usb:canchg", "bus": 1,
                          "address": 40}]
    with_fake_bus(lambda: B.open_role("pcanfd", bitrate=500000, text=text()))
    assert Rec.calls == [{"interface": "pcan", "bitrate": 500000, "device_id": 3}]


def test_open_role_applies_linux_fixes_and_releases_a_libusb_role():
    from canbench.live import linux_fixes, receiver
    applied, released = [], []
    real_apply, real_release = linux_fixes.apply, receiver.release_gs_usb
    linux_fixes.apply = lambda: applied.append(1)
    receiver.release_gs_usb = lambda bus: released.append(bus)
    try:
        bus = with_fake_bus(lambda: B.open_role("canchg", bitrate=500000, text=text()))
        assert applied == [1] and released == []
        bus.shutdown()
        assert Rec.shut == [bus] and released == [bus]
        sock = with_fake_bus(lambda: B.open_role("canmain", text=text()))
        sock.shutdown()
        assert released == [bus], "only a libusb gs_usb role is released"
    finally:
        linux_fixes.apply, receiver.release_gs_usb = real_apply, real_release


def test_hw_timestamps_only_on_a_socketcan_netdev():
    refused(lambda: B.open_role("canchg", bitrate=500000, hw_timestamps=True, text=text()),
            "hw_timestamps needs a SocketCAN netdev")
    import canbench.live.socketcan_hw as H
    real = H.SocketcanHwBus
    got = []
    H.SocketcanHwBus = lambda channel, **kw: got.append((channel, kw)) or object()
    try:
        up = text(kvaser=LINES["kvaser"].replace("link=down", "link=up"))
        B.open_role("kvaser", hw_timestamps=True, check_frames=10, text=up)
    finally:
        H.SocketcanHwBus = real
    assert got == [("kvaser", {"check_frames": 10})]


def test_tag_adapters_by_what_canctl_reports():
    t = text(canx2=LINES["canx2"])
    adapters = [
        Adapter("socketcan", "canmain", "socketcan canmain", ("socketcan:canmain",)),
        Adapter("socketcan", "can7", "socketcan can7", ("socketcan:can7",)),
        Adapter("gs_usb", {"channel": "gs_usb:x", "bus": 1, "address": 40}, "gs_usb", ("gs_usb:0",)),
        Adapter("gs_usb", {"channel": "gs_usb:y", "bus": 1, "address": 99}, "gs_usb", ("gs_usb:1",)),
        Adapter("pcan", "PCAN_USBBUS1", "pcan", ("pcan-id:3", "pcan:PCAN_USBBUS1")),
        Adapter("pcan", "PCAN_USBBUS2", "pcan", ("pcan-id:0", "pcan:PCAN_USBBUS2")),
    ]
    B.tag_adapters(adapters, text=t)
    roles = [[i for i in a.ids if i.startswith("role:")] for a in adapters]
    assert roles == [["role:canmain"], [], ["role:canchg"], [], ["role:pcanfd"], []], roles


def test_role_mismatch_reasons():
    assert B.role_mismatch("serial:X", text=text()) is None
    assert "not attached" in B.role_mismatch("role:canchg", text=text(canchg=LINES["canchg"].replace(
        "attached=1", "attached=0")))
    assert "is down" in B.role_mismatch("role:kvaser", text=text())


def test_replay_pins_a_bus_by_role():
    a = [Adapter("socketcan", "canmain", "socketcan canmain", ("socketcan:canmain", "role:canmain")),
         Adapter("pcan", "PCAN_USBBUS1", "pcan", ("pcan-id:3", "role:pcanfd"))]
    assert replay.assign_adapters(["b_can", "d_can"], a, ["b_can=role:pcanfd", "d_can=role:canmain"]) == \
        {"b_can": 1, "d_can": 0}


def test_canctl_missing_or_too_old():
    refused(lambda: B.canctl_roles_text("/nonexistent/canctl"), "not found")
    d = tempfile.mkdtemp()
    old = os.path.join(d, "canctl")
    with open(old, "w") as f:
        f.write("#!/bin/sh\necho 'canctl: usage: canctl up|down|mode|gs_usb|slcan|usbreset|status' >&2\nexit 2\n")
    os.chmod(old, os.stat(old).st_mode | stat.S_IXUSR)
    refused(lambda: B.canctl_roles_text(old), "exited 2", "no roles command")
    new = os.path.join(d, "canctl2")
    with open(new, "w") as f:
        f.write("#!/bin/sh\ncat <<'X'\n" + text() + "X\n")
    os.chmod(new, os.stat(new).st_mode | stat.S_IXUSR)
    assert "canmain" in B.parse_roles(B.canctl_roles_text(new))


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
