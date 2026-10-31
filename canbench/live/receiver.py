"""CAN hardware interface: bus detection, per-bus receive threads, USB reset.

This module is shared by can_logger.py and can_monitor.py so neither duplicates
the hardware plumbing.

Public API:
    detect_all_can_interfaces(bitrate, brands=None) -> list of Adapter
        (unpacks as (iface, ch_spec, desc); .ids names the physical adapter)
    adapter_matches(adapter, key) -> bool, for --adapter
    socketcan_interfaces() -> list of (name, driver, is_up), Linux only
    CANBusLogger(bus_id, bus, message_queue, iface, ch, bitrate)
"""

import can
import logging
import os
import re
import subprocess
import threading
import time
from collections import namedtuple

logger = logging.getLogger(__name__)


_gs_usb_libusb_ready = None


def _ensure_gs_usb_libusb():
    """Make a libusb-1.0 backend available to pyusb for the gs_usb interface.

    The python-can gs_usb backend uses pyusb's default libusb backend. On
    Windows pyusb cannot locate a libusb-1.0.dll unless it is on the DLL search
    path, so we inject the copy bundled with the ``libusb-package`` wheel. The
    Innomaker / candleLight device itself needs no vendor driver: Windows
    auto-binds WinUSB to it via its WCID (MS OS) descriptors.

    Returns True if a usable libusb backend is present (gs_usb can be used),
    False otherwise (gs_usb detection is skipped). Result is cached.
    """
    global _gs_usb_libusb_ready
    if _gs_usb_libusb_ready is not None:
        return _gs_usb_libusb_ready
    try:
        import libusb_package
        dlldir = os.path.dirname(libusb_package.get_library_path())
        if hasattr(os, "add_dll_directory"):
            os.add_dll_directory(dlldir)
        os.environ["PATH"] = dlldir + os.pathsep + os.environ.get("PATH", "")
    except Exception as e:
        logger.debug(f"libusb-package not available for gs_usb: {e}")
    try:
        import usb.backend.libusb1
        _gs_usb_libusb_ready = usb.backend.libusb1.get_backend() is not None
    except Exception as e:
        logger.debug(f"gs_usb libusb backend unavailable: {e}")
        _gs_usb_libusb_ready = False
    return _gs_usb_libusb_ready


def reset_gs_usb_cache():
    """Forget the cached gs_usb libusb-backend result so detection re-probes it.

    Call before re-running detection (e.g. a GUI 'Refresh') so a transient
    first-time failure can recover without restarting the process.
    """
    global _gs_usb_libusb_ready
    _gs_usb_libusb_ready = None


def gs_usb_backend_available():
    """True if the gs_usb (Innomaker) libusb backend is usable in this process."""
    return _ensure_gs_usb_libusb()

def release_gs_usb(bus):
    """Fully release a gs_usb (Innomaker) device so it can be reopened in-process.

    python-can's ``GsUsbBus.shutdown()`` stops the device but does not dispose the
    pyusb handle, so a second open of the same device in one process fails with
    ``USBError: Access denied``. Disposing the underlying pyusb resources frees it.
    No-op for every other backend. Call this after ``bus.shutdown()``.
    """
    dev = getattr(getattr(bus, "gs_usb", None), "gs_usb", None)
    if dev is None:
        return
    try:
        import usb.util
        usb.util.dispose_resources(dev)
    except Exception as e:
        logger.debug(f"gs_usb resource dispose failed: {e}")


# --- ixxat USB reset coordination ---
# Multiple bus threads share one physical USB reset; use a lock + cooldown so
# only one thread performs the reset while others wait and then skip.
_ixxat_reset_lock = threading.Lock()
_ixxat_last_reset_time = 0.0
_IXXAT_RESET_COOLDOWN_S = 15.0
_IXXAT_RE_ENUM_WAIT_S   = 4.0    # time to wait after USB re-enable for device to enumerate


