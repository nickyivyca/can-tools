"""Vehicle anchor configurations and bus assignment detection.

Anchor IDs are CAN message IDs that definitively identify a specific physical bus.
Silent buses (empty anchor set) are assigned by exclusion after all named buses are matched.

Usage:
    from canbench.live.vehicle_configs import VEHICLES, detect_bus_assignments
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, FrozenSet, Set

DETECTION_WINDOW_S = 5.0


@dataclass
class BusConfig:
    anchors: FrozenSet[int]
    description: str = ""


@dataclass
class VehicleConfig:
    log_subdir: str          # relative to project root, e.g. "projects/coda/logs"
    buses: Dict[str, BusConfig]


VEHICLES: Dict[str, VehicleConfig] = {
    "coda": VehicleConfig(
        log_subdir="projects/coda/logs",
        buses={
            # C CAN: BMS cell voltage frames 0x000-0x019 — always present when BMS is on
            "c_can": BusConfig(
                anchors=frozenset(range(0x000, 0x01A)),
                description="C CAN (BMS cell voltages 0x000-0x019, always present)",
            ),
            # B CAN: battery/body modules — key-on
            "b_can": BusConfig(
                anchors=frozenset({0x1D5, 0x48A, 0x275, 0x381, 0x383, 0x492}),
                description="B CAN (battery/body, key-on)",
            ),
            # D CAN: driveline control modules
            "d_can": BusConfig(
                anchors=frozenset({0x308, 0x312, 0x325, 0x330, 0x580}),
                description="D CAN (driveline, accessible at OBD connector)",
            ),
            # Diag CAN: silent at rest — zero broadcast frames confirmed in full drive capture
            "diag_can": BusConfig(
                anchors=frozenset(),
                description="Diag CAN (scan tool routing via GWM; silent at rest)",
            ),
        },
    ),
    "vtrux": VehicleConfig(
        log_subdir="projects/vtrux/logs",
        buses={
            "powertrain": BusConfig(
                anchors=frozenset({0x051}),
                description="Powertrain CAN (0x051 ~100 Hz)",
            ),
            "gm_unfiltered": BusConfig(
                anchors=frozenset({0x1E5, 0x500, 0x514, 0x52A}),
                description="Vehicle CAN",
            ),
            "gm_obd": BusConfig(
                anchors=frozenset({0x040, 0x041, 0x042, 0x043, 0x044}),
                description="OBD CAN (OBD-II DLC)",
            ),
        },
    ),
}


def detect_bus_assignments(
    vehicle_config: VehicleConfig,
    observed_ids_per_channel: Dict[int, Set[int]],
) -> Dict[int, str]:
    """Match observed ID sets to named buses via anchor intersection.

    Greedy best-match: each named bus is assigned to the channel with the most
    anchor hits. Silent buses (empty anchor set) are assigned by exclusion to
    any unmatched channel.

    Args:
        vehicle_config: VehicleConfig with bus definitions
        observed_ids_per_channel: {channel_id: set of observed CAN IDs}

    Returns:
        {channel_id: bus_name}
    """
    assignments: Dict[int, str] = {}
    assigned_channels: Set[int] = set()

    named  = [(n, c) for n, c in vehicle_config.buses.items() if c.anchors]
    silent = [(n, c) for n, c in vehicle_config.buses.items() if not c.anchors]

    for bus_name, bus_cfg in named:
        best_ch    = None
        best_hits  = 0
        for ch, ids in observed_ids_per_channel.items():
            if ch in assigned_channels:
                continue
            hits = len(bus_cfg.anchors & ids)
            if hits > best_hits:
                best_hits = hits
                best_ch   = ch
        if best_ch is not None and best_hits > 0:
            assignments[best_ch] = bus_name
            assigned_channels.add(best_ch)

    for bus_name, _ in silent:
        for ch in observed_ids_per_channel:
            if ch not in assigned_channels:
                assignments[ch] = bus_name
                break

    return assignments
