"""Apply a list of settings to a device in order, reading every value back."""

from __future__ import annotations

import math
from typing import Any

from .models import SPECIAL_VALUES, ApplyRecord, ApplyReport, Setting
from .nodemap import NodeInfo, NodeMapLike
from .profile import Resolved, Resolver

# unit -> (dimension, power of ten); integer exponents keep e.g. ms->us exactly 1000.
_UNITS = {
    "s": ("time", 0), "ms": ("time", -3), "us": ("time", -6), "µs": ("time", -6), "μs": ("time", -6),
    "ns": ("time", -9), "hz": ("freq", 0), "khz": ("freq", 3), "mhz": ("freq", 6), "db": ("db", 0),
}
# Units defined by SFNC, used when the device XML does not declare one.
SFNC_UNITS = {
    "ExposureTime": "us", "AcquisitionFrameRate": "Hz", "Gain": "dB", "BlackLevel": None,
    "TriggerDelay": "us", "LineDebouncerTime": "us", "ExposureAutoLimitMax": "us",
}
DEFAULT_FLOAT_REL_TOL = 1e-3  # 0.1 %


def unit_factor(src: str | None, dst: str | None) -> float | None:
    """Multiplier converting a value in ``src`` to ``dst``; None if not convertible."""
    if not src or not dst:
        return None
    a, b = _UNITS.get(src.strip().lower()), _UNITS.get(dst.strip().lower())
    if a is None or b is None or a[0] != b[0]:
        return 1.0 if src.strip().lower() == dst.strip().lower() else None
    return float(10 ** (a[1] - b[1]))


def apply_settings(nm: NodeMapLike, settings: list[Setting], resolver: Resolver | None = None) -> ApplyReport:
    resolver = resolver or Resolver()
    report = ApplyReport(profile=resolver.profile.name if resolver.profile else None)
    for s in settings:
        report.records.append(_apply_one(nm, s, resolver))
    return report


def _apply_one(nm: NodeMapLike, s: Setting, resolver: Resolver) -> ApplyRecord:
    rec = ApplyRecord(key=s.key, node=s.node, level=s.level, unit=s.unit,
                      requested="<execute>" if s.execute else s.value, status="ok")

    def unmet(msg: str) -> ApplyRecord:
        rec.status = "failed" if s.level == "required" else "skipped"
        rec.message = msg
        return rec

    for sel, sel_value in s.selector.items():
        r = resolver.resolve(sel, nm.has)
        if r is None:
            return unmet(f"selector {sel} not found on device")
        try:
            if nm.get(r.node) != sel_value:
                nm.set(r.node, sel_value)
        except Exception as e:
            return unmet(f"selector {sel}={sel_value}: {e}")

    res = resolver.resolve(s.node, nm.has)
    if res is None:
        return unmet("node not found on device")
    rec.node = res.node
    try:
        info = nm.info(res.node)
    except Exception as e:
        return unmet(f"cannot inspect node: {e}")

    if s.execute:
        if info.kind != "cmd" or not info.writable:
            return unmet(f"not an executable command (type {info.kind}, access {info.access})")
        try:
            nm.execute(res.node)
        except Exception as e:
            return unmet(f"execute failed: {e}")
        return rec

    if info.kind in ("int", "float"):
        return _apply_numeric(nm, s, res, info, rec, unmet)
    if info.kind in ("enum", "bool", "str"):
        return _apply_discrete(nm, s, res, info, rec, unmet)
    return unmet(f"unsupported node type {info.kind}")


def _apply_discrete(nm, s: Setting, res: Resolved, info: NodeInfo, rec: ApplyRecord, unmet):
    candidates = s.value if isinstance(s.value, list) else [s.value]
    if info.kind == "enum" and info.symbolics is not None:
        usable = [c for c in candidates if c in info.symbolics]
        if not usable:
            return unmet(f"none of {candidates} available (device offers {info.symbolics})")
        candidates = usable
    if info.kind == "bool":
        candidates = [_as_bool(c) for c in candidates]

    errors = []
    for i, c in enumerate(candidates):
        if info.readable and info.value == c:
            chosen = c
        elif not info.writable:
            errors.append(f"{c}: node not writable (access {info.access}), current value {info.value!r}")
            continue
        else:
            try:
                nm.set(res.node, c)
                chosen = c
            except Exception as e:
                errors.append(f"{c}: {e}")
                continue
        rec.applied = chosen
        rec.readback = _safe_get(nm, res.node)
        if rec.readback is not None and rec.readback != chosen:
            return unmet(f"readback {rec.readback!r} != {chosen!r}")
        if i > 0 or (isinstance(s.value, list) and chosen != s.value[0]):
            rec.message = f"used candidate {chosen!r}"
        return rec
    return unmet("; ".join(errors))