def _usb_reset_ixxat(bus_id, hwid=None):
    """Power-cycle a specific ixxat USB device via Windows Device Manager.

    hwid: unique_hardware_id from the ixxat channel spec (e.g. 'HW379841').
          Targets only that dongle so other connected ixxat devices are unaffected.
          If None, resets all VID_08D8 devices as a fallback.

    ixxat VCI4 devices enumerate as VID 08D8 (HMS Industrial Networks) with the
    hwid as the final path component, e.g. USB\\VID_08D8&PID_0008\\HW379841.

    Uses a module-level lock + cooldown so concurrent bus threads don't pile on.
    Requires administrator privileges.
    """
    global _ixxat_last_reset_time

    with _ixxat_reset_lock:
        now = time.time()
        if now - _ixxat_last_reset_time < _IXXAT_RESET_COOLDOWN_S:
            logger.info(f"Bus {bus_id}: USB reset recently completed, skipping duplicate reset")
            return True

        if hwid:
            match = f"$_.InstanceId -like 'USB\\VID_08D8*\\{hwid}'"
            label = hwid
        else:
            match = "$_.InstanceId -like 'USB\\VID_08D8*'"
            label = "all ixxat devices"

        logger.info(f"Bus {bus_id}: triggering USB hardware reset for {label}...")
        ps = (
            f"$devs = Get-PnpDevice -PresentOnly | Where-Object {{ {match} }}; "
            "if ($devs) { "
            "    $devs | Disable-PnpDevice -Confirm:$false; "
            "    Start-Sleep -Seconds 2; "
            f"    Get-PnpDevice | Where-Object {{ {match} }} | Enable-PnpDevice -Confirm:$false; "
            "    Write-Output 'reset_ok' "
            "} else { Write-Output 'not_found' }"
        )
        try:
            result = subprocess.run(
                ['powershell', '-NonInteractive', '-Command', ps],
                capture_output=True, text=True, timeout=20
            )
            if 'reset_ok' in result.stdout:
                _ixxat_last_reset_time = time.time()
                logger.info(f"Bus {bus_id}: USB hardware reset completed for {label}")
                return True
            else:
                logger.warning(
                    f"Bus {bus_id}: PnP device not found for {label} — "
                    f"ensure script runs as administrator"
                )
                return False
        except Exception as e:
            logger.warning(f"Bus {bus_id}: USB reset error: {e}")
            return False


class CANBusLogger:
    """Logs CAN messages from a single bus to a shared queue.

    The queue receives 4-tuples: (timestamp_s, bus_id, msg, direction)
    where direction is "Rx". Callers that also inject Tx frames (e.g.
    DiagnosticReader) should use "Tx".
    """

    def __init__(self, bus_id, bus, message_queue, iface, ch, bitrate):
        self.bus_id = bus_id
        self.bus = bus
        self.message_queue = message_queue
        self.running = False
        self.thread = None
        self.start_time = None
        self.iface = iface
        self.ch = ch
        self.bitrate = bitrate

    def start(self):
        """Start logging from this bus in a background thread."""
        self.running = True
        self.start_time = time.time()
        self.thread = threading.Thread(target=self._receive_loop, daemon=True)
        self.thread.start()
        logger.info(f"Bus {self.bus_id} logging started")

    def _receive_loop(self):
        """Continuously receive messages and queue them."""
        while self.running:
            try:
                msg = self.bus.recv(timeout=0.1)
                if msg is not None:
                    timestamp = time.time() - self.start_time
                    self.message_queue.put((timestamp, self.bus_id, msg, "Rx"))
            except Exception as e:
                if self.running:
                    if 'overrun' in str(e).lower():
                        logger.warning(f"Bus {self.bus_id} data overrun — attempting hardware reset...")
                        self._reconnect()
                    else:
                        logger.error(f"Bus {self.bus_id} receive error: {e}")

    def _reconnect(self, max_open_attempts=5):
        """USB-reset the ixxat dongle then reopen the bus."""
        try:
            self.bus.shutdown()
        except Exception:
            pass

        if self.iface == 'ixxat':
            hwid = self.ch.get('unique_hardware_id') if isinstance(self.ch, dict) else None
            _usb_reset_ixxat(self.bus_id, hwid=hwid)
            logger.info(f"Bus {self.bus_id}: waiting {_IXXAT_RE_ENUM_WAIT_S:.0f}s for USB re-enumeration...")
            time.sleep(_IXXAT_RE_ENUM_WAIT_S)

        for attempt in range(1, max_open_attempts + 1):
            logger.info(f"Bus {self.bus_id}: open attempt {attempt}/{max_open_attempts}...")
            try:
                if isinstance(self.ch, dict):
                    new_bus = can.interface.Bus(interface=self.iface, bitrate=self.bitrate, **self.ch)
                else:
                    new_bus = can.interface.Bus(interface=self.iface, channel=self.ch, bitrate=self.bitrate)
                self.bus = new_bus
                logger.info(f"Bus {self.bus_id}: reconnected successfully")
                return
            except Exception as e:
                logger.warning(f"Bus {self.bus_id}: open attempt {attempt} failed: {e}")
                time.sleep(2.0)

        logger.error(
            f"Bus {self.bus_id}: failed to reconnect after {max_open_attempts} attempts — "
            f"will retry on next overrun"
        )

    def stop(self):
        """Stop logging from this bus."""
        self.running = False
        if self.thread:
            self.thread.join(timeout=2.0)
        try:
            self.bus.shutdown()
        except Exception:
            pass
        release_gs_usb(self.bus)
        logger.info(f"Bus {self.bus_id} logging stopped")


