"""The library registry — the explicit *manifest* from docs/design/graph-vs-tree.md.

A library is a first-class object cells subscribe to (via a `#| export <lib>:<mod>`
tag). This registry is where a library's identity lives — its name, package name,
and the on-disk nbdev project path Build writes to. Stored as one JSON file
alongside the dialogs (override the dir with `SIDEKICK_DATA`), so it survives
restarts. This is the lightweight "library.json" variant the design note allows; a
manifest *dialog* can layer on later.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from .export import slug


def _path() -> Path:
    base = os.environ.get("SIDEKICK_DATA")
    base = Path(base).expanduser() if base else Path.home() / ".config" / "solveit-sidekick"
    return base / "libraries.json"


def load() -> list[dict]:
    """The registered libraries (``[{name, pkg, path}, ...]``), or ``[]``."""
    p = _path()
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text())
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def save(libs: list[dict]) -> None:
    p = _path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(libs, indent=2))
        tmp.replace(p)                       # atomic
    except OSError:
        pass                                 # never let a disk hiccup break the app


def names() -> list[str]:
    return [lib["name"] for lib in load()]


def get(name: str) -> dict | None:
    return next((lib for lib in load() if lib["name"] == name), None)


def add(name: str, pkg: str | None = None, path: str | None = None) -> dict:
    """Register a library. ``name`` is the tag users write (`#| export name:mod`);
    ``pkg`` is the importable package name (defaults to a slug of ``name``);
    ``path`` is the nbdev project dir Build writes to (defaults under SIDEKICK_DATA)."""
    name = (name or "").strip()
    if not name:
        raise ValueError("library name is required")
    libs = load()
    if any(lib["name"] == name for lib in libs):
        raise ValueError(f"a library named '{name}' already exists")
    pkg = slug(pkg or name)
    if not (path or "").strip():
        path = str(_path().parent / "libraries" / pkg)
    lib = {"name": name, "pkg": pkg, "path": path.strip()}
    libs.append(lib)
    save(libs)
    return lib


def remove(name: str) -> None:
    save([lib for lib in load() if lib["name"] != name])
