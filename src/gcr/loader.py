"""Reading and writing GCR documents (YAML for humans, JSON for machines)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

from .models import DOC_MODELS, Config, Recipe


class GcrError(Exception):
    pass


def read_raw(path: str | Path) -> dict[str, Any]:
    path = Path(path)
    text = path.read_text(encoding="utf-8")
    data = json.loads(text) if path.suffix.lower() == ".json" else yaml.safe_load(text)
    if not isinstance(data, dict):
        raise GcrError(f"{path}: top level must be a mapping")
    return data


def parse(data: dict[str, Any], source: str = "<data>"):
    kind = data.get("kind")
    model = DOC_MODELS.get(kind)
    if model is None:
        raise GcrError(f"{source}: unknown or missing 'kind' ({kind!r}); expected one of {sorted(DOC_MODELS)}")
    try:
        return model.model_validate(data)
    except Exception as e:  # pydantic.ValidationError, but keep the message
        raise GcrError(f"{source}: {e}") from e


def load(path: str | Path):
    """Load any GCR document. A bundle directory loads its manifest."""
    path = Path(path)
    if path.is_dir():
        path = path / "manifest.json"
    return parse(read_raw(path), str(path))


def load_as(path: str | Path, model: type):
    doc = load(path)
    if not isinstance(doc, model):
        raise GcrError(f"{path}: expected kind '{model.model_fields['kind'].default}', got '{doc.kind}'")
    return doc


def resolve_recipe(path: str | Path) -> Recipe:
    """Load a recipe and inline its config so the result is self-contained."""
    path = Path(path)
    recipe = load_as(path, Recipe)
    if isinstance(recipe.config, str):
        config = load_as(path.parent / recipe.config, Config)
        recipe = recipe.model_copy(update={"config": config})
    return recipe


def dump_doc(doc, path: str | Path) -> None:
    path = Path(path)
    data = doc.model_dump(mode="json", exclude_none=True)
    for s in data.get("settings", []):  # keep configs terse: drop per-setting defaults
        if s.get("execute") is False:
            del s["execute"]
        if s.get("selector") == {}:
            del s["selector"]
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix.lower() == ".json":
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    else:
        path.write_text(to_yaml(data), encoding="utf-8")


def to_yaml(data: dict[str, Any]) -> str:
    """YAML with one-line flow mappings for settings, which keeps configs diff-friendly."""

    class _Dumper(yaml.SafeDumper):
        pass

    def _repr_dict(dumper, d):
        flow = all(not isinstance(v, (dict, list)) or k == "selector" for k, v in d.items()) and "node" in d
        return dumper.represent_mapping("tag:yaml.org,2002:map", d, flow_style=flow)

    _Dumper.add_representer(dict, _repr_dict)
    return yaml.dump(data, Dumper=_Dumper, sort_keys=False, allow_unicode=True, width=120)
