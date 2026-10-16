"""Camera profiles: map SFNC names to device-specific nodes."""

from __future__ import annotations

import os
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path

from .loader import GcrError, load_as
from .models import Conversion, Profile


@dataclass
class Resolved:
    node: str
    conversion: Conversion | None = None

    def to_device(self, value):
        c = self.conversion
        if c is None or isinstance(value, (str, bool)):
            return value
        return (value - c.offset) / c.scale

    def from_device(self, value):
        c = self.conversion
        if c is None or isinstance(value, (str, bool)) or value is None:
            return value
        return value * c.scale + c.offset


class Resolver:
    """Resolves SFNC names against an optional profile and the actual node map."""

    def __init__(self, profile: Profile | None = None):
        self.profile = profile

    def resolve(self, name: str, has_node) -> Resolved | None:
        p = self.profile
        if p is not None:
            if name in p.conversions and has_node(p.conversions[name].node):
                return Resolved(p.conversions[name].node, p.conversions[name])
            if name in p.aliases and has_node(p.aliases[name]):
                return Resolved(p.aliases[name])
        if has_node(name):
            return Resolved(name)
        return None

    def sfnc_name(self, device_node: str) -> str:
        """Reverse lookup used when dumping a device into a portable config."""
        p = self.profile
        if p is not None:
            for sfnc, c in p.conversions.items():
                if c.node == device_node:
                    return sfnc
            for sfnc, alias in p.aliases.items():
                if alias == device_node:
                    return sfnc
        return device_node


def profile_dirs(extra: list[str] | None = None) -> list[Path]:
    dirs = [Path(d) for d in (extra or [])]
    env = os.environ.get("GCR_PROFILE_PATH")
    if env:
        dirs += [Path(d) for d in env.split(os.pathsep) if d]
    dirs.append(Path.cwd() / "profiles")
    return dirs


def load_profiles(dirs: list[Path]) -> list[Profile]:
    out = []
    for d in dirs:
        if d.is_dir():
            for f in sorted(d.glob("*.profile.y*ml")):
                out.append(load_as(f, Profile))
    return out


def match_profile(profiles: list[Profile], vendor: str, model: str, version: str = "") -> Profile | None:
    """Most specific match wins (most literal characters); ties keep file order."""

    def ok(pattern: str, value: str) -> bool:
        return fnmatch((value or "").lower(), pattern.lower())

    hits = [p for p in profiles if ok(p.match.vendor, vendor) and ok(p.match.model, model)
            and ok(p.match.device_version, version)]
    if not hits:
        return None
    def literal_chars(p: Profile) -> int:
        return sum(len(s) - s.count("*") - s.count("?") for s in (p.match.vendor, p.match.model, p.match.device_version))

    return max(hits, key=literal_chars)


def choose_profile(explicit: str | None, dirs: list[Path], vendor: str, model: str, version: str = "") -> Profile | None:
    if explicit:
        try:
            return load_as(explicit, Profile)
        except FileNotFoundError:
            raise GcrError(f"profile not found: {explicit}") from None
    return match_profile(load_profiles(dirs), vendor, model, version)
