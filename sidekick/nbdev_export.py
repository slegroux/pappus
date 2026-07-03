"""Project a cell-centric library into an nbdev project.

This is the tree side of `docs/design/graph-vs-tree.md`, the nbdev-delegation
path: Sidekick does not build the library itself, it *projects* the cells tagged
to a library into **nbdev-ready notebooks**, and nbdev builds the package + docs.

A library is defined by a per-cell directive that names a library and a module::

    #| export <lib>:<module>     this cell belongs to library <lib>, module <module>
    #| export <module>           (no colon) dialog-local, today's behaviour — NOT a library

The `<lib>:<module>` form is Sidekick-internal; on emit it is translated to native
nbdev directives — one notebook per module carrying ``#| default_exp <module>`` at
the top and a plain ``#| export`` on each cell. Each emitted cell also carries a
``# source: <dialog> #<id>`` provenance comment (a plain comment, so it survives
into the generated ``.py``) that is also a Sidekick message-ID link back to the
cell it came from.

The source of truth is always the dialog cells; the notebooks (and the ``.py``
nbdev builds from them) are generated and never hand-edited.
"""
from __future__ import annotations

import json

from .export import _directives, slug


def parse_lib_target(arg: str) -> tuple[str | None, str]:
    """Split an ``#| export`` argument into ``(library, module)``.

    ``"audiolib:layers"`` -> ``("audiolib", "layers")``; ``"audiolib:"`` ->
    ``("audiolib", "core")`` (empty module defaults to core). A bare ``"layers"``
    (no colon) is dialog-local, not a library tag -> ``(None, "layers")``.
    """
    if ":" in arg:
        lib, _, module = arg.partition(":")
        return (lib.strip() or None), (module.strip() or "core")
    return None, arg.strip()


def gather(backend, lib: str) -> dict[str, list[dict]]:
    """Collect every cell tagged to library ``lib`` across all dialogs.

    Returns ``{module: [{"body", "dialog", "id"}, ...]}`` in dialog then cell
    order. Cells with a plain ``#| export`` (no ``lib:`` prefix) are dialog-local
    and excluded. Muting is a context concern, orthogonal to export, so muted
    cells still export.
    """
    modules: dict[str, list[dict]] = {}
    for dialog in backend.list_dialogs() or []:
        for m in backend.messages(dialog):
            if getattr(m, "msg_type", "") != "code":
                continue
            directives, body = _directives(m.content)
            if not body.strip():
                continue
            for name, arg in directives:
                if name != "export":
                    continue
                cell_lib, module = parse_lib_target(arg)
                if cell_lib == lib:
                    modules.setdefault(module, []).append(
                        {"body": body, "dialog": dialog, "id": m.id})
    return modules


def _code_cell(source: str) -> dict:
    """An nbformat v4 code cell. `source` is stored as a list of lines (with
    trailing newlines except the last), the shape nbformat expects."""
    lines = source.splitlines(keepends=True)
    return {"cell_type": "code", "metadata": {}, "execution_count": None,
            "outputs": [], "source": lines}


def _notebook(cells: list[dict]) -> dict:
    return {"cells": cells,
            "metadata": {"kernelspec": {"display_name": "Python 3",
                                        "language": "python", "name": "python3"}},
            "nbformat": 4, "nbformat_minor": 5}


def module_notebook(module: str, entries: list[dict]) -> dict:
    """An nbdev-ready notebook for one module: a ``#| default_exp`` header cell,
    then one ``#| export`` code cell per entry (with a provenance comment)."""
    cells = [_code_cell(f"#| default_exp {module}")]
    for e in entries:
        src = f"#| export\n# source: {e['dialog']} #{e['id']}\n{e['body']}"
        cells.append(_code_cell(src))
    return _notebook(cells)


def _pyproject_toml(pkg: str) -> str:
    """A minimal nbdev `pyproject.toml` (nbdev 3.x dropped `settings.ini`). The
    `[tool.nbdev]` section is what marks the directory as an nbdev project and
    tells `nbdev-export` where to read notebooks (`nbs_path`) and write modules
    (`lib_path`)."""
    return (
        "[build-system]\n"
        'requires = ["setuptools>=64"]\n'
        'build-backend = "setuptools.build_meta"\n\n'
        "[project]\n"
        f'name = "{pkg}"\n'
        'version = "0.0.1"\n'
        'description = "Generated from Sidekick dialogs."\n'
        'readme = "README.md"\n'
        'requires-python = ">=3.10"\n\n'
        "[tool.nbdev]\n"
        f'lib_path = "{pkg}"\n'
        'nbs_path = "nbs"\n'
    )


