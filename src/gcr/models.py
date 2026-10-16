"""Data models of the GCR format.

These pydantic models are the single source of truth: the JSON Schemas in
``spec/schema`` are generated from them (``gcr schema -o spec/schema``).
"""

from __future__ import annotations

from typing import Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from . import GCR_VERSION

Scalar = Union[bool, int, float, str]
Level = Literal["required", "preferred", "optional"]
Status = Literal["ok", "approx", "skipped", "failed"]
SPECIAL_VALUES = ("@max", "@min", "@center")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _Doc(_Strict):
    gcr_version: str = Field(GCR_VERSION, description="Format version 'MAJOR.MINOR'.")

    @field_validator("gcr_version")
    @classmethod
    def _check_version(cls, v: str) -> str:
        major = v.split(".")[0]
        if major != GCR_VERSION.split(".")[0]:
            raise ValueError(f"unsupported gcr_version {v!r} (this tool supports {GCR_VERSION})")
        return v


# --------------------------------------------------------------------------- profile


class ProfileMatch(_Strict):
    vendor: str = Field("*", description="fnmatch pattern on DeviceVendorName (case-insensitive).")
    model: str = Field("*", description="fnmatch pattern on DeviceModelName (case-insensitive).")
    device_version: str = Field("*", description="fnmatch pattern on DeviceVersion / firmware.")


class Conversion(_Strict):
    node: str = Field(..., description="Device-specific node that holds the value.")
    scale: float = Field(1.0, description="sfnc_value = raw * scale + offset")
    offset: float = 0.0
    unit: str | None = Field(None, description="Unit of the SFNC-side value (e.g. 'dB', 'us').")


class Profile(_Doc):
    kind: Literal["profile"] = "profile"
    name: str
    match: ProfileMatch
    verified: bool = Field(False, description="True if checked against real hardware.")
    aliases: dict[str, str] = Field(default_factory=dict, description="SFNC name -> device node name.")
    conversions: dict[str, Conversion] = Field(
        default_factory=dict, description="SFNC name -> device node with linear value conversion."
    )
    quirks: list[str] = Field(default_factory=list, description="Free-text notes for humans.")


# --------------------------------------------------------------------------- config


class Setting(_Strict):
    node: str = Field(..., description="SFNC feature name (or device-specific name).")
    value: Scalar | list[Scalar] | None = Field(
        None,
        description="Target value. A list is an ordered list of candidates (first usable wins). "
        "'@max', '@min' and '@center' (offsets only) are resolved on the device.",
    )
    execute: bool = Field(False, description="Execute a command node instead of setting a value.")
    unit: str | None = Field(None, description="Unit of 'value' (s, ms, us, ns, Hz, kHz, dB, ...).")
    selector: dict[str, Scalar] = Field(
        default_factory=dict, description="Selectors set (in order) before this node, e.g. {GainSelector: All}."
    )
    level: Level = Field("required", description="What to do when the value cannot be reproduced.")
    tolerance: float | str | None = Field(
        None, description="Accepted readback deviation: absolute number or relative string like '1%'."
    )
    note: str | None = None

    @model_validator(mode="after")
    def _value_or_execute(self) -> "Setting":
        if self.execute == (self.value is not None):
            raise ValueError(f"{self.node}: specify exactly one of 'value' or 'execute: true'")
        if isinstance(self.tolerance, str) and not self.tolerance.strip().endswith("%"):
            raise ValueError(f"{self.node}: string tolerance must be a percentage like '1%'")
        return self

    @property
    def key(self) -> str:
        return setting_key(self.node, self.selector)


def setting_key(node: str, selector: dict[str, Any] | None) -> str:
    """Flat identifier used in snapshots/diffs, e.g. ``Gain[GainSelector=All]``."""
    if not selector:
        return node
    return node + "[" + ",".join(f"{k}={v}" for k, v in selector.items()) + "]"


class Target(_Strict):
    """Requirements on the camera, written instead of a concrete model name."""

    pixel_format_family: Literal["mono", "color", "bayer", "any"] = "any"
    min_bit_depth: int | None = None
    min_width: int | None = None
    min_height: int | None = None
    notes: str | None = None


class Config(_Doc):
    kind: Literal["config"] = "config"
    name: str
    description: str | None = None
    target: Target = Field(default_factory=Target)
    settings: list[Setting] = Field(default_factory=list, description="Applied top to bottom.")


# --------------------------------------------------------------------------- recipe


class Trigger(_Strict):
    mode: Literal["free_run", "software", "hardware"] = "free_run"
    source: str = Field("Line1", description="TriggerSource for hardware mode.")
    activation: str = Field("RisingEdge", description="TriggerActivation for hardware mode.")


class Acquisition(_Strict):
    trigger: Trigger = Field(default_factory=Trigger)
    frames: int = Field(1, ge=1)
    frame_rate: float | None = Field(None, gt=0, description="Hz; free_run only.")
    warmup_frames: int = Field(0, ge=0, description="Frames grabbed and discarded before recording.")
    timeout_ms: int = Field(3000, ge=1)


class Output(_Strict):
    format: Literal["tiff", "npy"] = "tiff"
    layout: str = Field("frames/{index:06d}", description="Path template without extension.")


class Recipe(_Doc):
    kind: Literal["recipe"] = "recipe"
    name: str
    description: str | None = None
    config: str | Config = Field(..., description="Path (relative to this file) or inline config.")
    acquisition: Acquisition = Field(default_factory=Acquisition)
    chunks: list[str] = Field(default_factory=list, description="ChunkSelector names to record per frame.")
    output: Output = Field(default_factory=Output)


# --------------------------------------------------------------------------- results


class ApplyRecord(_Strict):
    key: str
    node: str = Field(..., description="Node actually written on the device (after profile resolution).")
    level: Level
    requested: Any = None
    applied: Any = None
    readback: Any = None
    unit: str | None = None
    status: Status
    message: str | None = None


class ApplyReport(_Doc):
    kind: Literal["applied"] = "applied"
    profile: str | None = None
    records: list[ApplyRecord] = Field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(not (r.status == "failed" and r.level == "required") for r in self.records)

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for r in self.records:
            out[r.status] = out.get(r.status, 0) + 1
        return out


class SnapshotNode(_Strict):
    value: Any = None
    type: str
    access: str
    unit: str | None = None
    min: Any = None
    max: Any = None
    inc: Any = None
    symbolics: list[str] | None = None


class Snapshot(_Doc):
    kind: Literal["snapshot"] = "snapshot"
    taken_at: str
    nodes: dict[str, SnapshotNode] = Field(default_factory=dict, description="Key: node[selector=value].")


class Summary(_Strict):
    frames_requested: int
    frames_saved: int
    frames_incomplete: int = 0
    missing_frame_ids: list[int] = Field(default_factory=list)
    apply_ok: bool
    apply_counts: dict[str, int] = Field(default_factory=dict)


class Manifest(_Doc):
    kind: Literal["bundle"] = "bundle"
    created_at: str
    tool: dict[str, str]
    recipe: Recipe = Field(..., description="Recipe with the config inlined.")
    recipe_sha256: str
    summary: Summary
    files: dict[str, str] = Field(..., description="Relative path -> sha256 of every file in the bundle.")


DOC_MODELS: dict[str, type[_Doc]] = {
    "profile": Profile,
    "config": Config,
    "recipe": Recipe,
    "applied": ApplyReport,
    "snapshot": Snapshot,
    "bundle": Manifest,
}
