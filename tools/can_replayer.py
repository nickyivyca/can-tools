#!/usr/bin/env python3
"""CAN log replayer - plays back a captured log file through connected CAN hardware.

Reads the format by content (``canbench.replay``): BUSMASTER (including a session's
``_T`` chunks, chained on their headers) and PEAK TRC through ``canbench.parsers``;
candump/SocketCAN ``.log``, Vector ``.asc`` / ``.blf``, CSV and SavvyCAN through
python-can. Frames go out through connected dongles at the original timing.

Choosing a bus: a channel number in a BUSMASTER or TRC capture is dongle insertion
order, not a bus, so ``--bus 2`` is refused for those formats; pick the bus by
anchors with ``--vehicle coda --bus d_can``, or add ``--raw-channel`` to replay a
channel number as it is. candump input keeps ``--bus N`` (can_logger's ``canN``).

Never transmitted, and counted in the summary: non-frame records (BUSMASTER ``ERR``,
TRC ``ER`` / ``ST`` / ``EC`` / ``EV``), error frames, and frames marked as inferred
adapter error reports (``canbench/parsers/inferred_errors.py``).

Choosing an adapter: with one adapter detected, every replayed bus goes out on it.
With two or more, pin each bus with ``--adapter BUS=ID`` (ids are printed at startup:
``serial:<s>`` and ``usb:<port>`` name a physical adapter; canN, PCAN_USBBUSn and
Kvaser channel numbers follow plug order), or narrow ``--brands`` to one adapter.
Detection order follows plug order, so the replayer refuses rather than guess.
Only the adapters something is sent on are opened.

Usage:
    python tools/can_replayer.py capture.log
    python tools/can_replayer.py capture.log --brands ixxat
    python tools/can_replayer.py drive.log --vehicle coda --adapter b_can=serial:HW123456 \
        --adapter d_can=usb:1-10.2
    python tools/can_replayer.py capture.log --speed 2.0 --loop
    python tools/can_replayer.py capture.log --speed 0   # no delay, as fast as possible
    python tools/can_replayer.py drive_T0.log --session --vehicle coda --bus d_can
"""
import argparse
import can
import logging
import signal
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from canbench import replay
from canbench.buses import shutdown_bus
from canbench.live.vehicle_configs import VEHICLES
from canbench.live.receiver import detect_all_can_interfaces

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s'
)
logger = logging.getLogger(__name__)

# Suppress python-can debug noise
can_logger = logging.getLogger('can')
can_logger.setLevel(logging.WARNING)

# Read by content (BUSMASTER, TRC, SavvyCAN) or by python-can's extension dispatch
SUPPORTED_SUFFIXES = {'.log', '.asc', '.blf', '.csv', '.trc'}


def open_buses(interfaces, bitrate):
    """Open the given interfaces as live CAN buses.

    Returns list of (bus, description) tuples, closing already-opened buses
    on failure.
    """
    buses = []
    for iface, ch, desc in interfaces:
        try:
            if isinstance(ch, dict):
                bus = can.interface.Bus(interface=iface, bitrate=bitrate, **ch)
            else:
                bus = can.interface.Bus(interface=iface, channel=ch, bitrate=bitrate)
            buses.append((bus, desc))
            logger.info(f"Opened {desc}")
        except Exception as e:
            logger.error(f"Failed to open {desc}: {e}")
            for b, _ in buses:
                try:
                    b.shutdown()
                except Exception:
                    pass
            raise
    return buses


def replay_once(frames, channel_map, speed, abort_event):
    """Transmit all frames once, honouring timing.

    Args:
        frames: list of can.Message objects (pre-loaded)
        channel_map: dict mapping output key (log bus id or bus name) -> python-can Bus
        speed: float replay speed multiplier (0 = no delay)
        abort_event: threading.Event - set to request early stop

    Returns:
        Number of frames transmitted.
    """
    if not frames:
        return 0

    tx_count = 0
    start_wall = time.perf_counter()
    log_start_ts = frames[0].timestamp

    for frame in frames:
        if abort_event.is_set():
            break

        if speed > 0:
            target_elapsed = (frame.timestamp - log_start_ts) / speed
            actual_elapsed = time.perf_counter() - start_wall
            delay = target_elapsed - actual_elapsed
            if delay > 0:
                time.sleep(delay)

        output_bus = channel_map[frame.channel]
        msg = can.Message(
            arbitration_id=frame.arbitration_id,
            data=frame.data,
            is_extended_id=frame.is_extended_id,
            dlc=frame.dlc,
            is_remote_frame=frame.is_remote_frame,
        )
        try:
            output_bus.send(msg)
            tx_count += 1
        except can.CanError as e:
            logger.warning(f"Send error on frame {tx_count}: {e}")

    return tx_count


