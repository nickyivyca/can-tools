"""Real-hardware GUI ownership test (Kvaser + Innomaker back-to-back + terminator).

Proves the ownership rule: a dongle that is *listed but unchecked* in the GUI is
NOT opened/owned, so another program can use it -- e.g. a real loopback source --
while the GUI captures the checked dongle.

  1. Build the window; both dongles are listed and default-checked.
  2. UNCHECK the Innomaker (gs_usb); keep the Kvaser checked. Start.
  3. Open the Innomaker *externally* (this only succeeds if the GUI didn't own it)
     and transmit frames.
  4. The GUI's Kvaser row must show those frames.

SKIPs cleanly (exit 0) if both dongles are not present.
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
from canbench.live.receiver import detect_all_can_interfaces
from canbench.gui import LoggerWindow
from canbench.gui.logger_window import COL_TOTAL

BR = 500000


def _have_both():
    kinds = {i[0] for i in detect_all_can_interfaces(BR)}
    return {"kvaser", "gs_usb"} <= kinds


def test_gui_owns_only_checked_dongle():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    win = LoggerWindow()   # hardware only; both dongles listed + checked
    kv_row = gs_row = None
    gs_ch = None
    for row, (iface, ch, desc) in enumerate(win.interfaces):
        if iface == "kvaser":
            kv_row = row
        elif iface == "gs_usb":
            gs_row = row
            gs_ch = ch
    assert kv_row is not None and gs_row is not None, "need kvaser + gs_usb listed"

    win._row_checks[gs_row].setChecked(False)   # do NOT own the Innomaker
    win._row_checks[kv_row].setChecked(True)    # capture the Kvaser
    win.log_check.setChecked(False)             # monitor only
    win.toggle()                                 # Start
    assert win.engine and win.engine.running

    tx = None
    try:
        # This open would raise if the GUI had claimed the Innomaker -> ownership proof
        tx = buses.open_bus("gs_usb", gs_ch, BR)
        for i in range(12):
            tx.send(can.Message(arbitration_id=0x350 + i, data=bytes([i, 0x5A]), is_extended_id=False))
            time.sleep(0.02)
        time.sleep(0.5)

        win._refresh_stats()
        kv_total = int(win.table.item(kv_row, COL_TOTAL).text().replace(",", ""))
        assert kv_total >= 10, f"Kvaser captured only {kv_total}/12 from the unowned Innomaker"
        print(f"  Kvaser (GUI-owned) captured {kv_total}/12 frames TX'd by the unowned Innomaker")
    finally:
        if tx is not None:
            buses.shutdown_bus(tx)
        win.toggle()                             # Stop
        win.close()
    assert win.engine is None


def test_gui_gs_usb_start_stop_restart():
    """GUI owns the Innomaker across Start/Stop/Start (exercises the gs_usb reopen fix)."""
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    win = LoggerWindow()
    gs_row = next((r for r, (iface, ch, d) in enumerate(win.interfaces) if iface == "gs_usb"), None)
    assert gs_row is not None
    for r in win._row_checks:
        win._row_checks[r].setChecked(r == gs_row)     # capture ONLY the Innomaker
    win.log_check.setChecked(False)

    win.toggle()                                        # Start 1
    assert win.engine and win.engine.running
    time.sleep(0.2)
    win.toggle()                                        # Stop 1

    win.toggle()                                        # Start 2 (reopen gs_usb)
    assert win.engine and win.engine.running, "gs_usb failed to reopen through the GUI"
    time.sleep(0.2)
    win.toggle()                                        # Stop 2
    win.close()
    print("  gs_usb reopened cleanly across GUI Start/Stop/Start")


if __name__ == "__main__":
    if not _have_both():
        print("SKIP  test_gui_hardware: need both kvaser + gs_usb (Innomaker) connected")
        sys.exit(0)
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
