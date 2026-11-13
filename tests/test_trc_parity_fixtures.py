"""The committed TRC parity fixtures (tests/fixtures/trc_parity) are what tools/trc_parity_fixtures.py
generates now, and the start-time comments canbench writes read back.

Run standalone:   python tests/test_trc_parity_fixtures.py
Or under pytest:  python -m pytest tests/test_trc_parity_fixtures.py
"""
import importlib.util
import io
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import can  # noqa: E402

from canbench.parsers.peak_trc import start_local, start_time  # noqa: E402
from canbench.trcwriter import TRCWriter, local_start_text  # noqa: E402

FIX = ROOT / "tests" / "fixtures" / "trc_parity"


def _gen():
    spec = importlib.util.spec_from_file_location("trc_parity_fixtures", ROOT / "tools" / "trc_parity_fixtures.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_committed_fixtures_match_the_generator():
    gen = _gen()
    with tempfile.TemporaryDirectory() as d:
        names = gen.generate(Path(d))
        for name in names:
            new = (Path(d) / name).read_bytes()
            old = (FIX / name).read_bytes()
            assert new == old, f"{name} differs from the generator: run tools/trc_parity_fixtures.py and review"


def test_fixture_cover_every_case_and_records():
    import json
    gen = _gen()
    vec = json.loads((FIX / "decode_vectors.json").read_text())
    assert [v["name"] for v in vec] == [c[0] for c in gen.CASES]
    by = {v["name"]: v for v in vec}
    assert by["state_passive_with_prot"]["report"]["error_type"] is None          # a state change, not a bus error
    assert by["state_passive_with_prot"]["report"]["last_error_type"] == 2
    assert by["buserr_crc_rx"]["trc_records"] == [["ER", "Rx", [8, 1, 8, 25, 0]]]
    assert by["rx_overflow"]["trc_records"] == [["ST", "Rx", [0, 0, 0, 2]]]
    assert by["counters_cnt"]["trc_records"] == [["EC", "Rx", [12, 40]]]
    assert by["no_class_with_counters"]["trc_records"] == [["ER", "Rx", [8, 1, 0, 7, 5]]]   # no known class: a bus error
    assert by["state_active_no_prot_no_counters"]["report"]["rx_count"] is None


def test_start_time_is_utc_and_local_comment_reads_back():
    gen = _gen()
    p = FIX / "stream.trc"
    assert abs(start_time(p) - gen.T0) < 0.001
    when, zone = start_local(p)
    assert zone == "America/Los_Angeles" and when.utcoffset() == timedelta(hours=-8)
    assert abs(when.timestamp() - gen.T0) < 0.001


def test_start_local_none_without_the_comment():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "peak.trc"
        p.write_text(";$FILEVERSION=2.1\n;$STARTTIME=46339.5\n;   Start time: 13/11/2026 12:00:00.000.0\n"
                     "      1         0.000 DT  1     0123 Rx -  1    01\n")
        assert start_local(p) is None and start_time(p) is not None


def test_local_start_text_in_another_zone_and_without_one():
    ts = 1794586450.697
    assert local_start_text(ts, "Asia/Tokyo") == "2026-11-14T01:14:10.697+09:00 Asia/Tokyo"
    text = local_start_text(ts, "Not/AZone")         # an unknown name falls back to the machine's offset, no name
    assert " " not in text and text[-6] in "+-"


def test_writer_header_has_both_start_lines():
    buf = io.StringIO()
    w = TRCWriter(buf, zone="Europe/Berlin")
    w.on_message_received(can.Message(timestamp=1794586450.697, arbitration_id=1, data=b"\x01",
                                      is_extended_id=False, channel=0))
    head = buf.getvalue()
    assert ";   Start time (UTC): 2026-11-13T16:14:10.697+00:00" in head
    assert ";   Start time (local): 2026-11-13T17:14:10.697+01:00 Europe/Berlin" in head


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
