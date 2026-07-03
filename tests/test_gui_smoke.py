"""Headless smoke test of the LoggerWindow (Qt offscreen, virtual bus, no display).

Drives the real GUI code path: build window (--virtual so the in-process bus is
listed) -> select ONLY the virtual interface -> Start -> push frames -> poll stats
-> Stop. No display, no hardware owned.
"""
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import can
from PyQt5 import QtWidgets

from canbench import buses
from canbench.gui import LoggerWindow
from canbench.gui.logger_window import COL_TOTAL


def test_window_start_monitor_stop():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    win = LoggerWindow(show_virtual=True)      # in-process virtual bus is listed
    assert win.table.rowCount() >= 1

    # capture ONLY the in-process virtual interface (don't own real dongles)
    virt_row = None
    for row, (iface, ch, desc) in enumerate(win.interfaces):
        want = (iface == "virtual" and ch == buses.VIRTUAL_CHANNEL)
        win._row_checks[row].setChecked(want)
        if want:
            virt_row = row
    assert virt_row is not None

    win.log_check.setChecked(False)            # monitor only; no file
    win.toggle()                                # Start
    assert win.engine is not None and win.engine.running

    try:
        tx = can.Bus(interface="virtual", channel=buses.VIRTUAL_CHANNEL)
        for i in range(5):
            tx.send(can.Message(arbitration_id=0x70 + i, data=b"\x00", is_extended_id=False))
            time.sleep(0.02)
        time.sleep(0.4)
        tx.shutdown()

        win._refresh_stats()
        total = int(win.table.item(virt_row, COL_TOTAL).text().replace(",", ""))
        assert total == 5, total
    finally:
        win.toggle()                            # Stop
        assert win.engine is None

    win.close()


def test_profile_bitrate_applied():
    """Selecting a vehicle profile drives the bitrate (profile-configurable)."""
    import tempfile
    base = Path(tempfile.mkdtemp())
    ini = base / "canbench.ini"
    ini.write_text(
        "[general]\ndefault_profile = vtrux\n\n"
        "[profile:vtrux]\nlog_dir = V\nbitrate = 250000\n\n"
        "[profile:coda]\nlog_dir = C\nbitrate = 125000\n",
        encoding="utf-8")
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    win = LoggerWindow(ini_path=ini)
    assert win.current_bitrate() == 250000            # vtrux default
    win.profile_combo.setCurrentText("coda")
    assert win.current_bitrate() == 125000            # coda profile bitrate
    win.close()


def test_add_network_can_and_passthrough_options():
    """A network-CAN row can be added; passthrough combos list the other rows."""
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    win = LoggerWindow()
    before = win.table.rowCount()
    win.extra_interfaces.append(("udp_multicast", "239.1.2.3", "Network CAN (239.1.2.3)"))
    win._rebuild_table()
    assert win.table.rowCount() == before + 1
    last = win.table.rowCount() - 1
    assert win.interfaces[last][0] == "udp_multicast"
    combo = win._pt_combos[last]
    assert combo.count() == 1 + before               # "(none)" + every other row
    win.close()


def test_folder_saved_to_ini():
    """Changing the folder field persists to the profile's ini."""
    import tempfile
    from canbench.profiles import load_profiles
    base = Path(tempfile.mkdtemp())
    ini = base / "canbench.ini"
    ini.write_text(
        "[general]\ndefault_profile = vtrux\n\n"
        "[profile:vtrux]\nlog_dir = V\nbitrate = 500000\n\n"
        "[profile:coda]\nlog_dir = C\nbitrate = 500000\n",
        encoding="utf-8")
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    win = LoggerWindow(ini_path=ini)
    newfolder = str((base / "MyLogs").resolve())
    win.folder_edit.setText(newfolder)
    win._persist_folder()
    reloaded = load_profiles(ini_path=ini)
    assert str(reloaded.get("vtrux").log_dir) == newfolder
    win.close()


def test_columns_autoscale_and_pin():
    """Columns fill the window and reflow on resize; a dragged column stays pinned."""
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    win = LoggerWindow(show_virtual=True)
    win.resize(1000, 500)
    win.show()
    app.processEvents(); app.processEvents()

    ncols = win.table.columnCount()
    vw = win.table.viewport().width()
    assert abs(sum(win.table.columnWidth(c) for c in range(ncols)) - vw) < 12   # fills

    win.table.setColumnWidth(2, 260)       # simulate a user drag -> pins column 2
    app.processEvents(); app.processEvents()
    win.resize(1400, 500)
    app.processEvents(); app.processEvents()

    assert win.table.columnWidth(2) == 260                                       # pinned
    vw2 = win.table.viewport().width()
    assert abs(sum(win.table.columnWidth(c) for c in range(ncols)) - vw2) < 12   # still fills
    win.close()


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn(); print(f"PASS  {name}")
            except AssertionError as e:
                failures += 1; print(f"FAIL  {name}: {e}")
            except Exception as e:
                failures += 1; print(f"ERROR {name}: {e!r}")
    print(f"\n{'ALL PASS' if failures == 0 else str(failures) + ' FAILED'}")
    sys.exit(1 if failures else 0)
