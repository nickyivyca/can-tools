"""Write the TRC parity fixtures: synthetic inputs and canbench's outputs, for other implementations.

The Android loggers port canbench's error decoding (``canbench.errors.decode``) and TRC rendering
(``canbench.errors.trc_records``, ``canbench.trcwriter.TRCWriter``) to Kotlin, and hold their port to
these files (reverse-it, 2026-11-13; android-canbus-apps notes/plans/trc-cutover.md, Change 13). Every
input is synthetic: no adapter serial, no capture.

Files (in --out, default tests/fixtures/trc_parity):

  decode_vectors.json  one case per Linux can/error.h error frame: the frame (class bits, 8 data bytes),
                       the ErrorReport decode() makes of it, and the (type, direction, data) TRC records
                       trc_records() renders from that report.
  stream_messages.json the messages of a short capture: data, extended, remote and CAN FD frames on three
                       buses, both directions, and every gs_usb error kind, in time order.
  stream.trc           what TRCWriter writes for stream_messages.json, with the local zone fixed to
                       America/Los_Angeles and no file path in the header.
  README.md            the field meanings.

Run from the clone:  python tools/trc_parity_fixtures.py [--out DIR]
tests/test_trc_parity_fixtures.py regenerates them and fails if the committed copies differ.
"""
import argparse
import dataclasses
import io
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import can  # noqa: E402

from canbench import errors as E  # noqa: E402
from canbench.trcwriter import TRCWriter  # noqa: E402

ZONE = "America/Los_Angeles"
T0 = 1794586450.697          # 2026-11-13 16:14:10.697 UTC (08:14:10.697 PST): no DST change near it

# Locations (SJA1000 ECC segment codes, error.h CAN_ERR_PROT_LOC_*) used below.
LOC_ID28_21, LOC_DATA, LOC_CRC_SEQ, LOC_ACK, LOC_EOF = 0x02, 0x0A, 0x08, E.CAN_ERR_PROT_LOC_ACK, 0x1A


def _frame(cls, data):
    return {"class_bits": cls, "data": list(bytes(data).ljust(8, b"\x00")[:8])}


