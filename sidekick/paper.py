"""PDF → markdown for the reading panel.

Uses `marker` (marker-pdf) when installed — it preserves structure, tables, and
equations as LaTeX, which the app then renders with mistune + KaTeX. marker is
heavy (torch + model downloads) and slow, so results are cached on disk. When
marker isn't available we fall back to lightweight pypdf text extraction.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path


def _cache_dir() -> Path:
    base = os.environ.get("SIDEKICK_DATA")
    base = Path(base).expanduser() if base else Path.home() / ".config" / "solveit-sidekick"
    return base / "papers"


def cache_path(pdf: Path) -> Path:
    """Cache file keyed by absolute path + mtime, so an edited PDF re-converts."""
    try:
        stamp = pdf.stat().st_mtime_ns
    except OSError:
        stamp = 0
    key = hashlib.sha1(f"{pdf.resolve()}:{stamp}".encode()).hexdigest()[:16]
    return _cache_dir() / f"{key}.md"


_converter = None  # cache marker's (expensive) model load across conversions


def _marker_convert(path: str) -> str | None:
    """High-quality markdown via marker, or None if marker isn't installed/usable."""
    global _converter
    try:
        from marker.converters.pdf import PdfConverter
        from marker.models import create_model_dict
        from marker.output import text_from_rendered
        if _converter is None:
            _converter = PdfConverter(artifact_dict=create_model_dict())
        md, _, _ = text_from_rendered(_converter(path))
        return md
    except Exception:  # noqa: BLE001 — not installed, or a conversion error → fall back
        return None


def _pypdf_convert(path: str) -> str:
    from pypdf import PdfReader
    pages = [(p.extract_text() or "") for p in PdfReader(path).pages]
    return "\n\n".join(pages).strip()


import re

_IMG_ONLY = re.compile(r"^!\[[^\]]*\]\([^)]*\)$")


def split_blocks(md: str) -> list[str]:
    """Split markdown into block-level chunks (paragraphs, headings, equations,
    tables, lists) — one per note cell. Blank lines separate blocks, but fenced
    code (```), kept intact. Figure-only image placeholders are dropped."""
    blocks, cur, in_fence = [], [], False
    for line in (md or "").splitlines():
        if line.strip().startswith("```"):
            in_fence = not in_fence
            cur.append(line)
            continue
        if in_fence:
            cur.append(line)
            continue
        if line.strip() == "":
            if cur:
                blocks.append("\n".join(cur).strip())
                cur = []
        else:
            cur.append(line)
    if cur:
        blocks.append("\n".join(cur).strip())
    return [b for b in blocks if b and not _IMG_ONLY.match(b)]


def _clean_md(md: str) -> str:
    """Strip marker's raw-HTML noise so it doesn't show literally under our
    (safe) escape=True rendering: page-anchor spans and sup/sub tags."""
    md = re.sub(r'<span id="page-\d+-\d+">\s*</span>', "", md or "")
    # dead cross-ref links → their text (handles escaped brackets in citations like [\[13\]])
    md = re.sub(r"\[((?:\\.|[^\]])*)\]\(#page-[\d-]+\)", r"\1", md)
    md = re.sub(r"</?su[pb]>", "", md)
    return md


def convert(path: str) -> tuple[str, str]:
    """Return (markdown, engine) for `path`, using the on-disk cache when present.
    engine is 'cache', 'marker', or 'pypdf'. The cache stores marker's raw output;
    cleaning is applied on every return so improvements reach cached papers too."""
    p = Path(path).expanduser()
    cp = cache_path(p)
    if cp.exists():
        try:
            return _clean_md(cp.read_text()), "cache"
        except OSError:
            pass
    md = _marker_convert(str(p))
    engine = "marker"
    if md is None:
        md = _pypdf_convert(str(p))
        engine = "pypdf"
    try:
        cp.parent.mkdir(parents=True, exist_ok=True)
        cp.write_text(md)
    except OSError:
        pass
    return _clean_md(md), engine
