"""TRC 1.0-2.1 reading, non-frame records (CANEvent), and inferred-error marking.

Every fixture is written by hand inside the test -- never copied from a real
capture or from PEAK's document -- so nothing from a vehicle log or a vendor
file reaches this repo.  The layouts follow PEAK's "PEAK CAN TRC File Format"
(edition 2025-08-04).

Run standalone:   python tests/test_parsers_trc_events.py
Or under pytest:  python -m pytest tests/test_parsers_trc_events.py
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import can
from canbench.parsers import CANEvent, CANFrame, parse_file
from canbench.parsers.peak_trc import PeakTrcParser, start_time
from canbench.parsers.inferred_errors import layout_ok

# 45000.25 days after 1899-12-30 = 2023-03-15 06:00:00 (header local time)
START = ";$STARTTIME=45000.25"

TRC_10 = """;##########
;   hand-written TRC 1.0 fixture
;----+-   ---+--- ----+--- + -+ -- -- ...
      1)       100      0123 3 0A 0B 0C
      2)       150      0042 4 ERROR 01 0A 05 00
      3)       175  FFFFFFFF 4 00 00 00 08 -- -- -- -- BUSHEAVY
      4)       200      0321 2 RTR
      5)       250  00000456 1 7F
"""

TRC_11 = f""";$FILEVERSION=1.1
{START}
;   hand-written TRC 1.1 fixture
       1)       100.0 Rx         0123 3 0A 0B 0C
       2)       112.5 Tx         0200 1 FF
       3)       130.1 Warng  FFFFFFFF 4 00 00 00 04 BUSLIGHT
       4)       140.7 Error      0002 4 01 08 03 00
       5)       150.0 Rx     1ABCDE01 2 01 02
"""

TRC_12 = f""";$FILEVERSION=1.2
{START}
       1)       100.000 1 Rx         0123 3 0A 0B 0C
       2)       100.250 2 Rx         0124 1 55
"""

TRC_13 = f""";$FILEVERSION=1.3
{START}
       1)       100.000 1 Rx         0123 - 3      0A 0B 0C
       2)       101.500 2 Tx         0124 - 1      55
"""

TRC_20 = f""";$FILEVERSION=2.0
{START}
;$COLUMNS=N,O,T,I,d,l,D
;   hand-written TRC 2.0 fixture
       1       100.000 DT     0123 Rx 3 0A 0B 0C
       2       100.500 DT 00000042 Tx 2 11 22
       3       101.000 ER          Rx    02 01 0A 05 07
       4       101.010 ST          Rx    00 04 00 00
       5       101.020 EC          Rx    05 07
       6       101.100 RR     0321 Rx 2
       7       101.200 FD     0500 Rx 12 00 01 02 03 04 05 06 07 08 09 0A 0B
       8       102.000 DT     0123 Rx 3 0D 0E 0F
"""

TRC_21 = f""";$FILEVERSION=2.1
{START}
;$COLUMNS=N,O,T,B,I,d,R,L,D
;   hand-written TRC 2.1 fixture
       1       100.000 DT 1     0123 Rx - 3     0A 0B 0C
       2       100.400 DT 2 18DA00F1 Tx - 8     01 02 03 04 05 06 07 08
       3       100.900 ER 2        - Rx - 5     04 01 0A 60 02
       4       101.000 EV 1 start of injection phase 2
       5       101.001 EV - all buses: marker
       6       101.100 ST 2        - Rx - 4     00 00 00 10
       7       101.200 EC 2        - Rx - 2     5F 02
       8       101.300 RR 1     0321 Rx - 2
       9       102.000 DT 1     0123 Rx - 3     0D 0E 0F
"""

TRC_30 = f""";$FILEVERSION=3.0
{START}
;$COLUMNS=N,O,T,B,I,d,R,V,S,A,r,s,L,D
       1      100.000 DT 1      123 Rx - - - -           - - 3    0A 0B 0C
       2      100.500 XL 1      100 Tx - 00 03 00000456 0 0 3     03 11 22
       3      101.000 ER 1        - Rx - - - -           - - 5    04 01 0A 00 00
