"""candump and TRC readers: ID width, error frames, remote requests, CAN FD flags.

Found by the reverse-it project's step 6a round trip (an internal plan, not published): the
candump reader yielded python-can's error-frame line as a data frame with ID
0x20000080, a remote request as a 0-byte data frame, and an 8-digit ID of small
value as standard; the TRC reader read python-can's 7-digit 29-bit IDs as standard.

Every fixture is written by hand inside the test -- never copied from a capture.

Run standalone:   python tests/test_parsers_candump_ids.py
Or under pytest:  python -m pytest tests/test_parsers_candump_ids.py
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from canbench.parsers import CANEvent, CANFrame, parse_file
from canbench.parsers.candump import CandumpParser

# The lines python-can 4.6.1's CanutilsLogWriter writes, plus can-utils forms.
CANDUMP = """(1700000000.000000) can0 123#0102 R
(1700000000.001000) can0 20000080#0000000000000000
(1700000000.002000) can0 456#R R
(1700000000.003000) can1 001ABCDE#03 R
(1700000000.004000) can1 0000007F#04 R
(1700000000.005000) can0 456#R2
(1700000000.006000) can0 20000004#0000040000000000
(1700000000.007000) can0 7FF#1122
(1700000000.008000) can1 123##1AABBCCDDEEFF0011
"""


def _write(text, suffix):
    d = Path(tempfile.mkdtemp(prefix="canbench-ids-"))
    p = d / f"f{suffix}"
    p.write_text(text, encoding="utf-8")
    return p


def test_candump_frames_and_id_width():
    p = _write(CANDUMP, ".log")
    got = [(f.bus, f.arbitration_id, f.is_extended, f.dlc, bytes(f.data)) for f in parse_file(p)]
    assert got == [
        (0, 0x123, False, 2, b"\x01\x02"),
        (1, 0x1ABCDE, True, 1, b"\x03"),
        (1, 0x07F, True, 1, b"\x04"),        # 8 digits: extended though the value is small
        (0, 0x7FF, False, 2, b"\x11\x22"),
        (1, 0x123, False, 8, bytes.fromhex("AABBCCDDEEFF0011")),   # FD flags digit not data
    ]


def test_candump_error_frames_skipped_by_default_and_counted_as_remote():
    p = _write(CANDUMP, ".log")
    parser = CandumpParser()
    frames = list(parser.parse_file(p))
    assert all(not f.is_error for f in frames)
    assert 0x20000080 not in {f.arbitration_id for f in frames}
    assert parser.skipped == {"remote": 2}


def test_candump_error_frames_as_events_and_as_frames():
    p = _write(CANDUMP, ".log")
    ev = [r for r in parse_file(p, include_events=True) if isinstance(r, CANEvent)]
    assert [(e.kind, e.bus, e.arbitration_id, bytes(e.data)) for e in ev] == [
        ("ERR", 0, 0x080, b"\x00" * 8),
        ("ERR", 0, 0x004, bytes.fromhex("0000040000000000")),
    ]
    assert abs(ev[0].timestamp - 0.001) < 1e-6        # epoch-sized floats: ~0.1 us
    er = [r for r in parse_file(p, include_errors=True) if isinstance(r, CANFrame) and r.is_error]
    assert [(f.arbitration_id, f.dlc) for f in er] == [(0x080, 8), (0x004, 8)]
    # include_events takes precedence, as for BUSMASTER
    both = list(parse_file(p, include_errors=True, include_events=True))
    assert not any(isinstance(r, CANFrame) and r.is_error for r in both)


def test_trc_id_width_python_can_and_peak():
    # python-can 4.6.1 TRCWriter: 4 digits standard, 7 digits extended.
    # PEAK: 4 digits standard, 8 digits extended.
    p = _write(""";$FILEVERSION=2.1
;$STARTTIME=45000.25
;$COLUMNS=N,O,T,B,I,d,R,L,D
      0         0.000 DT  1     0123 Rx -  2    01 02
      1         3.000 DT  2  01ABCDE Rx -  1    03
      2         4.000 DT  2  000007F Rx -  1    04
      3         5.000 DT  2 0000007F Rx -  1    05
      4         6.000 DT  1     07FF Rx -  1    06
""", ".trc")
    got = [(f.bus, f.arbitration_id, f.is_extended) for f in parse_file(p)]
    assert got == [(1, 0x123, False), (2, 0x1ABCDE, True), (2, 0x7F, True),
                   (2, 0x7F, True), (1, 0x7FF, False)]


def test_trc_30_three_digit_standard():
    p = _write(""";$FILEVERSION=3.0
;$STARTTIME=45000.25
;$COLUMNS=N,O,T,B,I,d,R,L,D
      1         0.000 DT 1      123 Rx - 2    01 02
      2         1.000 DT 1 0000007F Rx - 1    03
""", ".trc")
    got = [(f.arbitration_id, f.is_extended) for f in parse_file(p)]
    assert got == [(0x123, False), (0x7F, True)]


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
