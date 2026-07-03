# PyInstaller spec for the canbench desktop CAN logger (Windows, onedir).
#
# Build from the repo root:
#   py -3.12 -m PyInstaller packaging/can_gui.spec --noconfirm \
#       --distpath packaging/dist --workpath packaging/build
#
# The critical bit is collecting libusb_package's bundled libusb-1.0.dll so the
# gs_usb (Innomaker) backend works in the frozen app with no Python install.
# console=True for now so --check output / logs are visible during bring-up;
# flip to False for the final windowed build.
import os
from PyInstaller.utils.hooks import collect_all

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))

datas = [(os.path.join(ROOT, "canbench.ini"), ".")]
binaries = []
hiddenimports = [
    "can.interfaces.gs_usb",
    "can.interfaces.kvaser",
    "can.interfaces.ixxat",
    "can.interfaces.pcan",
    "can.interfaces.socketcan",
    "can.interfaces.udp_multicast",
    "can.interfaces.virtual",
]

# Bundle the libusb DLL (libusb_package), the gs_usb backend, python-can, pyusb.
for pkg in ("libusb_package", "gs_usb", "can", "usb"):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

a = Analysis(
    [os.path.join(ROOT, "tools", "can_gui.py")],
    pathex=[ROOT],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="can_gui",
    debug=False,
    strip=False,
    upx=False,
    console=True,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="can_gui",
)
