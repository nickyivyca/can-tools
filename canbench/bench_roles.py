"""Open a bench adapter by its canctl role: canmain, canchg, canx2, kvaser, pcanfd.

User ruling 2026-11-18 (canbus-esp-fw-vtrux ``shared/bench/SPEC.md`` section 8):
every tool that opens a bench adapter asks for it by its canctl role; turning a
role into a device (serial, USB bus and address, netdev, vendor channel) is
canctl's and canbench's job. No tool takes an adapter by an enumeration index,
a channel number or the first one of its kind it finds. Plan: reverse-it
``tools/plans/bench-adapter-roles.md``.

canctl's ``DONGLES`` table is the bench's only list of adapters, and this module
reads it only through ``canctl roles`` (read-only, no root): one ``key=value``
line per role saying where it is now, with no serials. From that:

* :func:`roles` -- every role as a :class:`Role`.
* :func:`resolve` -- one role: its kind, its mode as a caller sees it, what a
  caller needs to bring it up itself (``netdev``, ``busnum`` / ``devnum``), and
  the python-can arguments that open it. It needs no link up.
* :func:`open_role` -- an open python-can bus for a role. **It runs after the
  caller's ``canctl up``**: a netdev role whose link is down is refused, since
  bringing links up or down (and choosing when, e.g. gen-inhibit keeps the
  Kvaser down between checks because an UP link ACKs) is the caller's.
  canbench never brings a link up or down.
* :func:`tag_adapters` -- adds ``role:<role>`` to the ids of detected adapters
  (``receiver.detect_all_can_interfaces`` calls it), so the CLIs' ``--adapter
  BUS=role:<role>`` and ``--roles`` work.

Modes, as ``open_role(modes=...)`` names them:

=========  ==========  ======================================================
kind       mode        opened as
=========  ==========  ======================================================
gs_usb     socketcan   python-can ``socketcan`` on netdev ``<role>``
gs_usb     libusb      python-can ``gs_usb`` by USB bus and address; the device
                       must not be bound to the kernel's gs_usb driver
kvaser     socketcan   netdev ``kvaser`` on kvaser_usb: python-can ``socketcan``,
                       or :class:`canbench.live.socketcan_hw.SocketcanHwBus`
                       with ``hw_timestamps=True``
kvaser     linuxcan    python-can ``kvaser`` on the canlib channel of the one
                       Kvaser present (leaf; owner 2026-11-18: not forced on
                       anyone, it oopsed on 2026-11-13)
pcan       pcan        python-can ``pcan`` by the device id canctl reads from
                       the driver (``device_id``)
slcan      socketcan   netdev ``<role>`` once ``canctl slcan attach``-ed
slcan      tty         python-can ``slcan`` on the role's tty
=========  ==========  ======================================================

Refusals raise :class:`RoleError` with the reason: canctl missing or too old
(no ``roles``), a role not in its table, a device not attached, more than one
device matching a role (two serial-less Kvasers or PCANs), a mode the caller
did not allow, a netdev missing or its link down, a libusb role still bound to
gs_usb, a Kvaser on leaf with no single canlib channel, ``hw_timestamps`` on a
mode that is not a SocketCAN netdev, a bitrate missing where the device needs
one.
"""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field

CANCTL = os.environ.get("CANBENCH_CANCTL", "/usr/local/sbin/canctl")
FIELDS = ("role", "kind", "mode", "attached", "usb", "busnum", "devnum", "driver", "netdev", "link", "tty",
          "chardev", "devid")


class RoleError(RuntimeError):
    """A role cannot be resolved or opened as asked; the message says why."""


@dataclass
class Role:
    """One line of ``canctl roles``. ``None`` where canctl printed ``none`` (or ``-`` for mode)."""
    role: str
    kind: str
    mode: str | None
    attached: int
    usb: str | None = None
    busnum: int | None = None
    devnum: int | None = None
    driver: str | None = None
    netdev: str | None = None
    link: str | None = None
    tty: str | None = None
    chardev: str | None = None
    devid: int | None = None


@dataclass
class Resolved:
    """A role, where it is now, and how to open it (``interface`` + ``kwargs`` for ``can.Bus``)."""
    role: Role
    mode: str
    interface: str
    kwargs: dict = field(default_factory=dict)
    needs_link_up: bool = False

    @property
    def netdev(self):
        return self.role.netdev

    @property
    def busnum(self):
        return self.role.busnum

    @property
    def devnum(self):
        return self.role.devnum


def _none(v):
    return None if v in ("none", "-", "") else v


def _int(v):
    v = _none(v)
    return None if v is None else int(v)


def parse_roles(text: str) -> dict[str, Role]:
    """``canctl roles`` output -> {role: Role}."""
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        kv = dict(part.split("=", 1) for part in line.split() if "=" in part)
        missing = [f for f in FIELDS if f not in kv]
        if missing:
            raise RoleError(f"canctl roles: a line lacks {', '.join(missing)}: {line!r} (canctl too old or changed?)")
        out[kv["role"]] = Role(role=kv["role"], kind=kv["kind"], mode=_none(kv["mode"]),
                               attached=int(kv["attached"]), usb=_none(kv["usb"]), busnum=_int(kv["busnum"]),
                               devnum=_int(kv["devnum"]), driver=_none(kv["driver"]), netdev=_none(kv["netdev"]),
                               link=_none(kv["link"]), tty=_none(kv["tty"]), chardev=_none(kv["chardev"]),
                               devid=_int(kv["devid"]))
    return out


