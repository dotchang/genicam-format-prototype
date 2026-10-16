"""Run a recipe on a camera and write a capture bundle."""

from __future__ import annotations

import csv
import json
import os
import platform
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from importlib import metadata
from pathlib import Path
from typing import Any, Callable, Protocol

import numpy as np

from . import __version__
from .apply import apply_settings
from .bundle import hash_tree, sha256_bytes, sha256_file, write_manifest
from .loader import GcrError
from .models import ApplyReport, Config, Manifest, Recipe, Setting, Summary
from .nodemap import FakeNodeMap, HarvestersNodeMap, NodeMapLike
from .profile import Resolver
from .snapshot import take_snapshot

DEVICE_NODES = ["DeviceVendorName", "DeviceModelName", "DeviceSerialNumber", "DeviceVersion",
                "DeviceFirmwareVersion", "DeviceManufacturerInfo", "DeviceUserID", "DeviceSFNCVersionMajor",
                "DeviceSFNCVersionMinor", "DeviceSFNCVersionSubMinor"]


@dataclass
class Frame:
    image: np.ndarray
    pixel_format: str
    frame_id: int | None = None
    device_timestamp_ns: int | None = None
    device_timestamp_ticks: int | None = None
    incomplete: bool = False
    chunks: dict[str, Any] = field(default_factory=dict)


class Camera(Protocol):
    node_map: NodeMapLike

    def device_info(self) -> dict[str, Any]: ...
    def genicam_xml(self) -> tuple[str, bytes] | None: ...
    def start(self) -> None: ...
    def stop(self) -> None: ...
    def fetch(self, timeout_ms: int, chunks: list[str]) -> Frame: ...
    def close(self) -> None: ...


class CaptureAborted(GcrError):
    def __init__(self, report: ApplyReport):
        super().__init__("required settings could not be applied")
        self.report = report


# --------------------------------------------------------------------------- harvesters


def find_cti_files(explicit: list[str] | None) -> list[Path]:
    """Explicit --cti paths, else every *.cti in GENICAM_GENTL64_PATH (GenTL standard)."""
    if explicit:
        files = [Path(p) for p in explicit]
    else:
        var = "GENICAM_GENTL64_PATH" if sys.maxsize > 2**32 else "GENICAM_GENTL32_PATH"
        files = [f for d in os.environ.get(var, "").split(os.pathsep) if d and Path(d).is_dir()
                 for f in sorted(Path(d).glob("*.cti"))]
    missing = [str(f) for f in files if not f.is_file()]
    if missing:
        raise GcrError(f"CTI file not found: {', '.join(missing)}")
    if not files:
        raise GcrError("no GenTL producer: pass --cti or set GENICAM_GENTL64_PATH")
    # Harvesters silently finds no devices when given a relative path.
    return [f.resolve() for f in files]


def list_devices(cti_files: list[Path]) -> list[dict[str, Any]]:
    from harvesters.core import Harvester

    h = Harvester()
    try:
        for f in cti_files:
            h.add_file(str(f))
        h.update()
        return [_device_info_dict(d) for d in h.device_info_list]
    finally:
        h.reset()


def _device_info_dict(d) -> dict[str, Any]:
    out = {}
    for k in ("vendor", "model", "serial_number", "id_", "version", "tl_type", "user_defined_name"):
        try:
            out[k.rstrip("_")] = getattr(d, k)
        except Exception:
            pass
    return out


class HarvestersCamera:
    def __init__(self, cti_files: list[Path], device: str | None = None):
        from harvesters.core import Harvester

        self.cti_files = cti_files
        self.h = Harvester()
        for f in cti_files:
            self.h.add_file(str(f))
        self.h.update()
        infos = [_device_info_dict(d) for d in self.h.device_info_list]
        if not infos:
            self.h.reset()
            raise GcrError("no camera found")
        index = _select_device(infos, device)
        self._info = infos[index]
        self.ia = self.h.create(index)
        self.node_map = HarvestersNodeMap(self.ia.remote_device.node_map)

    def device_info(self) -> dict[str, Any]:
        info: dict[str, Any] = {"gentl": self._info, "nodes": {}}
        for name in DEVICE_NODES:
            try:
                info["nodes"][name] = self.node_map.get(name)
            except Exception:
                pass
        info["producers"] = [{"file": f.name, "sha256": sha256_file(f)} for f in self.cti_files]
        return info

    def genicam_xml(self) -> tuple[str, bytes] | None:
        port = self.ia.remote_device.port
        for u in port.url_info_list:
            url = u.url
            if not url.lower().startswith("local:"):
                continue
            fname, addr, size = url.split(":", 1)[1].split(";")[:3]
            r = port.read(int(addr, 16), int(size, 16))
            data = r[1] if isinstance(r, (list, tuple)) else r
            return Path(fname).name, bytes(data)
        return None

    def start(self) -> None:
        self.ia.start()

    def stop(self) -> None:
        self.ia.stop()

    def fetch(self, timeout_ms: int, chunks: list[str]) -> Frame:
        try:
            buf_ctx = self.ia.fetch(timeout=timeout_ms / 1000)
        except Exception as e:
            if "timeout" in type(e).__name__.lower():
                raise TimeoutError(str(e)) from e
            raise
        with buf_ctx as buf:
            c = buf.payload.components[0]
            n = int(c.num_components_per_pixel or 1)
            data = np.array(c.data, copy=True)
            image = data.reshape(c.height, c.width, n) if n > 1 else data.reshape(c.height, c.width)
            frame = Frame(image=image, pixel_format=str(c.data_format))
            frame.frame_id = _try(lambda: int(buf.frame_id))
            frame.device_timestamp_ns = _try(lambda: int(buf.timestamp_ns))
            frame.device_timestamp_ticks = _try(lambda: int(buf.timestamp))
            frame.incomplete = bool(_try(lambda: buf.module.is_incomplete) or False)
            for ch in chunks:
                frame.chunks[ch] = _try(lambda: self.node_map.get("Chunk" + ch))
        return frame

    def close(self) -> None:
        try:
            self.ia.destroy()
        finally:
            self.h.reset()


