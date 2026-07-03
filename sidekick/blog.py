"""Project a dialog into a Quarto/nbdev blog post — the *publishing* view.

This is the third projection over the same cells, alongside the two in
`docs/design/graph-vs-tree.md`:

    graph  — how dialogs connect (message links + build_context)   → learning
    tree   — how a library is laid out (#| export → nbdev tangle)   → packaging
    blog   — what a reader sees (this file)                         → communicating

The library path (`nbdev_export.py`) *recombines* cells across dialogs into
modules and deliberately drops their outputs (`outputs: []`) — a `.py` file has
no use for a plot. The blog path is its mirror image: **one dialog is one post,
kept in order, with prose, code, and their outputs intact.** A dialog is already
a literate document (notes + code + results); publishing it is mostly deciding
what to *keep*, and the defaults keep everything.

Emit is a plain nbformat v4 notebook with a Quarto YAML front-matter raw cell.
Quarto (which nbdev wraps) renders it to HTML — no re-execution, because the
kernel's outputs travel with the cells. The dialog stays the source of truth;
the notebook is generated and never hand-edited.
"""
from __future__ import annotations

import json

from .export import _directives, slug


# ---- cell → nbformat mapping ------------------------------------------------
def _lines(text: str) -> list[str]:
    """nbformat stores multiline text as a list of lines, newline-terminated
    except the last. An empty string becomes an empty list."""
    return (text or "").splitlines(keepends=True)


def _raw_cell(source: str) -> dict:
    """A raw cell — Quarto reads the post's YAML front-matter from the first one."""
    return {"cell_type": "raw", "metadata": {}, "source": _lines(source)}


def _md_cell(source: str) -> dict:
    return {"cell_type": "markdown", "metadata": {}, "source": _lines(source)}


def _outputs(output: str, rich: list, execution_count: int) -> list[dict]:
    """Map a Sidekick cell's results to nbformat output nodes.

    `output` (stdout/stderr + last-expr repr) becomes a stream; each `rich` item
    (`{"type": mime, "data": ...}` from the kernel — a base64 image, or raw
    html/svg) becomes a display_data node. Images stay base64 strings; text-ish
    mimes stay strings — both shapes nbformat accepts under nbformat_minor 5.
    """
    nodes: list[dict] = []
    if (output or "").strip():
        nodes.append({"output_type": "stream", "name": "stdout", "text": _lines(output)})
    for r in rich or []:
        mime, data = r.get("type"), r.get("data")
        if not mime or data is None:
            continue
        nodes.append({"output_type": "display_data",
                      "data": {mime: data}, "metadata": {}})
    return nodes


def _code_cell(source: str, output: str, rich: list, execution_count: int) -> dict:
    return {"cell_type": "code", "metadata": {},
            "execution_count": execution_count if (output or rich) else None,
            "outputs": _outputs(output, rich, execution_count),
            "source": _lines(source)}


def _prompt_cell(question: str, answer: str) -> dict:
    """A prompt (AI Q&A) has no code to render; show it as prose — the question
    as a blockquote, the answer (already markdown from the model) beneath it."""
    q = "\n".join(f"> {ln}" for ln in (question or "").splitlines())
    body = q + (f"\n\n{answer}" if (answer or "").strip() else "")
    return _md_cell(body)


# ---- front matter + notebook assembly ---------------------------------------
def _frontmatter(title: str, date: str | None, author: str | None,
                 description: str | None, categories: list[str] | None) -> str:
    """Quarto post YAML. Only `title` is required; the rest are omitted when
    unset so the raw cell stays minimal. Values are JSON-encoded, which is valid
    YAML and safely quotes colons/quotes in a free-form dialog name."""
    lines = ["---", f"title: {json.dumps(title or 'Untitled')}"]
    if author:
        lines.append(f"author: {json.dumps(author)}")
    if date:
        lines.append(f"date: {json.dumps(date)}")
    if description:
        lines.append(f"description: {json.dumps(description)}")
    if categories:
        lines.append("categories: [" + ", ".join(json.dumps(c) for c in categories) + "]")
    lines.append("---")
    return "\n".join(lines)