def canctl_roles_text(canctl: str = CANCTL) -> str:
    """Run ``canctl roles`` (no root needed). Raises RoleError if canctl is missing or has no ``roles``."""
    if not os.path.exists(canctl):
        raise RoleError(f"{canctl} not found: bench roles exist only on a bench host with canctl installed")
    try:
        p = subprocess.run([canctl, "roles"], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError) as e:
        raise RoleError(f"canctl roles failed: {e}") from e
    if p.returncode != 0:
        raise RoleError(f"canctl roles exited {p.returncode}: {(p.stderr or p.stdout).strip()[:200]} "
                        f"(an installed canctl older than 2026-11-18 has no roles command)")
    return p.stdout


def bench_available(canctl: str = CANCTL) -> bool:
    return os.path.exists(canctl)


def roles(text: str | None = None) -> dict[str, Role]:
    """Every role canctl knows, where it is now. ``text``: a ``canctl roles`` output (tests)."""
    return parse_roles(canctl_roles_text() if text is None else text)


def _kvaser_canlib_channels():
    """[(canlib channel, device name)] of non-virtual Kvaser channels, read without opening any."""
    import ctypes
    from can.interfaces.kvaser import canlib as k
    n = ctypes.c_int(0)
    k.canGetNumberOfChannels(ctypes.byref(n))
    out = []
    for ch in range(n.value):
        name = k.get_channel_info(ch)["device_name"]
        if "virtual" not in name.lower():
            out.append((ch, name))
    return out


def resolve(role: str, *, text: str | None = None, _kvaser_channels=None) -> Resolved:
    """Where ``role`` is now and how to open it (module doc). Needs no link up."""
    table = roles(text)
    r = table.get(role)
    if r is None:
        raise RoleError(f"{role!r} is not a canctl role (roles: {', '.join(table)})")
    if r.attached == 0:
        raise RoleError(f"{role} is not attached (no USB device matches it; canctl status says more)")
    if r.attached > 1:
        raise RoleError(f"{r.attached} USB devices match {role}; a {r.kind} role with no serial must be the only "
                        f"one of its kind on the bench, so canbench will not pick one")
    if r.kind == "gs_usb":
        if r.mode == "libusb":
            if r.driver == "gs_usb":
                raise RoleError(f"{role} is in libusb mode but still bound to the kernel's gs_usb driver "
                                f"(canctl status; udev-bind may not have run)")
            if r.busnum is None or r.devnum is None:
                raise RoleError(f"{role}: canctl gave no USB bus and address")
            return Resolved(r, "libusb", "gs_usb",
                            {"channel": f"gs_usb:{role}", "bus": r.busnum, "address": r.devnum})
        if r.netdev is None:
            raise RoleError(f"{role} is in socketcan mode but has no netdev (unbound, e.g. passed to an emulator?)")
        return Resolved(r, "socketcan", "socketcan", {"channel": r.netdev}, needs_link_up=True)
    if r.kind == "kvaser":
        if r.mode == "linuxcan":
            if r.driver != "leaf":
                raise RoleError(f"{role} is in linuxcan mode but its driver is {r.driver} (canctl status)")
            chans = (_kvaser_channels or _kvaser_canlib_channels)()
            if len(chans) != 1:
                raise RoleError(f"{role} on leaf: {len(chans)} non-virtual canlib channels "
                                f"({', '.join(n for _, n in chans) or 'none'}); want exactly 1")
            return Resolved(r, "linuxcan", "kvaser", {"channel": chans[0][0]})
        if r.netdev is None:
            raise RoleError(f"{role} is in socketcan mode but has no netdev (driver {r.driver}; canctl status)")
        return Resolved(r, "socketcan", "socketcan", {"channel": r.netdev}, needs_link_up=True)
    if r.kind == "pcan":
        if r.driver != "pcan" or r.devid is None:
            raise RoleError(f"{role}: no PCAN chardev for it (driver {r.driver}, devid {r.devid}); is pcan.ko loaded?")
        return Resolved(r, "pcan", "pcan", {"device_id": r.devid})
    if r.kind == "slcan":
        if r.netdev is not None:
            return Resolved(r, "socketcan", "socketcan", {"channel": r.netdev}, needs_link_up=True)
        if r.tty is None:
            raise RoleError(f"{role}: attached but no tty /dev/{role} and no netdev")
        return Resolved(r, "tty", "slcan", {"channel": r.tty})
    raise RoleError(f"{role}: kind {r.kind!r} unknown to canbench")


