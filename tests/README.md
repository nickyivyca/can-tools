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

`test_bench_roles.py` covers `canbench.bench_roles` on synthetic `canctl roles` text: each kind and mode resolved, every refusal, `open_role` only after the caller's `canctl up`, `hw_timestamps` only on a SocketCAN netdev, `role:` ids added by what canctl reports, a replay bus pinned by role, a missing or pre-roles canctl, `open_role` applying `linux_fixes` and releasing a libusb gs_usb role on `shutdown()`.

**The default suite opens no real adapter** (2026-11-18): detection in the tests runs on fake sysfs with python-can's `Bus` and the libusb scan stubbed, the GUI smoke test stubs detection, and `test_gui_hardware.py` runs only with `CANBENCH_HARDWARE_TESTS=1`, inside your own bench take. Before that, the suite's GUI tests ran real detection (PCAN channels opened at 500k) during another session's take on the madhouse bench.

Every `udp_multicast` bus is opened through `canbench.buses.open_bus`, hop limit 0, checked
(`test_buses.py`). Tests that need buses kept apart give each its own port (47800-47999).

_Test modules land here in Phase 2._
