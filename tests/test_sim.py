"""Integration test against the Viky GenTL simulator bundled with the `genicam` package."""

import json
from pathlib import Path

import pytest

from conftest import ROOT
from gcr.cli import main

genicam = pytest.importorskip("genicam")
pytest.importorskip("harvesters")
VIKY = Path(genicam.__file__).parent / "viky.cti"
pytestmark = [pytest.mark.sim, pytest.mark.skipif(not VIKY.is_file(), reason="viky.cti not shipped")]


def test_viky_end_to_end(tmp_path, monkeypatch):
    monkeypatch.chdir(ROOT)  # profiles/ is auto-discovered from the working directory
    cti = ["--cti", str(VIKY)]
    dumped = tmp_path / "dumped.config.yaml"
    assert main(["dump", *cti, "-o", str(dumped), "--all"]) == 0
    assert main(["validate", str(dumped)]) == 0
    text = dumped.read_text()
    assert "node: BallSize" in text and "execute" not in text  # reverse profile alias, terse output

    assert main(["apply", *cti, "examples/viky.config.yaml", "-o", str(tmp_path / "applied.json")]) == 0
    applied = {r["key"]: r for r in json.loads((tmp_path / "applied.json").read_text())["records"]}
    assert applied["PixelFormat"]["applied"] == "Mono8"
    assert applied["ExposureTime"]["status"] == "skipped"
    assert applied["BallSize"]["node"] == "BouncingBallSize"

    out = tmp_path / "bundle"
    assert main(["capture", *cti, "examples/viky.recipe.yaml", "-o", str(out)]) == 0
    m = json.loads((out / "manifest.json").read_text())
    assert m["summary"]["frames_saved"] == 10
    assert any(p.startswith("genicam/") for p in m["files"])
    device = json.loads((out / "device.json").read_text())
    assert device["gentl"]["model"] == "Viky"
    snap = json.loads((out / "snapshot.json").read_text())["nodes"]
    assert "ChunkEnable[ChunkSelector=NrBounces]" in snap
    assert not any(k.endswith("_Val") for k in snap)  # only features, no internal nodes
    assert main(["verify", str(out)]) == 0
    assert main(["diff", "examples/viky.recipe.yaml", str(out), "--common"]) == 0