# (name, what it is, class bits, data). gs_usb / candleLight forms first, then the other error.h classes.
CASES = [
    ("state_warning_with_prot", "gs_usb state change to warning, repeating the last error code (CRTL|PROT, counters without CNT)",
     E.CAN_ERR_CRTL | E.CAN_ERR_PROT, [0, E.CAN_ERR_CRTL_TX_WARNING, E.CAN_ERR_PROT_BIT, 0, 0, 0, 97, 0]),
    ("state_passive_with_prot", "gs_usb state change to error passive with the last error code (CRTL|PROT)",
     E.CAN_ERR_CRTL | E.CAN_ERR_PROT, [0, E.CAN_ERR_CRTL_TX_PASSIVE, E.CAN_ERR_PROT_FORM | E.CAN_ERR_PROT_TX, 0, 0, 0, 128, 3]),
    ("state_rx_passive_with_prot", "gs_usb state change, receive side passive",
     E.CAN_ERR_CRTL | E.CAN_ERR_PROT, [0, E.CAN_ERR_CRTL_RX_PASSIVE, E.CAN_ERR_PROT_STUFF, 0, 0, 0, 0, 130]),
    ("state_active_with_prot", "gs_usb back to error active, last error code repeated",
     E.CAN_ERR_CRTL | E.CAN_ERR_PROT, [0, E.CAN_ERR_CRTL_ACTIVE, E.CAN_ERR_PROT_BIT0, 0, 0, 0, 90, 0]),
    ("state_warning_no_prot", "state change to warning without a protocol code",
     E.CAN_ERR_CRTL, [0, E.CAN_ERR_CRTL_RX_WARNING, 0, 0, 0, 0, 0, 96]),
    ("state_active_no_prot_no_counters", "back to active, no code, zero counters without CNT (counters unknown)",
     E.CAN_ERR_CRTL, [0, E.CAN_ERR_CRTL_ACTIVE, 0, 0, 0, 0, 0, 0]),
    ("busoff", "bus off (BUSOFF), counters with CNT",
     E.CAN_ERR_BUSOFF | E.CAN_ERR_CNT, [0, 0, 0, 0, 0, 0, 255, 0]),
    ("restarted", "controller restarted after bus off",
     E.CAN_ERR_RESTARTED, [0] * 8),
    ("buserr_bit_rx", "bus error, bit error, received, in the identifier",
     E.CAN_ERR_PROT | E.CAN_ERR_BUSERROR, [0, 0, E.CAN_ERR_PROT_BIT, LOC_ID28_21, 0, 0, 8, 0]),
    ("buserr_bit0_tx", "bus error, bit0 (dominant expected), while transmitting, in data",
     E.CAN_ERR_PROT | E.CAN_ERR_BUSERROR | E.CAN_ERR_CNT, [0, 0, E.CAN_ERR_PROT_BIT0 | E.CAN_ERR_PROT_TX, LOC_DATA, 0, 0, 16, 0]),
    ("buserr_bit1_tx", "bus error, bit1 (recessive expected), while transmitting",
     E.CAN_ERR_PROT | E.CAN_ERR_BUSERROR, [0, 0, E.CAN_ERR_PROT_BIT1 | E.CAN_ERR_PROT_TX, LOC_DATA, 0, 0, 24, 0]),
    ("buserr_form_rx", "bus error, form error, at end of frame",
     E.CAN_ERR_PROT | E.CAN_ERR_BUSERROR, [0, 0, E.CAN_ERR_PROT_FORM, LOC_EOF, 0, 0, 0, 9]),
    ("buserr_stuff_rx", "bus error, stuff error, in data",
     E.CAN_ERR_PROT | E.CAN_ERR_BUSERROR, [0, 0, E.CAN_ERR_PROT_STUFF, LOC_DATA, 0, 0, 0, 17]),
    ("buserr_crc_rx", "bus error, CRC sequence (type unspecified, location CRC)",
     E.CAN_ERR_PROT | E.CAN_ERR_BUSERROR, [0, 0, 0, LOC_CRC_SEQ, 0, 0, 0, 25]),
    ("buserr_ack_slot_tx", "bus error in the ACK slot while transmitting (no ACK), as PROT",
     E.CAN_ERR_PROT | E.CAN_ERR_BUSERROR, [0, 0, E.CAN_ERR_PROT_TX, LOC_ACK, 0, 0, 32, 0]),
    ("prot_no_state_change", "PROT without BUSERROR and without a state change: still a bus error",
     E.CAN_ERR_PROT, [0, 0, E.CAN_ERR_PROT_STUFF, LOC_DATA, 0, 0, 0, 0]),
    ("ack_class", "ACK class (no acknowledge received)",
     E.CAN_ERR_ACK, [0] * 8),
    ("buserror_alone", "BUSERROR with no PROT details",
     E.CAN_ERR_BUSERROR, [0] * 8),
    ("tx_timeout", "TX timeout",
     E.CAN_ERR_TX_TIMEOUT, [0] * 8),
    ("counters_cnt", "counters only, with CNT",
     E.CAN_ERR_CNT, [0, 0, 0, 0, 0, 0, 40, 12]),
    ("counters_cnt_zero", "CNT only with both counters zero (kvaser_usb's 512 ms report while not error-active): counters unknown, no record",
     E.CAN_ERR_CNT, [0] * 8),
    ("no_class_with_counters", "no class bits, counters in bytes 6/7 without CNT: an error frame with no known class is a bus error (ER, type other)",
     0, [0, 0, 0, 0, 0, 0, 5, 7]),
    ("rx_overflow", "receive overflow (CRTL, RX_OVERFLOW)",
     E.CAN_ERR_CRTL, [0, E.CAN_ERR_CRTL_RX_OVERFLOW, 0, 0, 0, 0, 0, 0]),
    ("tx_overflow", "transmit overflow (CRTL, TX_OVERFLOW)",
     E.CAN_ERR_CRTL, [0, E.CAN_ERR_CRTL_TX_OVERFLOW, 0, 0, 0, 0, 0, 0]),
    ("lost_arbitration", "lost arbitration at bit 11",
     E.CAN_ERR_LOSTARB, [11, 0, 0, 0, 0, 0, 0, 0]),
    ("no_class", "an error frame with no class bits and zero data",
     0, [0] * 8),
]


def _report_json(r):
    d = dataclasses.asdict(r)
    d["raw_data"] = list(bytes(r.raw_data))
    return d


def decode_vectors():
    out = []
    for name, what, cls, data in CASES:
        msg = E.error_frame(cls, data)
        r = E.decode(msg)
        out.append({"name": name, "what": what, "frame": _frame(cls, data), "report": _report_json(r),
                    "trc_records": [[t, d, list(b)] for t, d, b in E.trc_records(r)]})
    return out


