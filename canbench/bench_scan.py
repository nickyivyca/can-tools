"""The adapter-open scan: a check each project runs from its own test, on its own tree.

Asked for by both ESP reviewers (2026-11-19; reverse-it ``tools/plans/bench-role-layer.md``), merging the two scans
they had (gen-inhibit ``tools/bench/test_kvaser_bus.py``, charge-interposer ``adapter_roles_test.py``). It parses
Python files (``ast``; nothing is imported or run) and reports each place that opens a real adapter outside the
bench-role layer, or asks for one by something other than its canctl role (shared/bench SPEC section 8):

=================  ==========================================================================================
kind               what
=================  ==========================================================================================
``bus-open``       ``Bus(...)`` / ``ThreadSafeBus(...)`` (any receiver: ``can.Bus``, ``can.interface.Bus``) whose
                   interface is a real adapter's, named at run time, passed by position, missing (python-can then
                   reads its config), or hidden in ``**kwargs``. Only ``virtual`` and ``udp_multicast`` pass.
``adapter-class``  a python-can or canbench adapter class or detection named at all (``KvaserBus``, ``GsUsbBus``,
                   ``PcanBus``, ``SocketcanBus``, ``SlcanBus``, ``IXXATBus``, ``SocketcanHwBus``,
                   ``CanbenchPcanBus``, ``CanbenchKvaserBus``, ``detect_all_can_interfaces``,
                   ``detect_available_configs``), including in an import.
``usb-scan``       ``GsUsb.scan(...)`` (pyusb enumeration: the first Innomaker found).
``pcan-channel``   a string ``PCAN_USBBUS<n>`` (PCAN-Basic's n-th PCAN found, not the role pcanfd).
``role-pick``      a role call (``open_role``, ``resolve``, ``open_bench``, ``link_up``, ``link_down``, ``set_mode``,
                   ``to_libusb``, ``to_socketcan``) given ``channel``, ``index``, ``bus``, ``address``,
                   ``device_id``, ``serial`` or ``interface``; and, except for the common names ``resolve`` and
                   ``set_mode``, a role passed as a number or a literal name that is not a canctl role.
=================  ==========================================================================================

It reports; it does not decide. Each project compares the hits with its own allow list (its layer's own opener,
its tests' fakes) and fails on anything else. :func:`self_check` shows each kind is caught on a snippet, so a
project's test can show its scan able to fail.
"""
from __future__ import annotations

import ast
import os
import re
from dataclasses import dataclass

ROLE_NAMES = ("canmain", "canchg", "canx2", "kvaser", "pcanfd")
BUS_NAMES = {"Bus", "ThreadSafeBus"}
VIRTUAL = {"virtual", "udp_multicast"}
ADAPTER_NAMES = {"KvaserBus", "GsUsbBus", "PcanBus", "SocketcanBus", "SlcanBus", "IXXATBus", "SocketcanHwBus",
                 "CanbenchPcanBus", "CanbenchKvaserBus", "detect_all_can_interfaces", "detect_available_configs"}
ROLE_FNS = {"open_role", "resolve", "open_bench", "link_up", "link_down", "set_mode", "to_libusb", "to_socketcan"}
# Names common enough elsewhere (a board resolver's resolve("x2"), a device's set_mode(1)) that a literal argument
# says nothing: only a pick keyword is flagged on these.
GENERIC_FNS = {"resolve", "set_mode"}
PICK_KW = {"channel", "index", "bus", "address", "device_id", "serial", "interface"}
PCAN_CHANNEL_RX = re.compile(r"PCAN_USBBUS\d+\Z")


@dataclass(frozen=True)
class Hit:
    path: str
    line: int
    kind: str
    why: str

    def __str__(self):
        return f"{self.path}:{self.line} [{self.kind}] {self.why}"


def _name(f):
    return f.attr if isinstance(f, ast.Attribute) else f.id if isinstance(f, ast.Name) else None


