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

if not getattr(sys, "frozen", False):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _check():
    """Print detected interfaces + gs_usb backend status and exit (no GUI).

    Useful for verifying a machine's setup -- and, in the packaged build, that
    the bundled libusb/gs_usb backend works without a Python install.
    """
    from canbench.live.receiver import detect_all_can_interfaces, gs_usb_backend_available
    from canbench.profiles import load_profiles, app_base_dir
    frozen = getattr(sys, "frozen", False)
    print(f"canbench --check   (frozen={frozen})")
    print(f"app base dir: {app_base_dir()}")
    ps = load_profiles()
    print(f"profiles ({'from canbench.ini' if (app_base_dir() / 'canbench.ini').is_file() else 'built-in defaults'}):")
    for n in ps.names():
        p = ps.get(n)
        print(f"  {n}: log_dir={p.log_dir}  bitrate={p.bitrate}")
    print(f"gs_usb backend available: {gs_usb_backend_available()}")
    ifs = detect_all_can_interfaces(500000)
    print(f"Detected {len(ifs)} interface(s):")
    for iface, ch, desc in ifs:
        print(f"  {iface}: {desc}")


def main():
    ap = argparse.ArgumentParser(description="canbench desktop CAN logger")
    ap.add_argument("--virtual", action="store_true",
                    help="show the in-process virtual bus in the interface list (for testing)")
    ap.add_argument("--ini", default=None, help="path to a vehicle-profile .ini")
    ap.add_argument("--check", action="store_true",
                    help="print detected interfaces and exit (no GUI)")
    ap.add_argument("--selftest", action="store_true",
                    help="build the window offscreen and exit (bundling self-test)")
    args = ap.parse_args()

    if args.check:
        _check()
        return

    if args.selftest:
        import os
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt5 import QtWidgets, QtCore
        from canbench.gui import LoggerWindow
        app = QtWidgets.QApplication(sys.argv[:1])
        win = LoggerWindow(show_virtual=True)
        win.show()
        QtCore.QTimer.singleShot(0, app.quit)
        app.exec_()
        print("GUI selftest OK - window built; interfaces:", [i[0] for i in win.interfaces])
        return

    from PyQt5 import QtWidgets
    from canbench.gui import LoggerWindow

    app = QtWidgets.QApplication(sys.argv[:1])
    ini = Path(args.ini) if args.ini else None
    win = LoggerWindow(show_virtual=args.virtual, ini_path=ini)
    win.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
