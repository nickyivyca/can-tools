"""canbench.bench_layer and canbench.bench_scan: the bench-role layer, on a fake canctl and a fake ``ip``.

Synthetic: a stateful fake canctl (``roles``, ``up``, ``down``, ``mode``) and a fake ``ip -details -json link show``
share one JSON state file; python-can's Bus is replaced by a recorder. No adapter is opened and no root command
runs. Run standalone:
    python tests/test_bench_layer.py              # the checks
    python tests/test_bench_layer.py --can-fail   # the checks on the real modules (control), then on each mutant:
                                                  # every mutant must make a check fail
"""
import importlib.util
import json
import os
import stat
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import can

from canbench import bench_layer as L
from canbench import bench_roles
from canbench import bench_scan as S
from canbench import errors as E
from canbench.live import pcan_bus as P

PY = sys.executable

FAKE_CANCTL = r'''
import json, sys
P = %(state)r
st = json.load(open(P))
a = sys.argv[1:]
st["log"].append(" ".join(a))
def save():
    json.dump(st, open(P, "w"))
def out(rc, msg=""):
    save()
    if msg:
        print(msg)
    sys.exit(rc)
fail = st.get("fail", {}).get(a[0] if a else "")
if fail:
    out(fail, "canctl: injected failure")
R = st["roles"]
if a[0] == "roles":
    for name, r in R.items():
        print(" ".join("%%s=%%s" %% (k, r.get(k, "none")) for k in
              ("role", "kind", "mode", "attached", "usb", "busnum", "devnum", "driver", "netdev", "link", "tty",
               "chardev", "devid")))
    save(); sys.exit(0)
r = R.get(a[1]) if len(a) > 1 else None
if r is None:
    out(2, "canctl: unknown role")
if a[0] == "up":
    if r["netdev"] == "none":
        out(2, "canctl: no netdev")
    r["link"] = "up"; r["bitrate"] = int(a[2]); r["listen_only"] = False
    if "restart-ms" in a:
        r["restart_ms"] = int(a[a.index("restart-ms") + 1])
    lie = st.get("lie", {})
    for k, v in lie.items():
        r[k] = v
    out(0)
if a[0] == "down":
    if r["netdev"] == "none":
        out(2, "canctl: no netdev")
    r["link"] = "down" if not st.get("stuck_up") else "up"
    out(0)
if a[0] == "mode":
    m = a[2]
    if st.get("claimed") and m == "socketcan":
        out(3, "canctl: bind failed")
    r["mode"] = m
    if st.get("stuck_mode"):
        out(0)
    if r["kind"] == "gs_usb":
        if m == "libusb":
            r.update(driver="none", netdev="none", link="none")
        else:
            r.update(driver="gs_usb", netdev=a[1], link="down")
    elif r["kind"] == "kvaser":
        if m == "linuxcan":
            r.update(driver="leaf", netdev="none", link="none")
        else:
            r.update(driver="kvaser_usb", netdev="kvaser", link="down")
    out(0)
out(2, "canctl: usage")
'''

FAKE_IP = r'''
import json, sys
st = json.load(open(%(state)r))
dev = sys.argv[-1]
for r in st["roles"].values():
    if r["netdev"] == dev:
        info = {"state": "ERROR-ACTIVE", "restart_ms": r.get("restart_ms", 0),
                "bittiming": {"bitrate": r.get("bitrate", 500000)}}
        if r.get("listen_only"):
            info["ctrlmode"] = ["LISTEN-ONLY"]
        flags = ["NOARP", "ECHO"] + (["UP", "LOWER_UP"] if r["link"] == "up" else [])
        print(json.dumps([{"ifname": dev, "flags": flags, "linkinfo": {"info_kind": "can", "info_data": info}}]))
        sys.exit(0)
print('Device "%%s" does not exist.' %% dev, file=sys.stderr)
sys.exit(1)
'''


def base_roles():
    return {
        "canmain": dict(role="canmain", kind="gs_usb", mode="socketcan", attached=1, usb="1-3.2.3", busnum=1,
                        devnum=41, driver="gs_usb", netdev="canmain", link="down", bitrate=500000, restart_ms=0),
        "canchg": dict(role="canchg", kind="gs_usb", mode="socketcan", attached=1, usb="1-3.3", busnum=1, devnum=40,
                       driver="gs_usb", netdev="canchg", link="down", bitrate=500000, restart_ms=0),
        "canx2": dict(role="canx2", kind="slcan", mode="-", attached=1, usb="1-3.4", busnum=1, devnum=10,
                      driver="cdc_acm", netdev="none", link="none", tty="/dev/ttyACM1"),
        "kvaser": dict(role="kvaser", kind="kvaser", mode="socketcan", attached=1, usb="1-3.2.4", busnum=1,
                       devnum=17, driver="kvaser_usb", netdev="kvaser", link="down", bitrate=500000, restart_ms=100),
        "pcanfd": dict(role="pcanfd", kind="pcan", mode="-", attached=1, usb="1-3.2.2", busnum=1, devnum=11,
                       driver="pcan", netdev="none", link="none", chardev="pcanusbfd32", devid=3),
    }


