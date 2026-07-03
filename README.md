# can-tools (`canbench`)

Standalone CAN **acquisition, logging, replay, and GUI** toolkit. Detects every
connected CAN dongle, logs traffic from all of them at once, replays captures
back onto hardware, and (Phase 2) drives it all from a distributable desktop GUI.

This repo is the runtime/hardware/UI half of a CAN reverse-engineering workflow.
The offline analysis framework (surveys, correlation, DBC building, UDS) lives
separately; nothing in here depends on it.

## Status

| Area | State |
|------|-------|
| Hardware layer (`canbench.live`) | Working — ixxat, kvaser, pcan, socketcan, gs_usb |
| `can_logger` / `can_replayer` (CLI) | Working — candump / SocketCAN default format |
| GUI logger, vehicle profiles, passthrough, packaging | Phase 2 (in progress) |
| Live signal decode | Phase 3 (planned) |

## Log format

The default capture format is **candump / SocketCAN `.log`** — read and written
natively by [python-can](https://python-can.readthedocs.io), plain-text and
greppable, with absolute epoch timestamps and a per-line interface field that
preserves multi-bus identity (`can0`, `can1`, ...). Vector `.asc` / `.blf` and
CSV are also supported for read/replay (dispatched by file extension).

## Requirements

```
py -3.12 -m pip install -r requirements.txt     # Windows
python3   -m pip install -r requirements.txt     # Linux (socketcan)
```

No Python is required on machines that run the packaged Windows build (Phase 2).
The only prerequisites are the **vendor drivers** for whichever dongle you use
(Kvaser CANlib, ixxat VCI4, PCAN driver). The Innomaker/gs_usb dongle and the
virtual/localhost test bus need no driver at all.

## CLI usage

```bash
# Log every connected dongle to a timestamped candump .log in the current dir
python tools/can_logger.py

# On Windows with an ixxat dongle, use the self-elevating launcher so
# receive-overflow USB recovery can run (needs admin):
tools\can_logger_admin.bat --output capture.log

# Replay a capture back onto hardware at original timing
python tools/can_replayer.py capture.log
```

See `tools/README-can_logger.md` for the logger's hardware handling and ixxat
overflow recovery.

## Layout

```
canbench/
  live/          dongle detection, per-bus RX threads, ixxat USB reset
  logio.py       python-can candump read/write wrappers
  gui/           PyQt logger GUI                          (Phase 2)
  passthrough.py two-dongle bridge                        (Phase 2)
  profiles.py    vehicle-profile .ini loader              (Phase 2)
tools/           CLI entry points
packaging/       PyInstaller onedir build                 (Phase 2)
tests/           virtual/udp-bus tests, no hardware       (Phase 2)
```