def skipped_summary(load):
    """One line: what the log held that is not a frame to transmit."""
    parts = []
    if load.events:
        parts.append("events " + ", ".join(f"{k} {n:,}" for k, n in sorted(load.events.items())))
    if load.error_frames:
        parts.append(f"error frames {load.error_frames:,}")
    if load.inferred_errors:
        ids = ", ".join(f"0x{i:03X} {n:,}" for i, n in sorted(load.inferred_errors.items()))
        parts.append(f"inferred adapter errors {sum(load.inferred_errors.values()):,} ({ids})")
    return "; ".join(parts) or "nothing"


def main():
    # canbench's PCAN/Kvaser classes: Linux-layout error reports, PCAN bus-off recovery
    from canbench.live import backends
    backends.install()
    parser = argparse.ArgumentParser(
        description='CAN log replayer - transmit a captured log through connected hardware'
    )
    parser.add_argument(
        'logfile',
        nargs='+',
        help='Log file(s): BUSMASTER, PEAK TRC, candump/SocketCAN .log, Vector .asc/.blf, '
             'CSV, SavvyCAN (format by content). Several files: BUSMASTER _T chunks of one session'
    )
    parser.add_argument(
        '--session',
        action='store_true',
        help='Expand a BUSMASTER _T<n> chunk to every chunk of its session'
    )
    parser.add_argument(
        '--vehicle',
        choices=sorted(VEHICLES),
        default=None,
        help='Name buses by anchor ids (needed for --bus <name>)'
    )
    parser.add_argument(
        '--raw-channel',
        action='store_true',
        help='Allow --bus <number> on BUSMASTER/TRC input: replay that channel number as it is'
    )
    parser.add_argument(
        '--bitrate', '-b',
        type=int,
        default=500000,
        help='CAN bitrate in bps (default: 500000)'
    )
    parser.add_argument(
        '--brands',
        default=None,
        help='Comma-separated list of dongle brands to use for TX '
             '(e.g. ixxat or kvaser,pcan). Default: auto-detect all brands.'
    )
    parser.add_argument(
        '--adapter',
        action='append',
        default=[],
        metavar='BUS=ID',
        help='Send BUS on the adapter with this id, e.g. d_can=serial:HW123456 or '
             '1=usb:1-10.1 (ids are listed at startup). Repeat per bus. Needed '
             'whenever two or more adapters are detected.'
    )
    parser.add_argument(
        '--speed', '-s',
        type=float,
        default=1.0,
        help='Replay speed multiplier (default: 1.0). Use 0 for no inter-frame delay.'
    )
    parser.add_argument(
        '--bus',
        default=None,
        metavar='BUS',
        help='Only replay this bus: a name found by anchors (with --vehicle, e.g. d_can), '
             'or a channel number (candump; BUSMASTER/TRC need --raw-channel). '
             'Default: replay all buses.'
    )
    parser.add_argument(
        '--loop', '-l',
        action='store_true',
        help='Loop replay indefinitely (Ctrl+C to stop)'
    )
    parser.add_argument(
        '--skip-confirmation',
        action='store_true',
        help='Skip the "Press Enter to start" prompt'
    )
    parser.add_argument(
        '--debug',
        action='store_true',
        help='Enable debug logging'
    )
    args = parser.parse_args()

    if args.debug:
        logger.setLevel(logging.DEBUG)
        can_logger.setLevel(logging.DEBUG)

    paths = [Path(p) for p in args.logfile]
    for p in paths:
        if p.exists() and p.suffix.lower() not in SUPPORTED_SUFFIXES:
            logger.error(
                f"Unsupported log extension '{p.suffix}'. "
                f"Supported: {', '.join(sorted(SUPPORTED_SUFFIXES))}"
            )
            sys.exit(1)
    if args.session:
        if len(paths) != 1:
            logger.error("--session takes one _T chunk")
            sys.exit(1)
        paths = replay.session_chunks(paths[0])

    logger.info("Loading log file%s..." % ("s" if len(paths) > 1 else ""))
    try:
        load = replay.load_for_replay(paths, vehicle=args.vehicle, bus=args.bus,
                                      raw_channel=args.raw_channel)
    except replay.ReplayInputError as e:
        logger.error(str(e))
        sys.exit(1)
    except Exception as e:
        logger.error(f"Failed to read log: {e}")
        sys.exit(1)
    frames = load.messages
    if not frames:
        logger.error("No frames found in log file.")
        sys.exit(1)
    fmt_name = load.fmt

    log_channels = load.keys
    log_duration = frames[-1].timestamp - frames[0].timestamp

    # --- Parse brands filter ---
    brands_filter = None
    if args.brands:
        brands_filter = {b.strip().lower() for b in args.brands.split(',')}

    # --- Detect dongles ---
    logger.info("Detecting CAN interfaces...")
    interfaces = detect_all_can_interfaces(args.bitrate, brands=brands_filter)

    if not interfaces:
        logger.error("No CAN interfaces detected!")
        logger.error("Check that dongles are connected, drivers installed, and brands spelled correctly.")
        sys.exit(1)

    # --- Build channel map: pinned by --adapter, or the only adapter ---
    try:
        assignment = replay.assign_adapters(log_channels, interfaces, args.adapter)
    except replay.ReplayInputError as e:
        logger.error(str(e))
        sys.exit(1)
    mapping_desc = "pinned by --adapter" if args.adapter else "one adapter, every bus"
    channel_map_iface = {ch: interfaces[i] for ch, i in assignment.items()}

    # --- Print startup summary ---
    speed_str = f"{args.speed}x" if args.speed > 0 else "no delay (as fast as possible)"
    replay_duration = (log_duration / args.speed) if args.speed > 0 else 0.0

    print()
    print("=" * 60)
    print("CAN Log Replayer")
    print("=" * 60)
    if len(load.files) == 1:
        print(f"Log file:     {load.files[0]}  (format: {fmt_name})")
    else:
        print(f"Log files:    {len(load.files)} chunks, {load.files[0].name} .. "
              f"{load.files[-1].name}  (format: {fmt_name})")
    print(f"Selection:    {load.selection}")
    for p, keep in load.kept_channels.items():
        res = load.resolutions.get(p)
        if res is not None and (len(load.files) == 1 or args.debug):
            for ch in sorted(res.hits):
                print(f"  {p.name} ch{ch}: {res.assignments.get(ch) or res.unresolved.get(ch)}"
                      f"{'' if ch in keep else '  (not replayed)'}")
    print(f"Frames:       {len(frames):,}")
    print(f"Not sent:     {skipped_summary(load)}")
    print(f"Log buses:    {len(log_channels)}  (buses {', '.join(str(c) for c in log_channels)})")
    print(f"Log duration: {log_duration:.1f} s")
    print(f"Bitrate:      {args.bitrate:,} bps")
    print(f"Speed:        {speed_str}")
    if args.speed > 0:
        print(f"Est. runtime: {replay_duration:.1f} s per pass")
    print(f"Loop:         {'Yes' if args.loop else 'No'}")
    print()
    print(f"Detected dongles ({len(interfaces)}):")
    for i, (iface, ch, desc) in enumerate(interfaces):
        print(f"  Dongle {i}: {desc}  ids: {', '.join(getattr(interfaces[i], 'ids', ())) or 'none'}")
    print()
    print(f"Channel mapping ({mapping_desc}):")
    for log_ch, (iface, ch, desc) in channel_map_iface.items():
        print(f"  Log bus {log_ch} -> {desc}")
    print()
    print("Press Enter to start replay, or Ctrl+C to abort...")
    print("=" * 60)

    if not args.skip_confirmation:
        try:
            input()
        except KeyboardInterrupt:
            print("\nAborted.")
            sys.exit(0)

    # --- Open live buses: only the adapters something is sent on ---
    used = sorted(set(assignment.values()))
    try:
        buses = open_buses([interfaces[i] for i in used], args.bitrate)
    except Exception as e:
        logger.error(f"Could not open CAN interfaces: {e}")
        sys.exit(1)

    # Build channel_map: log bus id -> open Bus object.
    opened = {i: buses[n][0] for n, i in enumerate(used)}
    channel_map = {ch: opened[i] for ch, i in assignment.items()}

    # --- Set up signal handler ---
    abort_event = threading.Event()

    def signal_handler(sig, frame):
        logger.info("\nShutdown signal received, stopping after current frame...")
        abort_event.set()

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # --- Replay ---
    pass_num = 0
    total_tx = 0
    try:
        while True:
            pass_num += 1
            label = f"pass {pass_num}" if args.loop else "replay"
            logger.info(f"Starting {label} ({len(frames):,} frames)...")
            tx = replay_once(frames, channel_map, args.speed, abort_event)
            total_tx += tx
            logger.info(f"  Transmitted {tx:,} frames")

            if abort_event.is_set() or not args.loop:
                break

    finally:
        for bus_obj, desc in buses:
            shutdown_bus(bus_obj)

    print()
    print("=" * 60)
    print(f"Replay complete: {total_tx:,} frames transmitted ({pass_num} pass{'es' if pass_num > 1 else ''})")
    print(f"Not sent (per pass): {skipped_summary(load)}")
    print("=" * 60)


if __name__ == '__main__':
    main()
