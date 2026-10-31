"""Bus naming by anchors in canbench.live.vehicle_configs.

The id sets are written by hand here -- never taken from a real capture.

Run standalone:   python tests/test_vehicle_configs.py
Or under pytest:  python -m pytest tests/test_vehicle_configs.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from canbench.live.vehicle_configs import (
    VEHICLES, BusAssignmentError, BusConfig, VehicleConfig, detect_bus_assignments,
    resolve_buses,
)

CODA = VEHICLES["coda"]
CELLS = set(range(0x000, 0x01A)) - {0x004, 0x00C}
B_KEYON = {0x48A, 0x381}
D_IDS = {0x308, 0x312, 0x325, 0x330, 0x580}
REBROADCAST = {0x1D5, 0x275, 0x383, 0x212, 0x1C0, 0x208, 0x210}


def _raises(fn, *args):
    try:
        fn(*args)
    except BusAssignmentError as e:
        return str(e)
    raise AssertionError("expected BusAssignmentError")


def test_coda_table():
    assert CODA.buses["c_can"].anchors == frozenset(CELLS)
    assert CODA.buses["b_can"].anchors == frozenset(B_KEYON | {0x615, 0x616, 0x617})
    assert CODA.buses["d_can"].anchors == frozenset(D_IDS)
    assert not CODA.buses["diag_can"].anchors
    for bus in CODA.buses.values():
        assert not bus.anchors & REBROADCAST
        assert not bus.anchors & {0x004, 0x00C, 0x492}


def test_coda_three_buses():
    obs = {1: D_IDS | REBROADCAST, 2: B_KEYON | REBROADCAST, 3: CELLS | {0x200}}
    assert detect_bus_assignments(CODA, obs) == {1: "d_can", 2: "b_can", 3: "c_can"}


def test_lone_d_can_is_not_b_can():
    # The old greedy matcher named this b_can on three rebroadcast ids.
    assert detect_bus_assignments(CODA, {1: D_IDS | REBROADCAST}) == {1: "d_can"}


def test_error_report_ids_do_not_make_c_can():
    # Pre-fix Android error reports on a B CAN channel: 0x004 / 0x00C / 0x024 as frames.
    obs = {1: B_KEYON | {0x004, 0x00C, 0x024}}
    assert detect_bus_assignments(CODA, obs) == {1: "b_can"}


def test_single_anchor_is_not_evidence():
    res = resolve_buses(CODA, {1: {0x381} | REBROADCAST})
    assert res.assignments == {}
    assert not res.ambiguous
    assert "too few anchors" in res.unresolved[1]
    assert detect_bus_assignments(CODA, {1: {0x381}}) == {}


def test_no_diag_can_by_exclusion():
    obs = {1: D_IDS, 2: REBROADCAST, 3: set()}
    res = resolve_buses(CODA, obs)
    assert res.assignments == {1: "d_can"}
    assert "diag_can" not in res.assignments.values()
    assert res.unresolved[2].endswith("no anchors")
    assert res.unresolved[3].endswith("no anchors")


def test_contaminated_channel_raises():
    # Enough B CAN anchors, plus one cell id: the anchors do not separate.
    msg = _raises(detect_bus_assignments, CODA, {1: CELLS, 2: B_KEYON | {0x000}})
    assert "channel 2" in msg and "b_can=2" in msg and "c_can=1" in msg
    res = resolve_buses(CODA, {1: CELLS, 2: B_KEYON | {0x000}})
    assert res.assignments == {1: "c_can"}
    assert len(res.ambiguous) == 1


def test_merged_channel_raises():
    msg = _raises(detect_bus_assignments, CODA, {5: CELLS | B_KEYON | D_IDS})
    assert "anchors of 3 buses" in msg


def test_partial_hits_of_two_buses_are_not_ambiguous():
    res = resolve_buses(CODA, {1: {0x381, 0x308}})
    assert res.assignments == {} and not res.ambiguous


def test_same_bus_on_two_channels():
    # A logger that renumbered the bus after a reconnect: both labels stand on evidence.
    res = resolve_buses(CODA, {3: D_IDS, 21: {0x308, 0x312}})
    assert res.assignments == {3: "d_can", 21: "d_can"}
    assert res.channels_for("d_can") == [3, 21]


def test_charging_ids_count():
    assert detect_bus_assignments(CODA, {1: {0x615, 0x616}}) == {1: "b_can"}


def test_vtrux_powertrain_needs_two_anchors():
    vt = VEHICLES["vtrux"]
    assert len(vt.buses["powertrain"].anchors) == 22
    assert detect_bus_assignments(vt, {1: {0x051, 0x639}, 2: {0x1E5, 0x500},
                                       3: {0x040, 0x041}}) == {
        1: "powertrain", 2: "gm_unfiltered", 3: "gm_obd"}
    assert detect_bus_assignments(vt, {1: {0x051}}) == {}
    # All three buses merged onto one channel (a one-dongle replay of a 3-bus log).
    _raises(detect_bus_assignments, vt, {2: {0x051, 0x639, 0x1E5, 0x500, 0x040, 0x041}})


def test_per_bus_min_anchors():
    cfg = VehicleConfig("x", {"a": BusConfig(frozenset({1}), min_anchors=1),
                              "b": BusConfig(frozenset({2, 3}))})
    assert detect_bus_assignments(cfg, {1: {1}, 2: {2}}) == {1: "a"}
    assert "a=1, b=2" in _raises(detect_bus_assignments, cfg, {1: {1, 2, 3}})


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"PASS {t.__name__}")
    print(f"ALL PASS ({len(tests)})")
