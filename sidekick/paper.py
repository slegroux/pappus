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


def split_sections(md: str, blocks: list[str] | None = None) -> list[str]:
    """Group blocks into sections: each heading plus the blocks under it (until the
    next heading) become one cell. Content before the first heading is its own cell.
    Pass `blocks` (from a prior split_blocks) to avoid re-splitting the same md."""
    sections, cur = [], []
    for b in (split_blocks(md) if blocks is None else blocks):
        if b.lstrip().startswith("#") and cur:
            sections.append("\n\n".join(cur))
            cur = [b]
        else:
            cur.append(b)
    if cur:
        sections.append("\n\n".join(cur))
    return [s for s in sections if s.strip()]


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
    # Only cache a non-empty conversion. An empty result (image-only/scanned PDF,
    # or a failed extraction) must not be stored — the cache key is path+mtime and
    # uploads are content-deduped, so a cached "" would make the paper blank
    # forever with no way to retry short of deleting the cache file.
    if md and md.strip():
        try:
            cp.parent.mkdir(parents=True, exist_ok=True)
            cp.write_text(md)
        except OSError:
            pass
    return _clean_md(md), engine


# ---- web pages / blogs → markdown -------------------------------------------
def _url_cache_path(url: str) -> Path:
    key = hashlib.sha1(url.encode()).hexdigest()[:16]
    return _cache_dir() / f"url-{key}.md"


def _trafilatura_extract(url: str) -> str | None:
    """The main article as markdown via trafilatura, or None if unavailable."""
    try:
        import trafilatura
        html = trafilatura.fetch_url(url)
        if not html:
            return None
        return trafilatura.extract(html, output_format="markdown", include_links=True,
                                   include_formatting=True, include_tables=True)
    except Exception:  # noqa: BLE001 — not installed, or an extraction error → fall back
        return None


def _html_to_md_fallback(url: str) -> str:
    """No-extra fallback: fetch, drop page chrome, markdownify the main content."""
    import urllib.request
    from bs4 import BeautifulSoup
    from markdownify import markdownify
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (solveit-sidekick)"})
    with urllib.request.urlopen(req, timeout=20) as r:
        html = r.read().decode("utf-8", "replace")
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "header", "footer", "aside", "form", "noscript"]):
        tag.decompose()
    main = soup.find("article") or soup.find("main") or soup.body or soup
    return markdownify(str(main), heading_style="ATX").strip()


def convert_url(url: str) -> tuple[str, str]:
    """Return (markdown, engine) for a web page — article extraction via trafilatura
    (engine 'trafilatura'), falling back to a bs4 + markdownify heuristic ('bs4').
    Cached on disk by URL; empty results aren't cached so a transient fetch can retry."""
    cp = _url_cache_path(url)
    if cp.exists():
        try:
            return _clean_md(cp.read_text()), "cache"
        except OSError:
            pass
    md, engine = _trafilatura_extract(url), "trafilatura"
    if not (md and md.strip()):
        md, engine = _html_to_md_fallback(url), "bs4"
    md = md or ""
    if md.strip():
        try:
            cp.parent.mkdir(parents=True, exist_ok=True)
            cp.write_text(md)
        except OSError:
            pass
    return _clean_md(md), engine
