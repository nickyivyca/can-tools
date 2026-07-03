#!/usr/bin/env python3
"""CAN log replayer - plays back a captured log file through connected CAN hardware.

Reads any python-can-native log format (candump/SocketCAN ``.log``, Vector
``.asc``, Vector ``.blf``, CSV) and transmits frames through connected dongles at
the original timing. The per-line interface field (``can0``, ``can1``, ...) is
mapped back to a logical log-bus id.

Channel mapping:
  - If the log has the same number of buses as connected dongles, each log bus
    is sent through the corresponding dongle (log bus 0 -> dongle 0, etc.)
  - Otherwise all frames are sent on the first/only dongle.

Usage:
    python tools/can_replayer.py capture.log
    python tools/can_replayer.py capture.log --brands ixxat
    python tools/can_replayer.py capture.log --speed 2.0 --loop
    python tools/can_replayer.py capture.log --speed 0   # no delay, as fast as possible
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

from canbench import logio
from canbench.buses import shutdown_bus
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

# python-can LogReader dispatches these by extension
SUPPORTED_SUFFIXES = {'.log', '.asc', '.blf', '.csv', '.trc'}


def open_buses(interfaces, bitrate):
    """Open all detected interfaces as live CAN buses.

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
        channel_map: dict mapping log bus id int -> python-can Bus object
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

        output_bus = channel_map[logio.channel_bus_id(frame.channel)]
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


def main():
    parser = argparse.ArgumentParser(
        description='CAN log replayer - transmit a captured log through connected hardware'
    )
    parser.add_argument(
        'logfile',
        help='Path to log file (candump/SocketCAN .log, Vector .asc/.blf, CSV; by extension)'
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
        '--speed', '-s',
        type=float,
        default=1.0,
        help='Replay speed multiplier (default: 1.0). Use 0 for no inter-frame delay.'
    )
    parser.add_argument(
        '--bus',
        type=int,
        default=None,
        metavar='BUS_ID',
        help='Only replay frames from this log bus id (e.g. --bus 1). '
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

    logfile = Path(args.logfile)
    if not logfile.exists():
        logger.error(f"Log file not found: {logfile}")
        sys.exit(1)

    if logfile.suffix.lower() not in SUPPORTED_SUFFIXES:
        logger.error(
            f"Unsupported log extension '{logfile.suffix}'. "
            f"Supported: {', '.join(sorted(SUPPORTED_SUFFIXES))}"
        )
        sys.exit(1)

    # --- Load frames via python-can (dispatched by extension) ---
    logger.info("Loading log file...")
    try:
        frames = list(logio.read_messages(logfile))
    except Exception as e:
        logger.error(f"Failed to read log: {e}")
        sys.exit(1)
    if not frames:
        logger.error("No frames found in log file.")
        sys.exit(1)
    fmt_name = logfile.suffix.lower().lstrip('.')

    # --- Bus filter ---
    if args.bus is not None:
        all_buses = sorted(set(logio.channel_bus_id(f.channel) for f in frames))
        if args.bus not in all_buses:
            logger.error(f"Bus {args.bus} not found in log. Available buses: {all_buses}")
            sys.exit(1)
        frames = [f for f in frames if logio.channel_bus_id(f.channel) == args.bus]
        logger.info(f"Bus filter: keeping bus {args.bus} ({len(frames):,} frames)")

    log_channels = sorted(set(logio.channel_bus_id(f.channel) for f in frames))
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

    # --- Build channel map ---
    if len(log_channels) == len(interfaces):
        # 1:1 mapping: log bus N → dongle index N
        mapping_desc = "log buses == dongle count, mapping 1:1"
        channel_map_iface = {ch: interfaces[i] for i, ch in enumerate(log_channels)}
    else:
        # All frames → first dongle
        mapping_desc = "bus count mismatch, all frames → first dongle"
        channel_map_iface = {ch: interfaces[0] for ch in log_channels}

    # --- Print startup summary ---
    speed_str = f"{args.speed}x" if args.speed > 0 else "no delay (as fast as possible)"
    replay_duration = (log_duration / args.speed) if args.speed > 0 else 0.0

    print()
    print("=" * 60)
    print("CAN Log Replayer")
    print("=" * 60)
    print(f"Log file:     {logfile}  (format: {fmt_name})")
    if args.bus is not None:
        print(f"Bus filter:   bus {args.bus} only")
    print(f"Frames:       {len(frames):,}")
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
        print(f"  Dongle {i}: {desc}")
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

    # --- Open live buses ---
    try:
        buses = open_buses(interfaces, args.bitrate)
    except Exception as e:
        logger.error(f"Could not open CAN interfaces: {e}")
        sys.exit(1)

    # Build channel_map: log bus id → open Bus object.
    # buses[i] corresponds to interfaces[i] in order.
    if len(log_channels) == len(interfaces):
        channel_map = {ch: buses[i][0] for i, ch in enumerate(log_channels)}
    else:
        channel_map = {ch: buses[0][0] for ch in log_channels}

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
    print("=" * 60)


if __name__ == '__main__':
    main()
