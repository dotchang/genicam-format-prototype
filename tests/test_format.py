import json

import jsonschema
import pytest
import yaml

from conftest import ROOT
from gcr.cli import main
from gcr.loader import GcrError, load, parse, resolve_recipe, to_yaml
from gcr.models import Config, Setting

DOC_FILES = sorted((ROOT / "examples").glob("*.yaml")) + sorted((ROOT / "profiles").glob("*.yaml"))


@pytest.mark.parametrize("path", DOC_FILES, ids=lambda p: p.name)
def test_examples_validate(path):
    doc = load(path)
    if doc.kind == "recipe":
        assert isinstance(resolve_recipe(path).config, Config)


@pytest.mark.parametrize("path", DOC_FILES, ids=lambda p: p.name)
def test_examples_match_committed_schema(path):
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    schema = json.loads((ROOT / "spec" / "schema" / f"{data['kind']}.schema.json").read_text(encoding="utf-8"))
    jsonschema.validate(data, schema)


def test_committed_schema_is_current(tmp_path):
    assert main(["schema", "-o", str(tmp_path)]) == 0
    for f in tmp_path.iterdir():
        assert (ROOT / "spec" / "schema" / f.name).read_text(encoding="utf-8") == f.read_text(encoding="utf-8"), \
            f"{f.name} is stale: run `gcr schema -o spec/schema`"


@pytest.mark.parametrize("bad", [
    {"kind": "config", "name": "x", "settings": [{"node": "A"}]},                          # no value
    {"kind": "config", "name": "x", "settings": [{"node": "A", "value": 1, "execute": True}]},
    {"kind": "config", "name": "x", "settings": [{"node": "A", "value": 1, "tolerance": "1"}]},
    {"kind": "config", "name": "x", "settings": [{"node": "A", "value": 1, "levle": "optional"}]},  # typo
    {"kind": "config", "name": "x", "gcr_version": "2.0"},
    {"kind": "nope"},
])
def test_invalid_documents(bad):
    with pytest.raises(GcrError):
        parse(bad)


def test_yaml_roundtrip():
    cfg = Config(name="rt", settings=[Setting(node="Gain", value=1.5, selector={"GainSelector": "All"})])
    text = to_yaml(cfg.model_dump(mode="json", exclude_none=True))
    assert "{node: Gain" in text  # one setting per line
    assert parse(yaml.safe_load(text)) == cfg
