# tests/

Hardware-free tests for `canbench` (Phase 2).

Tests run against python-can's **`virtual`** bus (in-process) and
**`udp_multicast`** bus (cross-process localhost "network CAN"), so the logger,
replayer, passthrough bridge, and candump round-trip can be validated with no
dongles attached.

`test_udp_cli.py` replays a synthetic two-bus candump log with can_replayer onto two localhost buses and
records it with can_logger to TRC, end to end.

Every `udp_multicast` bus is opened through `canbench.buses.open_bus`, hop limit 0, checked
(`test_buses.py`). Tests that need buses kept apart give each its own port (47800-47999).

_Test modules land here in Phase 2._
