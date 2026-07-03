"""Headless smoke test of the LoggerWindow (Qt offscreen, virtual bus, no display).

Drives the real GUI code path: build window -> select only the in-process virtual
interface -> Start -> push frames on that virtual channel -> poll stats -> Stop.
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


def test_window_start_monitor_stop():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    win = LoggerWindow()
    # software interfaces are always listed, so there is always >=1 row
    assert win.table.rowCount() >= 1

    # capture ONLY the in-process virtual interface (avoid touching real dongles)
    virt_row = None
    for row, (iface, ch, desc) in enumerate(win.interfaces):
        want = (iface == "virtual" and ch == buses.VIRTUAL_CHANNEL)
        win._row_checks[row].setChecked(want)
        if want:
            virt_row = row
    assert virt_row is not None

    win.log_check.setChecked(False)      # monitor only; don't create files
    win.toggle()                          # Start
    assert win.engine is not None and win.engine.running

    try:
        tx = can.Bus(interface="virtual", channel=buses.VIRTUAL_CHANNEL)
        for i in range(5):
            tx.send(can.Message(arbitration_id=0x70 + i, data=b"\x00", is_extended_id=False))
            time.sleep(0.02)
        time.sleep(0.4)
        tx.shutdown()

        win._refresh_stats()
        total_cell = win.table.item(virt_row, 3).text().replace(",", "")
        assert int(total_cell) == 5
    finally:
        win.toggle()                      # Stop
        assert win.engine is None

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
