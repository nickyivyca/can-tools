#!/usr/bin/env python3
"""Multi-device CAN logger with candump / SocketCAN output.

Automatically detects and logs from all connected CAN interfaces simultaneously
to a single candump-format ``.log`` file. Each dongle becomes a logical bus,
written to the per-line interface field as ``can0``, ``can1``, ...

Supports: ixxat, kvaser, pcan, socketcan, gs_usb (Innomaker USB2CAN).

Usage:
    python tools/can_logger.py
    python tools/can_logger.py --output my_capture.log
    python tools/can_logger.py --output-dir ./VtruxLogs --bitrate 250000

Vehicle-profile log routing (default VtruxLogs/CodaLogs destinations) arrives
with the GUI profiles in Phase 2; for now, pass --output-dir explicitly.
"""
import argparse
import datetime
import logging
import signal
import sys
import threading
import time
from pathlib import Path
from queue import Queue, Empty

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import can
from canbench.live.receiver import CANBusLogger, detect_all_can_interfaces
from canbench import logio, buses
from canbench.passthrough import PassthroughBridge

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s'
)
logger = logging.getLogger(__name__)

# Suppress python-can debug noise unless we want it
can_logger = logging.getLogger('can')
can_logger.setLevel(logging.WARNING)


def run_passthrough(interfaces, output_path, bitrate):
    """Bridge exactly two interfaces, logging each original by its source bus."""
    if len(interfaces) != 2:
        logger.error(f"--passthrough needs exactly 2 interfaces; detected {len(interfaces)}.")
        logger.error("Narrow to the two dongles to bridge with --brands.")
        sys.exit(1)

    (ia, ca, da), (ib, cb, db) = interfaces
    bus_a = buses.open_bus(ia, ca, bitrate)
    bus_b = buses.open_bus(ib, cb, bitrate)
    logger.info(f"Passthrough: bus 0 = {da}  <->  bus 1 = {db}")
    logger.info(f"Logging sourced frames to: {output_path}")
    logger.info("Press Ctrl+C to stop")
    logger.info("")

    shutdown_event = threading.Event()

    def signal_handler(sig, frame):
        logger.info("\nShutdown signal received...")
        shutdown_event.set()

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    count = [0]
    with logio.open_writer(output_path) as writer:
        def on_frame(src_id, msg):
            msg.channel = logio.bus_channel(src_id)
            msg.timestamp = time.time()
            writer(msg)
            count[0] += 1

        bridge = PassthroughBridge(bus_a, bus_b, on_frame=on_frame)
        bridge.start()
        try:
            last_status = time.time()
            while not shutdown_event.is_set():
                time.sleep(0.25)
                now = time.time()
                if now - last_status >= 5.0:
                    logger.info(f"Bridged {count[0]} frames "
                                f"(bus0={bridge.counts[0]}, bus1={bridge.counts[1]})...")
                    last_status = now
        finally:
            bridge.stop()
            for b in (bus_a, bus_b):
                try:
                    b.shutdown()
                except Exception:
                    pass

    logger.info(f"Wrote {count[0]} frames to {output_path}")