# Linux SocketCAN driver -> the brand name --brands uses for the same adapter elsewhere
_SOCKETCAN_DRIVER_BRAND = {
    'gs_usb': 'gs_usb',          # Innomaker USB2CAN / candleLight
    'ix_usb_can': 'ixxat',       # HMS out-of-tree IXXAT driver
    'kvaser_usb': 'kvaser',
    'peak_usb': 'pcan',
}
_ARPHRD_CAN = '280'
_IFF_UP = 0x1


class Adapter(namedtuple('Adapter', 'interface channel desc')):
    """One detected adapter. Unpacks as ``(interface, channel_spec, desc)``.

    ``ids`` are the names ``--adapter`` matches. ``serial:<s>`` (the USB serial,
    IXXAT hardware id or Kvaser S/N) and ``usb:<port path>`` (SocketCAN) name a
    physical adapter and survive replugging. ``<interface>:<channel>`` (canN,
    PCAN_USBBUSn, a Kvaser channel number, a libusb scan index) follows plug or
    driver order and only says which slot the adapter is in today.
    ``pcan-id:<n>`` is the device number set with PEAK's tools.
    """

    def __new__(cls, interface, channel, desc, ids=()):
        self = super().__new__(cls, interface, channel, desc)
        self.ids = tuple(ids)
        return self


def adapter_matches(adapter, key):
    """True if ``key`` names ``adapter``: one of its ids, or a bare serial."""
    k = key.strip().lower()
    ids = {i.lower() for i in getattr(adapter, 'ids', ())}
    return k in ids or f'serial:{k}' in ids


def _with_ids(desc, ids):
    """desc plus the ids that name the physical adapter, for consoles and logs."""
    phys = [i for i in ids if i.startswith(('serial:', 'usb:', 'pcan-id:'))]
    return f"{desc} [{', '.join(phys)}]" if phys else desc


def _sysfs_usb_identity(root, name):
    """(serial, USB port path) of the USB device behind a SocketCAN interface.

    Either is None when sysfs does not give it (vcan, a non-USB adapter, a
    device without a serial string).
    """
    try:
        usb = os.path.dirname(os.path.realpath(os.path.join(root, name, 'device')))
    except OSError:
        return None, None
    if not os.path.exists(os.path.join(usb, 'busnum')):
        return None, None
    serial = None
    try:
        with open(os.path.join(usb, 'serial')) as fh:
            serial = fh.read().strip() or None
    except OSError:
        pass
    return serial, os.path.basename(usb)


