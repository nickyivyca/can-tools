# tests/

Hardware-free tests for `canbench` (Phase 2).

Tests run against python-can's **`virtual`** bus (in-process) and
**`udp_multicast`** bus (cross-process localhost "network CAN"), so the logger,
replayer, passthrough bridge, and candump round-trip can be validated with no
dongles attached.

`test_udp_cli.py` replays a synthetic two-bus candump log with can_replayer onto two localhost buses and
records it with can_logger to TRC, end to end, whole and with `--window` (a slice of the log).
`test_replay_load.py` covers `replay.parse_window` / `replay.window` on their own.

`test_socketcan_hw.py` covers `canbench.live.socketcan_hw` with a fake socket (crafted `struct can_frame`
and `SCM_TIMESTAMPING` data): both stamps per frame, error frames passed whole, and the per-open stamp check's
pass and its failures (all-zero hardware stamps, host time posing as hardware, stamps that don't advance).

`test_bench_roles.py` covers `canbench.bench_roles` on synthetic `canctl roles` text: each kind and mode resolved, every refusal, `open_role` only after the caller's `canctl up`, `hw_timestamps` only on a SocketCAN netdev, `role:` ids added by what canctl reports, a replay bus pinned by role, a missing or pre-roles canctl, `open_role` applying `linux_fixes` and releasing a libusb gs_usb role on `shutdown()`, and its `send(msg, timeout)` setting pyusb's write timeout for that send only (a socketcan role's send untouched).

`test_bench_layer.py` covers `canbench.bench_layer` and `canbench.bench_scan` on a stateful fake canctl and a fake `ip -details -json`: no guard or a refusing guard touches nothing; each link and mode read-back fails when the fake lies (wrong bitrate, listen-only, not up, restart-ms, still up, not settled, canctl rc 3); `open_bench` with the defaults, an override and `restore=False`, putting the role back as found on shutdown, on a failed open, on a mode move that exits 0 but does not settle and on a link that reads back wrong; `if_up="refuse"` touching nothing, raised as `LinkFoundUp` with the role and the link read; `drain=True` counting echoes, received and error frames, refusing `recv`, stopping before the device closes and within one 1 ms read, not spinning on a read that fails at once (its wait cut by the stop); splitting its counts at the first send, keeping the ids and data of what was read before it (capped), returning from `open_bench` only once the queue has been read empty, `stop_drain()` returning a copy; every pick refusal; the bus-error classifier; the scan's self-check and its count on can-tools' own tree. `--can-fail` runs the checks on each of 43 mutants of the two modules; each must make a check fail.

**The default suite opens no real adapter** (2026-11-18): detection in the tests runs on fake sysfs with python-can's `Bus` and the libusb scan stubbed, the GUI smoke test stubs detection, and `test_gui_hardware.py` runs only with `CANBENCH_HARDWARE_TESTS=1`, inside your own bench take. Before that, the suite's GUI tests ran real detection (PCAN channels opened at 500k) during another session's take on the madhouse bench.

Every `udp_multicast` bus is opened through `canbench.buses.open_bus`, hop limit 0, checked
(`test_buses.py`). Tests that need buses kept apart give each its own port (47800-47999).

_Test modules land here in Phase 2._