"""

BM_HEAD = """***BUSMASTER Ver 3.2.2***
***PROTOCOL CAN***
***[START LOGGING SESSION]***
***START DATE AND TIME 02:01:2026 03:04:05:678***
***HEX***
***START CHANNEL BAUD RATE***
***CHANNEL 1 - {a1} - 500000 bps***
***CHANNEL 2 - {a2} - 500000 bps***
***END CHANNEL BAUD RATE***
***<Time><Tx/Rx><Channel><CAN ID><Type><DLC><DataBytes>***
"""

# Channel 1: 0x004 carries cell-like data (fails the error layout) and one
# all-zero frame; 0x00C carries one layout-passing report.  Channel 2: an
# all-zero 0x004 with nothing failing beside it, a layout-passing 0x024, an
# id outside the gate with a sparse payload, and an extended 0x00C.
BM_BODY = """00:00:00:0000 Rx 1 0x004 s 8 0F A0 0F A1 0F A2 0F A3
00:00:00:1000 Rx 1 0x004 s 8 00 00 00 00 00 00 00 00
00:00:00:2000 Rx 1 0x00C s 8 00 30 02 00 00 00 6D FF
00:00:00:3000 Rx 2 0x004 s 8 00 00 00 00 00 00 00 00
00:00:00:4000 Rx 2 0x024 s 8 00 20 00 00 00 00 80 00
00:00:00:5000 Rx 2 0x07D s 8 00 00 00 00 00 00 00 01
00:00:00:6000 Rx 2 0x0000000C x 8 00 30 02 00 00 00 6D FF
"""


def _write(text, name):
    d = Path(tempfile.mkdtemp(prefix="canbench-trc-"))
    p = d / name
    p.write_text(text, encoding="utf-8")
    return p


def _frames(recs):
    return [r for r in recs if isinstance(r, CANFrame)]


def _events(recs):
    return [r for r in recs if isinstance(r, CANEvent)]


# ---- TRC: frames and times -------------------------------------------------

def test_trc_offsets_are_milliseconds_in_every_version():
    for text, dt in ((TRC_10, 0.150), (TRC_11, 0.0125), (TRC_12, 0.00025),
                     (TRC_13, 0.0015), (TRC_20, 0.0005), (TRC_21, 0.0004)):
        f = _frames(parse_file(_write(text, "t.trc")))
        assert abs(f[0].timestamp) < 1e-9
        assert abs(f[1].timestamp - dt) < 1e-9, (text.splitlines()[0], f[1].timestamp)


def test_trc_10_kinds_from_id_and_data_fields():
    p = _write(TRC_10, "t.trc")
    recs = list(parse_file(p, include_events=True))
    f, e = _frames(recs), _events(recs)
    assert [x.arbitration_id for x in f] == [0x123, 0x456]
    assert f[1].is_extended and f[1].data == b"\x7f"
    assert [(x.kind, x.arbitration_id, x.data) for x in e] == [
        ("Error", 0x042, bytes([1, 0x0A, 5, 0])), ("Warng", None, bytes([0, 0, 0, 8]))]


def test_trc_11_types_and_extended_id():
    recs = list(parse_file(_write(TRC_11, "t.trc"), include_events=True))
    f, e = _frames(recs), _events(recs)
    assert [(x.arbitration_id, x.is_tx) for x in f] == [(0x123, False), (0x200, True),
                                                        (0x1ABCDE01, False)]
    assert f[2].is_extended
    assert [x.kind for x in e] == ["Warng", "Error"]
    assert e[1].arbitration_id == 0x002 and e[1].data == bytes([1, 8, 3, 0])


def test_trc_12_13_bus_column():
    for text in (TRC_12, TRC_13):
        f = list(parse_file(_write(text, "t.trc")))
        assert [x.bus for x in f] == [1, 2]
        assert f[1].data == b"\x55"


def test_trc_20_columns_and_empty_event_columns():
    p = _write(TRC_20, "t.trc")
    parser = PeakTrcParser()
    recs = list(parser.parse_file(p, include_events=True))
    f, e = _frames(recs), _events(recs)
    assert [x.arbitration_id for x in f] == [0x123, 0x042, 0x500, 0x123]
    assert f[1].is_extended                       # 8 digits = 29-bit, value small
    assert f[2].data == bytes(range(12)) and f[2].dlc == 12
    assert [(x.kind, x.data) for x in e] == [
        ("ER", bytes([2, 1, 0x0A, 5, 7])), ("ST", bytes([0, 4, 0, 0])), ("EC", bytes([5, 7]))]
    assert all(x.bus == 1 for x in recs)
    assert parser.skipped == {"RR": 1}


def test_trc_21_bus_events_and_text():
    p = _write(TRC_21, "t.trc")
    parser = PeakTrcParser()
    recs = list(parser.parse_file(p, include_events=True))
    f, e = _frames(recs), _events(recs)
    assert [(x.bus, x.arbitration_id) for x in f] == [(1, 0x123), (2, 0x18DA00F1), (1, 0x123)]
    assert [(x.kind, x.bus) for x in e] == [("ER", 2), ("EV", 1), ("EV", None), ("ST", 2),
                                            ("EC", 2)]
    assert e[1].text == "start of injection phase 2"
    assert e[2].text == "all buses: marker"
    assert e[3].data == bytes([0, 0, 0, 0x10])     # BUSOFF
    assert parser.skipped == {"RR": 1}


def test_trc_30_reads_classic_frames_skips_xl():
    parser = PeakTrcParser()
    recs = list(parser.parse_file(_write(TRC_30, "t.trc"), include_events=True))
    assert [x.arbitration_id for x in _frames(recs)] == [0x123]
    assert not _frames(recs)[0].is_extended
    assert [x.kind for x in _events(recs)] == ["ER"]
    assert parser.skipped == {"XL": 1}


def test_trc_events_off_by_default_and_origin_unchanged():
    p = _write(TRC_21, "t.trc")
    plain = list(parse_file(p))
    assert all(isinstance(x, CANFrame) for x in plain)
    with_ev = _frames(parse_file(p, include_events=True))
    assert [x.timestamp for x in plain] == [x.timestamp for x in with_ev]


def test_trc_absolute_and_start_time():
    p = _write(TRC_21, "t.trc")
    f = list(parse_file(p, absolute=True))
    assert abs(f[0].timestamp - (6 * 3600 + 0.100)) < 1e-6   # 06:00:00 + 100 ms
    st = start_time(p)
    assert abs(st - 1678860000.0) < 1e-3                       # 2023-03-15 06:00:00 as UTC


def test_trc_frames_match_python_can():
    """python-can's TRCReader is an independent decoder of the same files."""
    for text in (TRC_11, TRC_20, TRC_21):
        p = _write(text, "t.trc")
        ours = list(parse_file(p))
        theirs = [m for m in can.TRCReader(str(p)) if not m.is_remote_frame]
        assert [(x.arbitration_id, bytes(x.data)) for x in ours] == \
               [(m.arbitration_id, bytes(m.data)) for m in theirs]
        t0 = theirs[0].timestamp
        for x, m in zip(ours, theirs):
            assert abs(x.timestamp - (m.timestamp - t0)) < 1e-6