def socketcan_interfaces(root='/sys/class/net'):
    """SocketCAN interfaces on this host as (name, driver, is_up), by content.

    Read from ``/sys/class/net``: every interface of link type CAN, with the
    kernel driver behind it (``None`` for vcan) and whether it is up. ``canN``
    names follow plug order, so an adapter is identified by its driver, never
    by its name. Empty where there is no ``/sys/class/net`` (not Linux).
    """
    out = []
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return out
    for name in names:
        d = os.path.join(root, name)
        try:
            with open(os.path.join(d, 'type')) as fh:
                if fh.read().strip() != _ARPHRD_CAN:
                    continue
            with open(os.path.join(d, 'flags')) as fh:
                is_up = bool(int(fh.read().strip(), 16) & _IFF_UP)
        except (OSError, ValueError):
            continue
        drv = os.path.join(d, 'device', 'driver')
        driver = os.path.basename(os.readlink(drv)) if os.path.islink(drv) else None
        out.append((name, driver, is_up))
    return out


def _gs_usb_serial(dev):
    """USB serial string of a gs_usb scan result, or None."""
    for obj in (dev, getattr(dev, 'gs_usb', None)):
        try:
            sn = getattr(obj, 'serial_number', None)
        except Exception:
            sn = None
        if sn:
            return str(sn).strip()
    return None