def _apply_numeric(nm, s: Setting, res: Resolved, info: NodeInfo, rec: ApplyRecord, unmet):
    node_unit = (res.conversion.unit if res.conversion and res.conversion.unit else None) \
        or info.unit or SFNC_UNITS.get(s.node)
    factor = 1.0
    notes = []
    if s.unit:
        f = unit_factor(s.unit, node_unit)
        if f is None:
            notes.append(f"unit '{s.unit}' not convertible to device unit '{node_unit}'; value used as-is")
        else:
            factor = f

    def user_to_device(v: float) -> float:
        return res.to_device(v * factor)

    def device_to_user(v: float) -> float:
        return res.from_device(v) / factor

    lo, hi = info.min, info.max
    candidates = s.value if isinstance(s.value, list) else [s.value]
    chosen_user = chosen_dev = None
    for c in candidates:
        if isinstance(c, str) and c in SPECIAL_VALUES:
            if lo is None or hi is None:
                continue
            dev = {"@min": lo, "@max": hi, "@center": lo + (hi - lo) / 2}[c]
            dev = _align(dev, info)
            chosen_user, chosen_dev = device_to_user(dev), dev
            break
        if isinstance(c, bool) or not isinstance(c, (int, float)):
            return unmet(f"value {c!r} is not numeric")
        dev = user_to_device(float(c))
        if (lo is None or dev >= lo - 1e-12) and (hi is None or dev <= hi + 1e-12):
            chosen_user, chosen_dev = float(c), dev
            break
    if chosen_dev is None:
        first = next((c for c in candidates if not isinstance(c, str)), None)
        if first is None:
            return unmet("special value needs node min/max")
        chosen_user = float(first)
        chosen_dev = min(max(user_to_device(chosen_user), lo if lo is not None else -math.inf),
                         hi if hi is not None else math.inf)
        notes.append(f"clamped to device range [{lo}, {hi}]")
    chosen_dev = _align(chosen_dev, info)
    if info.kind == "int":
        chosen_dev = int(round(chosen_dev))

    if not info.writable:
        notes.append(f"node not writable (access {info.access})")
    else:
        try:
            nm.set(res.node, chosen_dev)
        except Exception as e:
            return unmet("; ".join(notes + [f"set {chosen_dev}: {e}"]))
    rec.applied = _num(device_to_user(chosen_dev))
    rb = _safe_get(nm, res.node)
    rec.readback = None if rb is None else _num(device_to_user(rb))

    if rec.readback is None:
        notes.append("not readable; cannot verify")
    elif not _within(rec.readback, chosen_user, s.tolerance, info, factor, res):
        msg = f"readback {rec.readback} outside tolerance of {chosen_user}"
        rec.message = "; ".join(notes + [msg])
        rec.status = "failed" if s.level == "required" else "approx"
        return rec
    if not info.writable and rec.readback is not None:
        notes[-1] += ", but already within tolerance"
    rec.message = "; ".join(notes) or None
    return rec


def _within(readback: float, target: float, tol, info: NodeInfo, factor: float, res: Resolved) -> bool:
    diff = abs(readback - target)
    if isinstance(tol, str):
        return diff <= abs(target) * float(tol.strip().rstrip("%")) / 100 + 1e-12
    if isinstance(tol, (int, float)):
        return diff <= tol + 1e-12
    if info.kind == "int":
        step = abs(res.from_device(info.inc or 1) - res.from_device(0)) / factor
        return diff < step - 1e-12 or diff <= 1e-12
    return diff <= abs(target) * DEFAULT_FLOAT_REL_TOL + 1e-12


def _align(dev: float, info: NodeInfo) -> float:
    if info.kind == "int" and info.inc and info.min is not None:
        dev = info.min + round((dev - info.min) / info.inc) * info.inc
        if info.max is not None and dev > info.max:
            dev -= info.inc
    return dev


def _safe_get(nm: NodeMapLike, name: str) -> Any:
    try:
        return nm.get(name)
    except Exception:
        return None


def _num(v: float) -> float | int:
    return int(v) if float(v).is_integer() else round(v, 9)


def _as_bool(v: Any) -> bool:
    if isinstance(v, str):
        return v.strip().lower() in ("true", "1", "on", "yes")
    return bool(v)
