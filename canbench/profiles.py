"""Vehicle profiles for canbench: where captures go + capture defaults.

A profile selects the log destination and default bitrate for a vehicle. Profiles
load from an ``.ini`` file; with no ``.ini`` present, built-in **Vtrux** and
**Coda** profiles are used, writing to ``VtruxLogs/`` and ``CodaLogs/`` beside the
running executable (or the current working directory when run from source).

INI format (``canbench.ini`` by default)::

    [general]
    default_profile = vtrux

    [profile:vtrux]
    log_dir = VtruxLogs
    bitrate = 500000

    [profile:coda]
    log_dir = CodaLogs
    bitrate = 500000

``log_dir`` may be relative (resolved against the app base dir) or absolute.
"""
from __future__ import annotations

import configparser
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional

DEFAULT_BITRATE = 500000
DEFAULT_INI_NAME = "canbench.ini"

# name -> (default log subdir, description). vehicle_key links to
# canbench.live.vehicle_configs.VEHICLES for later anchor-based bus naming.
_BUILTIN = {
    "vtrux": ("VtruxLogs", "Via/VTRUX hybrid truck"),
    "coda":  ("CodaLogs",  "Coda Sedan EV"),
}


def app_base_dir() -> Path:
    """Directory captures are written under by default.

    When packaged with PyInstaller (``sys.frozen``), this is the folder the exe
    lives in — so a recipient's logs land next to the app. From source, it's the
    current working directory.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path.cwd()


@dataclass
class VehicleProfile:
    name: str
    log_dir: Path
    bitrate: int = DEFAULT_BITRATE
    description: str = ""
    vehicle_key: str = ""          # key into VEHICLES for anchor-based bus id (optional)

    def __post_init__(self):
        if not self.vehicle_key:
            self.vehicle_key = self.name


@dataclass
class ProfileSet:
    profiles: Dict[str, VehicleProfile]
    default_profile: str

    def names(self):
        return list(self.profiles.keys())

    def get(self, name: str) -> VehicleProfile:
        return self.profiles[name]

    @property
    def default(self) -> VehicleProfile:
        return self.profiles[self.default_profile]


def _resolve_dir(raw: str, base_dir: Path) -> Path:
    p = Path(raw)
    return p if p.is_absolute() else (base_dir / p)


def builtin_profiles(base_dir: Optional[Path] = None) -> ProfileSet:
    base = base_dir or app_base_dir()
    profs = {
        name: VehicleProfile(
            name=name,
            log_dir=(base / subdir),
            bitrate=DEFAULT_BITRATE,
            description=desc,
            vehicle_key=name,
        )
        for name, (subdir, desc) in _BUILTIN.items()
    }
    return ProfileSet(profiles=profs, default_profile="vtrux")


def load_profiles(ini_path: Optional[Path] = None,
                  base_dir: Optional[Path] = None) -> ProfileSet:
    """Load vehicle profiles.

    ``ini_path``: explicit .ini. If None, look for ``canbench.ini`` in the app
    base dir; if that is missing too, fall back to the built-in profiles.
    ``base_dir``: base for resolving relative ``log_dir`` values and default
    log folders (defaults to :func:`app_base_dir`).
    """
    base = base_dir or app_base_dir()

    if ini_path is None:
        candidate = base / DEFAULT_INI_NAME
        ini_path = candidate if candidate.is_file() else None

    if ini_path is None or not Path(ini_path).is_file():
        return builtin_profiles(base)

    cfg = configparser.ConfigParser()
    cfg.read(ini_path, encoding="utf-8")

    profiles: Dict[str, VehicleProfile] = {}
    for section in cfg.sections():
        if not section.startswith("profile:"):
            continue
        name = section.split(":", 1)[1].strip()
        sc = cfg[section]
        subdir = sc.get("log_dir", _BUILTIN.get(name, (name.capitalize() + "Logs",))[0])
        profiles[name] = VehicleProfile(
            name=name,
            log_dir=_resolve_dir(subdir, base),
            bitrate=sc.getint("bitrate", DEFAULT_BITRATE),
            description=sc.get("description", _BUILTIN.get(name, ("", ""))[-1] if name in _BUILTIN else ""),
            vehicle_key=sc.get("vehicle_key", name),
        )

    if not profiles:
        return builtin_profiles(base)

    default_profile = cfg.get("general", "default_profile", fallback=next(iter(profiles)))
    if default_profile not in profiles:
        default_profile = next(iter(profiles))

    return ProfileSet(profiles=profiles, default_profile=default_profile)