def main():
    parser = argparse.ArgumentParser(
        description='Multi-device CAN logger with candump / SocketCAN output'
    )
    parser.add_argument(
        '--output', '-o',
        default=None,
        help='Output log file path. If a bare filename (no directory), '
             'it is placed in --output-dir. Default: auto-generated timestamp name.'
    )
    parser.add_argument(
        '--output-dir', '-d',
        default=None,
        help='Directory for output log files. Used when --output is a bare filename '
             'or when no --output is given. Default: current working directory.'
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
        help='Comma-separated list of dongle brands to capture from '
             '(e.g. kvaser or ixxat,kvaser). Default: auto-detect all brands.'
    )
    parser.add_argument(
        '--passthrough',
        action='store_true',
        help='Passthrough bridge mode: forward frames between exactly 2 dongles '
             '(narrow with --brands if more are connected), logging each original '
             'frame tagged by the bus it was sourced on.'
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

    # Resolve output directory: explicit arg > current working directory.
    if args.output_dir is not None:
        output_dir = Path(args.output_dir)
    else:
        output_dir = Path.cwd()

    # Generate default output filename if not specified
    if args.output is None:
        now = datetime.datetime.now()
        stem = f"canlog_{now.strftime('%Y%m%d_%H%M%S')}.log"
    else:
        stem = args.output

    output_path = Path(stem)
    if output_path.parent == Path('.'):
        # Bare filename -> place in output_dir
        args.output = output_dir / output_path
    else:
        # Caller supplied a path with a directory — use it as-is
        args.output = output_path

    args.output.parent.mkdir(parents=True, exist_ok=True)

    logger.info("=" * 60)
    logger.info("Multi-device CAN Logger (candump / SocketCAN format)")
    logger.info("=" * 60)
    logger.info(f"Bitrate: {args.bitrate} bps")
    logger.info(f"Output: {args.output}")
    logger.info("")

    # Parse brands filter
    brands_filter = None
    if args.brands:
        brands_filter = {b.strip().lower() for b in args.brands.split(',')}
        logger.info(f"Brand filter: {', '.join(sorted(brands_filter))}")

    # Detect all CAN interfaces
    logger.info("Detecting CAN interfaces...")
    interfaces = detect_all_can_interfaces(args.bitrate, brands=brands_filter)

    if not interfaces:
        logger.error("No CAN interfaces detected!")
        logger.error("Please check:")
        logger.error("  - CAN devices are connected")
        logger.error("  - Drivers are installed")
        logger.error("  - python-can is properly configured")
        sys.exit(1)

    logger.info(f"Found {len(interfaces)} interface(s):")
    for i, (iface, ch, desc) in enumerate(interfaces):
        logger.info(f"  Bus {i} ({logio.bus_channel(i)}): {desc}")
    logger.info("")

    if args.passthrough:
        run_passthrough(interfaces, args.output, args.bitrate)
        return

    # Create buses
    buses = []
    message_queue = Queue()

    try:
        for bus_id, (iface, ch, desc) in enumerate(interfaces):
            try:
                if isinstance(ch, dict):
                    bus = can.interface.Bus(interface=iface, bitrate=args.bitrate, **ch)
                else:
                    bus = can.interface.Bus(interface=iface, channel=ch, bitrate=args.bitrate)
                bus_logger = CANBusLogger(bus_id, bus, message_queue, iface, ch, args.bitrate)
                buses.append(bus_logger)
                logger.info(f"Created bus {bus_id}: {desc}")
            except Exception as e:
                logger.error(f"Failed to create bus {bus_id} ({desc}): {e}")

        if not buses:
            logger.error("Failed to create any CAN buses!")
            sys.exit(1)

        # Set up signal handler for graceful shutdown
        shutdown_event = threading.Event()

        def signal_handler(sig, frame):
            logger.info("\nShutdown signal received...")
            shutdown_event.set()

        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)

        # Start logging from all buses
        logger.info("")
        logger.info("Starting logging...")
        logger.info("Press Ctrl+C to stop")
        logger.info("")

        for bus_logger in buses:
            bus_logger.start()

        # Drain the queue to the candump writer. The per-message channel carries
        # the logical bus id; the timestamp is stamped at consume time to give a
        # consistent absolute-epoch timeline regardless of dongle-backend clock.
        message_count = 0
        with logio.open_writer(args.output) as writer:
            last_status = time.time()

            while not shutdown_event.is_set():
                try:
                    timestamp, bus_id, msg, direction = message_queue.get(timeout=0.5)
                    msg.channel = logio.bus_channel(bus_id)
                    msg.timestamp = time.time()
                    writer(msg)
                    message_count += 1

                    now = time.time()
                    if now - last_status >= 5.0:
                        logger.info(f"Logged {message_count} messages...")
                        last_status = now

                except Empty:
                    continue
                except Exception as e:
                    logger.error(f"Error writing message: {e}")

        logger.info(f"Wrote {message_count} messages to {args.output}")

    finally:
        logger.info("")
        logger.info("Stopping buses...")
        for bus_logger in buses:
            bus_logger.stop()

    logger.info("")
    logger.info("=" * 60)
    logger.info("Logging complete")
    logger.info("=" * 60)


if __name__ == '__main__':
    main()