def _bus_open(n):
    iface = [k for k in n.keywords if k.arg == "interface"]
    if any(k.arg is None for k in n.keywords):
        return "passes **kwargs (the interface may hide there)"
    if len(n.args) >= 2:
        return "passes the interface by position"
    if not iface:
        return "passes no interface= (python-can then reads its config)"
    v = iface[0].value
    if not (isinstance(v, ast.Constant) and isinstance(v.value, str)):
        return "names its interface at run time"
    if v.value not in VIRTUAL:
        return f"opens interface {v.value!r} directly, outside the bench-role layer"
    return None


def _role_pick(n, fn):
    bad = sorted(k.arg for k in n.keywords if k.arg in PICK_KW)
    if bad:
        return f"{fn}() given {', '.join(bad)}"
    if fn in GENERIC_FNS:
        return None
    role = n.args[0] if n.args else next((k.value for k in n.keywords if k.arg == "role"), None)
    if isinstance(role, ast.Constant):
        if isinstance(role.value, (int, float)) and not isinstance(role.value, bool):
            return f"{fn}() given the number {role.value!r} for a role"
        if isinstance(role.value, str) and role.value not in ROLE_NAMES:
            return f"{fn}() given {role.value!r}, not a canctl role ({', '.join(ROLE_NAMES)})"
    return None


def scan_source(text: str, path: str = "<source>") -> list[Hit]:
    """Every hit in one file's source."""
    tree = ast.parse(text, path)
    hits = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Call):
            fn = _name(n.func)
            if fn in BUS_NAMES:
                why = _bus_open(n)
                if why:
                    hits.append(Hit(path, n.lineno, "bus-open", why))
            elif fn == "scan" and isinstance(n.func, ast.Attribute) and _name(n.func.value) == "GsUsb":
                hits.append(Hit(path, n.lineno, "usb-scan", "GsUsb.scan() enumerates Innomakers (first found)"))
            if fn in ROLE_FNS and (n.args or n.keywords):
                why = _role_pick(n, fn)
                if why:
                    hits.append(Hit(path, n.lineno, "role-pick", why))
        elif isinstance(n, ast.Name) and n.id in ADAPTER_NAMES:
            hits.append(Hit(path, n.lineno, "adapter-class", f"names {n.id}"))
        elif isinstance(n, ast.Attribute) and n.attr in ADAPTER_NAMES:
            hits.append(Hit(path, n.lineno, "adapter-class", f"names {n.attr}"))
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            for a in n.names:
                if a.name.split(".")[-1] in ADAPTER_NAMES:
                    hits.append(Hit(path, n.lineno, "adapter-class", f"imports {a.name}"))
        elif isinstance(n, ast.Constant) and isinstance(n.value, str) and PCAN_CHANNEL_RX.match(n.value):
            hits.append(Hit(path, n.lineno, "pcan-channel",
                            f"names PCAN channel {n.value} (the n-th PCAN found, not the role pcanfd)"))
    return sorted(hits, key=lambda h: (h.line, h.kind, h.why))


def scan_paths(paths, root: str | None = None) -> list[Hit]:
    """Every hit in the given .py files (paths reported relative to ``root`` when given)."""
    out = []
    for p in paths:
        with open(p, encoding="utf-8") as f:
            text = f.read()
        rel = os.path.relpath(p, root).replace(os.sep, "/") if root else p
        out += scan_source(text, rel)
    return out


def py_files(root: str, skip_dirs=("__pycache__", ".git")) -> list[str]:
    """Every .py file under ``root``, sorted, skipping ``skip_dirs`` (names or paths relative to root)."""
    out = []
    for dp, dn, fn in os.walk(root):
        rel = os.path.relpath(dp, root).replace(os.sep, "/")
        dn[:] = sorted(d for d in dn if d not in skip_dirs and (d if rel == "." else f"{rel}/{d}") not in skip_dirs)
        out += [os.path.join(dp, f) for f in sorted(fn) if f.endswith(".py")]
    return out