def open_role(role: str, *, bitrate: int | None = None, modes=None, hw_timestamps: bool = False,
              text: str | None = None, _kvaser_channels=None, **kw):
    """An open python-can bus for ``role`` (module doc). Runs after the caller's ``canctl up``."""
    import can
    res = resolve(role, text=text, _kvaser_channels=_kvaser_channels)
    if modes is not None and res.mode not in set(modes):
        raise RoleError(f"{role} is in mode {res.mode}; this caller takes {', '.join(sorted(set(modes)))}")
    if res.needs_link_up and res.role.link != "up":
        raise RoleError(f"{role}: netdev {res.netdev} is down. open_role runs after the caller's "
                        f"`canctl up {role} <bitrate>`; canbench does not bring links up")
    if hw_timestamps and res.interface != "socketcan":
        raise RoleError(f"{role}: hw_timestamps needs a SocketCAN netdev; {role} is in mode {res.mode}")
    args = dict(res.kwargs)
    args.update(kw)
    # receiver does this on import; a caller opening by role may never import it (Kvaser on linuxcan,
    # gs_usb over libusb: canbench.live.linux_fixes). Idempotent.
    from canbench.live import linux_fixes
    linux_fixes.apply()
    if res.interface == "socketcan":
        # the netdev's bitrate is canctl's (set at canctl up); nothing to pass
        if hw_timestamps:
            from canbench.live.socketcan_hw import SocketcanHwBus
            bus = SocketcanHwBus(args.pop("channel"), **args)
        else:
            bus = can.Bus(interface="socketcan", **args)
    else:
        if bitrate is None:
            raise RoleError(f"{role} in mode {res.mode} is set up when opened: give bitrate=")
        bus = can.Bus(interface=res.interface, bitrate=bitrate, **args)
    if res.interface == "gs_usb":
        _release_on_shutdown(bus)
    try:
        bus.bench_role = res
    except AttributeError:
        pass
    return bus


def _release_on_shutdown(bus):
    """Make ``bus.shutdown()`` also dispose the pyusb handle of a libusb gs_usb role.

    python-can's GsUsbBus.shutdown() stops the device but keeps it claimed until the process exits, so
    canctl cannot move the role (``canctl mode <role> socketcan`` fails "bind failed") and the next open of it
    in this process fails. Found in the step 3 bench check, 2026-11-18.
    """
    from canbench.live.receiver import release_gs_usb
    orig = bus.shutdown

    def shutdown():
        try:
            orig()
        finally:
            release_gs_usb(bus)

    bus.shutdown = shutdown


def tag_adapters(adapters, text: str | None = None, _kvaser_channels=None):
    """Add ``role:<role>`` to the ids of each detected adapter a role resolves to; returns the adapters.

    Matched by what canctl reports, never by position: a SocketCAN adapter by netdev name, a libusb
    gs_usb adapter by USB bus and address, a PCAN by ``pcan-id:<devid>``, a canlib Kvaser by the
    resolved canlib channel, an slcan adapter by its tty. Does nothing off a bench (no canctl).
    """
    if text is None and not bench_available():
        return adapters
    try:
        table = roles(text)
    except RoleError:
        return adapters
    for name in table:
        try:
            res = resolve(name, text=text if text is not None else _fmt(table), _kvaser_channels=_kvaser_channels)
        except RoleError:
            continue
        for a in adapters:
            iface, ch = a[0], a[1]
            hit = False
            if res.interface == "socketcan" and iface == "socketcan" and ch == res.netdev:
                hit = True
            elif res.interface == "gs_usb" and iface == "gs_usb" and isinstance(ch, dict) \
                    and ch.get("bus") == res.busnum and ch.get("address") == res.devnum:
                hit = True
            elif res.interface == "pcan" and iface == "pcan" and f"pcan-id:{res.role.devid}" in getattr(a, "ids", ()):
                hit = True
            elif res.interface == "kvaser" and iface == "kvaser" and ch == res.kwargs.get("channel"):
                hit = True
            elif res.interface == "slcan" and iface == "slcan" and ch == res.kwargs.get("channel"):
                hit = True
            if hit and f"role:{name}" not in a.ids:
                a.ids = a.ids + (f"role:{name}",)
    return adapters


def _fmt(table: dict[str, Role]) -> str:
    """A Role table back to ``canctl roles`` text, so one canctl run serves every resolve."""
    lines = []
    for r in table.values():
        vals = {"role": r.role, "kind": r.kind, "mode": r.mode or "-", "attached": r.attached}
        for f in FIELDS[4:]:
            v = getattr(r, f)
            vals[f] = "none" if v is None else v
        lines.append(" ".join(f"{f}={vals[f]}" for f in FIELDS))
    return "\n".join(lines)


def role_mismatch(key: str, text: str | None = None) -> str | None:
    """Why ``role:<name>`` matched no detected adapter (for the CLIs' refusals), or None for other keys."""
    k = key.strip()
    if not k.lower().startswith("role:"):
        return None
    name = k[5:]
    try:
        res = resolve(name, text=text)
    except RoleError as e:
        return str(e)
    if res.needs_link_up and res.role.link != "up":
        return (f"{name}: netdev {res.netdev} is down; bring it up first (`sudo -n /usr/local/sbin/canctl up "
                f"{name} <bitrate>`)")
    return f"{name} resolves to {res.interface} {res.kwargs} (mode {res.mode}), but detection did not find it there"