class Bench:
    """A fake bench: state file, fake canctl and fake ip wired into the layer under test."""

    def __init__(self, **roles_changes):
        self.dir = tempfile.mkdtemp(prefix="bench_layer_test_")
        self.state = os.path.join(self.dir, "state.json")
        roles = base_roles()
        for k, v in roles_changes.items():
            roles[k].update(v)
        self.write({"roles": roles, "log": []})
        self.canctl = os.path.join(self.dir, "canctl")
        with open(self.canctl, "w") as f:
            f.write("#!" + PY + "\n" + FAKE_CANCTL % {"state": self.state})
        os.chmod(self.canctl, os.stat(self.canctl).st_mode | stat.S_IXUSR)
        ip = os.path.join(self.dir, "fake_ip.py")
        with open(ip, "w") as f:
            f.write(FAKE_IP % {"state": self.state})
        self.saved = (L.SUDO, L.CANCTL, L.IP_SHOW, L.sleep, L.monotonic, L.WAIT_S)
        L.SUDO, L.CANCTL, L.IP_SHOW = [], self.canctl, [PY, ip]
        self.t = [0.0]
        L.sleep = lambda s: self.t.__setitem__(0, self.t[0] + s)
        L.monotonic = lambda: self.t[0]
        L.WAIT_S = 2.0

    def read(self):
        return json.load(open(self.state))

    def write(self, st):
        json.dump(st, open(self.state, "w"))

    def set(self, **kw):
        st = self.read()
        st.update(kw)
        self.write(st)

    def role(self, name, **kw):
        st = self.read()
        st["roles"][name].update(kw)
        self.write(st)

    def log(self):
        return [c for c in self.read()["log"] if c != "roles"]

    def close(self):
        L.SUDO, L.CANCTL, L.IP_SHOW, L.sleep, L.monotonic, L.WAIT_S = self.saved


class Rec:
    calls = []
    fail = None
    rx = []                     # what recv() hands out, in order; empty: recv waits its timeout and returns None
    gone = False                # recv returns None at once (a device gone)
    reads = 0
    gen = 0                     # bench() bumps it: a bus from an earlier bench() (a drain thread a mutant leaked)
                                # raises on its next read, so it cannot take a later check's frames
    rx_delay = 0.0              # each frame handed out takes this long to read

    def __init__(self, **kw):
        if Rec.fail:
            raise Rec.fail
        Rec.calls.append(kw)
        self.shut = 0
        self.gen = Rec.gen

    def send(self, msg, timeout=None):
        pass

    def recv(self, timeout=None):
        if self.gen != Rec.gen:
            raise RuntimeError("a bus from an earlier check")
        Rec.reads += 1
        if Rec.rx:
            time.sleep(Rec.rx_delay)
            return Rec.rx.pop(0)
        if not Rec.gone:
            time.sleep(timeout or 0)
        return None

    def shutdown(self):
        self.shut += 1
        self.shut_saw_drain_running = getattr(self, "bench_drain", {}).get("running")


def bench(fn, guard=True, **roles_changes):
    """Run fn(b) on a fresh fake bench, with a recording Bus, a passing guard (unless guard=False) and
    SocketcanHwBus faked."""
    import canbench.live.socketcan_hw as H
    b = Bench(**roles_changes)
    real_bus, real_hw = can.Bus, H.SocketcanHwBus
    can.Bus = Rec
    Rec.gen += 1
    Rec.calls, Rec.fail, Rec.rx, Rec.gone, Rec.reads, Rec.rx_delay = [], None, [], False, 0, 0.0
    H.SocketcanHwBus = lambda channel, **kw: Rec(interface="socketcan_hw", channel=channel, **kw)
    old = L.set_guard((lambda: None) if guard else None)
    try:
        return fn(b)
    finally:
        L.set_guard(old)
        can.Bus, H.SocketcanHwBus = real_bus, real_hw
        b.close()


def refused(fn, *words, cls=None):
    cls = cls or L.BenchLayerError
    try:
        fn()
    except cls as e:
        for w in words:
            assert w in str(e), (w, str(e))
        return str(e)
    raise AssertionError("not refused")


# ------------------------------------------------------------------ the guard

def test_no_guard_touches_nothing():
    def body(b):
        for fn in (lambda: L.link_up("kvaser", 500000), lambda: L.link_down("kvaser"),
                   lambda: L.set_mode("canmain", "libusb"), lambda: L.open_bench("canchg", 500000),
                   lambda: L.canctl("down", "kvaser")):
            refused(fn, "no bench guard set", cls=L.NoGuard)
        assert b.log() == [], b.log()
    bench(body, guard=False)


def test_a_guard_that_refuses_stops_everything():
    class NotHeld(Exception):
        pass

    def no():
        raise NotHeld("bench held by someone else")

    def body(b):
        L.set_guard(no)
        for fn in (lambda: L.link_up("kvaser", 500000), lambda: L.open_bench("kvaser", 500000)):
            try:
                fn()
                raise AssertionError("ran")
            except NotHeld:
                pass
        assert b.log() == [] and Rec.calls == []
    bench(body)


# ------------------------------------------------------------------ links

def test_link_up_reads_back():
    def body(b):
        got = L.link_up("kvaser", 500000, restart_ms=100)
        assert got["up"] and got["bitrate"] == 500000 and not got["listen_only"], got
        assert b.log() == ["up kvaser 500000 restart-ms 100"], b.log()
        for lie, word in (({"bitrate": 250000}, "bitrate 250000"), ({"listen_only": True}, "listen-only on"),
                          ({"link": "down"}, "not UP"), ({"restart_ms": 0}, "restart-ms 0")):
            b.set(lie=lie)
            refused(lambda: L.link_up("kvaser", 500000, restart_ms=100), "exited 0 but", word)
        b.set(lie={}, fail={"up": 3})
        refused(lambda: L.link_up("kvaser", 500000), "rc 3, did not take")
    bench(body)


