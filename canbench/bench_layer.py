"""The bench-role layer: set a bench adapter up by its canctl role, open it, and put it back.

Asked for by both ESP reviewers (charge-interposer and gen-inhibit, 2026-11-19) so neither project carries its own
copy; plan: reverse-it ``tools/plans/bench-role-layer.md``. It sits on :mod:`canbench.bench_roles` (which stays
read-only: it resolves and opens, and never touches a link or a mode).

This module runs ``sudo -n /usr/local/sbin/canctl``, and only when its caller asks (owner, 2026-11-19: canbench may
bring a link up or down, on an explicit call, behind a plugged-in bench guard):

* :func:`set_guard` -- **the bench guard.** A callable run before every canctl call and every open here. With none
  set, everything here that touches the bench refuses (:class:`NoGuard`). Each project plugs in its own (the ESP
  projects' ``require_bench`` reads shared/bench's lock; canbench takes no lock dependency, since the Android and
  can-tools hand-take roles have no shared/bench).
* :func:`link_up` / :func:`link_down` -- ``canctl up <role> <bitrate> [restart-ms N]`` / ``canctl down <role>``,
  each read back with ``ip -details -json link show <netdev>`` (no root): UP, that bitrate, not listen-only (and
  the restart-ms asked for); or not UP.
* :func:`set_mode` (and :func:`to_libusb`, :func:`to_socketcan`) -- ``canctl mode <role> <mode>`` (``--wait`` for
  socketcan), read back from ``canctl roles`` until the role resolves in that mode (gs_usb socketcan: bound to
  gs_usb, with a netdev, link down). A netdev role whose link is up is refused: take it down first.
* :func:`open_bench` -- the one call a harness needs: the role brought to the caller's mode (or
  :data:`DEFAULT_MODES`), a socketcan link brought up at the bitrate, then :func:`bench_roles.open_role`. With
  ``restore=True`` (the default) the bus's ``shutdown()``, and a failed open, put the role back as found (mode, and
  link down or up at its old bitrate); with ``restore=False`` the caller keeps it set for a whole run and puts it
  back with :func:`set_mode` / :func:`link_down` itself.
* :func:`refuse_picks` -- index, channel, USB bus / address, device id, serial and interface keywords are refused
  everywhere here: an adapter is asked for by its role (shared/bench SPEC section 8).
* :func:`bus_error` / :func:`bus_error_class` -- the error-frame classifier (gen-inhibit reviewer, 2026-11-18): on
  SocketCAN only the bus-error classes count (TX_TIMEOUT, LOSTARB, PROT, TRX, ACK, BUSERROR), not the controller's
  CRTL, BUSOFF, RESTARTED, nor CNT alone; on any other backend every error frame counts.

The adapter-open scan each project runs on its own tree is :mod:`canbench.bench_scan`.

Errors are :class:`BenchLayerError` (a :class:`bench_roles.RoleError` passes through as it is), naming the step,
canctl's exit (2 refused, 3 did not take) and what was read back. The command lists, the clock and the sleep are
module globals so tests run on a fake canctl and a fake ``ip``.
"""
from __future__ import annotations

import json
import subprocess
import time

from canbench import bench_roles
from canbench import errors as _e

SUDO = ["sudo", "-n"]
CANCTL = None                           # None: bench_roles.CANCTL (env CANBENCH_CANCTL or /usr/local/sbin/canctl)
IP_SHOW = ["ip", "-details", "-json", "link", "show"]
CANCTL_TIMEOUT_S = 60
WAIT_S = 10.0
POLL_S = 0.25
RC_MEANING = {2: "refused", 3: "did not take"}         # canctl's header
sleep = time.sleep
monotonic = time.monotonic

PICK_KW = ("channel", "index", "bus", "address", "device_id", "serial", "interface")
MODES_BY_KIND = {"gs_usb": ("socketcan", "libusb"), "kvaser": ("socketcan", "linuxcan"), "pcan": ("pcan",),
                 "slcan": ("socketcan", "tty")}
# The defaults the two ESP projects use; a caller passes mode= for anything else (gen-inhibit's runs that hold
# canmain in libusb, a caller wanting canmain on SocketCAN, the Kvaser on leaf). canx2 has none: say which.
DEFAULT_MODES = {"pcanfd": "pcan", "canchg": "libusb", "canmain": "libusb", "kvaser": "socketcan"}

BUS_ERROR_CLASSES = (_e.CAN_ERR_TX_TIMEOUT | _e.CAN_ERR_LOSTARB | _e.CAN_ERR_PROT | _e.CAN_ERR_TRX
                     | _e.CAN_ERR_ACK | _e.CAN_ERR_BUSERROR)