# ---- BUSMASTER events -------------------------------------------------------

def test_busmaster_err_as_event():
    body = BM_HEAD.format(a1="Innomaker", a2="Innomaker") + \
        "00:00:00:0000 Rx 1 0x123 s 1 01\n00:00:00:0500 Rx 2 0x24 ERR 8 00 20 00 00 00 00 80 00\n"
    p = _write(body, "c.log")
    recs = list(parse_file(p, include_events=True))
    assert isinstance(recs[1], CANEvent) and recs[1].kind == "ERR"
    assert recs[1].arbitration_id == 0x024 and recs[1].bus == 2
    assert abs(recs[1].timestamp - 0.05) < 1e-9
    assert [type(r) for r in parse_file(p)] == [CANFrame]
    old = list(parse_file(p, include_errors=True))     # the older form still works
    assert isinstance(old[1], CANFrame) and old[1].is_error


# ---- inferred-error marking -------------------------------------------------

def _marked(text):
    p = _write(text, "c.log")
    return [(f.bus, f.arbitration_id, f.inferred_error) for f in parse_file(p, mark_inferred_errors=True)]


def test_inferred_marking_follows_the_rule():
    got = _marked(BM_HEAD.format(a1="Innomaker", a2="Innomaker") + BM_BODY)
    assert got == [
        (1, 0x004, False),   # cell-like: fails the layout
        (1, 0x004, False),   # all-zero, but 0x004 on this channel has failing frames
        (1, 0x00C, True),    # fits the layout
        (2, 0x004, True),    # all-zero, nothing failing on this channel's 0x004
        (2, 0x024, True),
        (2, 0x07D, False),   # outside the id gate
        (2, 0x00C, False),   # extended
    ]


def test_inferred_marking_needs_innomaker_and_no_err_lines():
    other = _marked(BM_HEAD.format(a1="Kvaser", a2="Kvaser") + BM_BODY)
    assert not any(m for _, _, m in other)
    post_fix = BM_HEAD.format(a1="Innomaker", a2="Innomaker") + BM_BODY + \
        "00:00:00:7000 Rx 1 0xC ERR 8 00 30 02 00 00 00 6D FF\n"
    assert not any(m for _, _, m in _marked(post_fix))


def test_inferred_marking_is_opt_in_and_drops_nothing():
    p = _write(BM_HEAD.format(a1="Innomaker", a2="Innomaker") + BM_BODY, "c.log")
    plain = list(parse_file(p))
    marked = list(parse_file(p, mark_inferred_errors=True))
    assert not any(f.inferred_error for f in plain)
    assert [(f.arbitration_id, f.data) for f in plain] == [(f.arbitration_id, f.data) for f in marked]


def test_layout_ok_examples():
    assert layout_ok(0x00C, bytes.fromhex("0030020000006DFF"))
    assert layout_ok(0x024, bytes.fromhex("0020000000008000"))
    assert not layout_ok(0x004, bytes.fromhex("0FA00FA10FA20FA3"))
    assert not layout_ok(0x00C, bytes.fromhex("0030020000006D"))      # 7 bytes
    assert not layout_ok(0x004, bytes.fromhex("0000020000000000"))    # protocol byte, no 0x008


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
