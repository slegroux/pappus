"""Tangle a dialog into an installable Python package (nbdev-style).

This is the inverse of ``build_context`` in :mod:`pappus.client`: instead of
serializing a dialog's cells *into* the AI's notebook context, it selects the
cells a user has marked for export and emits them *as* a pip-installable package.

Marking follows nbdev's directive convention — a comment line at the top of a
code cell:

    #| default_exp core      set the default submodule for the whole dialog
    #| export                emit this cell into the default submodule
    #| export utils          emit this cell into src/<pkg>/utils.py

Directive lines are stripped from the emitted source. Code cells with no export
directive are treated as scratch and dropped, exactly like a muted cell never
reaches the AI. ``note`` cells are the literate narrative and flow into
``README.md`` (never injected into code, so tangling can't malform it).

Execution model: the dialog that *defines* the package keeps using its live
in-kernel definitions (no self-import). The generated package is for *cross-
dialog* reuse via an editable install (``uv pip install -e <pkg>``).
"""
from __future__ import annotations

import ast
import re
import sys

# import-name -> PyPI distribution name, for the cases where they differ.
_PYPI_NAMES = {
    "sklearn": "scikit-learn",
    "cv2": "opencv-python",
    "PIL": "pillow",
    "yaml": "pyyaml",
    "bs4": "beautifulsoup4",
    "skimage": "scikit-image",
}

_DIRECTIVE = re.compile(r"^\s*#\|\s*(\S+)\s*(.*?)\s*$")


def slug(name: str) -> str:
    """Normalize a free-form dialog name into an importable package identifier."""
    s = re.sub(r"[^0-9a-zA-Z]+", "_", (name or "").strip().lower()).strip("_")
    if not s or s[0].isdigit():
        s = "pkg_" + s
    return s


def _directives(code: str) -> tuple[list[tuple[str, str]], str]:
    """Split leading ``#|`` directive lines from the rest of a cell.

    Returns ``(directives, body)`` where directives is ``[(name, arg), ...]`` and
    body is the cell with every directive line removed.
    """
    found, kept = [], []
    for line in (code or "").splitlines():
        m = _DIRECTIVE.match(line)
        if m:
            found.append((m.group(1), m.group(2).strip()))
        else:
            kept.append(line)
    return found, "\n".join(kept).strip("\n")


def has_export(code: str) -> bool:
    """True if a code cell carries an ``#| export`` directive."""
    return any(name == "export" for name, _ in _directives(code)[0])


def toggle_export(code: str) -> str:
    """Add or remove the plain (argument-less) ``#| export`` directive.

    The UI's per-cell Export toggle calls this. It is **target-preserving**: only
    the plain ``#| export`` (no argument) the button owns is toggled — a
    hand-authored or library ``#| export <target>`` line, and ``#| default_exp``,
    are left untouched (``tangle`` emits a cell to *every* target, so those extra
    lines are real). Removing drops just the plain line; adding prepends one.
    """
    out, removed = [], False
    for ln in (code or "").splitlines():
        m = _DIRECTIVE.match(ln)
        if m and m.group(1) == "export" and not m.group(2).strip() and not removed:
            removed = True                      # drop only this plain export line
            continue
        out.append(ln)
    if removed:
        return "\n".join(out)
    return "#| export\n" + (code or "")


def set_export_target(code: str, target: str) -> str:
    """Point the cell's ``#| export`` directive at ``<target>``.

    Used by the per-cell library picker to tag a cell into ``<lib>:<module>``. It
    replaces the **first** ``#| export`` line (the one the picker shows) in place
    and **preserves any additional** ``#| export`` lines — so a cell hand-authored
    to export to two modules keeps both. ``#| default_exp`` and everything else are
    kept. If the cell has no ``#| export`` yet, one is prepended.
    """
    out, replaced = [], False
    for ln in (code or "").splitlines():
        m = _DIRECTIVE.match(ln)
        if m and m.group(1) == "export" and not replaced:
            out.append(f"#| export {target}")   # replace the first export in place
            replaced = True
        else:
            out.append(ln)
    if not replaced:
        out.insert(0, f"#| export {target}")
    return "\n".join(out)


def _public_names(module_src: str) -> list[str]:
    """Top-level def/class/assignment names not starting with ``_`` (in source order)."""
    try:
        tree = ast.parse(module_src)
    except SyntaxError:
        return []
    names: list[str] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if not node.name.startswith("_"):
                names.append(node.name)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and not t.id.startswith("_"):
                    names.append(t.id)
    # de-dup, keep first occurrence
    seen: set[str] = set()
    return [n for n in names if not (n in seen or seen.add(n))]


