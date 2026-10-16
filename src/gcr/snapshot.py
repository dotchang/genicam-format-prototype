"""Read back every readable feature of a device (the "what was really set" record)."""

from __future__ import annotations

from datetime import datetime, timezone

from .models import Config, Setting, Snapshot, SnapshotNode, setting_key
from .nodemap import NodeInfo, NodeMapLike, iter_value_features
from .profile import Resolver

# Common SFNC features in a safe application order (auto modes off before manual
# values, format before size before offset, selectors before selected features).
SFNC_ORDER = [
    "UserSetSelector",
    "PixelFormat", "BinningHorizontal", "BinningVertical", "DecimationHorizontal", "DecimationVertical",
    "ReverseX", "ReverseY", "Width", "Height", "OffsetX", "OffsetY",
    "AcquisitionMode", "TriggerSelector", "TriggerMode", "TriggerSource", "TriggerActivation", "TriggerDelay",
    "ExposureMode", "ExposureAuto", "ExposureTime",
    "GainAuto", "GainSelector", "Gain", "BlackLevelSelector", "BlackLevel", "Gamma",
    "BalanceWhiteAuto", "BalanceRatioSelector", "BalanceRatio",
    "AcquisitionFrameRateEnable", "AcquisitionFrameRate",
]


# Writable features that describe the session, not the imaging conditions.
NOT_EXPORTED = {"TLParamsLocked", "DeviceUserID", "DeviceLinkHeartbeatTimeout", "GevHeartbeatTimeout",
                "GenDCStreamingMode", "EventNotification", "EventSelector"}


def _node_entry(info: NodeInfo) -> SnapshotNode:
    return SnapshotNode(value=info.value, type=info.kind, access=info.access, unit=info.unit,
                        min=info.min, max=info.max, inc=info.inc, symbolics=info.symbolics)


def take_snapshot(nm: NodeMapLike, expand_selectors: bool = True) -> Snapshot:
    """Dump all readable value features.

    With ``expand_selectors`` every enum selector is stepped through its entries and
    the features it selects are recorded as ``Feature[Selector=Entry]``; the selector
    is restored afterwards.
    """
    snap = Snapshot(taken_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
    infos = list(iter_value_features(nm))
    for info in infos:
        if info.readable:
            snap.nodes[info.name] = _node_entry(info)

    if expand_selectors:
        for sel in infos:
            if not (sel.is_selector and sel.kind == "enum" and sel.writable and sel.symbolics and sel.selected):
                continue
            original = sel.value
            try:
                for entry in sel.symbolics:
                    try:
                        nm.set(sel.name, entry)
                    except Exception:
                        continue
                    for feat in sel.selected:
                        try:
                            fi = nm.info(feat)
                        except Exception:
                            continue
                        if fi.readable and fi.kind in ("int", "float", "bool", "enum", "str"):
                            snap.nodes[setting_key(feat, {sel.name: entry})] = _node_entry(fi)
            finally:
                try:
                    nm.set(sel.name, original)
                except Exception:
                    pass
    return snap


def config_from_device(nm: NodeMapLike, name: str, resolver: Resolver | None = None,
                       include_all: bool = False) -> Config:
    """Build a portable config from the current device state.

    By default only well-known SFNC features (``SFNC_ORDER``) are exported as
    ``required``. ``include_all`` adds every other writable feature as ``optional``
    because device-specific features rarely exist on other cameras.
    """
    resolver = resolver or Resolver()
    settings: list[Setting] = []
    seen: set[str] = set()

    def add(sfnc: str, info: NodeInfo, level: str) -> None:
        if not (info.readable and info.writable) or info.value is None or info.kind == "str":
            return
        unit = info.unit or None
        settings.append(Setting(node=sfnc, value=info.value, unit=unit, level=level))

    for sfnc in SFNC_ORDER:
        res = resolver.resolve(sfnc, nm.has)
        if res is None:
            continue
        try:
            info = nm.info(res.node)
        except Exception:
            continue
        seen.add(res.node)
        if res.conversion is not None and info.value is not None:
            info.value = res.from_device(info.value)
            info.unit = res.conversion.unit
        add(sfnc, info, "required")

    if include_all:
        infos = list(iter_value_features(nm))
        selected_by = {f: i for i in infos if i.is_selector for f in i.selected}
        for info in infos:
            if info.name in seen or info.is_selector or info.name in NOT_EXPORTED:
                continue
            add(resolver.sfnc_name(info.name), info, "optional")
            sel = selected_by.get(info.name)
            if sel is not None and settings and settings[-1].node == resolver.sfnc_name(info.name):
                settings[-1].selector = {resolver.sfnc_name(sel.name): sel.value}

    return Config(name=name, settings=settings)
