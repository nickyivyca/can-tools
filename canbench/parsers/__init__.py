"""Log file parsers for various CAN bus tools."""
from .base import CANFrame, BaseParser
from .autodetect import detect_format, parse_file