def test_link_down_reads_back():
    def body(b):
        b.role("kvaser", link="up")
        assert L.link_down("kvaser")["up"] is False
        b.set(stuck_up=True)
        refused(lambda: L.link_down("kvaser"), "still reads UP")
        refused(lambda: L.link_up("canx2", 500000), "has no netdev")
    bench(body)


# ------------------------------------------------------------------ modes

def test_set_mode_reads_back():
    def body(b):
        r = L.to_libusb("canmain")
        assert r.mode == "libusb" and (r.busnum, r.devnum) == (1, 41)
        assert L.to_libusb("canmain").mode == "libusb"
        assert b.log() == ["mode canmain libusb"], "a role already there is left alone"
        r = L.to_socketcan("canmain")
        assert r.netdev == "canmain" and r.role.link == "down"
        assert b.log()[-1].startswith("mode canmain socketcan --wait"), b.log()
        b.role("canchg", link="up")
        refused(lambda: L.to_libusb("canchg"), "link canchg is up")
        b.role("canchg", link="down")
        b.set(stuck_mode=True)
        refused(lambda: L.to_libusb("canchg"), "not settled in libusb")
        b.set(stuck_mode=False)
        L.to_libusb("canchg")
        b.set(claimed=True)
        refused(lambda: L.to_socketcan("canchg"), "rc 3, did not take")
        refused(lambda: L.set_mode("pcanfd", "libusb"), "has no canctl mode")
    bench(body)


# ------------------------------------------------------------------ open_bench

def test_open_bench_default_libusb_and_back():
    def body(b):
        bus = L.open_bench("canchg", 500000)
        assert Rec.calls == [{"interface": "gs_usb", "bitrate": 500000, "channel": "gs_usb:canchg", "bus": 1,
                              "address": 40}], Rec.calls
        assert b.read()["roles"]["canchg"]["mode"] == "libusb"
        bus.shutdown()
        bus.shutdown()
        assert bus.shut == 1, "a second shutdown() does nothing"
        r = b.read()["roles"]["canchg"]
        assert (r["mode"], r["driver"], r["link"]) == ("socketcan", "gs_usb", "down"), r
        assert [c.split(" --")[0] for c in b.log()] == ["mode canchg libusb", "mode canchg socketcan"], b.log()
    bench(body)


def test_open_bench_kvaser_link_up_then_down():
    def body(b):
        bus = L.open_bench("kvaser", 500000, restart_ms=100, hw_timestamps=True)
        assert Rec.calls == [{"interface": "socketcan_hw", "channel": "kvaser"}], Rec.calls
        bus.shutdown()
        assert b.log() == ["up kvaser 500000 restart-ms 100", "down kvaser"], b.log()
    bench(body)


def test_open_bench_leaves_a_matching_link_and_restores_a_different_one():
    def body(b):
        b.role("kvaser", link="up", bitrate=500000, restart_ms=100)
        bus = L.open_bench("kvaser", 500000, restart_ms=100)
        bus.shutdown()
        assert b.log() == [], "a link already up as asked is not bounced"
        b.role("kvaser", bitrate=250000)
        bus = L.open_bench("kvaser", 500000, restart_ms=100)
        bus.shutdown()
        assert b.log() == ["up kvaser 500000 restart-ms 100", "up kvaser 250000 restart-ms 100"], b.log()
    bench(body)


def test_open_bench_override_and_no_restore():
    def body(b):
        b.role("canmain", mode="libusb", driver="none", netdev="none", link="none")
        bus = L.open_bench("canmain", 500000, mode="socketcan")
        assert Rec.calls[-1] == {"interface": "socketcan", "channel": "canmain"}, Rec.calls
        bus.shutdown()
        assert [c.split(" --")[0] for c in b.log()] == ["mode canmain socketcan", "up canmain 500000",
                                                        "down canmain", "mode canmain libusb"], b.log()
        bus = L.open_bench("canchg", 500000, restore=False)
        bus.shutdown()
        assert b.read()["roles"]["canchg"]["mode"] == "libusb", "restore=False keeps the mode"
        assert bus.bench_layer["restore"] is False
    bench(body)


def test_a_failed_open_puts_the_role_back():
    def body(b):
        Rec.fail = OSError("usb gone")
        try:
            L.open_bench("canchg", 500000)
            raise AssertionError("opened")
        except OSError:
            pass
        assert b.read()["roles"]["canchg"]["mode"] == "socketcan", b.log()
        b.set(claimed=True)
        refused(lambda: L.open_bench("canmain", 500000), "usb gone", "not put back as found")
    bench(body)


def test_a_failed_step_puts_the_role_back():
    """gen-inhibit reviewer, 2026-11-19: a set_mode that exits 0 but does not settle, or a link that reads back
    wrong, may have changed the role already; it is put back as found, as a failed open is."""
    def body(b):
        b.set(stuck_mode=True)
        refused(lambda: L.open_bench("canchg", 500000), "not settled in libusb")
        r = b.read()["roles"]["canchg"]
        assert (r["mode"], r["driver"], r["netdev"], r["link"]) == ("socketcan", "gs_usb", "canchg", "down"), r
        assert [c.split(" --")[0] for c in b.log()] == ["mode canchg libusb", "mode canchg socketcan"], b.log()
        b.set(log=[])
        b.role("canmain", link="up", bitrate=500000)
        refused(lambda: L.open_bench("canmain", 500000), "not settled in libusb")
        r = b.read()["roles"]["canmain"]
        assert (r["mode"], r["link"], r["bitrate"]) == ("socketcan", "up", 500000), r
        assert [c.split(" --")[0] for c in b.log()] == ["down canmain", "mode canmain libusb",
                                                        "mode canmain socketcan", "up canmain 500000 restart-ms 0"], b.log()
        b.set(stuck_mode=False, log=[], lie={"bitrate": 250000})
        refused(lambda: L.open_bench("kvaser", 500000), "reads back bitrate 250000")
        assert b.read()["roles"]["kvaser"]["link"] == "down"
        assert b.log() == ["up kvaser 500000", "down kvaser"], b.log()
        assert Rec.calls == [], Rec.calls
    bench(body)


