"""Content-detected log reading in canbench.logio (BUSMASTER via canbench.parsers).

Every fixture here is written by hand inside the test -- never copied from a real
capture -- so nothing from a vehicle log reaches this repo.

Run standalone:   python tests/test_logio_readers.py
Or under pytest:  python -m pytest tests/test_logio_readers.py
"""
import ast
import calendar
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import can
from canbench import logio

REPO = Path(__file__).resolve().parent.parent

BUSMASTER = """***BUSMASTER Ver 3.2.2***
***PROTOCOL CAN***
***NOTE: PLEASE DO NOT EDIT THIS DOCUMENT***
***[START LOGGING SESSION]***
***START DATE AND TIME 02:01:2026 03:04:05:678***
***HEX***
***SYSTEM MODE***
***START CHANNEL BAUD RATE***
***CHANNEL 1 - Innomaker - 500000 bps***
***CHANNEL 2 - Innomaker - 500000 bps***
***END CHANNEL BAUD RATE***
***START DATABASE FILES***
***END DATABASE FILES***
***<Time><Tx/Rx><Channel><CAN ID><Type><DLC><DataBytes>***

00:00:00:0000 Rx 1 0x123 s 2 01 02
00:00:00:0015 Rx 2 0x456 s 8 11 22 33 44 55 66 77 88
00:00:00:2500 Tx 1 0x600 s 1 AA
00:00:01:0000 Rx 1 0xC ERR 8 00 30 02 00 00 00 6D FF
00:00:01:5000 Rx 2 0x18FF0001 x 3 01 02 03
"""

CANDUMP = """(1767323045.678000) can0 123#0102
(1767323045.680000) can1 456#1122334455667788
"""


def _write(text, name):
    d = Path(tempfile.mkdtemp(prefix="canbench-logio-"))
    p = d / name
    p.write_text(text, encoding="utf-8")
    return p


def test_busmaster_log_is_not_read_as_candump():
    p = _write(BUSMASTER, "capture.log")
    assert logio.detect_reader(p) is not None
    msgs = list(logio.read_messages(p))
    assert [m.arbitration_id for m in msgs] == [0x123, 0x456, 0x600, 0x18FF0001]


def test_busmaster_timestamps_ticks_and_header_ms():
    p = _write(BUSMASTER, "capture.log")
    msgs = list(logio.read_messages(p))
    base = calendar.timegm((2026, 1, 2, 3, 4, 5)) + 0.678   # header field is ms
    assert abs(msgs[0].timestamp - base) < 1e-6
    assert abs((msgs[1].timestamp - msgs[0].timestamp) - 0.0015) < 1e-6   # 15 ticks of 0.1 ms
    assert abs((msgs[2].timestamp - msgs[0].timestamp) - 0.25) < 1e-6
    assert abs((msgs[3].timestamp - msgs[0].timestamp) - 1.5) < 1e-6


def test_busmaster_fields():
    p = _write(BUSMASTER, "capture.log")
    m0, m1, m2, m3 = logio.read_messages(p)
    assert m0.channel == "can1" and m1.channel == "can2"
    assert logio.channel_bus_id(m1.channel) == 2
    assert bytes(m1.data) == bytes.fromhex("1122334455667788") and m1.dlc == 8
    assert m0.is_rx and not m2.is_rx
    assert m3.is_extended_id and not m0.is_extended_id


def test_busmaster_error_lines_skipped_by_default_and_yielded_on_request():
    p = _write(BUSMASTER, "capture.log")
    assert not any(m.is_error_frame for m in logio.read_messages(p))
    errs = [m for m in logio.read_messages(p, include_errors=True) if m.is_error_frame]
    assert len(errs) == 1
    assert errs[0].arbitration_id == 0x00C
    assert bytes(errs[0].data) == bytes.fromhex("0030020000006DFF")


def test_busmaster_without_header_starts_at_zero():
    body = BUSMASTER.replace("***START DATE AND TIME 02:01:2026 03:04:05:678***\n", "")
    p = _write(body, "noheader.log")
    msgs = list(logio.read_messages(p))
    assert msgs and abs(msgs[0].timestamp) < 1e-9


def test_candump_still_goes_through_python_can():
    p = _write(CANDUMP, "capture.log")
    assert logio.detect_reader(p) is None
    msgs = list(logio.read_messages(p))
    assert [m.arbitration_id for m in msgs] == [0x123, 0x456]
    assert abs(msgs[0].timestamp - 1767323045.678) < 1e-6


def test_canbench_never_imports_canre():
    """The bundled exe contains canbench only, so canbench must not import canre."""
    offenders = []
    for py in (REPO / "canbench").rglob("*.py"):
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                names = [node.module]
            offenders += [f"{py.relative_to(REPO)}: {n}" for n in names
                          if n == "canre" or n.startswith("canre.")]
    assert not offenders, offenders


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
