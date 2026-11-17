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

    A ``udp_multicast`` bus is always opened with ``hop_limit=0`` and checked
    by :func:`confine_to_host` before it is returned; any other hop limit is
    refused. Every bus can-tools opens comes through here.
    """
    kwargs = dict(extra)
    if iface not in SOFTWARE_INTERFACES:
        kwargs["bitrate"] = bitrate
    if iface == "udp_multicast":
        if kwargs.setdefault("hop_limit", 0) != 0:
            raise ValueError(f"udp_multicast hop_limit {kwargs['hop_limit']!r} refused: only 0 keeps "
                             "frames on this host")
    if isinstance(ch, dict):
        bus = can.interface.Bus(interface=iface, **ch, **kwargs)
    else:
        bus = can.interface.Bus(interface=iface, channel=ch, **kwargs)
    if iface == "udp_multicast":
        try:
            confine_to_host(bus)
        except Exception:
            shutdown_bus(bus)
            raise
    return bus


def confine_to_host(bus):
    """Check that a ``udp_multicast`` bus cannot put frames on the network.

    python-can's default hop limit is 1, and the kernel routes 239.0.0.0/8 out
    the default interface, so a localhost bus opened with defaults transmits
    every frame on the LAN: on 2026-09-01 that flooded the house network at
    66,450 packets/s (reverse-it ``simulators.md``). This sets the socket's
    multicast TTL (IPv4) or hop limit (IPv6) to 0, reads it back, and raises
    RuntimeError if the socket cannot be reached or does not read 0. The bus
    joins its group when it opens, which a TTL-0 sender also needs to stay off
    the wire (measured 2026-11-12, ``simulators.md``). It does not pin the
    outgoing interface: python-can joins the group on the default interface, and
    TTL 0 alone keeps the datagrams local.
    """
    import socket
    import struct

    mc = getattr(bus, "_multicast", None)
    sock = getattr(mc, "_socket", None)
    if sock is None:
        raise RuntimeError("udp_multicast: cannot reach the socket to check its hop limit; refusing, "
                           "since frames may go on the network")
    if sock.family == socket.AF_INET6:
        level, opt = socket.IPPROTO_IPV6, socket.IPV6_MULTICAST_HOPS
    else:
        level, opt = socket.IPPROTO_IP, socket.IP_MULTICAST_TTL
    sock.setsockopt(level, opt, struct.pack("@I", 0))
    got = sock.getsockopt(level, opt)
    if got != 0:
        raise RuntimeError(f"udp_multicast: hop limit reads {got}, not 0; refusing, since frames would go "
                           "on the network")
    return got


UDP_MULTICAST_PORT = 43113     # python-can's default


def udp_adapters(specs):
    """``udp_multicast`` buses from ``GROUP`` or ``GROUP:PORT`` specs (IPv4), shaped like
    detected adapters (``receiver.Adapter``), each with the ids ``udp:GROUP:PORT`` and
    ``udp:GROUP`` for ``--adapter``. The port defaults to python-can's 43113.

    Raises ValueError if two buses share a port: python-can binds every socket to
    ``("", port)``, and Linux then hands each socket on that port the datagrams of every
    group joined on the host, so two groups on one port hear each other's frames
    (measured 2026-11-16 on Linux: a frame sent to 239.99.0.231 was received by a
    bus on 239.99.0.232 at the same port, and not by one on another port).
    """
    from .live.receiver import Adapter
    out, ports = [], {}
    for spec in specs:
        group, sep, port = spec.strip().partition(":")
        try:
            port = int(port) if sep else UDP_MULTICAST_PORT
        except ValueError:
            raise ValueError(f"udp_multicast {spec!r}: expected GROUP or GROUP:PORT") from None
        if port in ports:
            raise ValueError(f"udp_multicast {spec!r}: port {port} is already used by {ports[port]!r}; "
                             "buses on one port hear each other's frames, so give each its own port")
        ports[port] = spec
        out.append(Adapter("udp_multicast", {"channel": group, "port": port},
                           f"Network CAN {group}:{port} (localhost)", ids=(f"udp:{group}:{port}", f"udp:{group}")))
    return out