def test_open_bench_if_up_refuse():
    """gen-inhibit reviewer, 2026-11-19: runs that treat an up link as interference refuse it, touching nothing."""
    def body(b):
        b.role("canmain", link="up", bitrate=500000)
        refused(lambda: L.open_bench("canmain", 500000, if_up="refuse"), "link canmain found up", "refused")
        b.role("kvaser", link="up", bitrate=500000, restart_ms=100)
        refused(lambda: L.open_bench("kvaser", 500000, restart_ms=100, if_up="refuse"), "link kvaser found up")
        refused(lambda: L.open_bench("canchg", 500000, if_up="leave"), "if_up='leave'")
        try:
            L.open_bench("kvaser", 500000, if_up="refuse")
            raise AssertionError("not refused")
        except L.LinkFoundUp as e:
            assert isinstance(e, L.BenchLayerError) and (e.role, e.netdev) == ("kvaser", "kvaser"), e
            assert e.link["up"] and e.link["bitrate"] == 500000 and e.link["restart_ms"] == 100, e.link
            assert "bitrate 500000" in str(e), e
        assert b.log() == [] and Rec.calls == [], (b.log(), Rec.calls)
        b.role("canmain", link="down")
        L.open_bench("canmain", 500000, if_up="refuse").shutdown()
        assert [c.split(" --")[0] for c in b.log()] == ["mode canmain libusb", "mode canmain socketcan"], b.log()
    bench(body)


def _counts(bus):
    d = bus.bench_drain
    return d["echoes"], d["rx"], d["errors"]


def test_open_bench_drain():
    """gen-inhibit reviewer, 2026-11-19: a libusb gs_usb device stops taking sends after 257 unread TX echoes, so a
    caller that only sends asks for a drain thread."""
    def body(b):
        Rec.rx = ([can.Message(arbitration_id=0x6B0, is_rx=False) for _ in range(3)]
                  + [can.Message(arbitration_id=0x6B1) for _ in range(2)] + [can.Message(is_error_frame=True)])
        bus = L.open_bench("canchg", 500000, drain=True)
        t_end = time.monotonic() + 5
        while sum(_counts(bus)) < 6 and time.monotonic() < t_end:
            time.sleep(0.01)
        assert _counts(bus) == (3, 2, 1), bus.bench_drain
        refused(lambda: bus.recv(0.1), "drain thread is reading")
        bus.shutdown()
        assert bus.shut == 1 and bus.shut_saw_drain_running is False, "the drain stops before the device closes"
        assert b.read()["roles"]["canchg"]["mode"] == "socketcan", "and the role is put back after"
        bus = L.open_bench("canchg", 500000, drain=True, restore=False)
        assert bus.stop_drain()["running"] is False
        assert bus.recv(0.01) is None, "recv works once the drain is stopped"
        assert L.DRAIN_READ_S <= 0.001, "each drain read is gs_usb's least, 1 ms"
        worst = 0
        for _ in range(3):
            bus = L.open_bench("canchg", 500000, drain=True, restore=False)
            time.sleep(0.05)
            worst = max(worst, bus.stop_drain()["stop_ms"])
            bus.shutdown()
        assert worst < 30, f"stop_drain waits out at most one read (worst {worst} ms)"
        Rec.gone, Rec.reads = True, 0
        saved, L.DRAIN_FAIL_WAIT_S = L.DRAIN_FAIL_WAIT_S, 1.0
        try:
            bus = L.open_bench("canmain", 500000, drain=True, restore=False)    # time the stop, not a put-back
            time.sleep(0.3)
            t0 = time.monotonic()
            bus.shutdown()
            assert time.monotonic() - t0 < 0.5, "the wait after a failed read is cut by the stop"
        finally:
            L.DRAIN_FAIL_WAIT_S = saved
        assert Rec.reads < 60, f"a read that fails at once is not spun on ({Rec.reads} reads in 0.3 s)"
        n = len(b.log())
        refused(lambda: L.open_bench("kvaser", 500000, drain=True), "drain is for a gs_usb role in libusb mode")
        refused(lambda: L.open_bench("canchg", 500000, mode="socketcan", drain=True), "not gs_usb socketcan")
        assert len(b.log()) == n, b.log()
    bench(body)


