"""Command line interface: ``gcr <command> ...``."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .apply import apply_settings
from .bundle import verify
from .capture import CaptureAborted, HarvestersCamera, find_cti_files, list_devices, recipe_settings, run_capture
from .loader import GcrError, dump_doc, load, load_as, resolve_recipe
from .models import DOC_MODELS, SPECIAL_VALUES, ApplyReport, Config, Manifest, Recipe, Snapshot
from .profile import Resolver, choose_profile, profile_dirs

_STATUS_MARK = {"ok": "OK  ", "approx": "~   ", "skipped": "SKIP", "failed": "FAIL"}


def _camera_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--cti", action="append", help="GenTL producer (.cti); repeatable. Default: GENICAM_GENTL64_PATH")
    p.add_argument("--device", help="serial number, device id, user name or index (default: first)")
    p.add_argument("--profile", help="profile file (default: auto-match from --profiles-dir / ./profiles)")
    p.add_argument("--profiles-dir", action="append", help="extra directory with *.profile.yaml")


def _open(args) -> tuple[HarvestersCamera, Resolver]:
    cam = HarvestersCamera(find_cti_files(args.cti), args.device)
    g = cam.device_info()
    nodes, gentl = g["nodes"], g["gentl"]
    vendor = nodes.get("DeviceVendorName") or gentl.get("vendor", "")
    model = nodes.get("DeviceModelName") or gentl.get("model", "")
    version = nodes.get("DeviceVersion") or gentl.get("version", "")
    profile = choose_profile(args.profile, profile_dirs(args.profiles_dir), vendor, model, version)
    print(f"camera: {vendor} {model} (serial {gentl.get('serial_number')}); "
          f"profile: {profile.name if profile else '-'}", file=sys.stderr)
    return cam, Resolver(profile)


def print_report(report: ApplyReport) -> None:
    for r in report.records:
        line = f"{_STATUS_MARK[r.status]} {r.key:<40} {r.level:<9} req={r.requested!r}"
        if r.readback is not None:
            line += f" -> {r.readback!r}"
        if r.unit:
            line += f" {r.unit}"
        if r.node != r.key.split("[")[0]:
            line += f" (node {r.node})"
        if r.message:
            line += f"  # {r.message}"
        print(line)
    print("result:", "OK" if report.ok else "REQUIRED SETTINGS FAILED", report.counts())


def cmd_list(args) -> int:
    for i, d in enumerate(list_devices(find_cti_files(args.cti))):
        print(i, json.dumps(d, ensure_ascii=False))
    return 0


def cmd_dump(args) -> int:
    from .snapshot import config_from_device, take_snapshot

    cam, resolver = _open(args)
    try:
        if args.snapshot:
            doc = take_snapshot(cam.node_map)
        else:
            doc = config_from_device(cam.node_map, args.name or Path(args.output).name.split(".")[0],
                                     resolver, include_all=args.all)
    finally:
        cam.close()
    dump_doc(doc, args.output)
    print(f"wrote {args.output}")
    return 0


def cmd_validate(args) -> int:
    rc = 0
    for f in args.files:
        try:
            doc = load(f)
            if isinstance(doc, Recipe) and isinstance(doc.config, str):
                resolve_recipe(f)
            print(f"OK   {f} ({doc.kind})")
        except (GcrError, OSError) as e:
            print(f"FAIL {e}")
            rc = 1
    return rc


def cmd_apply(args) -> int:
    doc = load(args.file)
    if isinstance(doc, Recipe):
        doc = resolve_recipe(args.file)
        settings = doc.config.settings + recipe_settings(doc)
    elif isinstance(doc, Config):
        settings = doc.settings
    else:
        raise GcrError("apply expects a config or recipe")
    cam, resolver = _open(args)
    try:
        if args.dry_run:
            # Evaluate against an in-memory copy of the current node values.
            from .nodemap import FakeNodeMap, iter_value_features

            spec = {i.name: {"kind": i.kind, "value": i.value, "access": i.access, "min": i.min, "max": i.max,
                             "inc": i.inc, "unit": i.unit, "symbolics": i.symbolics}
                    for i in iter_value_features(cam.node_map)}
            for n in cam.node_map.feature_names():
                spec.setdefault(n, {"kind": "cmd", "value": None})
            report = apply_settings(FakeNodeMap(spec), settings, resolver)
        else:
            report = apply_settings(cam.node_map, settings, resolver)
    finally:
        cam.close()
    print_report(report)
    if args.output:
        dump_doc(report, args.output)
    return 0 if report.ok else 2


def cmd_capture(args) -> int:
    recipe = resolve_recipe(args.recipe)
    if args.frames:
        recipe.acquisition.frames = args.frames
    cam, resolver = _open(args)
    try:
        m = run_capture(cam, recipe, args.output, resolver, force=args.force,
                        log=lambda s: print(s, file=sys.stderr))
    except CaptureAborted as e:
        print_report(e.report)
        print("aborted (use --force to capture anyway)", file=sys.stderr)
        return 2
    finally:
        cam.close()
    print(f"wrote {args.output}: {m.summary.model_dump()}")
    return 0


def _flatten(path: str) -> dict[str, Any]:
    """Flat {key: value} view. For bundles: snapshot values overlaid with applied readbacks,
    so SFNC keys from a config line up even when a profile mapped them to other nodes."""
    p = Path(path)
    if p.is_dir() or p.name == "manifest.json":
        root = p if p.is_dir() else p.parent
        out = _flatten(str(root / "snapshot.json"))
        if (root / "applied.json").is_file():
            out.update({k: v for k, v in _flatten(str(root / "applied.json")).items() if v is not None})
        return out
    doc = load(p)
    if isinstance(doc, Snapshot):
        return {k: v.value for k, v in doc.nodes.items()}
    if isinstance(doc, Config):
        return {s.key: s.value for s in doc.settings if not s.execute}
    if isinstance(doc, Recipe):
        cfg = resolve_recipe(p).config
        return {s.key: s.value for s in cfg.settings + recipe_settings(doc) if not s.execute}
    if isinstance(doc, ApplyReport):
        return {r.key: r.readback for r in doc.records}
    raise GcrError(f"cannot diff kind {doc.kind}")


def _same(va: Any, vb: Any) -> bool:
    """Equal, numerically close, or satisfied by a candidate list / special value."""
    if isinstance(va, list) or isinstance(vb, list):
        la = va if isinstance(va, list) else [va]
        lb = vb if isinstance(vb, list) else [vb]
        return any(_same(x, y) for x in la for y in lb)
    if any(isinstance(v, str) and v in SPECIAL_VALUES for v in (va, vb)):
        return True  # resolved on the device; not comparable offline
    if isinstance(va, (int, float)) and isinstance(vb, (int, float)) and not isinstance(va, bool):
        return abs(va - vb) <= 1e-9 * max(1.0, abs(va))
    return va == vb


def cmd_diff(args) -> int:
    a, b = _flatten(args.a), _flatten(args.b)
    keys = list(dict.fromkeys(list(a) + list(b)))
    if args.common:
        keys = [k for k in keys if k in a and k in b]
    diffs = 0
    for k in keys:
        va, vb = a.get(k, "<absent>"), b.get(k, "<absent>")
        if not _same(va, vb):
            diffs += 1
            print(f"{k:<45} {va!r:>20}  |  {vb!r}")
    print(f"{diffs} difference(s)")
    return 1 if diffs else 0


def cmd_verify(args) -> int:
    problems = verify(args.bundle)
    for p in problems:
        print("FAIL", p)
    if not problems:
        m = load_as(Path(args.bundle) / "manifest.json", Manifest) if Path(args.bundle).is_dir() else None
        print("OK", m.summary.model_dump() if m else "")
    return 1 if problems else 0


def cmd_schema(args) -> int:
    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=True)
    for kind, model in DOC_MODELS.items():
        schema = model.model_json_schema()
        schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
        schema["$id"] = f"https://github.com/dotchang/genicam-format-prototype/spec/schema/{kind}.schema.json"
        (out / f"{kind}.schema.json").write_text(json.dumps(schema, indent=2) + "\n", encoding="utf-8")
        print(f"wrote {out / (kind + '.schema.json')}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="gcr", description="GenICam Capture Recipe tool")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("list", help="list cameras")
    p.add_argument("--cti", action="append")
    p.set_defaults(fn=cmd_list)

    p = sub.add_parser("dump", help="export current camera settings as a config (or full snapshot)")
    _camera_args(p)
    p.add_argument("-o", "--output", required=True)
    p.add_argument("--name")
    p.add_argument("--all", action="store_true", help="also export device-specific features (as optional)")
    p.add_argument("--snapshot", action="store_true", help="write a full snapshot instead of a config")
    p.set_defaults(fn=cmd_dump)

    p = sub.add_parser("validate", help="validate GCR files")
    p.add_argument("files", nargs="+")
    p.set_defaults(fn=cmd_validate)

    p = sub.add_parser("apply", help="apply a config or recipe and report each setting")
    _camera_args(p)
    p.add_argument("file")
    p.add_argument("--dry-run", action="store_true", help="evaluate against a copy of the node values only")
    p.add_argument("-o", "--output", help="write the applied report (.json/.yaml)")
    p.set_defaults(fn=cmd_apply)

    p = sub.add_parser("capture", help="run a recipe and write a capture bundle")
    _camera_args(p)
    p.add_argument("recipe")
    p.add_argument("-o", "--output", required=True)
    p.add_argument("--frames", type=int, help="override acquisition.frames")
    p.add_argument("--force", action="store_true", help="capture even if required settings failed")
    p.set_defaults(fn=cmd_capture)

    p = sub.add_parser("diff", help="compare configs / recipes / snapshots / bundles / applied reports")
    p.add_argument("a")
    p.add_argument("b")
    p.add_argument("--common", action="store_true", help="only keys present in both")
    p.set_defaults(fn=cmd_diff)

    p = sub.add_parser("verify", help="check bundle hashes and manifest")
    p.add_argument("bundle")
    p.set_defaults(fn=cmd_verify)

    p = sub.add_parser("schema", help="export JSON Schemas")
    p.add_argument("-o", "--output", default="spec/schema")
    p.set_defaults(fn=cmd_schema)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.fn(args)
    except GcrError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
