"""Test the passthrough bridge across two virtual segments (no hardware).

Segment A and segment B are separate virtual channels. A node on A sends; the
bridge forwards to B where a node receives it, and the on_frame callback logs the
original tagged with source bus 0. Same in reverse for B->A.
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import can
from canbench.passthrough import PassthroughBridge


def test_bridge_forwards_and_tags_source():
    node_a = can.Bus(interface="virtual", channel="segA")
    bridge_a = can.Bus(interface="virtual", channel="segA")
    bridge_b = can.Bus(interface="virtual", channel="segB")
    node_b = can.Bus(interface="virtual", channel="segB")

    logged = []
    bridge = PassthroughBridge(bridge_a, bridge_b, on_frame=lambda src, m: logged.append((src, m.arbitration_id)))
    bridge.start()
    try:
        node_a.send(can.Message(arbitration_id=0x111, data=b"\x01", is_extended_id=False))
        node_b.send(can.Message(arbitration_id=0x222, data=b"\x02", is_extended_id=False))
        time.sleep(0.4)

        got_b = node_b.recv(timeout=1.0)   # A's frame forwarded onto segB
        got_a = node_a.recv(timeout=1.0)   # B's frame forwarded onto segA
    finally:
        bridge.stop()
        for b in (node_a, bridge_a, bridge_b, node_b):
            b.shutdown()

    assert got_b is not None and got_b.arbitration_id == 0x111
    assert got_a is not None and got_a.arbitration_id == 0x222
    # each original logged once, tagged with its source bus id
    assert (0, 0x111) in logged     # sourced on bus A (id 0)
    assert (1, 0x222) in logged     # sourced on bus B (id 1)
    assert bridge.counts == [1, 1]


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn(); print(f"PASS  {name}")
            except AssertionError as e:
                failures += 1; print(f"FAIL  {name}: {e}")
            except Exception as e:
                failures += 1; print(f"ERROR {name}: {e!r}")
    print(f"\n{'ALL PASS' if failures == 0 else str(failures) + ' FAILED'}")
    sys.exit(1 if failures else 0)
