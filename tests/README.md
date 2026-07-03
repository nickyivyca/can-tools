# tests/

Hardware-free tests for `canbench` (Phase 2).

Tests run against python-can's **`virtual`** bus (in-process) and
**`udp_multicast`** bus (cross-process localhost "network CAN"), so the logger,
replayer, passthrough bridge, and candump round-trip can be validated with no
dongles attached.

_Test modules land here in Phase 2._
