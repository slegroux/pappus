"""Export a dialog's messages to a Jupyter notebook (.ipynb) or Markdown document.

These are pure serializers: they take a list of message objects (duck-typed on
``id``/``msg_type``/``content``/``output``/``rich``/``model``) and build
nbformat/Markdown. They read no application state, so they live outside the UI
module (``sidekick.app``) and can be unit-tested in isolation.

This is distinct from :mod:`sidekick.export`, which *tangles* a dialog into a
pip-installable package; here we render the dialog as a document.
"""
from __future__ import annotations


def _lines(s: str) -> list:
    """nbformat wants source/text as a list of lines (keeping newlines)."""
    return (s or "").splitlines(keepends=True)


def _code_outputs(m) -> list:
    outs = []
    if m.output:
        outs.append({"output_type": "stream", "name": "stdout", "text": _lines(m.output)})
    for item in m.rich:
        t, data = item.get("type", ""), item.get("data", "")
        if t in ("image/png", "image/jpeg"):
            outs.append({"output_type": "display_data", "data": {t: data}, "metadata": {}})
        elif t == "text/html":
            outs.append({"output_type": "display_data",
                         "data": {"text/html": _lines(data)}, "metadata": {}})
    return outs


def _prompt_md(m) -> str:
    md = f"**Prompt:** {m.content}"
    if m.output:
        md += f"\n\n**{m.model or 'AI'}:**\n\n{m.output}"
    return md


def to_ipynb(msgs) -> dict:
    """Export cells to a Jupyter notebook: code→code cells (with outputs/plots),
    notes→markdown, prompts→markdown (question + AI answer)."""
    cells = []
    for m in msgs:
        cid = (m.id or "").lstrip("_") or "cell"      # nbformat cell id (no leading _)
        if m.msg_type == "code":
            cells.append({"id": cid, "cell_type": "code", "metadata": {}, "execution_count": None,
                          "source": _lines(m.content), "outputs": _code_outputs(m)})
        elif m.msg_type == "note":
            cells.append({"id": cid, "cell_type": "markdown", "metadata": {},
                          "source": _lines(m.content)})
        else:
            cells.append({"id": cid, "cell_type": "markdown", "metadata": {},
                          "source": _lines(_prompt_md(m))})
    return {"cells": cells, "nbformat": 4, "nbformat_minor": 5,
            "metadata": {"kernelspec": {"name": "python3", "display_name": "Python 3",
                                        "language": "python"},
                         "language_info": {"name": "python"}}}


def to_markdown(msgs) -> str:
    """Export cells to a single Markdown document."""
    out = []
    for m in msgs:
        if m.msg_type == "code":
            out.append(f"```python\n{m.content}\n```")
            if m.output:
                out.append(f"```\n{m.output}\n```")
            for item in m.rich:
                if item.get("type", "").startswith("image/"):
                    out.append(f"![output](data:{item['type']};base64,{item['data']})")
        elif m.msg_type == "note":
            out.append(m.content)
        else:
            out.append(_prompt_md(m))
    return "\n\n".join(out) + "\n"
