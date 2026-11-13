"""Put canbench's PCAN and Kvaser bus classes behind python-can's interface names.

After :func:`install`, ``can.Bus(interface="pcan", ...)`` builds
``canbench.live.pcan_bus.CanbenchPcanBus`` and ``interface="kvaser"`` builds
``canbench.live.kvaser_bus.CanbenchKvaserBus``, wherever the bus is opened.
Both report errors in the Linux ``can/error.h`` layout (``canbench.errors``);
the PCAN one also recovers from bus-off itself.

It is explicit, not a side effect of importing canbench: can_logger,
can_replayer and can_gui call it at start-up. Other users of canbench (the ESP
harnesses that pin it) keep python-can's classes unless they call it.
"""
from __future__ import annotations

from can.interfaces import BACKENDS

_OURS = {
    "pcan": ("canbench.live.pcan_bus", "CanbenchPcanBus"),
    "kvaser": ("canbench.live.kvaser_bus", "CanbenchKvaserBus"),
}
_ORIGINAL = {name: BACKENDS.get(name) for name in _OURS}


def install() -> None:
    """Select canbench's classes for ``pcan`` and ``kvaser``."""
    BACKENDS.update(_OURS)


def uninstall() -> None:
    """Restore python-can's classes (tests)."""
    for name, entry in _ORIGINAL.items():
        if entry is None:
            BACKENDS.pop(name, None)
        else:
            BACKENDS[name] = entry


def installed() -> bool:
    return all(BACKENDS.get(k) == v for k, v in _OURS.items())