class BenchLayerError(RuntimeError):
    """A bench step that failed or was refused, with the reason."""


class NoGuard(BenchLayerError):
    """No bench guard is set (set_guard): nothing on the bench is touched."""


_guard = None


def set_guard(fn):
    """Register the bench guard: a no-argument callable that raises unless this process may use the bench now.
    ``set_guard(None)`` clears it. Returns the previous one."""
    global _guard
    old, _guard = _guard, fn
    return old


def guard(what: str):
    """Run the bench guard before ``what``; NoGuard if none is set. A guard's own exception passes through."""
    if _guard is None:
        raise NoGuard(f"{what}: no bench guard set; canbench.bench_layer.set_guard(fn) first (each tool plugs in "
                      f"its own check that it holds the bench)")
    _guard()


def refuse_picks(kw, who="this call"):
    """Refuse any keyword that picks an adapter by something other than its role."""
    bad = [k for k in PICK_KW if k in kw]
    if bad:
        raise BenchLayerError(f"{who}: {', '.join(f'{k}={kw[k]!r}' for k in bad)} refused; a bench adapter is asked "
                              f"for by its canctl role, never by index, channel, USB bus/address, device id, serial "
                              f"or interface (shared/bench SPEC section 8)")


def _check_name(role):
    if not isinstance(role, str):
        raise BenchLayerError(f"role {role!r} refused: a bench adapter is asked for by its canctl role name")


def _canctl_argv(args):
    return list(SUDO) + [CANCTL or bench_roles.CANCTL] + [str(a) for a in args]


def _resolve(role):
    _check_name(role)
    return bench_roles.resolve(role, text=bench_roles.canctl_roles_text(CANCTL or bench_roles.CANCTL))


def _rc_text(rc):
    return f"rc {rc}" + (f", {RC_MEANING[rc]}" if rc in RC_MEANING else "")


def canctl(*args) -> str:
    """Run a canctl command (as root, through sudo -n) after the guard; BenchLayerError unless it exits 0."""
    guard("canctl " + " ".join(str(a) for a in args))
    argv = _canctl_argv(args)
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=CANCTL_TIMEOUT_S, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        raise BenchLayerError(f"canctl {' '.join(map(str, args))}: timed out after {CANCTL_TIMEOUT_S} s")
    except OSError as e:
        raise BenchLayerError(f"canctl {' '.join(map(str, args))}: could not run: {e}")
    out = (p.stdout + p.stderr).strip()
    if p.returncode != 0:
        last = out.splitlines()[-1] if out else "no output"
        raise BenchLayerError(f"canctl {' '.join(map(str, args))} failed ({_rc_text(p.returncode)}): {last}")
    return out


def link_info(netdev: str) -> dict:
    """The netdev's state from ``ip -details -json link show`` (no root): up, bitrate, listen_only, restart_ms,
    state. BenchLayerError if it cannot be read."""
    try:
        p = subprocess.run(IP_SHOW + [netdev], capture_output=True, text=True, timeout=10, stdin=subprocess.DEVNULL)
        if p.returncode != 0:
            raise BenchLayerError(f"ip link show {netdev}: rc {p.returncode}: {(p.stderr or p.stdout).strip()[:200]}")
        d = json.loads(p.stdout)[0]
    except BenchLayerError:
        raise
    except Exception as e:
        raise BenchLayerError(f"the link state of {netdev} cannot be read: {type(e).__name__}: {e}")
    data = (d.get("linkinfo") or {}).get("info_data") or {}
    return {"up": "UP" in (d.get("flags") or []),
            "bitrate": (data.get("bittiming") or {}).get("bitrate"),
            "listen_only": "LISTEN-ONLY" in (data.get("ctrlmode") or []),
            "restart_ms": data.get("restart_ms"),
            "state": data.get("state")}


def _netdev(role, res=None):
    res = res or _resolve(role)
    if not res.netdev:
        raise BenchLayerError(f"{role} has no netdev (mode {res.mode}); a link is brought up or down only on a "
                              f"SocketCAN netdev")
    return res.netdev