def library_files(backend, lib: str, pkg_name: str | None = None) -> dict[str, str]:
    """Build ``{relative_path: content}`` for a library's nbdev project.

    Layout::

        pyproject.toml       # minimal [tool.nbdev]; nbdev-new writes a fuller one
        nbs/index.ipynb
        nbs/<module>.ipynb   # one per module, nbdev-ready

    Pure (no disk) so it's easy to test. Returns an empty-module project (just
    scaffolding) if no cell is tagged to the library.
    """
    pkg = slug(pkg_name or lib)
    modules = gather(backend, lib)
    files: dict[str, str] = {"pyproject.toml": _pyproject_toml(pkg),
                             "nbs/index.ipynb": _index_ipynb(pkg)}
    for module, entries in modules.items():
        files[f"nbs/{slug(module)}.ipynb"] = (
            json.dumps(module_notebook(module, entries), indent=1) + "\n")
    return files


def _index_ipynb(pkg: str) -> str:
    nb = _notebook([_code_cell(f"#| hide\n# {pkg} — generated from Sidekick dialogs.")])
    return json.dumps(nb, indent=1) + "\n"


def _module_files(modules: dict[str, list[dict]]) -> dict[str, str]:
    return {f"nbs/{slug(m)}.ipynb": json.dumps(module_notebook(m, e), indent=1) + "\n"
            for m, e in modules.items()}