def _select_device(infos: list[dict[str, Any]], device: str | None) -> int:
    if device is None:
        return 0
    for i, d in enumerate(infos):
        if device in (d.get("serial_number"), d.get("id"), d.get("user_defined_name")):
            return i
    if device.isdigit() and int(device) < len(infos):
        return int(device)
    raise GcrError(f"device {device!r} not found; available: {[d.get('serial_number') for d in infos]}")


def _try(fn):
    try:
        return fn()
    except Exception:
        return None


# --------------------------------------------------------------------------- fake


class FakeCamera:
    """Simulated camera for tests: frames are filled with the frame index."""

    def __init__(self, spec: dict[str, dict[str, Any]], info: dict[str, Any] | None = None):
        self.node_map = FakeNodeMap(spec)
        self._info = info or {"vendor": "GCR", "model": "FakeCam", "serial_number": "0"}
        self._count = 0
        self._running = False

    def device_info(self):
        return {"gentl": self._info, "nodes": {}, "producers": []}

    def genicam_xml(self):
        return "fake.xml", b"<RegisterDescription/>"

    def start(self):
        self._running = True

    def stop(self):
        self._running = False

    def fetch(self, timeout_ms, chunks):
        nm = self.node_map
        triggered = nm.has("TriggerMode") and nm.get("TriggerMode") == "On"
        if not self._running:
            raise RuntimeError("not started")
        if triggered:
            if not nm.executed or nm.executed[-1] != "TriggerSoftware":
                raise TimeoutError("no trigger")
            nm.executed.append("<consumed>")
        self._count += 1
        h, w = nm.get("Height"), nm.get("Width")
        img = np.full((h, w), self._count % 256, dtype=np.uint8)
        return Frame(image=img, pixel_format=nm.get("PixelFormat") if nm.has("PixelFormat") else "Mono8",
                     frame_id=self._count, device_timestamp_ns=self._count * 1_000_000,
                     chunks={c: nm.get("Chunk" + c) if nm.has("Chunk" + c) else None for c in chunks})

    def close(self):
        pass


# --------------------------------------------------------------------------- recipe execution


def recipe_settings(recipe: Recipe) -> list[Setting]:
    """Settings implied by the acquisition section, applied after the config."""
    acq, trig = recipe.acquisition, recipe.acquisition.trigger
    frame_start = {"TriggerSelector": "FrameStart"}
    s = [Setting(node="AcquisitionMode", value="Continuous", level="optional")]
    if trig.mode == "free_run":
        s.append(Setting(node="TriggerMode", value="Off", selector=frame_start, level="preferred"))
        if acq.frame_rate:
            s.append(Setting(node="AcquisitionFrameRateEnable", value=True, level="optional"))
            s.append(Setting(node="AcquisitionFrameRate", value=acq.frame_rate, unit="Hz", level="preferred"))
    else:
        source = "Software" if trig.mode == "software" else trig.source
        s.append(Setting(node="TriggerMode", value="On", selector=frame_start))
        s.append(Setting(node="TriggerSource", value=source, selector=frame_start))
        if trig.mode == "hardware":
            s.append(Setting(node="TriggerActivation", value=trig.activation, selector=frame_start,
                             level="preferred"))
    if recipe.chunks:
        s.append(Setting(node="ChunkModeActive", value=True))
        for c in recipe.chunks:
            s.append(Setting(node="ChunkEnable", value=True, selector={"ChunkSelector": c}, level="preferred"))
    return s