# One snippet per form, each of which must be caught (kind), and clean snippets that must not be.
SNIPPETS = [
    ("bus-open", "'kvaser' directly", "import can\nb = can.Bus(interface='kvaser', channel=0, bitrate=500000)\n"),
    ("bus-open", "'gs_usb' directly",
     "import can\nb = can.interface.Bus(interface='gs_usb', channel='x', index=0, bitrate=500000)\n"),
    ("bus-open", "'pcan' directly", "import can\nb = can.ThreadSafeBus(interface='pcan', channel='x', bitrate=500000)\n"),
    ("bus-open", "'socketcan' directly", "import can\nb = can.Bus(interface='socketcan', channel='kvaser')\n"),
    ("bus-open", "at run time", "import can\nb = can.Bus(interface=iface, channel=ch)\n"),
    ("bus-open", "by position", "import can\nb = can.Bus('kvaser0', 'socketcan')\n"),
    ("bus-open", "no interface=", "import can\nb = can.Bus(channel='can0')\n"),
    ("bus-open", "**kwargs", "import can\nb = can.Bus(**kw)\n"),
    ("adapter-class", "imports PcanBus", "from can.interfaces.pcan import PcanBus\n"),
    ("adapter-class", "names GsUsbBus", "import can\nb = can.interfaces.gs_usb.GsUsbBus(channel=0, bitrate=500000)\n"),
    ("adapter-class", "imports detect_all_can_interfaces",
     "from canbench.live.receiver import detect_all_can_interfaces\n"),
    ("adapter-class", "names SocketcanHwBus",
     "from canbench.live import socketcan_hw\nb = socketcan_hw.SocketcanHwBus('kvaser')\n"),
    ("usb-scan", "GsUsb.scan", "from gs_usb.gs_usb import GsUsb\ndevs = GsUsb.scan()\n"),
    ("pcan-channel", "names PCAN channel", "CH = 'PCAN_USBBUS1'\n"),
    ("role-pick", "given device_id",
     "from canbench import bench_roles\nb = bench_roles.open_role('pcanfd', bitrate=500000, device_id=0)\n"),
    ("role-pick", "given index", "from canbench import bench_layer\nb = bench_layer.open_bench('canchg', 500000, index=1)\n"),
    ("role-pick", "the number 0", "from canbench import bench_layer\nb = bench_layer.open_bench(0, 500000)\n"),
    ("role-pick", "'can0', not a canctl role", "from canbench import bench_layer\nbench_layer.link_up('can0', 500000)\n"),
    ("role-pick", "given serial", "from canbench import bench_roles\nr = bench_roles.resolve('canmain', serial='X')\n"),
]
CLEAN = [
    "import can\nb = can.Bus(interface='virtual', channel='t')\n",
    "from canbench.buses import open_bus\nb = open_bus('udp_multicast', channel='239.0.0.1')\n",
    "import can\nb = can.Bus(interface='udp_multicast', channel='239.0.0.1')\n",
    "from canbench import bench_layer\nb = bench_layer.open_bench('kvaser', 500000, hw_timestamps=True)\n",
    "from canbench import bench_roles\nb = bench_roles.open_role(role, bitrate=500000)\n",
    "from canbench import bench_layer\nbench_layer.link_down('canmain')\n",
    "board = boards.resolve('x2')\n",
    "dev.set_mode(1)\n",
]


def self_check() -> list[str]:
    """[] when every snippet is caught as its kind and every clean snippet passes; otherwise what went wrong."""
    wrong = []
    for kind, word, src in SNIPPETS:
        hits = scan_source(src)
        if not any(h.kind == kind and word in h.why for h in hits):
            wrong.append(f"not caught as {kind} ({word}): {src.strip()!r} (got {[str(h) for h in hits]})")
    for src in CLEAN:
        hits = scan_source(src)
        if hits:
            wrong.append(f"clean snippet flagged: {src.strip()!r}: {[str(h) for h in hits]}")
    return wrong
