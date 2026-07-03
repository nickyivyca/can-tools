# packaging/

Windows distribution build for the `canbench` GUI (Phase 2).

**Plan:** PyInstaller **`--onedir`**, zipped for handoff — no per-launch temp
extraction, reliable UAC elevation (embedded manifest so the app can run the
ixxat USB-reset), and clean bundling of the Qt plugins + the gs_usb
`libusb-1.0.dll`. Recipients need no Python; only vendor CAN drivers for their
dongle (gs_usb/virtual need none).

Linux is CLI-only and not packaged.

_Contents (spec file, build script, UAC manifest) land here in Phase 2._