def detect_all_can_interfaces(bitrate, brands=None, _sysfs='/sys/class/net'):
    """Detect all available CAN interfaces.

    Args:
        bitrate: CAN bitrate in bps
        brands: Optional set of brand names to restrict detection to
                (e.g. {'ixxat', 'kvaser'}). None means detect all brands.

    Returns:
        List of Adapter, each unpacking as (interface, channel_spec, description)
        where channel_spec can be:
        - int for simple channel number
        - dict for ixxat with {'channel': 0, 'unique_hardware_id': 'HW123456'}
        and with ``.ids`` naming the physical adapter where the backend says
        (serial, USB port; see Adapter). The list is in detection order, which
        follows plug order: pick adapters by ids, not by position.
    """
    interfaces = []
    sockcan = socketcan_interfaces(_sysfs)

    # Detect ixxat devices using hardware IDs
    if brands is None or 'ixxat' in brands:
        try:
            from can.interfaces.ixxat import get_ixxat_hwids
            hwids = get_ixxat_hwids()
            for hwid in hwids:
                try:
                    channel_spec = {'channel': 0, 'unique_hardware_id': hwid}
                    bus = can.interface.Bus(interface='ixxat', bitrate=bitrate, **channel_spec)
                    bus.shutdown()
                    ids = (f'serial:{hwid}', f'ixxat:{hwid}')
                    interfaces.append(Adapter('ixxat', channel_spec, _with_ids(f'ixxat {hwid}', ids), ids))
                    logger.info(f"Detected {interfaces[-1].desc}")
                except Exception as e:
                    logger.debug(f"ixxat {hwid}: {e}")
        except ImportError:
            logger.warning("ixxat interface not available")
        except Exception as e:
            logger.debug(f"ixxat enumeration failed: {e}")

    # Detect gs_usb devices (Innomaker USB2CAN / candleLight firmware).
    # These enumerate as VID 0x1D50 / PID 0x606F with WinUSB auto-bound.
    if brands is None or 'gs_usb' in brands or (brands and 'innomaker' in brands):
        if any(drv == 'gs_usb' for _, drv, _ in sockcan):
            # The kernel driver holds it: it is a SocketCAN interface (below), and
            # opening it through libusb would take it from the kernel.
            logger.debug("gs_usb: bound to the Linux gs_usb driver; using SocketCAN, not libusb")
        elif _ensure_gs_usb_libusb():
            try:
                from gs_usb.gs_usb import GsUsb
                devs = GsUsb.scan()
                serials = [_gs_usb_serial(d) for d in devs]
                # Release scan handles before opening so the per-device open
                # (which re-scans internally) does not hit a busy interface.
                del devs
                for i, sn in enumerate(serials):
                    channel_spec = {'channel': 'can0', 'index': i}
                    ids = ((f'serial:{sn}',) if sn else ()) + (f'gs_usb:{i}',)
                    interfaces.append(Adapter('gs_usb', channel_spec,
                                              _with_ids(f'gs_usb (Innomaker) index {i}', ids), ids))
                    logger.info(f"Detected {interfaces[-1].desc}")
            except ImportError:
                logger.warning("gs_usb interface not available "
                               "(pip install \"python-can[gs_usb]\" libusb-package)")
            except Exception as e:
                logger.debug(f"gs_usb enumeration failed: {e}")
        else:
            logger.debug("gs_usb: no libusb backend; skipping detection")

    # Try kvaser channels (filter out virtual devices)
    if brands is None or 'kvaser' in brands:
        for ch in range(8):
            try:
                bus = can.interface.Bus(interface='kvaser', channel=ch, bitrate=bitrate)
                channel_info = str(bus.channel_info) if hasattr(bus, 'channel_info') else ''
                bus.shutdown()

                if 'Virtual' in channel_info or 'virtual' in channel_info:
                    logger.debug(f"Skipping kvaser channel {ch} (virtual device)")
                    continue

                ids = (f'kvaser:{ch}',)
                m = re.search(r'S/N\s*(\S+)\s*\(#(\d+)\)', channel_info)
                if m:
                    # A two-channel Kvaser has one S/N: serial:<sn>#<n> names the channel
                    ids = (f'serial:{m.group(1)}#{m.group(2)}', f'serial:{m.group(1)}') + ids
                interfaces.append(Adapter('kvaser', ch, _with_ids(f'kvaser channel {ch}', ids), ids))
                logger.info(f"Detected {interfaces[-1].desc}")
            except Exception as e:
                logger.debug(f"kvaser channel {ch}: {e}")

    # Try pcan channels
    if brands is None or 'pcan' in brands:
        for ch in ['PCAN_USBBUS1', 'PCAN_USBBUS2', 'PCAN_USBBUS3', 'PCAN_USBBUS4']:
            try:
                bus = can.interface.Bus(interface='pcan', channel=ch, bitrate=bitrate)
                ids = (f'pcan:{ch}',)
                try:
                    ids = (f'pcan-id:{int(bus.get_device_number())}',) + ids
                except Exception as e:
                    logger.debug(f"pcan {ch}: no device number: {e}")
                bus.shutdown()
                interfaces.append(Adapter('pcan', ch, _with_ids(f'pcan {ch}', ids), ids))
                logger.info(f"Detected {interfaces[-1].desc}")
            except Exception as e:
                logger.debug(f"pcan {ch}: {e}")

    # SocketCAN (Linux): every CAN interface, picked by its driver. 'socketcan'
    # selects all of them; a brand ('ixxat', 'gs_usb'/'innomaker', 'kvaser',
    # 'pcan') selects the interfaces whose driver is that brand's. The bit rate
    # belongs to the interface (ip link), so an interface that is down is skipped.
    for ch, driver, is_up in sockcan:
        brand = _SOCKETCAN_DRIVER_BRAND.get(driver)
        wanted = (brands is None or 'socketcan' in brands or (brand is not None and brand in brands)
                  or (brand == 'gs_usb' and 'innomaker' in brands))
        if not wanted:
            continue
        if not is_up:
            logger.info(f"socketcan {ch} ({driver or 'virtual'}) is down; skipped")
            continue
        try:
            bus = can.interface.Bus(interface='socketcan', channel=ch, bitrate=bitrate)
            bus.shutdown()
            serial, port = _sysfs_usb_identity(_sysfs, ch)
            ids = ((f'serial:{serial}',) if serial else ()) + ((f'usb:{port}',) if port else ()) \
                + (f'socketcan:{ch}',)
            desc = _with_ids(f'socketcan {ch} ({driver or "virtual"})', ids)
            interfaces.append(Adapter('socketcan', ch, desc, ids))
            logger.info(f"Detected {desc}")
        except Exception as e:
            logger.debug(f"socketcan {ch}: {e}")

    return interfaces
