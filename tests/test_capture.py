import json

import numpy as np
import pytest
import tifffile

from conftest import ROOT
from gcr.bundle import verify
from gcr.capture import CaptureAborted, FakeCamera, run_capture
from gcr.cli import main
from gcr.loader import parse, resolve_recipe
from gcr.models import Config, Recipe, Setting


def make_recipe(**acq):
    cfg = Config(name="c", settings=[Setting(node="Width", value=64), Setting(node="Height", value=32),
                                     Setting(node="Gamma", value=1.0, level="optional")])
    return Recipe(name="r", config=cfg, acquisition={"frames": 4, "warmup_frames": 1, **acq},
                  chunks=["Timestamp"])


def test_software_trigger_capture(spec, tmp_path):
    cam = FakeCamera(spec)
    out = tmp_path / "b"
    m = run_capture(cam, make_recipe(trigger={"mode": "software"}), out)
    assert m.summary.frames_saved == 4 and m.summary.apply_ok and m.summary.missing_frame_ids == []
    assert cam.node_map.get("TriggerMode") == "On" and cam.node_map.get("TriggerSource") == "Software"
    img = tifffile.imread(out / "frames" / "000000.tiff")
    assert img.shape == (32, 64) and np.all(img == 2)  # frame 1 was warm-up
    rows = (out / "frames.csv").read_text().splitlines()
    assert rows[0].startswith("index,file,frame_id") and "chunk_Timestamp" in rows[0] and len(rows) == 5
    applied = json.loads((out / "applied.json").read_text())
    assert {r["key"]: r["status"] for r in applied["records"]}["Gamma"] == "skipped"
    snap = json.loads((out / "snapshot.json").read_text())
    assert "Gain[GainSelector=Red]" in snap["nodes"]
    assert verify(out) == []
    assert main(["verify", str(out)]) == 0


def test_verify_detects_tampering(spec, tmp_path):
    out = tmp_path / "b"
    run_capture(FakeCamera(spec), make_recipe(), out)
    (out / "frames" / "000001.tiff").write_bytes(b"x")
    (out / "extra.txt").write_text("x")
    problems = verify(out)
    assert "hash mismatch: frames/000001.tiff" in problems
    assert "file not listed in manifest: extra.txt" in problems


def test_required_failure_aborts(spec, tmp_path):
    r = make_recipe()
    r.config.settings.append(Setting(node="PixelFormat", value="Mono16"))
    with pytest.raises(CaptureAborted) as e:
        run_capture(FakeCamera(spec), r, tmp_path / "b")
    assert not (tmp_path / "b").exists()
    assert not e.value.report.ok
    m = run_capture(FakeCamera(spec), r, tmp_path / "b", force=True)
    assert not m.summary.apply_ok


def test_manifest_roundtrip_and_diff(spec, tmp_path, capsys):
    out = tmp_path / "b"
    run_capture(FakeCamera(spec), make_recipe(), out)
    m = parse(json.loads((out / "manifest.json").read_text()))
    assert m.recipe.config.name == "c"
    assert main(["diff", str(out), str(out)]) == 0
    cfg = tmp_path / "c.yaml"
    cfg.write_text("kind: config\nname: x\nsettings:\n  - {node: Width, value: 100}\n")
    assert main(["diff", str(cfg), str(out), "--common"]) == 1
    assert "Width" in capsys.readouterr().out


def test_example_recipe_resolves():
    r = resolve_recipe(ROOT / "examples" / "low-noise-mono.recipe.yaml")
    assert r.config.settings[0].node == "PixelFormat"