def link_up(role: str, bitrate: int, restart_ms: int | None = None) -> dict:
    """``canctl up <role> <bitrate> [restart-ms N]``, then read back: UP, that bitrate, listen-only off, and the
    restart-ms asked for. Returns the read-back."""
    guard(f"link_up {role}")
    dev = _netdev(role)
    args = ["up", role, int(bitrate)] + (["restart-ms", int(restart_ms)] if restart_ms is not None else [])
    canctl(*args)
    got = link_info(dev)
    wrong = []
    if not got["up"]:
        wrong.append("not UP")
    if got["bitrate"] != int(bitrate):
        wrong.append(f"bitrate {got['bitrate']} (asked {bitrate})")
    if got["listen_only"]:
        wrong.append("listen-only on")
    if restart_ms is not None and got["restart_ms"] != int(restart_ms):
        wrong.append(f"restart-ms {got['restart_ms']} (asked {restart_ms})")
    if wrong:
        raise BenchLayerError(f"canctl {' '.join(map(str, args))} exited 0 but {dev} reads back {', '.join(wrong)}")
    return got


def link_down(role: str) -> dict:
    """``canctl down <role>``, then read back: not UP. Returns the read-back."""
    guard(f"link_down {role}")
    dev = _netdev(role)
    canctl("down", role)
    got = link_info(dev)
    if got["up"]:
        raise BenchLayerError(f"canctl down {role} exited 0 but {dev} still reads UP")
    return got


def _in_mode(res, mode):
    """Whether a Resolved is settled in ``mode`` (gs_usb socketcan: bound, netdev present, link down). A gs_usb role
    in libusb mode resolves only when unbound, with a bus and address (bench_roles refuses it otherwise)."""
    if res.mode != mode:
        return False
    if res.role.kind == "gs_usb" and mode == "socketcan":
        return res.role.driver == "gs_usb" and bool(res.netdev) and res.role.link != "up"
    return True


def set_mode(role: str, mode: str, wait_s: float = WAIT_S):
    """``canctl mode <role> <mode>`` (gs_usb: socketcan / libusb; kvaser: socketcan / linuxcan), then read back
    from ``canctl roles`` until the role resolves settled in that mode, for up to ``wait_s``. A role already
    there is left alone. Refused while a netdev role's link is up. Returns the Resolved."""
    guard(f"set_mode {role} {mode}")
    _check_name(role)
    table = bench_roles.roles(bench_roles.canctl_roles_text(CANCTL or bench_roles.CANCTL))
    if role not in table:
        _resolve(role)                                  # bench_roles' own refusal, with the role list
    r = table[role]
    if r.kind not in ("gs_usb", "kvaser") or mode not in MODES_BY_KIND[r.kind]:
        raise BenchLayerError(f"{role} ({r.kind}) has no canctl mode {mode!r} (gs_usb: socketcan, libusb; kvaser: "
                              f"socketcan, linuxcan)")
    try:
        res = _resolve(role)
        if _in_mode(res, mode):
            return res
    except bench_roles.RoleError:
        pass                                            # half-moved (e.g. libusb mode, still bound): canctl again
    if r.netdev and r.link == "up":
        raise BenchLayerError(f"{role}: link {r.netdev} is up; take it down (link_down) before moving it to {mode}")
    canctl("mode", role, mode, *(["--wait", int(wait_s)] if mode == "socketcan" else []))
    t_end = monotonic() + wait_s
    last = None
    while True:
        try:
            res = _resolve(role)
            if _in_mode(res, mode):
                return res
            last = f"mode {res.mode}, driver {res.role.driver}, netdev {res.netdev}, link {res.role.link}"
        except bench_roles.RoleError as e:
            last = str(e)
        if monotonic() >= t_end:
            raise BenchLayerError(f"canctl mode {role} {mode} exited 0 but {role} is not settled in {mode} after "
                                  f"{wait_s} s: {last}")
        sleep(POLL_S)


def to_libusb(role: str, wait_s: float = WAIT_S):
    """A gs_usb role handed to libusb (python-can's gs_usb by bus and address), read back."""
    return set_mode(role, "libusb", wait_s)


def to_socketcan(role: str, wait_s: float = WAIT_S):
    """A role handed back to its kernel driver (netdev present, link down), read back."""
    return set_mode(role, "socketcan", wait_s)


def _found(res):
    """What open_bench must put back: the mode, and a netdev's link (up or down, and its settings)."""
    f = {"mode": res.mode, "link": None}
    if res.netdev:
        f["link"] = link_info(res.netdev)
    return f


