#!/usr/bin/env python3
"""Launch the canbench desktop CAN logger GUI (Windows primary).

    py -3.12 tools/can_gui.py            # Windows / dev
    python3   tools/can_gui.py           # Linux (needs a display)

Vehicle profiles come from canbench.ini beside the app if present, else the
built-in Vtrux/Coda defaults. See canbench/profiles.py.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PyQt5 import QtWidgets

from canbench.gui import LoggerWindow


def main():
    app = QtWidgets.QApplication(sys.argv)
    win = LoggerWindow()
    win.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
