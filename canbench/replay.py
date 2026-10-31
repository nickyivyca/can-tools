"""Loading a capture for replay: format by content, buses by anchors.

``load_for_replay`` turns one or more capture files into the time-ordered list of
``can.Message`` objects ``tools/can_replayer.py`` transmits, and reports what it left
out. Each message's ``channel`` is its output key: the bus name when the buses were
resolved by anchors, else the log's channel number.

Rules (owner ruling 4, reverse-it ``tools/plans/can-replay-readers.md`` step 5):

- **Format by content.** BUSMASTER and PEAK TRC are read by ``canbench.parsers``;
  everything else by ``logio.read_messages`` (python-can by extension, SavvyCAN CSV).
- **BUSMASTER ``_T`` chunks chain** on each chunk's ``***START DATE AND TIME***``
  header (date and time): a chunk's frames keep their spacing relative to its first
  frame, which is placed at the header start (``logio.busmaster_start``), so a session
  crossing midnight stays in order. Several files without that header are refused:
  there is nothing to chain them on.
- **Buses by anchors.** ``vehicle`` + a bus name resolves every file on its own
  (a logger can renumber a bus between chunks) and keeps the channels named that
  bus. A file whose anchors do not separate raises instead of guessing.
- **A bare channel number is not a bus.** For BUSMASTER and TRC input, a numeric bus
  is refused unless ``raw_channel`` is set. candump channels keep the old behaviour.
- **Never transmitted, always counted:** non-frame records (``CANEvent`` by kind),
  error frames, and frames marked ``inferred_error`` (adapter error reports and
  status frames written as data frames; ``parsers/inferred_errors.py``).
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Union

import can

from canbench import logio
from canbench.live.vehicle_configs import (
    VEHICLES, BusResolution, observed_ids_per_channel, resolve_buses,
)
from canbench.parsers import CANEvent, CANFrame, detect_format, parse_file
from canbench.parsers.busmaster import BusmasterParser
from canbench.parsers.peak_trc import PeakTrcParser, start_time

_CHUNK_RE = re.compile(r"^(?P<prefix>.+_T)(?P<n>\d+)$")


class ReplayInputError(ValueError):
    """The input cannot be replayed as asked; the message says why and what to do."""


def assign_adapters(buses, adapters, pins=(), matches=None):
    """Which adapter each replayed bus goes out on: ``{bus: index into adapters}``.

    ``pins`` are ``BUS=ID`` strings (``--adapter``): BUS is a replayed bus as
    the summary names it (``d_can``, ``1``), ID one of the adapter's ids (see
    ``canbench.live.receiver.Adapter``; a bare serial works). ``matches(adapter,
    id)`` defaults to ``receiver.adapter_matches``.

    One adapter and no pins: every bus goes out on it. Two or more adapters:
    every bus must be pinned to exactly one. Detection order follows plug order
    (canN, PCAN_USBBUSn, Kvaser channel numbers, a libusb scan index), so
    taking the first adapter, or pairing buses with adapters by position, would
    be a guess about which wire each adapter is on. Raises ReplayInputError
    naming the adapters and their ids instead.
    """
    if matches is None:
        from canbench.live.receiver import adapter_matches as matches
    listing = "\n".join(f"  {getattr(a, 'desc', a[2])}  ids: "
                         f"{', '.join(getattr(a, 'ids', ())) or 'none'}" for a in adapters)
    names = {str(b).lower(): b for b in buses}
    out = {}
    for pin in pins:
        bus, sep, key = pin.partition("=")
        if not sep or not bus.strip() or not key.strip():
            raise ReplayInputError(f"--adapter {pin!r}: expected BUS=ID, e.g. d_can=serial:HW123456")
        b = names.get(bus.strip().lower())
        if b is None:
            raise ReplayInputError(f"--adapter {pin!r}: no replayed bus {bus.strip()!r}; "
                                   f"buses: {', '.join(str(x) for x in buses)}")
        hit = [i for i, a in enumerate(adapters) if matches(a, key)]
        if len(hit) != 1:
            raise ReplayInputError(
                f"--adapter {pin!r}: {'no adapter has' if not hit else f'{len(hit)} adapters have'} "
                f"id {key.strip()!r}. Detected:\n{listing}")
        if b in out and out[b] != hit[0]:
            raise ReplayInputError(f"--adapter: bus {b} pinned twice")
        out[b] = hit[0]
    if out:
        missing = [b for b in buses if b not in out]
        if missing:
            raise ReplayInputError(f"--adapter: pin every replayed bus; not pinned: "
                                   f"{', '.join(str(b) for b in missing)}")
        return out
    if len(adapters) == 1:
        return {b: 0 for b in buses}
    raise ReplayInputError(
        f"{len(adapters)} adapters detected and nothing says which bus goes out on which. "
        f"Pin each bus with --adapter BUS=ID (buses: {', '.join(str(b) for b in buses)}), "
        f"or narrow --brands to one adapter. Detected:\n{listing}")


@dataclass
class ReplayLoad:
    """What ``load_for_replay`` read, kept and left out."""
    messages: List[can.Message]
    files: List[Path]
    fmt: str
    selection: str
    events: Counter = field(default_factory=Counter)          # CANEvent kind -> count
    error_frames: int = 0
    inferred_errors: Counter = field(default_factory=Counter)  # arbitration id -> count
    resolutions: Dict[Path, BusResolution] = field(default_factory=dict)
    # {file: {channel: output key}} for every channel kept
    kept_channels: Dict[Path, Dict[int, Union[int, str]]] = field(default_factory=dict)

    @property
    def keys(self) -> List[Union[int, str]]:
        """Output keys in a stable order (numbers first, then names)."""
        ks = {m.channel for m in self.messages}
        return sorted(ks, key=lambda k: (isinstance(k, str), k))


def session_chunks(path: Union[Path, str]) -> List[Path]:
    """All ``<prefix>_T<n>`` siblings of a chunk, in chunk order (``_M`` numbers are
    separate sessions and are not joined). A file not named ``_T<n>`` is returned alone."""
    path = Path(path)
    m = _CHUNK_RE.match(path.stem)
    if not m:
        return [path]
    prefix = m.group("prefix")
    found = []
    for p in path.parent.glob(f"{Path(prefix).name}*{path.suffix}"):
        mm = _CHUNK_RE.match(p.stem)
        if mm and mm.group("prefix") == prefix:
            found.append((int(mm.group("n")), p))
    return [p for _, p in sorted(found)]


def _is_bus_name(bus) -> bool:
    return bus is not None and not str(bus).isdigit()


def load_for_replay(paths, *, vehicle: Optional[str] = None, bus=None,
                    raw_channel: bool = False) -> ReplayLoad:
    """Read *paths* for replay. *bus* is a bus name (needs *vehicle*) or a channel
    number; see the module docstring for the rules. Raises ``ReplayInputError``."""
    files = [Path(p) for p in paths]
    if not files:
        raise ReplayInputError("no input files")
    for p in files:
        if not p.exists():
            raise ReplayInputError(f"log file not found: {p}")
    if vehicle is not None and vehicle not in VEHICLES:
        raise ReplayInputError(f"unknown vehicle {vehicle!r}; known: {', '.join(sorted(VEHICLES))}")
    if _is_bus_name(bus) and vehicle is None:
        raise ReplayInputError(f"--bus {bus} is a bus name: give --vehicle so it can be found by anchors")

    kinds = set()
    for p in files:
        try:
            kinds.add(type(detect_format(p)))
        except ValueError:
            kinds.add(None)
    if len(kinds) != 1:
        raise ReplayInputError("all input files must be one format")
    kind = kinds.pop()
    own = kind in (BusmasterParser, PeakTrcParser)
    if len(files) > 1 and kind is not BusmasterParser:
        raise ReplayInputError("several files are joined only for BUSMASTER _T chunks")
    if own and bus is not None and not _is_bus_name(bus) and not raw_channel:
        raise ReplayInputError(
            f"--bus {bus} is a channel number, and in a BUSMASTER/TRC capture that is dongle "
            f"insertion order, not a bus. Use --vehicle <v> --bus <name> to pick the bus by "
            f"anchors, or add --raw-channel to replay channel {bus} as it is.")

    load = ReplayLoad(messages=[], files=files, fmt="", selection="all channels")
    per_file: List[tuple] = []          # (path, [(t, frame-like, channel)])

    if own:
        load.fmt = "busmaster" if kind is BusmasterParser else "trc"
        bases = {}
        if kind is BusmasterParser:
            for p in files:
                bases[p] = logio.busmaster_start(p)
            if len(files) > 1 and any(b is None for b in bases.values()):
                missing = [p.name for p, b in bases.items() if b is None]
                raise ReplayInputError(
                    f"cannot chain chunks without a START DATE AND TIME header: {', '.join(missing)}")
        for p in files:
            frames = []
            base = (bases[p] if kind is BusmasterParser else start_time(p)) or 0.0
            for rec in parse_file(p, include_events=True, mark_inferred_errors=True):
                if isinstance(rec, CANEvent):
                    load.events[rec.kind] += 1
                elif rec.is_error:
                    load.error_frames += 1
                elif rec.inferred_error:
                    load.inferred_errors[rec.arbitration_id] += 1
                else:
                    frames.append(rec)
            per_file.append((p, base, frames))
    else:
        load.fmt = files[0].suffix.lower().lstrip(".") or "unknown"
        frames = []
        for msg in logio.read_messages(files[0]):
            if msg.is_error_frame:
                load.error_frames += 1
                continue
            frames.append(msg)
        per_file.append((files[0], 0.0, frames))

    def chan(fr):
        return fr.bus if isinstance(fr, CANFrame) else logio.channel_bus_id(fr.channel)

    # ---- bus resolution, per file ----
    if vehicle is not None:
        cfg = VEHICLES[vehicle]
        problems = []
        for p, _, frames in per_file:
            if frames and isinstance(frames[0], CANFrame):
                observed = observed_ids_per_channel(frames)
            else:
                observed = {}
                for fr in frames:
                    observed.setdefault(chan(fr), set()).add(fr.arbitration_id)
            res = resolve_buses(cfg, observed)
            load.resolutions[p] = res
            problems += [f"{p.name}: {why}" for why in res.ambiguous]
        if problems:
            raise ReplayInputError(
                "the anchors do not separate, so no channel can be named without guessing: "
                + "; ".join(problems))

    # ---- selection ----
    want_name = _is_bus_name(bus)
    want_num = int(bus) if bus is not None and not want_name else None
    out = []
    for p, base, frames in per_file:
        res = load.resolutions.get(p)
        keep: Dict[int, Union[int, str]] = {}
        for ch in sorted({chan(fr) for fr in frames}):
            name = res.assignments.get(ch) if res else None
            if want_name:
                if name == bus:
                    keep[ch] = name
            elif want_num is not None:
                if ch == want_num:
                    keep[ch] = ch
            else:
                keep[ch] = name if name else ch
        load.kept_channels[p] = keep
        for fr in frames:
            k = keep.get(chan(fr))
            if k is None:
                continue
            if isinstance(fr, CANFrame):
                out.append(can.Message(timestamp=base + fr.timestamp, arbitration_id=fr.arbitration_id,
                                       is_extended_id=fr.is_extended, dlc=fr.dlc, data=fr.data,
                                       channel=k, is_rx=not fr.is_tx))
            else:
                out.append(can.Message(timestamp=fr.timestamp, arbitration_id=fr.arbitration_id,
                                       is_extended_id=fr.is_extended_id, dlc=fr.dlc, data=fr.data,
                                       is_remote_frame=fr.is_remote_frame, channel=k,
                                       is_rx=fr.is_rx))

    if want_name:
        if not out:
            seen = {p.name: dict(r.assignments) for p, r in load.resolutions.items()}
            raise ReplayInputError(f"no channel resolves to bus {bus!r} ({vehicle}); "
                                   f"resolved per file: {seen}")
        load.selection = f"bus {bus} by anchors ({vehicle})"
    elif want_num is not None:
        if not out:
            have = sorted({c for keep in load.kept_channels.values() for c in keep} |
                          {chan(fr) for _, _, frs in per_file for fr in frs})
            raise ReplayInputError(f"channel {want_num} not found; channels present: {have}")
        load.selection = f"raw channel {want_num}" if own else f"log bus {want_num}"
    elif vehicle is not None:
        load.selection = f"all channels, named by anchors ({vehicle})"

    out.sort(key=lambda m: m.timestamp)        # file order is not time order
    load.messages = out
    return load