def _imports(module_src: str) -> set[str]:
    """Top-level imported root module names (``import a.b`` / ``from a import x`` -> ``a``)."""
    roots: set[str] = set()
    try:
        tree = ast.parse(module_src)
    except SyntaxError:
        return roots
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for n in node.names:
                roots.add(n.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:   # skip relative imports
                roots.add(node.module.split(".")[0])
    return roots


def _third_party(roots: set[str], pkg_name: str) -> list[str]:
    """Drop stdlib and the package's own modules, mapping the rest to PyPI names."""
    stdlib = getattr(sys, "stdlib_module_names", frozenset())
    deps = []
    for r in sorted(roots):
        if r in stdlib or r == pkg_name or not r:
            continue
        deps.append(_PYPI_NAMES.get(r, r))
    return deps


def tangle(msgs: list, pkg_name: str) -> tuple[dict[str, str], list[str]]:
    """Group exported code cells into ``{module_name: source}`` and collect notes.

    Returns ``(modules, notes)``. The default module name comes from the first
    ``#| default_exp`` directive, falling back to ``"core"``.
    """
    default_mod = "core"
    for m in msgs:
        if getattr(m, "msg_type", "") != "code":
            continue
        for name, arg in _directives(m.content)[0]:
            if name == "default_exp" and arg:
                default_mod = slug(arg)
                break
        else:
            continue
        break

    modules: dict[str, list[str]] = {}
    notes: list[str] = []
    for m in msgs:
        mtype = getattr(m, "msg_type", "")
        if mtype == "note":
            body = (m.content or "").strip()
            if body:
                notes.append(body)
            continue
        if mtype != "code":
            continue
        directives, body = _directives(m.content)
        targets = [arg or default_mod for name, arg in directives if name == "export"]
        if not targets or not body.strip():
            continue
        for t in targets:
            modules.setdefault(slug(t), []).append(body)

    return {name: "\n\n\n".join(cells) + "\n" for name, cells in modules.items()}, notes


def _header(pkg_name: str, source_url: str | None, dialog_name: str | None) -> str:
    # Provenance as comment lines, not a docstring: dialog_name / source_url are
    # user-controlled, and a `"""` (or trailing `\`) in them would terminate a
    # docstring early, producing an invalid / injected module. A `#` comment is
    # injection-proof for single-line content (newlines flattened to spaces).
    lines = [f"Part of the `{pkg_name}` package, generated from a SolveIt dialog."]
    if dialog_name:
        lines.append(f"Source dialog: {dialog_name}")
    if source_url:
        lines.append(f"Derived from: {source_url}")
    return "".join(f"# {ln.replace(chr(10), ' ')}\n" for ln in lines) + "\n"


def dialog_to_package(msgs: list, pkg_name: str, source_url: str | None = None,
                      dialog_name: str | None = None, version: str = "0.0.1") -> dict[str, str]:
    """Tangle a dialog into ``{relative_path: file_content}`` for a pip-installable package.

    Layout (src/ so the package is importable only once installed, not from cwd)::

        pyproject.toml
        README.md
        src/<pkg>/__init__.py     # re-exports public symbols from each module
        src/<pkg>/<module>.py     # tangled code cells, in dialog order
    """
    pkg = slug(pkg_name)
    modules, notes = tangle(msgs, pkg)
    files: dict[str, str] = {}

    all_imports: set[str] = set()
    init_lines: list[str] = []
    all_names: list[str] = []
    taken: set[str] = set()                  # names already re-exported by an earlier module
    for mod, src in modules.items():
        body = _header(pkg, source_url, dialog_name) + src
        files[f"src/{pkg}/{mod}.py"] = body
        all_imports |= _imports(src)
        # Re-export only names not already taken: two modules defining `f` would
        # otherwise shadow each other at the package top level and duplicate `f`
        # in __all__. First definition wins; the module itself is still importable.
        fresh = [n for n in _public_names(src) if n not in taken]
        taken.update(fresh)
        all_names += fresh
        if fresh:
            init_lines.append(f"from .{mod} import {', '.join(fresh)}")
        else:
            init_lines.append(f"from . import {mod}  # noqa: F401")

    init = f'"""{pkg} — generated from a SolveIt dialog."""\n'
    init += f'__version__ = "{version}"\n\n'
    init += "\n".join(init_lines) + ("\n" if init_lines else "")
    if all_names:
        listed = ", ".join(f'"{n}"' for n in all_names)
        init += f"\n__all__ = [{listed}]\n"
    files[f"src/{pkg}/__init__.py"] = init

    deps = _third_party(all_imports, pkg)
    if deps:
        dep_lines = "".join(f'\n    "{d}",' for d in deps)
        dependencies = f"dependencies = [{dep_lines}\n]\n"
    else:
        dependencies = "dependencies = []\n"
    files["pyproject.toml"] = (
        "[build-system]\n"
        'requires = ["hatchling"]\n'
        'build-backend = "hatchling.build"\n\n'
        "[project]\n"
        f'name = "{pkg.replace("_", "-")}"\n'
        f'version = "{version}"\n'
        'description = "Generated from a SolveIt dialog."\n'
        'requires-python = ">=3.10"\n'
        + dependencies
    )

    readme = [f"# {pkg}", ""]
    if source_url:
        readme.append(f"Implementation derived from <{source_url}>.\n")
    readme.append("Generated from a SolveIt dialog"
                  + (f" (`{dialog_name}`)" if dialog_name else "") + ".\n")
    readme.append("```bash\nuv pip install -e .\n```\n")
    if notes:
        readme.append("---\n")
        readme += notes
    files["README.md"] = "\n".join(readme).rstrip() + "\n"

    return files


def package_zip(files: dict[str, str], root: str) -> bytes:
    """Zip ``{relpath: content}`` under a top-level ``root/`` directory."""
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for rel, content in sorted(files.items()):
            zf.writestr(f"{root}/{rel}", content)
    return buf.getvalue()
