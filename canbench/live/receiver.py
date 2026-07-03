"""CAN hardware interface: bus detection, per-bus receive threads, USB reset.

This module is shared by can_logger.py and can_monitor.py so neither duplicates
the hardware plumbing.

Public API:
    detect_all_can_interfaces(bitrate, brands=None) -> list of (iface, ch_spec, desc)
    CANBusLogger(bus_id, bus, message_queue, iface, ch, bitrate)
"""

import can
import logging
import os
import subprocess
import threading
import time

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
        logger.info(f"Bus {self.bus_id} logging stopped")


def detect_all_can_interfaces(bitrate, brands=None):
    """Detect all available CAN interfaces.

    Args:
        bitrate: CAN bitrate in bps
        brands: Optional set of brand names to restrict detection to
                (e.g. {'ixxat', 'kvaser'}). None means detect all brands.

    Returns:
        List of (interface, channel_spec, description) tuples
        where channel_spec can be:
        - int for simple channel number
        - dict for ixxat with {'channel': 0, 'unique_hardware_id': 'HW123456'}
    """
    interfaces = []

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
                    interfaces.append(('ixxat', channel_spec, f'ixxat {hwid}'))
                    logger.info(f"Detected ixxat {hwid}")
                except Exception as e:
                    logger.debug(f"ixxat {hwid}: {e}")
        except ImportError:
            logger.warning("ixxat interface not available")
        except Exception as e:
            logger.debug(f"ixxat enumeration failed: {e}")

    # Detect gs_usb devices (Innomaker USB2CAN / candleLight firmware).
    # These enumerate as VID 0x1D50 / PID 0x606F with WinUSB auto-bound.
    if brands is None or 'gs_usb' in brands or (brands and 'innomaker' in brands):
        if _ensure_gs_usb_libusb():
            try:
                from gs_usb.gs_usb import GsUsb
                devs = GsUsb.scan()
                count = len(devs)
                # Release scan handles before opening so the per-device open
                # (which re-scans internally) does not hit a busy interface.
                del devs
                for i in range(count):
                    channel_spec = {'channel': 'can0', 'index': i}
                    interfaces.append(('gs_usb', channel_spec, f'gs_usb (Innomaker) index {i}'))
                    logger.info(f"Detected gs_usb (Innomaker) index {i}")
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

                interfaces.append(('kvaser', ch, f'kvaser channel {ch}'))
                logger.info(f"Detected kvaser channel {ch}")
            except Exception as e:
                logger.debug(f"kvaser channel {ch}: {e}")

    # Try pcan channels
    if brands is None or 'pcan' in brands:
        for ch in ['PCAN_USBBUS1', 'PCAN_USBBUS2', 'PCAN_USBBUS3', 'PCAN_USBBUS4']:
            try:
                bus = can.interface.Bus(interface='pcan', channel=ch, bitrate=bitrate)
                bus.shutdown()
                interfaces.append(('pcan', ch, f'pcan {ch}'))
                logger.info(f"Detected pcan {ch}")
            except Exception as e:
                logger.debug(f"pcan {ch}: {e}")

    # Try socketcan
    if brands is None or 'socketcan' in brands:
        for ch in ['can0', 'can1', 'can2', 'can3']:
            try:
                bus = can.interface.Bus(interface='socketcan', channel=ch, bitrate=bitrate)
                bus.shutdown()
                interfaces.append(('socketcan', ch, f'socketcan {ch}'))
                logger.info(f"Detected socketcan {ch}")
            except Exception as e:
                logger.debug(f"socketcan {ch}: {e}")

    return interfaces
