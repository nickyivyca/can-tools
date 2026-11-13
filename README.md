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
| `can_logger` / `can_replayer` (CLI) | Working — candump / SocketCAN default format; the replayer reads BUSMASTER (incl. `_T` sessions) and PEAK TRC by content and picks buses by anchors |
| Passthrough bridge (`can_logger --passthrough`) + virtual/udp test bus | Working |
| Desktop GUI logger (`can_gui.py`): profiles, dongle select, per-bus frames/sec, candump logging | Phase 2 proto — working, iterating |
| Windows packaging (PyInstaller onedir zip) | Working — `packaging/build.py`; gs_usb/libusb bundled, verified in the frozen exe |
| Live signal decode | Phase 3 (planned) |

## Log format

The default capture format is **candump / SocketCAN `.log`** — read and written
natively by [python-can](https://python-can.readthedocs.io), plain-text and
greppable, with absolute epoch timestamps (each frame's own, from its adapter's clock put on the
wall clock: `canbench/live/clock.py`) and a per-line interface field that
preserves multi-bus identity (`can0`, `can1`, ...). Vector `.asc` / `.blf` and
CSV are also supported for read/replay (dispatched by file extension).

**Reading is by content first.** `canbench.parsers` (stdlib-only readers for
BUSMASTER, candump, PEAK TRC 1.x/2.0, Vector ASC and SavvyCAN CSV; see
`canbench/parsers/parsers.md`) detects a file's format from its first lines, and
`canbench.logio.read_messages` reads BUSMASTER and SavvyCAN through it, since
python-can has no reader for either. A BUSMASTER `.log` is therefore no longer
mistaken for candump. The replayer loads through `canbench.replay`, which also
reads PEAK TRC with its error and status records.

**Error frames, one layout for every adapter.** Adapters report bus errors in
their own styles. canbench carries them all as Linux `can/error.h` error
frames: SocketCAN and gs_usb already give that layout, and the tools'
PCAN and Kvaser classes (`canbench.live.pcan_bus`, `canbench.live.kvaser_bus`,
selected by `canbench.live.backends.install()`, which `can_logger`,
`can_replayer` and `can_gui` call) translate theirs, keeping the adapter's own
id and bytes on the message. `canbench.errors` decodes the layout into one
record, and `can_logger -o x.trc` writes TRC 2.1 with each error as PEAK's
`ER` / `ST` / `EC` records plus an `EV` line holding what the adapter itself
reported (python-can's TRC writer drops error, remote and CAN FD frames). The
PCAN class also turns on PCAN-Basic's hard reset (without it a reset does not
clear bus-off on Linux) and resets the channel itself after bus-off, because
PCAN-Basic for Linux's auto-reset does not fire on a failed send
(`canbench/live/pcan-busoff-recovery.md`).

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

# Replay one bus of a BUSMASTER session, every _T chunk, chosen by anchor ids
python tools/can_replayer.py coda_20261025_193221_T0.log --session --vehicle coda --bus d_can
```

A channel number in a BUSMASTER or TRC capture is dongle insertion order, not a
bus, so `--bus 2` is refused for those formats unless `--raw-channel` is given;
`--vehicle <v> --bus <name>` finds the bus by anchor ids
(`canbench/live/vehicle_configs.py`) and refuses when they do not separate.
The replayer never transmits non-frame records (BUSMASTER `ERR`, TRC
`ER`/`ST`/`EC`/`EV`), error frames, or frames marked as inferred adapter error
reports (`canbench/parsers/inferred_errors.py`); it counts each in its summary.

Adapters are chosen the same way: by identity, not position. With one adapter
detected, every replayed bus goes out on it. With two or more, pin each bus with
`--adapter BUS=ID`, using the ids printed at startup (`serial:<s>` or
`usb:<port>` name a physical adapter; `canN`, `PCAN_USBBUSn` and Kvaser channel
numbers follow plug order), or narrow `--brands` to one; otherwise the replayer
refuses rather than guess which wire each adapter is on.

`<interface>:serial:<s>` also names the backend. On Linux an Innomaker is either on
the kernel's gs_usb driver (SocketCAN) or on libusb (python-can `gs_usb`), with the
same serial: `serial:<s>` matches it on whichever it is on, `socketcan:serial:<s>`
or `gs_usb:serial:<s>` only on that backend, and a pin to the wrong backend is
refused with the reason. Innomakers can be on both backends at once: detection
lists each libusb one (opened by USB bus and address) beside the SocketCAN ones.

See `tools/README-can_logger.md` for the logger's hardware handling and ixxat
overflow recovery.

## Layout

```
canbench/
  live/          dongle detection, per-bus RX threads, ixxat USB reset
                 (see live/pcan-busoff-recovery.md for the PCAN bus-off / no-ACK failure mode)
                 linux_fixes.py: two Linux workarounds applied when receiver is imported (or call
                 linux_fixes.apply()): python-can's Kvaser backend on linuxcan (SET_LOCAL_TXACK needs a
                 4-byte buffer) and gs_usb's start() without the USB reset that makes an Innomaker
                 re-enumerate. Hand an Innomaker back to the kernel in-process only after release_gs_usb().
                 backends.py: install() puts pcan_bus.CanbenchPcanBus and kvaser_bus.CanbenchKvaserBus
                 behind python-can's "pcan" / "kvaser" names (Linux-layout error reports, PCAN bus-off reset)
  live/clock.py  frame timestamps: the adapter's clock on the wall clock (drift, wrap)
  errors.py      the canonical error record: Linux error-frame decoding, PCAN / Kvaser translations, TRC records
  trcwriter.py   TRC 2.1 writer keeping error (ER/ST/EC + EV), remote and CAN FD frames
  logio.py       log read/write: content-detected reading, candump writing (.trc: trcwriter)
  replay.py      loading a capture for replay: _T chunks, buses by anchors, what is not sent; per-bus send failures (SendStats), the wait on a full SocketCAN queue (send_waiting, ENOBUFS)
  parsers/       stdlib log readers (BUSMASTER, candump, TRC 1.0-2.1, ASC, SavvyCAN); parsers.md
  gui/           PyQt logger GUI                          (Phase 2)
  passthrough.py two-dongle bridge                        (Phase 2)
  profiles.py    vehicle-profile .ini loader              (Phase 2)
tools/           CLI entry points
packaging/       PyInstaller onedir build                 (Phase 2)
tests/           virtual/udp-bus tests, no hardware       (Phase 2)
```

## License

GPL-3.0-or-later: see `LICENSE`. The packaged Windows build bundles PyQt5 (GPL) along with
python-can and libusb (LGPL), so a copy of the zip passed on carries the same terms, with this
repository as its source.
