"""parse_file(ids=..., clock_ids=...) for BUSMASTER and PEAK TRC.

Every fixture is written by hand inside the test; nothing comes from a capture.

The contract (canbench.parsers.base.id_filter_sets):
  ids        only frames of these ids exist for the call: other ids and non-frame
             lines are skipped before the time origin / rollover, i.e. the result
             equals parsing a copy of the file filtered to those frames.
  clock_ids  the kept ids whose frames define the origin and (BUSMASTER) the
             midnight rollover; other kept frames are stamped with the time base
             in force and dropped before the first clock frame.

Run standalone:   python tests/test_parsers_id_filter.py
Or under pytest:  python -m pytest tests/test_parsers_id_filter.py
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from canbench.parsers import parse_file
from canbench.parsers.base import id_filter_sets

HEADER = """***BUSMASTER Ver 3.2.2***
***PROTOCOL CAN***
***NOTE: PLEASE DO NOT EDIT THIS DOCUMENT***
***[START LOGGING SESSION]***
***START DATE AND TIME 02:01:2026 23:59:58:000***
***HEX***
***SYSTEM MODE***
***START CHANNEL BAUD RATE***
***CHANNEL 1 - Innomaker - 500000 bps***
***CHANNEL 2 - Innomaker - 500000 bps***
***END CHANNEL BAUD RATE***
***START DATABASE FILES***
***END DATABASE FILES***
***<Time><Tx/Rx><Channel><CAN ID><Type><DLC><DataBytes>***
"""

# An ERR line first (its class-mask prints as 0x200, which a text grep for id
# 0x200 would match), a yaw-like id 0x300 before the first clock frame, an
# out-of-order write, and a midnight rollover seen by the clock frames.
LINES = [
    "23:59:58:0000 Rx 1 0x200 ERR 8 00 30 02 00 00 00 6D FF",
    "23:59:58:1000 Rx 2 0x300 s 8 00 00 00 00 00 00 80 18",
    "23:59:58:5000 Rx 1 0x100 s 8 01 00 00 00 00 00 00 00",
    "23:59:58:6000 Rx 2 0x300 s 8 00 00 00 00 00 00 80 19",
    "23:59:58:7000 Rx 1 0x7AB s 8 AA 00 00 00 00 00 00 00",
    "23:59:58:9000 Rx 1 0x200 s 8 02 00 00 00 00 00 00 00",
    "23:59:58:8500 Rx 1 0x100 s 8 03 00 00 00 00 00 00 00",
    "23:59:59:9000 Rx 1 0x100 s 8 04 00 00 00 00 00 00 00",
    "00:00:00:1000 Rx 2 0x300 s 8 00 00 00 00 00 00 80 1A",
    "00:00:00:2000 Rx 1 0x100 s 8 05 00 00 00 00 00 00 00",
    "00:00:00:3000 Rx 1 0x200 s 8 06 00 00 00 00 00 00 00",
]


def _write(tmp, name, text):
    p = Path(tmp) / name
    p.write_text(text)
    return p


def _key(fr):
    return (fr.timestamp, fr.arbitration_id, fr.bus, bytes(fr.data))


def test_bm_ids_equals_prefiltered_copy():
    keep = {0x100, 0x200}
    with tempfile.TemporaryDirectory() as tmp:
        full = _write(tmp, "full.log", HEADER + "\n".join(LINES) + "\n")
        kept = [ln for ln in LINES
                if " ERR " not in ln and int(ln.split()[3], 16) in keep]
        filt = _write(tmp, "filt.log", HEADER + "\n".join(kept) + "\n")
        a = [_key(f) for f in parse_file(full, ids=keep)]
        b = [_key(f) for f in parse_file(filt)]
    assert a == b, (a, b)
    assert a[0][0] == 0.0 and a[0][1] == 0x100      # origin: first KEPT frame, not the ERR
    assert abs(a[-1][0] - 1.8) < 1e-9               # the rollover was applied
    print("PASS test_bm_ids_equals_prefiltered_copy")


def test_bm_clock_ids():
    with tempfile.TemporaryDirectory() as tmp:
        full = _write(tmp, "full.log", HEADER + "\n".join(LINES) + "\n")
        got = [(round(f.timestamp, 6), f.arbitration_id)
               for f in parse_file(full, ids={0x100, 0x300}, clock_ids={0x100})]
        # 0x100 alone defines the time base, as when parsed with ids={0x100}
        clock = [(round(f.timestamp, 6), f.arbitration_id)
                 for f in parse_file(full, ids={0x100})]
    assert [g for g in got if g[1] == 0x100] == clock
    yaw = [g for g in got if g[1] == 0x300]
    # the 0x300 before the first 0x100 is dropped; the others are on 0x100's base,
    # the one after midnight carrying the rollover the clock frames saw
    assert yaw == [(0.1, 0x300), (1.6, 0x300)], yaw
    print("PASS test_bm_clock_ids")


TRC_HEAD = """;$FILEVERSION=2.1
;$STARTTIME=46054.99997685
;$COLUMNS=N,O,T,B,I,d,R,L,D
;
;   hand-written TRC 2.1 fixture
;---+-- ------+------ +- +- --+----- +- +- +- -- -- -- -- -- -- -- --
"""
TRC_LINES = [
    "      1         0.000 ER  1     -    Rx -  5    00 04 02 00 00",
    "      2       100.000 DT  2     0300 Rx -  8    00 00 00 00 00 00 80 18",
    "      3       500.000 DT  1     0100 Rx -  8    01 00 00 00 00 00 00 00",
    "      4       600.000 DT  2     0300 Rx -  8    00 00 00 00 00 00 80 19",
    "      5       700.000 DT  1     07AB Rx -  8    AA 00 00 00 00 00 00 00",
    "      6       900.000 DT  1     0200 Rx -  8    02 00 00 00 00 00 00 00",
    "      7      1900.000 DT  1     0100 Rx -  8    04 00 00 00 00 00 00 00",
    "      8      2100.000 DT  2     0300 Rx -  8    00 00 00 00 00 00 80 1A",
    "      9      2200.000 DT  1     0100 Rx -  8    05 00 00 00 00 00 00 00",
]


def test_trc_ids_and_clock_ids():
    with tempfile.TemporaryDirectory() as tmp:
        p = _write(tmp, "t.trc", TRC_HEAD + "\n".join(TRC_LINES) + "\n")
        a = [(round(f.timestamp, 6), f.arbitration_id) for f in parse_file(p, ids={0x100, 0x200})]
        assert a == [(0.0, 0x100), (0.4, 0x200), (1.4, 0x100), (1.7, 0x100)], a
        b = [(round(f.timestamp, 6), f.arbitration_id)
             for f in parse_file(p, ids={0x100, 0x300}, clock_ids={0x100})]
        assert b == [(0.0, 0x100), (0.1, 0x300), (1.4, 0x100), (1.6, 0x300), (1.7, 0x100)], b
        # without ids the ER record is the origin, as before
        c = [round(f.timestamp, 6) for f in parse_file(p)]
        assert c[0] == 0.1, c
    print("PASS test_trc_ids_and_clock_ids")


def test_argument_checks():
    for bad in (dict(ids=None, clock_ids={1}), dict(ids={1}, clock_ids={2}),
                dict(ids={1}, clock_ids=set())):
        try:
            id_filter_sets(**bad)
        except ValueError:
            continue
        raise AssertionError("accepted %r" % bad)
    with tempfile.TemporaryDirectory() as tmp:
        p = _write(tmp, "c.log", "(1700000000.000000) can0 123#0102\n"
                         "(1700000000.100000) can0 123#0304\n")
        try:
            list(parse_file(p, ids={0x123}))
        except NotImplementedError:
            pass
        else:
            raise AssertionError("candump accepted ids=")
    print("PASS test_argument_checks")


if __name__ == "__main__":
    test_bm_ids_equals_prefiltered_copy()
    test_bm_clock_ids()
    test_trc_ids_and_clock_ids()
    test_argument_checks()
