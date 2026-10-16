"""Capture bundle: manifest with hashes, and its verification."""

from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

from .loader import parse
from .models import Manifest

MANIFEST = "manifest.json"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def hash_tree(root: Path) -> dict[str, str]:
    return {p.relative_to(root).as_posix(): sha256_file(p)
            for p in sorted(root.rglob("*")) if p.is_file() and p.name != MANIFEST}


def write_manifest(root: Path, manifest: Manifest) -> None:
    (root / MANIFEST).write_text(
        json.dumps(manifest.model_dump(mode="json", exclude_none=True), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8")


def verify(path: str | Path) -> list[str]:
    """Return a list of problems; empty means the bundle is intact.

    Accepts a bundle directory or a zip of one (manifest at the root or one level down).
    """
    path = Path(path)
    if path.is_dir():
        files = {p.relative_to(path).as_posix(): p.read_bytes for p in path.rglob("*") if p.is_file()}
    elif zipfile.is_zipfile(path):
        zf = zipfile.ZipFile(path)
        names = [n for n in zf.namelist() if not n.endswith("/")]
        prefix = ""
        if MANIFEST not in names:
            tops = [n for n in names if n.count("/") == 1 and n.endswith("/" + MANIFEST)]
            if not tops:
                return [f"{MANIFEST} not found in zip"]
            prefix = tops[0][: -len(MANIFEST)]
        files = {n[len(prefix):]: (lambda n=n: zf.read(n)) for n in names if n.startswith(prefix)}
    else:
        return [f"not a bundle directory or zip: {path}"]

    if MANIFEST not in files:
        return [f"{MANIFEST} missing"]
    try:
        manifest = parse(json.loads(files[MANIFEST]()), MANIFEST)
    except Exception as e:
        return [f"invalid manifest: {e}"]
    if not isinstance(manifest, Manifest):
        return [f"{MANIFEST} is not a bundle manifest"]

    problems = []
    for rel, digest in manifest.files.items():
        if rel not in files:
            problems.append(f"missing file: {rel}")
        elif sha256_bytes(files[rel]()) != digest:
            problems.append(f"hash mismatch: {rel}")
    extra = set(files) - set(manifest.files) - {MANIFEST}
    problems += [f"file not listed in manifest: {rel}" for rel in sorted(extra)]
    recipe_json = json.dumps(manifest.recipe.model_dump(mode="json", exclude_none=True), sort_keys=True)
    if sha256_bytes(recipe_json.encode()) != manifest.recipe_sha256:
        problems.append("recipe_sha256 does not match embedded recipe")
    return problems