def test_open_bench_drain_splits_at_the_first_send():
    """gen-inhibit reviewer, 2026-11-19: canmain handed a replay one frame more than it sent (an echo or an rx frame
    left from an earlier session, read at the next open). The drain counts what it read before the first send apart,
    with the frames' ids, and open_bench returns only once the device's queue has been read empty."""
    def body(b):
        Rec.rx_delay = 0.01
        Rec.rx = [can.Message(arbitration_id=0x6B5, is_extended_id=False, is_rx=False, data=b"\x00\x00\x01\xf4"),
                  can.Message(arbitration_id=0x18DAF110, is_extended_id=True, data=b"\x01")]
        bus = L.open_bench("canmain", 500000, drain=True, restore=False)
        d = bus.bench_drain
        assert d["primed"] and d["before_send"] == {"echoes": 1, "rx": 1, "errors": 0, "frames": [
            {"kind": "echo", "id": 0x6B5, "extended": False, "data": "000001f4"},
            {"kind": "rx", "id": 0x18DAF110, "extended": True, "data": "01"}]}, d
        Rec.rx_delay = 0.0
        for i in range(3):
            Rec.rx.append(can.Message(arbitration_id=0x6B0, is_rx=False))
            bus.send(can.Message(arbitration_id=0x6B0))
        Rec.rx.append(can.Message(is_error_frame=True))
        t_end = time.monotonic() + 5
        while sum(_counts(bus)) < 6 and time.monotonic() < t_end:
            time.sleep(0.01)
        got = bus.stop_drain()
        assert got["after_send"] == {"echoes": 3, "rx": 0, "errors": 1}, got
        assert (got["echoes"], got["rx"], got["errors"]) == (4, 1, 1), got
        assert len(got["before_send"]["frames"]) == 2 and got["before_send"]["echoes"] == 1, got
        got["before_send"]["frames"].clear()
        assert len(bus.bench_drain["before_send"]["frames"]) == 2, "stop_drain returns a copy"
        bus.shutdown()
        Rec.rx = [can.Message(arbitration_id=i) for i in range(L.DRAIN_FRAMES_KEPT + 5)]
        bus = L.open_bench("canmain", 500000, drain=True, restore=False)
        t_end = time.monotonic() + 5
        while Rec.rx and time.monotonic() < t_end:
            time.sleep(0.01)
        got = bus.stop_drain()
        assert got["before_send"]["rx"] == L.DRAIN_FRAMES_KEPT + 5, got
        assert len(got["before_send"]["frames"]) == L.DRAIN_FRAMES_KEPT, "the kept frames are capped"
        bus.shutdown()
    bench(body)


class _PcanRec(Rec):
    pass


_PcanRec.__name__ = "CanbenchPcanBus"


def test_open_bench_pcan_bus_states():
    """Both ESP reviewers, 2026-11-19: bus_states="report" opens CanbenchPcanBus by role (bus states as error frames,
    never raised); the default stays python-can's pcan."""
    def body(b):
        real = P.CanbenchPcanBus
        P.CanbenchPcanBus = lambda **kw: _PcanRec(cls="CanbenchPcanBus", **kw)
        try:
            bus = L.open_bench("pcanfd", 500000)
            assert Rec.calls[-1] == {"interface": "pcan", "bitrate": 500000, "device_id": 3}, Rec.calls[-1]
            assert L.bus_error(bus, can.Message(is_error_frame=True, arbitration_id=E.CAN_ERR_CRTL)), \
                "python-can's pcan: every error frame counts (not the Linux layout)"
            bus = L.open_bench("pcanfd", 500000, bus_states="report", canbench_busoff_reset=False)
            assert Rec.calls[-1] == {"cls": "CanbenchPcanBus", "bitrate": 500000, "device_id": 3,
                                     "canbench_busoff_reset": False}, Rec.calls[-1]
            assert not L.bus_error(bus, can.Message(is_error_frame=True, arbitration_id=E.CAN_ERR_CRTL)), \
                "a CanbenchPcanBus state report is not a bus error"
            assert L.bus_error(bus, can.Message(is_error_frame=True, arbitration_id=E.CAN_ERR_PROT | E.CAN_ERR_BUSERROR))
            n = len(Rec.calls)
            refused(lambda: L.open_bench("pcanfd", 500000, bus_states="quiet"), "bus_states='quiet'", cls=bench_roles.RoleError)
            refused(lambda: L.open_bench("pcanfd", 500000, canbench_busoff_reset=False), "give bus_states='report'", cls=bench_roles.RoleError)
            refused(lambda: L.open_bench("kvaser", 500000, bus_states="report"), "for a PCAN role, not socketcan", cls=bench_roles.RoleError)
            assert len(Rec.calls) == n
        finally:
            P.CanbenchPcanBus = real
    bench(body)


class _PMsg:
    def __init__(self, ID=0, MSGTYPE=0, LEN=0, data=b""):
        self.ID, self.MSGTYPE, self.LEN = ID, MSGTYPE, LEN
        self.DATA = list(data) + [0] * (8 - len(data))


class _PTs:
    millis, millis_overflow, micros = 5, 0, 0


class _FakeBasic:
    """PCAN-Basic's Read, as Linux 9.3.3 returns it: a status message's word as the result (pcanbasic_read)."""

    def __init__(self, reads):
        self.reads = list(reads)

    def Read(self, handle):
        if not self.reads:
            return 0x20, _PMsg(), _PTs()
        result, mid, mtype, data = self.reads.pop(0)
        return result, _PMsg(mid, mtype, len(data), data), _PTs()

    def GetStatus(self, handle):
        return 0

    def SetValue(self, handle, param, value):
        return 0

    def Reset(self, handle):
        return 0


def _real_pcan_bus(reads):
    import collections
    from can.bus import CanProtocol
    cls = P.CanbenchPcanBus
    bus = cls.__new__(cls)
    bus.m_objPCANBasic, bus.m_PcanHandle = _FakeBasic(reads), 0x51
    bus._can_protocol, bus.channel_info, bus._recv_event = CanProtocol.CAN_20, "PCAN_USBBUS1", None
    bus._pending = collections.deque()
    bus._last_type, bus._last_raw, bus._synth_status, bus._last_ts = 0, (0, b""), None, 0.0
    bus.busoff_reset, bus.busoff_reset_interval, bus.busoff_resets = False, 0.0, 0
    bus._last_reset = float("-inf")
    bus._filters = None
    bus._wrap_read("Read")
    return bus


