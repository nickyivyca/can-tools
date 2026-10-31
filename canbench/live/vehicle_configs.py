"""Vehicle anchor configurations and bus assignment detection.

Anchor IDs are CAN message IDs that identify one physical bus: they appear on that bus
and on no other, so a gateway rebroadcast (same frames on two buses) is never an anchor.
Channel numbers in a log are arbitrary (dongle insertion order), so buses are named from
anchors alone.

A channel is named only on its own evidence: >= ``min_anchors`` distinct anchors of one
bus and **zero** anchors of every other bus. Nothing is assigned by exclusion, so a bus
with no anchors (Coda Diag CAN) is never named. Anything ambiguous raises
``BusAssignmentError`` from ``detect_bus_assignments`` instead of picking a winner;
``resolve_buses`` returns the same verdicts with the evidence and never raises.

Coda's sets are the ones audited exclusive in reverse-it (`projects/coda/README.md`,
"Bus Identification -- Anchor IDs"; `anchor_exclusivity_audit.py` checks this table
against its own results). The audits are the evidence; this table is the one copy code
reads.

Build the observed ids with ``observed_ids_per_channel`` from frames parsed with
``mark_inferred_errors=True``: an adapter error report written as a data frame carries an
anchor's id (Coda C CAN 0x000, 0x004) and would put a C CAN anchor on another bus.

Usage:
    from canbench.live.vehicle_configs import VEHICLES, detect_bus_assignments
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, FrozenSet, Iterable, List, Set

DETECTION_WINDOW_S = 5.0

# Distinct anchor ids a channel must carry before it is named, so one frame cannot decide.
MIN_ANCHORS = 2


@dataclass
class BusConfig:
    anchors: FrozenSet[int]
    description: str = ""
    min_anchors: int = MIN_ANCHORS


@dataclass
class VehicleConfig:
    log_subdir: str          # relative to project root, e.g. "projects/coda/logs"
    buses: Dict[str, BusConfig]


VEHICLES: Dict[str, VehicleConfig] = {
    "coda": VehicleConfig(
        log_subdir="projects/coda/logs",
        buses={
            # C CAN: BMS cell voltage frames 0x000-0x019 -- always present when BMS is on.
            # 0x004 and 0x00C are not anchors: they are also the ids of gs_usb error
            # reports that pre-fix Android logs wrote as plain frames, on any bus.
            "c_can": BusConfig(
                anchors=frozenset(range(0x000, 0x01A)) - {0x004, 0x00C},
                description="C CAN (BMS cell voltages 0x000-0x019 except 0x004/0x00C)",
            ),
            # B CAN: 0x48A (DCDC) and 0x381 (cell min/max/avg) key-on; 0x615-0x617 charger,
            # only while charging. 0x1D5, 0x275, 0x383 are rebroadcast to D CAN.
            "b_can": BusConfig(
                anchors=frozenset({0x48A, 0x381, 0x615, 0x616, 0x617}),
                description="B CAN (battery/body; 0x48A, 0x381 key-on, 0x615-0x617 charging)",
            ),
            # D CAN: driveline control modules
            "d_can": BusConfig(
                anchors=frozenset({0x308, 0x312, 0x325, 0x330, 0x580}),
                description="D CAN (driveline, accessible at OBD connector)",
            ),
            # Diag CAN: silent at rest, so it has no anchors and is never named.
            "diag_can": BusConfig(
                anchors=frozenset(),
                description="Diag CAN (scan tool routing via GWM; silent at rest, never named)",
            ),
        },
    ),
    "vtrux": VehicleConfig(
        log_subdir="projects/vtrux/logs",
        buses={
            # Powertrain: 0x051 plus the ids of the README's 0x592-0x5A2 and 0x639-0x64B
            # ranges that every powertrain channel carries, none ever seen on another bus
            # (reverse-it anchor_discovery_audit.py + pt_anchor_crosscheck.py, 2026-10-30).
            "powertrain": BusConfig(
                anchors=frozenset({
                    0x051,
                    0x592, 0x594, 0x596, 0x597, 0x599, 0x5A0, 0x5A2,
                    0x639, 0x63A, 0x63C, 0x63D, 0x63F, 0x640,
                    0x644, 0x645, 0x646, 0x647, 0x648, 0x649, 0x64A, 0x64B,
                }),
                description="Powertrain CAN (0x051 ~100 Hz, 0x592-0x5A2, 0x639-0x64B)",
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


class BusAssignmentError(ValueError):
    """The anchors do not separate: a channel cannot be named without guessing."""


@dataclass
class BusResolution:
    """Verdicts of ``resolve_buses``.

    assignments  {channel: bus} for channels named on their own evidence
    hits         {channel: {bus: distinct anchors seen}} for every channel, zeros left out
    unresolved   {channel: reason} for channels not named, ambiguous or not
    ambiguous    reasons that make ``detect_bus_assignments`` raise
    """
    assignments: Dict[int, str] = field(default_factory=dict)
    hits: Dict[int, Dict[str, int]] = field(default_factory=dict)
    unresolved: Dict[int, str] = field(default_factory=dict)
    ambiguous: List[str] = field(default_factory=list)

    def channels_for(self, bus_name: str) -> List[int]:
        """Channels named ``bus_name`` (more than one when a logger renumbered a bus)."""
        return sorted(ch for ch, b in self.assignments.items() if b == bus_name)


def observed_ids_per_channel(frames: Iterable) -> Dict[int, Set[int]]:
    """{channel: ids seen} from ``canbench.parsers`` records, for ``resolve_buses``.

    Leaves out error frames and frames marked ``inferred_error`` (parse with
    ``mark_inferred_errors=True``), so an adapter's error report cannot act as an anchor.
    Non-frame records (``CANEvent``) are skipped.
    """
    from canbench.parsers.base import CANFrame
    out: Dict[int, Set[int]] = {}
    for f in frames:
        if not isinstance(f, CANFrame) or f.is_error or f.inferred_error:
            continue
        out.setdefault(f.bus, set()).add(f.arbitration_id)
    return out


def resolve_buses(
    vehicle_config: VehicleConfig,
    observed_ids_per_channel: Dict[int, Set[int]],
) -> BusResolution:
    """Name each channel from its anchors, per the module rule; never raises.

    A channel is ambiguous when it carries >= min_anchors of one bus and any anchor of
    another (a merged or rebroadcast channel). A channel with no qualifying bus is left
    unresolved but is not ambiguous: too few anchors is missing evidence, not a conflict.
    Two channels may carry the same bus (a logger that renumbered it after a reconnect);
    each is named, since each label rests on its own evidence.

    Args:
        vehicle_config: VehicleConfig with bus definitions
        observed_ids_per_channel: {channel_id: set of observed CAN IDs}
    """
    res = BusResolution()
    named = [(n, c) for n, c in vehicle_config.buses.items() if c.anchors]

    for ch, ids in observed_ids_per_channel.items():
        hits = {n: len(c.anchors & ids) for n, c in named}
        res.hits[ch] = {n: k for n, k in hits.items() if k}
        qualified = [n for n, c in named if hits[n] >= c.min_anchors]
        if len(qualified) == 1 and len(res.hits[ch]) == 1:
            res.assignments[ch] = qualified[0]
            continue
        if qualified:
            why = (f"channel {ch}: anchors of {len(res.hits[ch])} buses "
                   f"({_fmt(res.hits[ch])}), so they do not separate")
            res.unresolved[ch] = why
            res.ambiguous.append(why)
        elif res.hits[ch]:
            res.unresolved[ch] = f"channel {ch}: too few anchors ({_fmt(res.hits[ch])})"
        else:
            res.unresolved[ch] = f"channel {ch}: no anchors"
    return res


def detect_bus_assignments(
    vehicle_config: VehicleConfig,
    observed_ids_per_channel: Dict[int, Set[int]],
) -> Dict[int, str]:
    """{channel: bus} for the channels anchors name; raises if any channel is ambiguous.

    Channels with too few or no anchors are left out of the result. See
    ``resolve_buses`` for the rule and for the evidence behind each verdict.

    Raises:
        BusAssignmentError: a channel carries anchors of more than one bus.
    """
    res = resolve_buses(vehicle_config, observed_ids_per_channel)
    if res.ambiguous:
        raise BusAssignmentError("; ".join(res.ambiguous))
    return res.assignments


def _fmt(hits: Dict[str, int]) -> str:
    return ", ".join(f"{n}={k}" for n, k in sorted(hits.items())) or "none"
