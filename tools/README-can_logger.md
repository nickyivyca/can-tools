# Multi-Device CAN Logger

## Overview

`can_logger.py` automatically detects all connected CAN interfaces and logs data
from them simultaneously to a single **candump / SocketCAN `.log`** file. Each
dongle becomes a logical bus, written to the per-line interface field as `can0`,
`can1`, ... so multi-bus captures round-trip cleanly through python-can.

## Features

- **Automatic multi-device detection** across ixxat, kvaser, pcan, socketcan, gs_usb
- **Virtual-device filtering** — excludes phantom Kvaser Virtual CAN channels
- **candump / SocketCAN output** — plain text, greppable, absolute epoch timestamps, python-can native
- **Concurrent logging** — one daemon receive thread per device, drained by a single writer thread
- **ixxat overflow recovery** — targeted USB hardware reset of the affected dongle, leaving other ixxat dongles unaffected

## Output format

candump / SocketCAN line format:

```
(1735689600.123456) can0 123#01020304
```

- `(epoch.us)` absolute Unix timestamp, microsecond resolution
- `can0` / `can1` / ... the logical bus (dongle detection order)
- `id#data` hex arbitration id and payload

Read/replay of `.asc` and `.blf` is also supported (by extension); the *writer*
default is candump.

## Administrator privileges (ixxat only)

`can_logger.py` needs **administrator privileges** when ixxat dongles are
connected, because overflow recovery uses PowerShell `Disable-PnpDevice` /
`Enable-PnpDevice` (admin-only). Use the self-elevating wrapper:

```
tools\can_logger_admin.bat [--output file.log] [--bitrate 500000]
```

Double-click in Explorer or run from any non-admin cmd prompt; a UAC prompt
appears (skipped if already elevated). Running `python tools/can_logger.py`
directly requires an Administrator prompt, or overflow recovery fails silently
(capture still works; the ixxat won't recover from a latched overrun).

## Usage

```bash
python tools/can_logger.py                          # auto-detect, timestamped file in CWD
python tools/can_logger.py --output my_capture.log  # explicit filename
python tools/can_logger.py --output-dir ./VtruxLogs # explicit directory
python tools/can_logger.py --bitrate 250000         # non-default bitrate
python tools/can_logger.py --brands ixxat,kvaser    # restrict to specific brands
python tools/can_logger.py --debug                  # verbose
```

Vehicle-profile log routing (default `VtruxLogs`/`CodaLogs` destinations) arrives
with the GUI profiles in Phase 2; until then, pass `--output-dir`.

## ixxat overflow recovery

ixxat dongles (VCI4, VID `08D8`) can latch in a hardware receive-buffer overflow;
`bus.recv()` then raises `"Data overrun occurred"` and a soft reopen does not
clear it. Recovery, triggered by the `"overrun"` exception:

1. `bus.shutdown()`
2. PowerShell `Disable-PnpDevice` on the specific dongle (matched by
   `unique_hardware_id` in the PnP instance id, e.g. `USB\VID_08D8&PID_0008\HW379841`)
3. 2 s settle
4. PowerShell `Enable-PnpDevice` — USB re-enumeration
5. 4 s for Windows to re-enumerate
6. `Bus()` reopen with the same `unique_hardware_id`

**Multi-dongle safety:** a module-level lock plus a 15 s cooldown prevent pile-on
resets; only the overflowed dongle is targeted. VID is `08D8` (HMS, which acquired
ixxat) — older ixxat hardware may use `0A71`; update `_usb_reset_ixxat()` if so.

## Threading model

- N daemon receive threads (one per interface) push `(rel_ts, bus_id, msg, dir)` onto a queue
- One writer thread drains the queue, stamps each message with an absolute wall-clock timestamp, sets `msg.channel = canN`, and writes it via python-can
- SIGINT/SIGTERM → graceful shutdown

## Known limitations

- **No traffic verification** — all detected non-virtual devices are included even if idle
- **Single bitrate** — every device uses `--bitrate`
- **ixxat overflow recovery requires admin** — without it the USB reset silently no-ops

## Dependencies

```
python >= 3.10
python-can >= 4.6.1
```

ixxat: `can.interfaces.ixxat` + IXXAT VCI drivers. Kvaser: `kvaser` backend +
CANlib drivers. gs_usb (Innomaker): `python-can[gs_usb]` + `libusb-package`
(no vendor driver needed).