def test_pcan_bus_states_never_raise_and_an_overrun_stays_a_loss():
    """gen-inhibit reviewer's condition, 2026-11-19: with bus_states="report" an overrun must never read as only a
    state change (its witnesses VOID on a lost frame)."""
    import can.interfaces.pcan.pcan as pc
    saved = pc.HAS_EVENTS
    pc.HAS_EVENTS = False
    try:
        bus = _real_pcan_bus([
            (0x00000, 0x123, 0x00, b"\x01\x02"),                 # a data frame
            (0x40000, 0x000, 0x80, b"\x00\x04\x00\x00"),        # status message: error passive
            (0x00002, 0x000, 0x80, b"\x00\x00\x00\x02"),        # status message: overrun
            (0x00048, 0x000, 0x00, b""),                         # queue overrun with bus-heavy, returned by Read
            (0x40020, 0x000, 0x00, b""),                         # error passive with an empty queue
            (0x00000, 0x004, 0x40, b"\x01\x0A\x05\x00"),        # error message: stuff error, Rx
        ])
        got = [bus.recv(0.01) for _ in range(6)]
    finally:
        pc.HAS_EVENTS = saved
    assert not got[0].is_error_frame and got[0].arbitration_id == 0x123, got[0]
    assert all(m is not None and m.is_error_frame for m in got[1:]), got
    assert [E.lost_frames(m) for m in got] == [False, False, True, True, False, False], [E.lost_frames(m) for m in got]
    assert [E.decode(m).state for m in got[1:5]] == ["passive", None, "warning", "passive"], \
        [E.decode(m).state for m in got[1:5]]
    assert [L.bus_error(bus, m) for m in got] == [False, False, False, False, False, True], \
        "only the error message is a bus error"
    assert not E.lost_frames(can.Message(is_error_frame=True, arbitration_id=E.CAN_ERR_PROT, data=bytes([0, 1]))), \
        "a protocol error with data[1] 0x01 is not a loss"


def test_open_bench_refusals():
    def body(b):
        for kw in ({"channel": "canchg"}, {"index": 0}, {"bus": 1}, {"address": 40}, {"device_id": 3},
                   {"serial": "X"}, {"interface": "gs_usb"}):
            refused(lambda: L.open_bench("canchg", 500000, **kw), "refused", "canctl role")
        refused(lambda: L.open_bench(0, 500000), "role 0 refused")
        refused(lambda: L.open_bench("can0", 500000), "not a canctl role", cls=bench_roles.RoleError)
        refused(lambda: L.open_bench("canx2", 500000), "no default mode")
        refused(lambda: L.open_bench("pcanfd", 500000, mode="libusb"), "has no mode 'libusb'")
        refused(lambda: L.open_bench("kvaser"), "needs bitrate=")
        refused(lambda: L.open_bench("pcanfd"), "give bitrate=")
        assert b.log() == [] and Rec.calls == [], (b.log(), Rec.calls)
        L.open_bench("pcanfd", 500000)
        assert Rec.calls == [{"interface": "pcan", "bitrate": 500000, "device_id": 3}], Rec.calls
    bench(body)


def test_defaults_are_the_projects():
    assert L.DEFAULT_MODES == {"pcanfd": "pcan", "canchg": "libusb", "canmain": "libusb", "kvaser": "socketcan"}


# ------------------------------------------------------------------ classifier

class _M:
    def __init__(self, can_id, err=True):
        self.arbitration_id, self.is_error_frame = can_id, err


class _Res:
    def __init__(self, interface):
        self.interface = interface


class _Bus:
    def __init__(self, interface):
        self.bench_role = _Res(interface)


def test_bus_error_classifier():
    sc, pc = _Bus("socketcan"), _Bus("pcan")
    assert L.bus_error(sc, _M(E.CAN_ERR_PROT | E.CAN_ERR_BUSERROR | E.CAN_ERR_CNT))
    for cls in ("TX_TIMEOUT", "LOSTARB", "PROT", "TRX", "ACK", "BUSERROR"):
        assert L.bus_error(sc, _M(getattr(E, "CAN_ERR_" + cls))), cls
    for cls in ("CRTL", "BUSOFF", "RESTARTED", "CNT"):
        assert not L.bus_error(sc, _M(getattr(E, "CAN_ERR_" + cls))), cls
    assert L.bus_error(pc, _M(E.CAN_ERR_CRTL)), "every error frame counts off SocketCAN"
    assert not L.bus_error(sc, _M(E.CAN_ERR_PROT, err=False))
    assert not L.bus_error(type("SocketcanHwBus", (), {})(), _M(E.CAN_ERR_CRTL))


# ------------------------------------------------------------------ the scan

def test_scan_self_check():
    assert S.self_check() == [], S.self_check()


def test_scan_counts_role_calls_only_with_a_pick():
    hits = S.scan_source("from canbench import bench_layer as L\nL.open_bench('kvaser', 500000)\n"
                         "L.open_bench(role, 500000, channel=0)\nL.link_up('PCAN_USBBUS1', 500000)\n")
    assert [(h.line, h.kind) for h in hits] == [(3, "role-pick"), (4, "pcan-channel"), (4, "role-pick")], hits


