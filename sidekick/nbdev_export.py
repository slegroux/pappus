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

        settings.ini
        nbs/index.ipynb
        nbs/<module>.ipynb   # one per module, nbdev-ready

    Pure (no disk) so it's easy to test. Returns an empty-module project (just
    scaffolding) if no cell is tagged to the library.
    """
    pkg = slug(pkg_name or lib)
    modules = gather(backend, lib)
    files: dict[str, str] = {"pyproject.toml": _pyproject_toml(pkg)}
    index = _notebook([_code_cell(f"#| hide\n# {pkg} — generated from Sidekick dialogs.")])
    files["nbs/index.ipynb"] = json.dumps(index, indent=1) + "\n"
    for module, entries in modules.items():
        files[f"nbs/{slug(module)}.ipynb"] = (
            json.dumps(module_notebook(module, entries), indent=1) + "\n")
    return files


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


def run_nbdev(dest: str) -> tuple[bool, str]:
    """Best-effort: run ``nbdev_export`` in ``dest`` to tangle the notebooks into
    the package. Returns ``(ok, detail)``; a missing nbdev is a soft failure —
    the notebooks are still valid and can be built later."""
    import shutil
    import subprocess
    # The console script is installed as `nbdev_export` or `nbdev-export`
    # depending on the packaging toolchain — try both.
    exe = shutil.which("nbdev_export") or shutil.which("nbdev-export")
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
    """Emit ``lib``'s nbdev project into ``dest`` and try to build it with nbdev.

    Returns ``{"pkg", "modules", "files", "nbdev_ok", "nbdev_detail"}``.
    """
    files = library_files(backend, lib, pkg_name)
    write_files(dest, files)
    nbdev_ok, detail = run_nbdev(dest)
    modules = [k[len("nbs/"):-len(".ipynb")] for k in files
               if k.startswith("nbs/") and k != "nbs/index.ipynb"]
    return {"pkg": slug(pkg_name or lib), "modules": sorted(modules),
            "files": sorted(files), "nbdev_ok": nbdev_ok, "nbdev_detail": detail}
