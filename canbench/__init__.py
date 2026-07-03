"""canbench — standalone CAN acquisition, logging, replay, and GUI package.

Migrated out of the reverse-it `canre` framework so the runtime/logger/GUI
stack can live in its own distributable repo. Depends only on python-can
(+ PyQt5 for the GUI); no analysis code lives here.

Subpackages / modules:
    live        hardware interface: dongle detection, per-bus RX threads, USB reset
    gui         PyQt logger GUI (Phase 2)
    logio       python-can candump read/write wrappers
    passthrough two-dongle bridge (Phase 2)
    profiles    vehicle-profile .ini loader (Phase 2)
"""
