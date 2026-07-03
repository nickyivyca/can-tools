"""canbench.gui -- PyQt5 desktop front-end for the CAN logger.

Phase 2: the logger window (vehicle profile + interface selection + per-bus
frames/sec + candump logging). Live signal decode is Phase 3.
"""
from .logger_window import LoggerWindow

__all__ = ["LoggerWindow"]
