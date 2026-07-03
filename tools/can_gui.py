#!/usr/bin/env python3
"""Launch the canbench desktop CAN logger GUI (Windows primary).

    py -3.12 tools/can_gui.py                  # Windows / dev
    py -3.12 tools/can_gui.py --virtual        # also show the in-process virtual bus (testing)
    py -3.12 tools/can_gui.py --ini my.ini     # explicit vehicle-profile .ini
    python3   tools/can_gui.py                 # Linux (needs a display)

Vehicle profiles come from canbench.ini beside the app if present, else the
built-in Vtrux/Coda defaults. See canbench/profiles.py.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PyQt5 import QtWidgets

from canbench.gui import LoggerWindow


def main():
    ap = argparse.ArgumentParser(description="canbench desktop CAN logger")
    ap.add_argument("--virtual", action="store_true",
                    help="show the in-process virtual bus in the interface list (for testing)")
    ap.add_argument("--ini", default=None, help="path to a vehicle-profile .ini")
    args = ap.parse_args()

    app = QtWidgets.QApplication(sys.argv[:1])
    ini = Path(args.ini) if args.ini else None
    win = LoggerWindow(show_virtual=args.virtual, ini_path=ini)
    win.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