def _save_frame(root: Path, recipe: Recipe, index: int, frame: Frame) -> str:
    rel = recipe.output.layout.format(index=index) + "." + recipe.output.format
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    if recipe.output.format == "npy":
        np.save(path, frame.image)
    else:
        import tifffile

        img = frame.image
        kw: dict[str, Any] = {"photometric": "minisblack"}
        if img.ndim == 3 and img.shape[2] in (3, 4):
            kw = {"photometric": "rgb"}
            if img.shape[2] == 4:
                kw["extrasamples"] = ("unassalpha",)
        tifffile.imwrite(path, img, metadata={"pixel_format": frame.pixel_format, "frame_id": frame.frame_id}, **kw)
    return rel


def _versions() -> dict[str, str]:
    out = {"gcr": __version__, "python": platform.python_version(), "os": platform.platform()}
    for pkg in ("harvesters", "genicam", "numpy", "tifffile"):
        try:
            out[pkg] = metadata.version(pkg)
        except metadata.PackageNotFoundError:
            pass
    return out


def run_capture(camera: Camera, recipe: Recipe, out_dir: str | Path, resolver: Resolver | None = None,
                force: bool = False, log: Callable[[str], None] = lambda m: None) -> Manifest:
    if not isinstance(recipe.config, Config):
        raise GcrError("recipe config must be resolved (use loader.resolve_recipe)")
    root = Path(out_dir)
    if root.exists() and any(root.iterdir()):
        raise GcrError(f"output directory is not empty: {root}")

    nm = camera.node_map
    report = apply_settings(nm, recipe.config.settings + recipe_settings(recipe), resolver)
    if not report.ok and not force:
        raise CaptureAborted(report)
    root.mkdir(parents=True, exist_ok=True)

    def write_json(rel: str, obj: Any) -> None:
        (root / rel).write_text(json.dumps(obj, indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8")

    write_json("applied.json", report.model_dump(mode="json", exclude_none=True))
    log("taking snapshot")
    write_json("snapshot.json", take_snapshot(nm).model_dump(mode="json", exclude_none=True))
    device = camera.device_info()
    device["host"] = _versions()
    write_json("device.json", device)
    xml = _try(camera.genicam_xml)
    if xml:
        (root / "genicam").mkdir(exist_ok=True)
        (root / "genicam" / xml[0]).write_bytes(xml[1])

    acq = recipe.acquisition
    software = acq.trigger.mode == "software"
    trigger_node = None
    if software:
        trigger_node = (resolver or Resolver()).resolve("TriggerSoftware", nm.has)
        if trigger_node is None:
            raise GcrError("software trigger requested but TriggerSoftware node not found")

    rows, saved, incomplete, timeouts = [], 0, 0, 0
    camera.start()
    try:
        for i in range(acq.warmup_frames + acq.frames):
            if software:
                nm.execute(trigger_node.node)
            try:
                frame = camera.fetch(acq.timeout_ms, recipe.chunks)
            except TimeoutError:
                timeouts += 1
                log(f"timeout on frame {i}")
                if timeouts >= 3:
                    log("3 consecutive timeouts; stopping")
                    break
                continue
            timeouts = 0
            host_ns = time.time_ns()
            if i < acq.warmup_frames:
                continue
            index = i - acq.warmup_frames
            rel = _save_frame(root, recipe, index, frame)
            saved += 1
            incomplete += int(frame.incomplete)
            rows.append({"index": index, "file": rel, "frame_id": frame.frame_id,
                         "device_timestamp_ns": frame.device_timestamp_ns,
                         "device_timestamp_ticks": frame.device_timestamp_ticks, "host_timestamp_ns": host_ns,
                         "pixel_format": frame.pixel_format, "incomplete": frame.incomplete,
                         **{f"chunk_{k}": v for k, v in frame.chunks.items()}})
            log(f"frame {index + 1}/{acq.frames}")
    finally:
        camera.stop()

    fieldnames = list(rows[0]) if rows else ["index", "file", "frame_id"]
    with open(root / "frames.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)

    ids = sorted(r["frame_id"] for r in rows if r["frame_id"])
    missing = sorted(set(range(ids[0], ids[-1] + 1)) - set(ids)) if ids else []
    recipe_json = json.dumps(recipe.model_dump(mode="json", exclude_none=True), sort_keys=True)
    manifest = Manifest(
        created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        tool={"name": "gcr", "version": __version__},
        recipe=recipe,
        recipe_sha256=sha256_bytes(recipe_json.encode()),
        summary=Summary(frames_requested=acq.frames, frames_saved=saved, frames_incomplete=incomplete,
                        missing_frame_ids=missing, apply_ok=report.ok, apply_counts=report.counts()),
        files=hash_tree(root),
    )
    write_manifest(root, manifest)
    return manifest