def dialog_to_post(msgs: list, title: str, date: str | None = None,
                   author: str | None = None, description: str | None = None,
                   categories: list[str] | None = None) -> dict:
    """Project a dialog's cells into one Quarto post notebook (nbformat v4 dict).

    note   → markdown cell (the narrative)
    code   → code cell, Sidekick `#|` directives stripped (library/context noise,
             not for readers), with its kernel output + plots preserved
    prompt → markdown cell (the AI Q&A rendered as prose)

    Cells with no content and no output are dropped, the way an empty cell never
    reaches the AI. `date`/`author`/`description`/`categories` are optional
    front-matter (a blank value is simply omitted).
    """
    cells: list[dict] = [_raw_cell(_frontmatter(title, date, author, description, categories))]
    n = 0
    for m in msgs:
        mtype = getattr(m, "msg_type", "")
        content = getattr(m, "content", "") or ""
        output = getattr(m, "output", "") or ""
        rich = getattr(m, "rich", None) or []
        if mtype == "note":
            if content.strip():
                cells.append(_md_cell(content))
        elif mtype == "prompt":
            if content.strip() or output.strip():
                cells.append(_prompt_cell(content, output))
        elif mtype == "code":
            body = _directives(content)[1]          # strip #| directive lines
            if not (body.strip() or output.strip() or rich):
                continue
            n += 1
            cells.append(_code_cell(body, output, rich, n))
    return {"cells": cells,
            "metadata": {"kernelspec": {"display_name": "Python 3",
                                        "language": "python", "name": "python3"}},
            "nbformat": 4, "nbformat_minor": 5}


# ---- site scaffolding -------------------------------------------------------
def _quarto_yml(title: str) -> str:
    return ("project:\n"
            "  type: website\n\n"
            "website:\n"
            f"  title: {json.dumps(title)}\n\n"
            "format:\n"
            "  html:\n"
            "    theme: cosmo\n"
            "    toc: true\n")


def _index_qmd(title: str) -> str:
    """The blog landing page: a Quarto listing that indexes everything in posts/,
    newest first."""
    return ("---\n"
            f"title: {json.dumps(title)}\n"
            "listing:\n"
            "  contents: posts\n"
            "  sort: \"date desc\"\n"
            "  type: default\n"
            "---\n")


def blog_files(posts: dict[str, dict], title: str) -> dict[str, str]:
    """Assemble ``{relpath: content}`` for a Quarto blog: one `posts/<slug>.ipynb`
    per (slug → notebook) entry, plus `_quarto.yml` and the `index.qmd` listing.
    Pure (no disk), so it's easy to test."""
    files: dict[str, str] = {"_quarto.yml": _quarto_yml(title),
                             "index.qmd": _index_qmd(title)}
    for post_slug, nb in posts.items():
        files[f"posts/{post_slug}.ipynb"] = json.dumps(nb, indent=1) + "\n"
    return files


def zip_dir(root: str, arcprefix: str = "") -> bytes:
    """Zip a directory tree on disk into bytes — for handing a built blog (the
    rendered ``_site/``, or the source project) to the browser as a download.
    Every entry is nested under ``arcprefix/`` so unzipping makes one folder."""
    import io
    import os
    import zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for dirpath, _, files in os.walk(root):
            for f in sorted(files):
                full = os.path.join(dirpath, f)
                rel = os.path.relpath(full, root)
                zf.write(full, os.path.join(arcprefix, rel) if arcprefix else rel)
    return buf.getvalue()


# ---- disk + render ----------------------------------------------------------
def render_site(dest: str) -> tuple[bool, str]:
    """Best-effort ``quarto render <dest>`` → the static site under ``_site/``.
    A missing quarto is a soft failure: the post notebooks are valid and can be
    rendered later. Mirrors ``nbdev_export.run_nbdev``."""
    import shutil
    import subprocess
    exe = shutil.which("quarto")
    if not exe:
        return False, "quarto not installed — posts emitted; run `quarto render` to build the site."
    try:
        r = subprocess.run([exe, "render", dest], capture_output=True, text=True, timeout=300)
    except Exception as e:  # noqa: BLE001
        return False, f"quarto render failed to run: {e}"
    if r.returncode != 0:
        return False, (r.stderr or r.stdout or "").strip()[:400] or "quarto render nonzero exit"
    return True, "quarto rendered the site to _site/."


def build_blog(backend, dialogs: list[str], dest: str, title: str = "Sidekick",
               date: str | None = None, author: str | None = None) -> dict:
    """Build a blog at ``dest`` from the named ``dialogs`` (one post each), then
    render it with quarto if installed.

    ``author`` bylines every post; unset, it defaults to the git ``user.name``
    (the same identity the library path uses), and is omitted entirely if git has
    none configured.

    Same shape as ``nbdev_export.build_library``'s return:
    ``{"posts", "files", "render_ok", "render_detail"}``. Post slugs are made
    unique so two dialogs named ``foo/a`` and ``bar/a`` don't collide.
    """
    from .nbdev_export import _git_identity, write_files

    if author is None:
        author = _git_identity()[0] or None   # git user.name, or omit the byline

    posts: dict[str, dict] = {}
    used: set[str] = set()
    for name in dialogs:
        s = slug(name) or "post"
        while s in used:                    # de-dup collides
            s += "_"
        used.add(s)
        posts[s] = dialog_to_post(backend.messages(name), title=name, date=date,
                                  author=author)

    files = blog_files(posts, title)
    write_files(dest, files)
    render_ok, detail = render_site(dest)
    return {"posts": sorted(posts), "files": sorted(files),
            "render_ok": render_ok, "render_detail": detail}
