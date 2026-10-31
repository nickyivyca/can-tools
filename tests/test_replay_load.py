"""Loading captures for replay (canbench.replay): format by content, _T chunks,
buses by anchors, refusal of a bare channel number, and what is never sent.

Every fixture is written by hand inside the test -- never copied from a capture.

Run standalone:   python tests/test_replay_load.py
Or under pytest:  python -m pytest tests/test_replay_load.py
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from canbench.replay import ReplayInputError, load_for_replay, session_chunks


def _head(date, time, a1="Innomaker", a2="Innomaker"):
    return f"""***BUSMASTER Ver 3.2.2***
***PROTOCOL CAN***
***[START LOGGING SESSION]***
***START DATE AND TIME {date} {time}***
***HEX***
***START CHANNEL BAUD RATE***
***CHANNEL 1 - {a1} - 500000 bps***
***CHANNEL 2 - {a2} - 500000 bps***
***END CHANNEL BAUD RATE***
***<Time><Tx/Rx><Channel><CAN ID><Type><DLC><DataBytes>***
"""


# Coda: channel 1 C CAN (cells), channel 2 D CAN; T1 renumbers D CAN to channel 1
# and runs past midnight.
T0 = _head("02:01:2026", "23:59:58:000") + """23:59:58:0000 Rx 1 0x001 s 8 0F A0 0F A1 0F A2 0F A3
23:59:58:0010 Rx 1 0x002 s 8 0F A0 0F A1 0F A2 0F A3
23:59:58:0020 Rx 2 0x308 s 2 01 02
23:59:58:0030 Rx 2 0x312 s 2 03 04
23:59:58:5000 Rx 2 0x7DF ERR 8 00 00 00 00 00 00 00 00
"""
T1 = _head("03:01:2026", "00:00:00:500") + """00:00:00:5000 Rx 1 0x308 s 2 05 06
00:00:00:5010 Rx 1 0x325 s 2 07 08
00:00:00:5020 Rx 2 0x001 s 8 0F A0 0F A1 0F A2 0F A3
00:00:00:5030 Rx 2 0x003 s 8 0F A0 0F A1 0F A2 0F A3
"""


def _dir():
    return Path(tempfile.mkdtemp(prefix="canbench-replay-"))


def _session():
    d = _dir()
    (d / "coda_20260102_235958_T0.log").write_text(T0, encoding="utf-8")
    (d / "coda_20260102_235958_T1.log").write_text(T1, encoding="utf-8")
    (d / "coda_20260102_235958_M1_T0.log").write_text(T0, encoding="utf-8")
    return d


def _raises(fn, *a, **k):
    try:
        fn(*a, **k)
    except ReplayInputError as e:
        return str(e)
    raise AssertionError("expected ReplayInputError")


def test_session_chunks_join_t_not_m():
    d = _session()
    got = [p.name for p in session_chunks(d / "coda_20260102_235958_T1.log")]
    assert got == ["coda_20260102_235958_T0.log", "coda_20260102_235958_T1.log"]
    assert session_chunks(d / "other.log") == [d / "other.log"]


def test_chunks_chain_across_midnight_in_time_order():
    d = _session()
    load = load_for_replay(session_chunks(d / "coda_20260102_235958_T0.log"))
    ts = [m.timestamp for m in load.messages]
    assert ts == sorted(ts) and len(ts) == 8
    assert 2.4 < ts[4] - ts[3] < 2.6        # 23:59:58.003 -> 00:00:00.500 next day
    assert load.events == {"ERR": 1} and load.error_frames == 0


def test_bus_by_anchors_follows_renumbering():
    d = _session()
    load = load_for_replay(session_chunks(d / "coda_20260102_235958_T0.log"),
                           vehicle="coda", bus="d_can")
    assert [m.arbitration_id for m in load.messages] == [0x308, 0x312, 0x308, 0x325]
    assert {m.channel for m in load.messages} == {"d_can"}
    kept = {p.name: k for p, k in load.kept_channels.items()}
    assert kept == {"coda_20260102_235958_T0.log": {2: "d_can"},
                    "coda_20260102_235958_T1.log": {1: "d_can"}}


def test_all_channels_named_by_anchors():
    d = _session()
    load = load_for_replay([d / "coda_20260102_235958_T0.log"], vehicle="coda")
    assert load.keys == ["c_can", "d_can"]


def test_bare_channel_refused_for_busmaster_unless_raw():
    d = _session()
    p = [d / "coda_20260102_235958_T0.log"]
    msg = _raises(load_for_replay, p, bus="2")
    assert "--raw-channel" in msg and "--vehicle" in msg
    load = load_for_replay(p, bus="2", raw_channel=True)
    assert [m.arbitration_id for m in load.messages] == [0x308, 0x312]
    assert {m.channel for m in load.messages} == {2}


def test_bus_name_needs_vehicle_and_must_exist():
    d = _session()
    p = [d / "coda_20260102_235958_T0.log"]
    assert "--vehicle" in _raises(load_for_replay, p, bus="d_can")
    assert "b_can" in _raises(load_for_replay, p, vehicle="coda", bus="b_can")


def test_ambiguous_channel_refused():
    d = _dir()
    p = d / "merged.log"
    p.write_text(_head("02:01:2026", "10:00:00:000") +
                 "10:00:00:0000 Rx 1 0x001 s 8 0F A0 0F A1 0F A2 0F A3\n"
                 "10:00:00:0010 Rx 1 0x002 s 8 0F A0 0F A1 0F A2 0F A3\n"
                 "10:00:00:0020 Rx 1 0x308 s 2 01 02\n"
                 "10:00:00:0030 Rx 1 0x312 s 2 03 04\n", encoding="utf-8")
    msg = _raises(load_for_replay, [p], vehicle="coda", bus="d_can")
    assert "do not separate" in msg and "merged.log" in msg


def test_inferred_errors_and_status_frames_never_sent():
    d = _dir()
    p = d / "kv.log"
    # ch1 Kvaser B CAN with one status frame; ch2 Innomaker with a 0x024 report
    p.write_text(_head("02:01:2026", "10:00:00:000", a1="Kvaser") +
                 "10:00:00:0000 Rx 1 0x48A s 2 01 02\n"
                 "10:00:00:0010 Rx 1 0x000 s 4 00 48 00 00\n"
                 "10:00:00:0020 Rx 1 0x381 s 2 03 04\n"
                 "10:00:00:0030 Rx 2 0x024 s 8 00 20 00 00 00 00 80 00\n"
                 "10:00:00:0040 Rx 2 0x308 s 2 01 02\n", encoding="utf-8")
    load = load_for_replay([p], vehicle="coda")
    assert [m.arbitration_id for m in load.messages] == [0x48A, 0x381, 0x308]
    assert load.inferred_errors == {0x000: 1, 0x024: 1}
    assert load.resolutions[p].assignments == {1: "b_can"}


def test_trc_events_counted_and_channel_refused():
    d = _dir()
    p = d / "t.trc"
    p.write_text(""";$FILEVERSION=2.1