def stream():
    """Time-ordered messages of a short synthetic capture on buses can0, can1, can2."""
    msgs = []
    t = T0

    def add(m):
        nonlocal t
        m.timestamp = round(t, 6)     # the value stream_messages.json carries, so both sides start from it
        msgs.append(m)
        t += 0.0013

    add(can.Message(arbitration_id=0x7A0, data=bytes([1, 2, 3, 4]), is_extended_id=False, channel=0))
    add(can.Message(arbitration_id=0x18FEF100, data=bytes(range(8)), is_extended_id=True, channel=1))
    add(can.Message(arbitration_id=0x7A1, dlc=2, is_remote_frame=True, is_extended_id=False, channel=0))
    add(can.Message(arbitration_id=0x123, data=bytes(range(12)), is_fd=True, bitrate_switch=True,
                    is_extended_id=False, channel=2))
    add(can.Message(arbitration_id=0x124, data=bytes(range(16)), is_fd=True, error_state_indicator=True,
                    is_extended_id=False, channel=2))
    add(can.Message(arbitration_id=0x7A2, data=b"\xAA", is_extended_id=False, channel=1, is_rx=False))
    add(can.Message(arbitration_id=0x0, data=b"", is_extended_id=False, channel=0))
    for i, (name, what, cls, data) in enumerate(CASES):
        add(E.error_frame(cls, data, channel=i % 3))
    add(can.Message(arbitration_id=0x7A3, data=bytes([0xFF] * 8), is_extended_id=False, channel=0))
    return msgs


def _msg_json(m):
    return {"timestamp": round(m.timestamp, 6), "channel": m.channel, "arbitration_id": m.arbitration_id,
            "is_extended_id": m.is_extended_id, "is_remote_frame": m.is_remote_frame,
            "is_error_frame": m.is_error_frame, "is_fd": m.is_fd, "bitrate_switch": m.bitrate_switch,
            "error_state_indicator": m.error_state_indicator, "is_rx": m.is_rx, "dlc": m.dlc,
            "data": list(bytes(m.data))}


def stream_trc(msgs):
    buf = io.StringIO()
    w = TRCWriter(buf, zone=ZONE)
    for m in msgs:
        w.on_message_received(m)
    text = buf.getvalue()
    return text.replace("\r\n", "\n").replace("\n", "\r\n")


README = """# TRC parity fixtures

Generated by can-tools `tools/trc_parity_fixtures.py`; do not edit by hand. All inputs are synthetic.
`tests/test_trc_parity_fixtures.py` regenerates them and fails if these files differ.

- `decode_vectors.json`: a list of cases. `frame.class_bits` is the Linux can/error.h error class (the
  CAN id without CAN_ERR_FLAG), `frame.data` the 8 data bytes. `report` is `canbench.errors.decode()`'s
  ErrorReport: `error_type` (PEAK type 1 bit, 2 form, 4 stuff, 8 other, null = no bus error),
  `direction` ("tx", "rx" or null), `position` (SJA1000 segment code or null), `rx_count` / `tx_count`
  (null = not reported), `state` (active, warning, passive, busoff, restarted or null), `rx_overflow`,
  `tx_overflow`, `lost_arbitration_bit`, `last_error_type` (the code a state-change frame repeats), and
  the `raw_*` fields (the frame itself for a plain Linux error frame). `trc_records` are
  `canbench.errors.trc_records()`'s `[type, direction, data bytes]`.
- `stream_messages.json`: the messages of `stream.trc`, in order. `channel` N is written as TRC bus N+1.
- `stream.trc`: `canbench.trcwriter.TRCWriter` output for them (CRLF line ends), local zone
  America/Los_Angeles, no file path in the header. `$STARTTIME` is UTC (owner ruling 2026-11-13); the
  header's `Start time (UTC)` and `Start time (local)` comments are the forms to write.
"""


def generate(out: Path):
    out.mkdir(parents=True, exist_ok=True)
    msgs = stream()
    files = {
        "decode_vectors.json": json.dumps(decode_vectors(), indent=1) + "\n",
        "stream_messages.json": json.dumps([_msg_json(m) for m in msgs], indent=1) + "\n",
        "stream.trc": stream_trc(msgs),
        "README.md": README,
    }
    for name, text in files.items():
        with open(out / name, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
    return sorted(files)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=str(ROOT / "tests" / "fixtures" / "trc_parity"))
    a = ap.parse_args()
    for name in generate(Path(a.out)):
        print(os.path.join(a.out, name))