# Where can-tools itself opens adapters (it is the layer, and its detection): (path, kind) -> count. A change in
# any count fails, so a new open in can-tools is seen and listed here with why.
CAN_TOOLS_ALLOW = {
    ("canbench/bench_roles.py", "adapter-class"): 3,    # open_role builds SocketcanHwBus, CanbenchPcanBus
    ("canbench/bench_roles.py", "bus-open"): 2,         # open_role: python-can Bus from the resolved role
    ("canbench/buses.py", "adapter-class"): 2,          # open_bus: hardware through detection
    ("canbench/buses.py", "bus-open"): 2,
    ("canbench/gui/logger_window.py", "adapter-class"): 2,
    ("canbench/live/kvaser_bus.py", "adapter-class"): 3,   # CanbenchKvaserBus subclasses python-can's
    ("canbench/live/pcan_bus.py", "adapter-class"): 2,     # CanbenchPcanBus subclasses python-can's
    ("canbench/live/pcan_bus.py", "pcan-channel"): 1,      # its default channel
    ("canbench/live/receiver.py", "bus-open"): 4,          # detection (non-bench machines; tagged by role on one)
    ("canbench/live/receiver.py", "usb-scan"): 1,
    ("canbench/live/receiver.py", "pcan-channel"): 4,
    ("tools/can_gui.py", "adapter-class"): 2,
    ("tools/can_logger.py", "adapter-class"): 2,
    ("tools/can_replayer.py", "adapter-class"): 2,
}


def test_scan_of_can_tools_matches_its_allow_list():
    from collections import Counter
    hits = S.scan_paths(S.py_files(str(ROOT), skip_dirs=("__pycache__", ".git", "tests")), root=str(ROOT))
    got = Counter((h.path, h.kind) for h in hits)
    assert dict(got) == CAN_TOOLS_ALLOW, "\n".join(str(h) for h in hits)


# ------------------------------------------------------------------ runner and can-fail

MUTANTS = [
    ("bench_layer", "    if _guard is None:\n        raise NoGuard(", "    if False:\n        raise NoGuard("),
    ("bench_layer", "    _guard()\n", "    pass\n"),
    ("bench_layer", 'if got["bitrate"] != int(bitrate):', "if False:"),
    ("bench_layer", 'if got["listen_only"]:', "if False:"),
    ("bench_layer", "    if not got[\"up\"]:\n        wrong.append", "    if False:\n        wrong.append"),
    ("bench_layer", "if restart_ms is not None and got[\"restart_ms\"]", "if False and got[\"restart_ms\"]"),
    ("bench_layer", "    if got[\"up\"]:\n        raise BenchLayerError(f\"canctl down",
     "    if False:\n        raise BenchLayerError(f\"canctl down"),
    ("bench_layer", "        if monotonic() >= t_end:\n            raise", "        if True:\n            return res\n            raise"),
    ("bench_layer", "    if r.netdev and r.link == \"up\":\n        raise BenchLayerError(f\"{role}: link",
     "    if False:\n        raise BenchLayerError(f\"{role}: link"),
    ("bench_layer", "    if restore and changed:\n", "    if False:\n"),
    ("bench_layer", "        if changed:\n            try:\n                _put_back", "        if False:\n            try:\n                _put_back"),
    ("bench_layer", "    bad = [k for k in PICK_KW if k in kw]", "    bad = []"),
    ("bench_layer", '"canmain": "libusb", "kvaser": "socketcan"}', '"canmain": "socketcan", "kvaser": "socketcan"}'),
    ("bench_layer", "        if state[\"done\"]:\n            return", "        if False:\n            return"),
    ("bench_layer", "            if lk[\"up\"]:\n                link_up(", "            if False:\n                link_up("),
    ("bench_layer", "res.mode == want and _link_matches(", "False and _link_matches("),
    ("bench_layer", "            changed = True                              # before the mode move", "            pass"),
    ("bench_layer", "            changed = True                              # before the link up", "            pass"),
    ("bench_layer", "            r = _table_role(role)\n            if r.netdev",
     "            r = _resolve(role).role\n            if r.netdev"),
    ("bench_layer", 'if if_up == "refuse" and res.netdev', 'if False and res.netdev'),
    ("bench_layer", "    if if_up not in IF_UP:", "    if False:"),
    ("bench_layer", "        raise LinkFoundUp(f\"open_bench", "        raise BenchLayerError(f\"open_bench"),
    ("bench_layer", "        sent.set()                                      # before", "        pass  # before"),
    ("bench_layer", '                part = stats["after_send" if sent.is_set() else "before_send"]',
     '                part = stats["before_send"]'),
    ("bench_layer", "                if not sent.is_set() and len(part[\"frames\"])", "                if len(part[\"frames\"])"),
    ("bench_layer", "    primed.wait(DRAIN_PRIME_S)\n", "\n"),
    ("bench_layer", "                    elif not primed.is_set():", "                    elif False:"),
    ("bench_layer", "        return {k: (dict(v, frames=list(v[\"frames\"]))", "        return {k: (v"),
    ("bench_layer", 'else "rx" if m.is_rx else "echoes"', 'else "rx" if True else "echoes"'),
    ("bench_layer", "        bus.recv = orig_recv\n", "        pass\n"),
    ("bench_layer", "            stop_drain()\n        finally:", "            pass\n        finally:"),
    ("bench_layer", 'if drain and not (kind == "gs_usb" and want == "libusb"):', "if False:"),
    ("bench_layer", "    if drain:\n        _start_drain", "    if False:\n        _start_drain"),
    ("bench_layer", "bus.bench_drain, bus.stop_drain, bus.recv, bus.shutdown, bus.send = stats, stop_drain, recv, shutdown, send",
     "bus.bench_drain, bus.stop_drain, bus.shutdown, bus.send = stats, stop_drain, shutdown, send"),
    ("bench_layer", "                        stop.wait(DRAIN_FAIL_WAIT_S)", "                        pass"),
    ("bench_layer", "                        stop.wait(DRAIN_FAIL_WAIT_S)", "                        time.sleep(DRAIN_FAIL_WAIT_S)"),
    ("bench_layer", "DRAIN_READ_S = 0.001", "DRAIN_READ_S = 0.05"),
    ("bench_layer", "| _e.CAN_ERR_ACK | _e.CAN_ERR_BUSERROR)", "| _e.CAN_ERR_ACK | _e.CAN_ERR_BUSERROR | _e.CAN_ERR_CRTL)"),
    ("bench_layer", "    if not _is_socketcan(bus):\n        return True", "    if not _is_socketcan(bus):\n        return False"),
    ("bench_layer", '    if any(c.__name__ == "CanbenchPcanBus" for c in type(bus).__mro__):', "    if False:"),
    ("live/pcan_bus", "            r = r & ~_v(PCAN_ERROR_QRCVEMPTY) if r & _STATE_RESULT else r\n", "\n"),
    ("errors", "    if status & (PCAN_ERROR_OVERRUN | PCAN_ERROR_QOVERRUN):\n        d[1] |= CAN_ERR_CRTL_RX_OVERFLOW",
     "    if False:\n        d[1] |= CAN_ERR_CRTL_RX_OVERFLOW"),
    ("errors", "    if status & (PCAN_ERROR_OVERRUN | PCAN_ERROR_QOVERRUN):", "    if status & PCAN_ERROR_OVERRUN:"),
    ("errors", "    return len(d) > 1 and bool(d[1] & CAN_ERR_CRTL_RX_OVERFLOW)", "    return len(d) > 1"),
    ("errors", "    if not msg.is_error_frame or not msg.arbitration_id & CAN_ERR_CRTL:", "    if not msg.is_error_frame:"),
    ("bench_scan", "    if not (isinstance(v, ast.Constant) and isinstance(v.value, str)):\n        return",
     "    if False:\n        return"),
    ("bench_scan", 'VIRTUAL = {"virtual", "udp_multicast"}', 'VIRTUAL = {"virtual", "udp_multicast", "socketcan"}'),
    ("bench_scan", "    if len(n.args) >= 2:\n        return", "    if False:\n        return"),
    ("bench_scan", "    if any(k.arg is None for k in n.keywords):\n        return", "    if False:\n        return"),
    ("bench_scan", "            elif fn == \"scan\" and", "            elif False and"),
    ("bench_scan", "        if isinstance(role.value, str) and role.value not in ROLE_NAMES:",
     "        if False:"),
    ("bench_scan", "        if isinstance(role.value, (int, float))", "        if False and isinstance(role.value, (int, float))"),
    ("bench_scan", '"device_id", "serial", "interface"}', '"device_id", "interface"}'),
    ("bench_scan", "        elif isinstance(n, (ast.Import, ast.ImportFrom)):\n            for a in n.names:\n                if",
     "        elif isinstance(n, (ast.Import, ast.ImportFrom)):\n            for a in n.names:\n                if False and"),
    ("bench_scan", "    if fn in GENERIC_FNS:\n        return None", "    if False:\n        return None"),
    ("bench_scan", 'PCAN_CHANNEL_RX = re.compile(r"PCAN_USBBUS\\d+\\Z")', 'PCAN_CHANNEL_RX = re.compile(r"PCAN_USBBUS0\\Z")'),
]