def write_files(dest: str, files: dict[str, str]) -> list[str]:
    """Write ``{relpath: content}`` under ``dest``, creating dirs. Returns paths."""
    import os
    written = []
    for rel, content in sorted(files.items()):
        path = os.path.join(dest, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(content)
        written.append(path)
    return written


def prune_orphan_modules(dest: str, pkg: str, keep_modules) -> list[str]:
    """Delete generated module notebooks (and their tangled ``.py``) that are no
    longer in the library, so a retagged or deleted cell can't leave a phantom
    module behind — the cells stay the source of truth (graph-vs-tree.md).

    Scoped tightly: only ``nbs/<module>.ipynb`` files are candidates (never
    ``index.ipynb``), and a ``<pkg>/<module>.py`` is removed only when its matching
    generated notebook is being removed — so user files and nbdev's own
    ``__init__.py`` / ``_modidx.py`` are never touched. Returns removed relpaths.
    """
    import glob
    import os
    keep = {slug(m) for m in keep_modules}
    removed: list[str] = []
    for nb in glob.glob(os.path.join(dest, "nbs", "*.ipynb")):
        stem = os.path.splitext(os.path.basename(nb))[0]
        if stem == "index" or stem in keep:
            continue
        os.remove(nb)
        removed.append(os.path.relpath(nb, dest))
        py = os.path.join(dest, pkg, f"{stem}.py")
        if os.path.isfile(py):
            os.remove(py)
            removed.append(os.path.relpath(py, dest))
    return removed


def _find_exe(*names: str) -> str | None:
    """Find a console script by any of `names`, on PATH or in the running
    interpreter's own bin dir (so it's found when the app runs from a venv whose
    bin isn't on PATH, e.g. `.venv/bin/python -m uvicorn`). The scripts install as
    either `nbdev_export` or `nbdev-export` depending on the packaging toolchain."""
    import os
    import shutil
    import sys
    for n in names:
        p = shutil.which(n)
        if p:
            return p
    bindir = os.path.dirname(sys.executable)
    for n in names:
        p = os.path.join(bindir, n)
        if os.path.isfile(p):
            return p
    return None


def is_nbdev_project(dest: str) -> bool:
    """True if `dest` is already an nbdev project — a pyproject.toml with a
    [tool.nbdev] section (nbdev's own marker). Rebuilds skip scaffolding."""
    import os
    p = os.path.join(dest, "pyproject.toml")
    try:
        return os.path.isfile(p) and "[tool.nbdev]" in open(p, encoding="utf-8").read()
    except OSError:
        return False


def _git_identity() -> tuple[str, str]:
    """(author, email) from git config, best-effort — used to fill the nbdev
    project metadata so scaffolding needs no prompts. Double-quotes are stripped:
    some configs store the name *with* quotes (`user.name = "Jane Doe"`), which
    would double up when interpolated into the pyproject TOML and break it."""
    import subprocess
    def cfg(key):
        try:
            r = subprocess.run(["git", "config", "--get", key], capture_output=True,
                               text=True, timeout=5)
            return r.stdout.replace('"', "").strip() if r.returncode == 0 else ""
        except Exception:  # noqa: BLE001
            return ""
    return cfg("user.name"), cfg("user.email")


def scaffold_nbdev(dest: str, pkg: str, author: str = "", email: str = "",
                   user: str = "", description: str = "") -> tuple[bool, str]:
    """Generate a real nbdev `pyproject.toml` (with the full `[tool.nbdev]`,
    `[project.entry-points.nbdev]`, setuptools config) using nbdev's own config
    generator, ``nbdev_create_config`` — **offline**.

    This deliberately avoids `nbdev-new`, whose one-time template download hits the
    *unauthenticated* GitHub API (hardcoded `authenticate=False`, so a token can't
    fix it) and gets rate-limited. `nbdev_create_config` touches no network, so it
    never rate-limits; the tradeoff is the template extras (LICENSE, CI, docs
    `index.ipynb`, styles) that full `nbdev-new` adds are not generated — Sidekick
    writes the module notebooks + a stub index itself. Run in a subprocess of the
    interpreter that has nbdev; the caller falls back to a minimal pyproject if it
    can't run (nbdev not installed)."""
    import json
    import os
    import subprocess
    import sys
    os.makedirs(dest, exist_ok=True)
    kw = {"repo": pkg, "user": user or pkg, "author": author or "",
          "author_email": email or "", "path": dest,
          "description": description or f"{pkg} — generated from Sidekick dialogs"}
    script = ("import sys, json\n"
              "from nbdev.config import nbdev_create_config\n"
              "nbdev_create_config(**json.loads(sys.argv[1]))\n")
    try:
        r = subprocess.run([sys.executable, "-c", script, json.dumps(kw)],
                           cwd=dest, capture_output=True, text=True, timeout=60)
    except Exception as e:  # noqa: BLE001 — caller falls back to minimal pyproject
        return False, f"nbdev_create_config failed to run: {e}"
    if r.returncode != 0:
        return False, (r.stderr or r.stdout or "").strip()[:300] or "nbdev_create_config error"
    return True, "generated nbdev pyproject.toml with nbdev_create_config (offline)"


def run_nbdev(dest: str) -> tuple[bool, str]:
    """Best-effort: run ``nbdev_export`` in ``dest`` to tangle the notebooks into
    the package. Returns ``(ok, detail)``; a missing nbdev is a soft failure —
    the notebooks are still valid and can be built later."""
    import subprocess
    exe = _find_exe("nbdev_export", "nbdev-export")
    if not exe:
        return False, "nbdev not installed — notebooks emitted; run `nbdev_export` to build the package."
    try:
        r = subprocess.run([exe], cwd=dest, capture_output=True, text=True, timeout=120)
    except Exception as e:  # noqa: BLE001
        return False, f"nbdev_export failed to run: {e}"
    if r.returncode != 0:
        return False, (r.stderr or r.stdout or "").strip()[:400] or "nbdev_export nonzero exit"
    return True, "nbdev_export built the package."


def build_library(backend, lib: str, dest: str, pkg_name: str | None = None) -> dict:
    """Build ``lib`` into ``dest``: scaffold the nbdev project once, write the
    module notebooks from the tagged cells, and tangle to the ``.py`` package.

    Scaffolding (see docs/design/graph-vs-tree.md): the *first* build to a fresh dir
    generates a real nbdev ``pyproject.toml`` with ``nbdev_create_config`` —
    **offline**, so no GitHub rate limit (unlike ``nbdev-new``); *rebuilds* reuse
    the existing project and just refresh the notebooks. If nbdev isn't installed,
    fall back to a minimal ``pyproject.toml`` — still enough to tangle to ``.py``.
    Force the minimal path with ``SIDEKICK_NBDEV_SCAFFOLD=0``.

    Returns ``{"pkg", "modules", "files", "scaffold", "nbdev_ok", "nbdev_detail"}``.
    """
    import os
    pkg = slug(pkg_name or lib)
    modules = gather(backend, lib)
    module_files = _module_files(modules)

    if is_nbdev_project(dest):
        scaffold = "existing nbdev project (reused)"
    else:
        want = os.environ.get("SIDEKICK_NBDEV_SCAFFOLD", "1") != "0"
        ok, detail = (scaffold_nbdev(dest, pkg, *_git_identity())
                      if want else (False, "scaffold disabled"))
        if ok:
            # create_config writes only pyproject.toml. Add the stub index, and a
            # stub __init__.py with __version__ — its pyproject reads the version
            # dynamically from `<pkg>.__version__`, which must resolve before the
            # first `nbdev-export` can run (nbdev regenerates __init__ afterwards).
            write_files(dest, {"nbs/index.ipynb": _index_ipynb(pkg),
                               f"{pkg}/__init__.py": '__version__ = "0.0.1"\n'})
            scaffold = detail
        else:                               # minimal, offline-safe project
            write_files(dest, {"pyproject.toml": _pyproject_toml(pkg),
                               "nbs/index.ipynb": _index_ipynb(pkg)})
            scaffold = f"minimal pyproject ({detail})"

    # Reconcile against the live cell set BEFORE writing: drop notebooks (and their
    # tangled .py) for modules that no longer have any tagged cell, so a rebuild
    # after a retag/delete doesn't leave a phantom module in the package.
    pruned = prune_orphan_modules(dest, pkg, modules.keys())
    write_files(dest, module_files)         # our module notebooks, always ours
    nbdev_ok, nb_detail = run_nbdev(dest)
    return {"pkg": pkg, "modules": sorted(modules), "files": sorted(module_files),
            "pruned": pruned, "scaffold": scaffold,
            "nbdev_ok": nbdev_ok, "nbdev_detail": nb_detail}
