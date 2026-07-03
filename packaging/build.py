#!/usr/bin/env python3
"""Build the canbench GUI onedir bundle + distributable zip (Windows).

    py -3.12 packaging/build.py        # run from anywhere; paths are absolute

Produces:
    packaging/dist/can_gui/                  the runnable onedir (exe + _internal + canbench.ini)
    packaging/dist/canbench-logger-win64.zip the thing you send someone

Requires:  py -3.12 -m pip install pyinstaller
Recipients need no Python; only vendor CAN drivers for Kvaser/ixxat/PCAN
(gs_usb/Innomaker and the virtual/localhost bus need nothing).
"""
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "packaging" / "dist"
WORK = ROOT / "packaging" / "build"
SPEC = ROOT / "packaging" / "can_gui.spec"
ZIP_STEM = DIST / "canbench-logger-win64"


def main():
    subprocess.run(
        [sys.executable, "-m", "PyInstaller", str(SPEC), "--noconfirm",
         "--distpath", str(DIST), "--workpath", str(WORK)],
        check=True, cwd=str(ROOT),
    )
    app = DIST / "can_gui"
    # ship an editable profile .ini next to the exe (app base dir when frozen)
    shutil.copy2(ROOT / "canbench.ini", app / "canbench.ini")
    out = shutil.make_archive(str(ZIP_STEM), "zip", str(DIST), "can_gui")

    size_mb = Path(out).stat().st_size / 1e6
    print(f"\nBuilt onedir: {app}")
    print(f"Zip:          {out}  ({size_mb:.1f} MB)")


if __name__ == "__main__":
    main()
