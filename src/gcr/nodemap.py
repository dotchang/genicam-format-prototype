"""A small node-map abstraction so the GCR logic does not depend on Harvesters.

``HarvestersNodeMap`` wraps a ``genicam.genapi.NodeMap``; ``FakeNodeMap`` is an
in-memory device used by tests and dry runs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Protocol

# genapi EInterfaceType -> GCR type name
_INTF_KIND = {2: "int", 3: "bool", 4: "cmd", 5: "float", 6: "str", 8: "category", 9: "enum"}
# genapi EAccessMode
_ACCESS = {0: "NI", 1: "NA", 2: "WO", 3: "RO", 4: "RW"}
VALUE_KINDS = ("int", "float", "bool", "enum", "str")


@dataclass
class NodeInfo:
    name: str
    kind: str  # int | float | bool | enum | str | cmd | category | other
    access: str  # RW | RO | WO | NA | NI
    value: Any = None
    min: Any = None
    max: Any = None
    inc: Any = None
    unit: str | None = None
    symbolics: list[str] | None = None
    is_selector: bool = False
    selected: list[str] = field(default_factory=list)

    @property
    def readable(self) -> bool:
        return self.access in ("RW", "RO")

    @property
    def writable(self) -> bool:
        return self.access in ("RW", "WO")


class NodeMapLike(Protocol):
    def has(self, name: str) -> bool: ...
    def info(self, name: str) -> NodeInfo: ...
    def get(self, name: str) -> Any: ...
    def set(self, name: str, value: Any) -> None: ...
    def execute(self, name: str) -> None: ...
    def feature_names(self) -> list[str]: ...


def _flag(attr) -> bool:
    """genapi exposes some INode flags (is_feature, is_selector) as methods."""
    return bool(attr() if callable(attr) else attr)


class HarvestersNodeMap:
    def __init__(self, node_map):
        self._nm = node_map

    def _node(self, name: str):
        return self._nm.get_node(name)

    def has(self, name: str) -> bool:
        try:
            self._node(name)
        except Exception:
            return False
        return True

    def info(self, name: str) -> NodeInfo:
        n = self._node(name)
        kind = _INTF_KIND.get(int(n.node.principal_interface_type), "other")
        info = NodeInfo(name=name, kind=kind, access=_ACCESS.get(int(n.node.get_access_mode()), "NI"))
        try:
            info.is_selector = bool(_flag(n.node.is_selector))
            if info.is_selector:
                info.selected = [f.node.name for f in n.node.selected_features]
        except Exception:
            pass
        if not info.readable or kind not in VALUE_KINDS:
            return info
        try:
            info.value = n.value
        except Exception:
            return info
        if kind in ("int", "float"):
            for attr in ("min", "max", "inc", "unit"):
                try:
                    setattr(info, attr, getattr(n, attr))
                except Exception:
                    pass
            info.unit = info.unit or None
        elif kind == "enum":
            try:
                info.symbolics = list(n.symbolics)
            except Exception:
                pass
        return info

    def get(self, name: str) -> Any:
        return self._node(name).value

    def set(self, name: str, value: Any) -> None:
        self._node(name).value = value

    def execute(self, name: str) -> None:
        self._node(name).execute()

    def feature_names(self) -> list[str]:
        names = []
        for n in self._nm.nodes:
            try:
                if _flag(n.node.is_feature):
                    names.append(n.node.name)
            except Exception:
                pass
        return names


class FakeNodeMap:
    """In-memory node map.

    ``spec`` maps node names to dicts with keys ``kind``, ``value`` and optionally
    ``access``, ``min``, ``max``, ``inc``, ``unit``, ``symbolics``, ``selected``
    (for selectors) and ``per`` (selector name whose value indexes this node).
    """

    def __init__(self, spec: dict[str, dict[str, Any]]):
        self.spec = {k: dict(v) for k, v in spec.items()}
        self.store: dict[tuple[str, Any], Any] = {}
        self.writes: list[tuple[str, Any]] = []
        self.executed: list[str] = []
        for name, s in self.spec.items():
            self.store[(name, None)] = s.get("value")

    def _slot(self, name: str):
        per = self.spec[name].get("per")
        return (name, self.get(per) if per else None)

    def has(self, name: str) -> bool:
        return name in self.spec

    def info(self, name: str) -> NodeInfo:
        if name not in self.spec:
            raise KeyError(name)
        s = self.spec[name]
        info = NodeInfo(
            name=name,
            kind=s["kind"],
            access=s.get("access", "RW"),
            min=s.get("min"),
            max=s.get("max"),
            inc=s.get("inc", 1 if s["kind"] == "int" else None),
            unit=s.get("unit"),
            symbolics=s.get("symbolics"),
            is_selector=bool(s.get("selected")),
            selected=list(s.get("selected", [])),
        )
        if info.readable and info.kind in VALUE_KINDS:
            info.value = self.get(name)
        return info

    def get(self, name: str) -> Any:
        slot = self._slot(name)
        return self.store.get(slot, self.store.get((name, None)))

    def set(self, name: str, value: Any) -> None:
        s = self.spec[name]
        if s.get("access", "RW") not in ("RW", "WO"):
            raise PermissionError(f"{name} is not writable")
        kind = s["kind"]
        if kind == "enum" and value not in s.get("symbolics", []):
            raise ValueError(f"{name}: invalid entry {value!r}")
        if kind in ("int", "float"):
            if s.get("min") is not None and value < s["min"] or s.get("max") is not None and value > s["max"]:
                raise ValueError(f"{name}: {value} out of range")
            if kind == "int":
                value = int(value)
            value = s.get("quantize", lambda v: v)(value)
        self.store[self._slot(name)] = value
        self.writes.append((name, value))

    def execute(self, name: str) -> None:
        if self.spec[name]["kind"] != "cmd":
            raise TypeError(f"{name} is not a command")
        self.executed.append(name)

    def feature_names(self) -> list[str]:
        return list(self.spec)


def iter_value_features(nm: NodeMapLike, names: Iterable[str] | None = None) -> Iterable[NodeInfo]:
    for name in names if names is not None else nm.feature_names():
        try:
            info = nm.info(name)
        except Exception:
            continue
        if info.kind in VALUE_KINDS:
            yield info