def _put_back(role, found, now_mode):
    """Undo open_bench's changes: the mode as found, then a netdev's link as found (up at its old settings, or
    down). Every step is tried; BenchLayerError naming each that failed."""
    fails = []
    if found["mode"] != now_mode:
        try:
            res = _resolve(role)
            if res.netdev and res.role.link == "up":
                link_down(role)
            set_mode(role, found["mode"])
        except Exception as e:
            fails.append(f"mode back to {found['mode']}: {e}")
    lk = found["link"]
    if lk is not None and not fails:
        try:
            if lk["up"]:
                link_up(role, lk["bitrate"], lk["restart_ms"])
            elif _resolve(role).role.link == "up":
                link_down(role)
        except Exception as e:
            fails.append(f"link back {'up' if lk['up'] else 'down'}: {e}")
    if fails:
        raise BenchLayerError(f"{role} not put back as found: " + "; ".join(fails))


def _link_matches(lk, bitrate, restart_ms):
    return bool(lk and lk["up"] and lk["bitrate"] == int(bitrate) and not lk["listen_only"]
                and (restart_ms is None or lk["restart_ms"] == int(restart_ms)))


def open_bench(role: str, bitrate: int | None = None, *, mode: str | None = None, hw_timestamps: bool = False,
               restore: bool = True, restart_ms: int | None = None, wait_s: float = WAIT_S, **kw):
    """Bring ``role`` to ``mode`` (or DEFAULT_MODES[role]), bring a SocketCAN link up at ``bitrate``, and open it
    by role. ``restore=True``: shutdown() (and a failed open) puts the role back as found. Extra keywords go to
    python-can (receive_own_messages, can_filters, auto_reset, ...), never a pick (refuse_picks)."""
    refuse_picks(kw, f"open_bench({role!r})")
    _check_name(role)
    guard(f"open_bench {role}")
    res = _resolve(role)
    kind = res.role.kind
    want = mode or DEFAULT_MODES.get(role)
    if want is None:
        raise BenchLayerError(f"open_bench({role!r}): no default mode for {role}; pass mode= "
                              f"({', '.join(MODES_BY_KIND.get(kind, ()))})")
    if want not in MODES_BY_KIND.get(kind, ()):
        raise BenchLayerError(f"open_bench({role!r}): {kind} has no mode {want!r} "
                              f"({', '.join(MODES_BY_KIND.get(kind, ()))})")
    if want == "socketcan" and bitrate is None and not (res.mode == "socketcan" and res.role.link == "up"):
        raise BenchLayerError(f"open_bench({role!r}): socketcan needs bitrate= to bring the link up")
    if want != "socketcan" and bitrate is None:
        raise BenchLayerError(f"open_bench({role!r}): mode {want} is set up when opened: give bitrate=")
    found = _found(res)
    changed = False
    try:
        if kind in ("gs_usb", "kvaser") and res.mode != want:
            if res.netdev and res.role.link == "up":
                link_down(role)
            set_mode(role, want, wait_s)
            changed = True
        if want == "socketcan" and bitrate is not None and not (
                not changed and _link_matches(found["link"], bitrate, restart_ms)):
            link_up(role, bitrate, restart_ms)
            changed = True
        bus = bench_roles.open_role(role, bitrate=None if want == "socketcan" else bitrate, modes=(want,),
                                    hw_timestamps=hw_timestamps,
                                    text=bench_roles.canctl_roles_text(CANCTL or bench_roles.CANCTL), **kw)
    except Exception as e:
        if changed:
            try:
                _put_back(role, found, want)
            except BenchLayerError as e2:
                raise BenchLayerError(f"open_bench({role!r}) failed: {e}; and {e2}") from e
        raise
    try:
        bus.bench_layer = {"role": role, "mode": want, "found": found, "restore": restore}
    except AttributeError:
        pass
    if restore and changed:
        _restore_on_shutdown(bus, role, found, want)
    return bus


def _restore_on_shutdown(bus, role, found, now_mode):
    orig = bus.shutdown
    state = {"done": False}

    def shutdown():
        if state["done"]:
            return
        state["done"] = True
        try:
            orig()
        finally:
            _put_back(role, found, now_mode)

    bus.shutdown = shutdown


def _is_socketcan(bus) -> bool:
    res = getattr(bus, "bench_role", None)
    if res is not None:
        return res.interface == "socketcan"
    return type(bus).__name__ in ("SocketcanBus", "SocketcanHwBus")


def bus_error_class(bus, can_id: int) -> bool:
    """For an error frame read from ``bus`` with arbitration_id ``can_id``: does it count as a bus error? On
    SocketCAN only a bus-error class counts; on any other backend every error frame does."""
    if not _is_socketcan(bus):
        return True
    return bool(can_id & BUS_ERROR_CLASSES)


def bus_error(bus, msg) -> bool:
    """Whether ``msg``, read from ``bus``, is an error frame that counts as a bus error."""
    return bool(msg.is_error_frame) and bus_error_class(bus, msg.arbitration_id)
