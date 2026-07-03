"""Unit tests for canbench.profiles (no hardware)."""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from canbench.profiles import load_profiles, builtin_profiles, DEFAULT_BITRATE


def test_builtin_defaults():
    base = Path(tempfile.gettempdir())
    ps = builtin_profiles(base_dir=base)
    assert set(ps.names()) == {"vtrux", "coda"}
    assert ps.default_profile == "vtrux"
    assert ps.get("vtrux").log_dir == base / "VtruxLogs"
    assert ps.get("coda").log_dir == base / "CodaLogs"
    assert ps.get("vtrux").bitrate == DEFAULT_BITRATE


def test_no_ini_falls_back_to_builtin():
    base = Path(tempfile.gettempdir())
    ps = load_profiles(ini_path=base / "does_not_exist.ini", base_dir=base)
    assert set(ps.names()) == {"vtrux", "coda"}


def test_ini_relative_and_absolute(tmp_path=None):
    base = Path(tempfile.mkdtemp())
    abs_dir = (base / "abs_logs").resolve()
    ini = base / "canbench.ini"
    ini.write_text(
        "[general]\n"
        "default_profile = coda\n\n"
        "[profile:vtrux]\n"
        "log_dir = MyVtrux\n"
        "bitrate = 250000\n\n"
        "[profile:coda]\n"
        f"log_dir = {abs_dir}\n",
        encoding="utf-8",
    )
    ps = load_profiles(ini_path=ini, base_dir=base)
    assert ps.default_profile == "coda"
    assert ps.get("vtrux").log_dir == base / "MyVtrux"     # relative -> resolved vs base
    assert ps.get("vtrux").bitrate == 250000
    assert ps.get("coda").log_dir == abs_dir                # absolute preserved


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
