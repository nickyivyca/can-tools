"""Uniform CAN bus opening, including hardware-free software buses.

Wraps ``canbench.live.receiver.detect_all_can_interfaces`` and adds two software
buses so the logger/GUI can run with no dongle attached:

  - ``virtual``        in-process (python-can VirtualBus); same process only.
                       Used by unit tests.
  - ``udp_multicast``  a localhost "network CAN" that works across processes.
                       Used by cross-process test suites and for GUI demos.

Interfaces are the same ``(interface, channel_spec, description)`` shape that the
hardware detector returns, so callers treat hardware and software uniformly.
:func:`open_bus` knows that software buses take no ``bitrate``.
"""
from __future__ import annotations

import can

from .live.receiver import detect_all_can_interfaces, release_gs_usb

VIRTUAL_CHANNEL = "canbench-virtual"
UDP_MULTICAST_GROUP = "239.99.0.1"

# Software interfaces are not real CAN hardware and reject a bitrate kwarg.
SOFTWARE_INTERFACES = {"virtual", "udp_multicast"}


def software_interfaces():
    """The always-available hardware-free interfaces (for selectors/tests)."""
    return [
        ("virtual", VIRTUAL_CHANNEL, "Virtual (in-process)"),
        ("udp_multicast", UDP_MULTICAST_GROUP, "Virtual (localhost UDP)"),
    ]


def list_interfaces(bitrate, brands=None, include_software=True):
    """Detected hardware interfaces, optionally followed by the software ones."""
    ifaces = detect_all_can_interfaces(bitrate, brands=brands)
    if include_software:
        ifaces = ifaces + software_interfaces()
    return ifaces


def shutdown_bus(bus):
    """Shut down a bus and fully release it (incl. the gs_usb pyusb handle)."""
    try:
        bus.shutdown()
    except Exception:
        pass
    release_gs_usb(bus)


def open_bus(iface, ch, bitrate, **extra):
    """Open one interface as a live ``can.Bus``.

    ``ch`` is either a plain channel value or a dict of kwargs (as ixxat uses).
    ``bitrate`` is passed only for real hardware; software buses reject it.
    ``extra`` kwargs (e.g. ``receive_own_messages``) are forwarded as-is.
    """
    kwargs = dict(extra)
    if iface not in SOFTWARE_INTERFACES:
        kwargs["bitrate"] = bitrate
    if isinstance(ch, dict):
        return can.interface.Bus(interface=iface, **ch, **kwargs)
    return can.interface.Bus(interface=iface, channel=ch, **kwargs)