;$STARTTIME=45000.25
;$COLUMNS=N,O,T,B,I,d,R,L,D
       1       100.000 DT 1     0308 Rx - 2    01 02
       2       101.000 ER 1        - Rx - 5    04 01 0A 00 00
       3       102.500 DT 2     0001 Rx - 8    0F A0 0F A1 0F A2 0F A3
""", encoding="utf-8")
    load = load_for_replay([p])
    assert [m.arbitration_id for m in load.messages] == [0x308, 0x001]
    assert load.events == {"ER": 1} and load.fmt == "trc"
    # epoch-sized floats resolve ~0.2 us
    assert abs(load.messages[1].timestamp - load.messages[0].timestamp - 0.0025) < 1e-6
    assert "--raw-channel" in _raises(load_for_replay, [p], bus="1")


def test_candump_keeps_numeric_bus_and_skips_error_frames():
    d = _dir()
    p = d / "c.log"
    p.write_text("(1700000000.000000) can0 308#0102\n"
                 "(1700000000.001000) can1 001#0FA00FA10FA20FA3\n"
                 "(1700000000.002000) can0 20000080#0000000000000000\n", encoding="utf-8")
    load = load_for_replay([p], bus="1")
    assert [m.arbitration_id for m in load.messages] == [0x001]
    full = load_for_replay([p])
    assert full.error_frames == 1 and len(full.messages) == 2


def test_several_files_only_for_busmaster_chunks():
    d = _dir()
    a, b = d / "a.log", d / "b.log"
    a.write_text("(1700000000.000000) can0 308#0102\n", encoding="utf-8")
    b.write_text("(1700000001.000000) can0 308#0102\n", encoding="utf-8")
    assert "_T chunks" in _raises(load_for_replay, [a, b])


if __name__ == "__main__":
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except Exception as e:
                failed += 1
                print(f"FAIL {name}: {type(e).__name__}: {e}")
    sys.exit(1 if failed else 0)
