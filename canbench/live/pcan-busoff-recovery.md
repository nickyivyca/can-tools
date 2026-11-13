# PCAN bus-off / no-ACK recovery

*Last updated: 2026-11-12*

Notes on a PCAN failure mode that looks like broken hardware but isn't, and
the python-can knobs that make it recoverable. Applies to any tool that opens a
`pcan` bus through `receiver.py` (`can_logger.py`, `dlcm_flash_proxy.py`, the
`read_dtcs_coda.py` diagnostic scripts, etc.).

## Symptom

A PCAN adapter that has worked for months suddenly fails on a live vehicle:

```
Bus error: an error counter reached the 'heavy'/'warning' limit
Bus error: an error counter reached the 'heavy'/'warning' limit
  ABS: No response
```

The script then hangs in the receive loop and has to be Ctrl+C'd. A second run
may also emit `Exception on PCANBasic.Uninitialize`. A Kvaser (or any other
brand) on the *same wiring* reads every module cleanly, so it looks like the
PCAN adapter has gone bad.

## What is actually happening

The `Bus error: ... 'heavy'/'warning' limit` lines are **`log.warning`
messages from python-can's PCAN backend, not exceptions** (see
`can/interfaces/pcan/pcan.py`, `recv` handling of
`PCAN_ERROR_BUSLIGHT | PCAN_ERROR_BUSHEAVY`). They do not stop the script by
themselves. What stops it is the downstream effect:

1. Every CAN frame transmitted with **no other node to ACK it** bumps the
   transmit error counter by 8.
2. Past 127 the controller goes error-passive; past 255 it goes **bus-off** and
   removes itself from the bus entirely.
3. **Bus-off does not self-clear.** Without auto-reset the channel stays inert
   until it is reinitialized -- even if the bus comes back to life.

So ~32 unacknowledged sends (a couple of seconds of UDS requests to a sleeping
module) latch the adapter into bus-off. From that point it receives and
transmits nothing for the rest of the run, and the ISO-TP receive loop spins on
its timeout until the user aborts.

The common trigger is **bus state, not hardware**: the vehicle (or the target
diagnostic module) was asleep when transmission started -- e.g. after a long
multi-module read plus a dongle swap, the bus dropped to sleep before the PCAN
run began. A pure receive workload (logging) never transmits, so it never
error-counts; that is why `can_logger.py` on the same adapter/bus stays clean
while a UDS request script latches into bus-off.

This is not specific to the PCAN-USB FD hardware or to a driver version. It is
the standard ISO 11898-1 bus-off rule. Confirmed 2026-07-20: the same PCAN
adapter that appeared "dead" logged 25,350 B-CAN frames over 23 s with zero bus
errors once the vehicle was awake.

## Recovery API (python-can 4.x, pcan backend)

| Call | Effect |
|------|--------|
| `Bus(..., auto_reset=True)` | Sets `PCAN_BUSOFF_AUTORESET` -> driver auto-recovers from bus-off. **The actual fix for the latch.** |
| `bus.reset()` | `PCANBasic.Reset()` -- flushes TX/RX queues and clears post-error state. Good to call once at startup. |
| `bus.status_is_ok()` / `bus.status_string` | Query controller state so a tool can fail loudly instead of hanging. |

**On Linux `auto_reset` is not enough by itself (2026-11-12, jammer bench on
madhouse).** Two things in PCAN-Basic for Linux 9.3.3:

1. `PCANBasic.Reset` (and the library's own bus-off reset) only flushes the
   queues unless the channel's `PCAN_HARD_RESET_STATUS` (0x2E) is on; only then
   does it reset the controller (`pcanbasic_reset`: `PCANFD_RESET_ALL`). With it
   off, the PCAN stayed bus-off for the rest of a run, through 124 resets.
   With it on, `auto_reset=True` recovered the PCAN 299 times in a 3 s error
   train with no failed sends.
2. The library's write-path auto-reset never fires: it compares PCAN-Basic's
   `PCAN_ERROR_BUSOFF` (0x10) with the driver's `PCANFD_ERROR_BUSOFF` (4). Only
   the read path resets, so a channel that only transmits is not recovered.

The tools' own PCAN class (`canbench/live/pcan_bus.py`, installed by
`canbench.live.backends.install()`) turns `PCAN_HARD_RESET_STATUS` on at open,
and after a failed send or a bus-off report checks the status and resets the
channel itself, reporting it as a `CAN_ERR_RESTARTED` error frame. With the
library's auto-reset off, that alone recovered the PCAN 29 times in the same
train, back to error-active as the train ended.

`auto_reset` is a pcan-only kwarg; pass it only when `interface == "pcan"` (ixxat
/ kvaser / gs_usb / socketcan do not accept it).

## Recommended handling for tools that transmit

Bus-off recovery keeps the channel alive, but it does not shorten a long
per-request timeout: with recovery alone a script waits the full timeout on a
sleeping bus and *then* returns nothing. Pair the two:

1. Open pcan buses with `auto_reset=True`, and `bus.reset()` at startup.
2. After the **first** request returns empty, check `status_is_ok()`; if the
   bus is not OK, abort immediately with a clear message
   ("bus appears asleep or bus-off -- is the vehicle awake?") rather than
   grinding through every remaining request.

For the two-dongle flash proxy the stakes are higher: a `dlcm_flash_proxy.py`
session runs ~46 minutes and the patch window is only the final ~100 s. A
transient bus-off mid-session would silently kill one direction of the bridge
and the CAL patch would never land. `auto_reset=True` on both proxy buses is
cheap insurance.

## Where this applies in the code

- `canbench/live/receiver.py` -- `detect_all_can_interfaces()` and
  `CANBusLogger` open pcan buses via
  `can.interface.Bus(interface="pcan", channel=ch, bitrate=bitrate)`.
- `dlcm_flash_proxy.py` -- `open_bus()` (reverse-it mirror only).
- `read_dtcs_coda.py` / sibling diagnostic scripts -- `create_bus()`
  (coda-diagnostics repo).

None of these currently pass `auto_reset` -- this note documents the option; it
is not yet implemented.