def run_all(verbose=True):
    failed = []
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                if verbose:
                    print(f"PASS {name}")
            except Exception as e:
                failed.append(name)
                if verbose:
                    print(f"FAIL {name}: {type(e).__name__}: {e}")
    return failed


def _load_mutant(modname, old, new):
    src = (ROOT / "canbench" / f"{modname}.py").read_text()
    assert src.count(old) == 1, f"mutant anchor not unique in {modname}: {old!r} ({src.count(old)})"
    spec = importlib.util.spec_from_loader(f"canbench.{modname.replace('/', '.')}", loader=None)
    mod = importlib.util.module_from_spec(spec)
    exec(compile(src.replace(old, new), f"<mutant {modname}>", "exec"), mod.__dict__)
    return mod


def can_fail():
    global L, S, P, E
    real = {"bench_layer": L, "bench_scan": S, "live/pcan_bus": P, "errors": E}
    control = run_all(verbose=False)
    print(f"control: {'PASS' if not control else 'FAIL ' + ', '.join(control)}")
    if control:
        return 1
    survived = 0
    for i, (modname, old, new) in enumerate(MUTANTS, 1):
        mod = _load_mutant(modname, old, new)
        if modname == "bench_layer":
            L = mod
        elif modname == "bench_scan":
            S = mod
        elif modname == "live/pcan_bus":
            P = mod
        else:                                   # errors: the tests' E and the PCAN bus's own reference
            E = mod
            P.errors = mod
        try:
            failed = run_all(verbose=False)
        finally:
            L, S, P, E = real["bench_layer"], real["bench_scan"], real["live/pcan_bus"], real["errors"]
            P.errors = real["errors"]
        print(f"mutant {i:2d} {modname}: {'caught by ' + failed[0] if failed else 'SURVIVED'}  ({old.strip()[:60]!r})")
        survived += not failed
    print(f"{len(MUTANTS)} mutants, {survived} survived")
    return 1 if survived else 0


if __name__ == "__main__":
    if "--can-fail" in sys.argv:
        sys.exit(can_fail())
    sys.exit(1 if run_all() else 0)
