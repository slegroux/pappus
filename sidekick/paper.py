"""PDF → markdown for the reading panel.

Requires `marker` (marker-pdf, the `paper` extra) — it preserves structure,
tables, figures, and equations as LaTeX, which the app then renders with
mistune + KaTeX. marker is heavy (torch + model downloads) and slow, so results
are cached on disk; without it, opening a PDF reports how to install it (no
degraded text-only fallback — a paper without structure or figures isn't worth
importing). Web pages don't need it.
"""
from __future__ import annotations

import hashlib
import http.client
import ipaddress
import os
import re
import socket
from pathlib import Path
from urllib.parse import urlparse


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


def _marker_convert(path: str) -> tuple[str, dict]:
    """(markdown, images) via marker. `images` maps the filenames referenced by
    the markdown (`![](_page_2_Figure_1.jpeg)`) to PIL images — marker extracts
    figures, diagrams, and pictures by default. Raises RuntimeError with an
    install hint when marker (the `paper` extra) isn't installed; conversion
    errors propagate to the caller (the panel shows them)."""
    global _converter
    try:
        from marker.converters.pdf import PdfConverter
        from marker.models import create_model_dict
        from marker.output import text_from_rendered
    except ImportError as e:
        raise RuntimeError(
            "PDF conversion requires marker — install the `paper` extra: "
            'uv sync --extra paper  (or: uv pip install "solveit-sidekick[paper]")'
        ) from e
    if _converter is None:
        _converter = PdfConverter(artifact_dict=create_model_dict())
    md, _, images = text_from_rendered(_converter(path))
    return md, (images or {})


def assets_dir(key: str) -> Path:
    """Where a converted paper's extracted figures live, next to its .md cache."""
    return _cache_dir() / "assets" / key


def _store_assets(md: str, images: dict, key: str) -> str:
    """Save marker's extracted images under assets/<key>/ and rewrite their
    markdown refs to the app's /paper/asset/<key>/<name> route, so figures render
    in the reading panel and in imported note cells (and sync with the cache).
    An image that fails to save keeps its original (dead) ref, which the block
    splitter then drops — same behavior as before images were kept."""
    adir = assets_dir(key)
    for name, img in images.items():
        safe = os.path.basename(name)
        if safe != name or not safe:
            continue                             # refuse path-shaped names
        try:
            adir.mkdir(parents=True, exist_ok=True)
            img.save(adir / safe)                # format inferred from the extension
        except Exception:  # noqa: BLE001 — a figure we can't save must not kill the text
            continue
        md = md.replace(f"]({name})", f"](/paper/asset/{key}/{safe})")
    return md


_IMG_ONLY = re.compile(r"^!\[[^\]]*\]\(([^)]*)\)$")


def _dead_image_block(block: str) -> bool:
    """A figure-only block whose ref can't render: a bare relative path, as marker
    emits when its images weren't saved (papers cached before figure extraction).
    Served assets (/paper/asset/…) and remote (http…) images are kept."""
    m = _IMG_ONLY.match(block)
    return bool(m) and not m.group(1).startswith(("/paper/asset/", "http://", "https://"))


def split_blocks(md: str) -> list[str]:
    """Split markdown into block-level chunks (paragraphs, headings, equations,
    tables, lists, figures) — one per note cell. Blank lines separate blocks, but
    fenced code (```) is kept intact. Image blocks with a dead ref are dropped."""
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
    return [b for b in blocks if b and not _dead_image_block(b)]


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
    engine is 'cache' or 'marker'. Requires marker: a missing install raises with
    the install hint (see _marker_convert) — but a cached paper still opens
    without it, since the cache is checked first. The cache stores the converted
    output; cleaning is applied on every return so improvements reach cached
    papers too."""
    p = Path(path).expanduser()
    cp = cache_path(p)
    if cp.exists():
        try:
            return _clean_md(cp.read_text()), "cache"
        except OSError:
            pass
    md, images = _marker_convert(str(p))
    if images:
        md = _store_assets(md, images, cp.stem)       # same key as the .md cache file
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
    return _clean_md(md), "marker"


# ---- URL sources: a web page/blog OR a PDF paper (e.g. arXiv) ----------------
def _url_cache_path(url: str) -> Path:
    key = hashlib.sha1(url.encode()).hexdigest()[:16]
    return _cache_dir() / f"url-{key}.md"


def _arxiv_pdf(url: str) -> str | None:
    """Rewrite an arXiv abstract/pdf URL to its PDF URL (so we fetch the paper, not
    the abstract page). e.g. arxiv.org/abs/2305.18247 → arxiv.org/pdf/2305.18247."""
    m = re.match(r"https?://arxiv\.org/(?:abs|pdf)/([\w.\-]+?)(v\d+)?(?:\.pdf)?/?$", url, re.I)
    return f"https://arxiv.org/pdf/{m.group(1)}{m.group(2) or ''}" if m else None


class _BlockedURLError(ValueError):
    """A URL was refused by the SSRF guard (bad scheme or a private/loopback host)."""


def _ip_is_blocked(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return True                          # unparseable address → refuse
    return (addr.is_private or addr.is_loopback or addr.is_link_local
            or addr.is_reserved or addr.is_multicast or addr.is_unspecified)


def _check_url_allowed(url: str) -> None:
    """Raise _BlockedURLError unless `url` is a plain http(s) URL whose host resolves
    only to public addresses. Guards the paper importer against SSRF: `file://` and
    other schemes, loopback (127/8, ::1), private ranges (10/8, 172.16/12,
    192.168/16, fc00::/7), and cloud metadata (link-local 169.254/16, fe80::/10) are
    refused. Set SIDEKICK_ALLOW_PRIVATE_URLS=1 to permit private/loopback hosts
    (legitimate intranet papers)."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise _BlockedURLError(f"refusing non-http(s) URL scheme: {parsed.scheme or '(none)'}")
    if os.environ.get("SIDEKICK_ALLOW_PRIVATE_URLS") == "1":
        return
    host = parsed.hostname
    if not host:
        raise _BlockedURLError("URL has no host")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        raise _BlockedURLError(f"cannot resolve host {host!r}: {e}") from e
    for info in infos:
        ip = info[4][0]
        if _ip_is_blocked(ip):
            raise _BlockedURLError(f"refusing private/loopback host {host!r} → {ip}")


def _vet_and_pick_ip(host: str, port: int) -> str:
    """Resolve `host` and return one address to actually connect to. Unless
    SIDEKICK_ALLOW_PRIVATE_URLS=1, the returned IP is guaranteed public — private/
    loopback/link-local results are skipped, and if none qualify it raises. This
    runs *at socket-connect time* (see the guarded connections below), so the IP we
    vet is the IP we connect to — closing the DNS-rebinding TOCTOU where a name
    passes _check_url_allowed and then re-resolves to 127.0.0.1/169.254.169.254."""
    infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    if os.environ.get("SIDEKICK_ALLOW_PRIVATE_URLS") == "1":
        return infos[0][4][0]
    for info in infos:
        ip = info[4][0]
        if not _ip_is_blocked(ip):
            return ip
    raise _BlockedURLError(f"refusing private/loopback host {host!r}")


class _PinnedHTTPConnection(http.client.HTTPConnection):
    """HTTPConnection that connects to a freshly-vetted public IP for self.host."""
    def connect(self):
        ip = _vet_and_pick_ip(self.host, self.port)
        self.sock = socket.create_connection((ip, self.port), self.timeout,
                                              self.source_address)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def connect(self):
        ip = _vet_and_pick_ip(self.host, self.port)
        sock = socket.create_connection((ip, self.port), self.timeout,
                                        self.source_address)
        # server_hostname=self.host keeps SNI + cert verification against the real
        # hostname even though the socket is dialed by IP.
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


def _fetch(url: str) -> tuple[bytes, str]:
    """Fetch a URL → (bytes, content_type). Only http(s) to public hosts (SSRF
    guard, _check_url_allowed); redirects are followed but re-checked at each hop,
    and every socket connects to a vetted IP (_Pinned*Connection) so a rebind can't
    slip a private address in between the check and the connect."""
    import urllib.request

    class _GuardedRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            _check_url_allowed(newurl)       # a redirect must not escape the guard
            return super().redirect_request(req, fp, code, msg, headers, newurl)

    class _HTTPHandler(urllib.request.HTTPHandler):
        def http_open(self, req):
            return self.do_open(_PinnedHTTPConnection, req)

    class _HTTPSHandler(urllib.request.HTTPSHandler):
        def https_open(self, req):
            return self.do_open(_PinnedHTTPSConnection, req)

    _check_url_allowed(url)
    opener = urllib.request.build_opener(_GuardedRedirect(), _HTTPHandler(), _HTTPSHandler())
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (solveit-sidekick)"})
    with opener.open(req, timeout=25) as r:
        return r.read(), (r.headers.get("Content-Type") or "")


def _save_pdf_bytes(data: bytes) -> str:
    """Save downloaded PDF bytes under the uploads cache (content-hashed, so the
    same paper reuses its conversion)."""
    updir = _cache_dir() / "uploads"
    updir.mkdir(parents=True, exist_ok=True)
    dst = updir / (hashlib.sha1(data).hexdigest()[:16] + ".pdf")
    if not dst.exists():
        dst.write_bytes(data)
    return str(dst)


def _extract_article(html: str, url: str) -> tuple[str, str]:
    """Main article of an HTML page as markdown: trafilatura, else a bs4 heuristic."""
    try:
        import trafilatura
        md = trafilatura.extract(html, url=url, output_format="markdown",
                                 include_links=True, include_formatting=True,
                                 include_tables=True, include_images=True)
        if md and md.strip():
            return md, "trafilatura"
    except Exception:  # noqa: BLE001 — not installed / extraction error → bs4 fallback
        pass
    from bs4 import BeautifulSoup
    from markdownify import markdownify
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "header", "footer", "aside", "form", "noscript"]):
        tag.decompose()
    main = soup.find("article") or soup.find("main") or soup.body or soup
    return markdownify(str(main), heading_style="ATX").strip(), "bs4"


def normalize_url(url: str) -> str:
    """Add a scheme to a bare URL so users can paste `arxiv.org/abs/1706.03762`
    (or `example.com/post`) instead of the full `https://…`. Anything already
    carrying a `scheme://` prefix is left untouched (an ftp:// URL still reaches
    the SSRF guard, which refuses non-http(s))."""
    url = (url or "").strip()
    if url and not re.match(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://", url):
        url = "https://" + url.lstrip("/")
    return url


def convert_url(url: str) -> tuple[str, str]:
    """Fetch a URL and convert to markdown, auto-routing by what it actually is:
    a PDF (arXiv, a `.pdf` link, or `application/pdf`) goes through the PDF pipeline
    (marker — equations/tables/figures preserved); anything else is article-extracted
    (trafilatura/bs4). Returns (markdown, engine); cached on disk by URL."""
    url = normalize_url(url)
    cp = _url_cache_path(url)
    if cp.exists():
        try:
            return _clean_md(cp.read_text()), "cache"
        except OSError:
            pass
    target = _arxiv_pdf(url) or url          # arXiv abstract → its PDF
    data, ctype = _fetch(target)
    if data[:5] == b"%PDF-" or "application/pdf" in ctype.lower():
        md, engine = convert(_save_pdf_bytes(data))     # PDF pipeline (marker)
    else:
        md, engine = _extract_article(data.decode("utf-8", "replace"), target)
    md = md or ""
    if md.strip():
        try:
            cp.parent.mkdir(parents=True, exist_ok=True)
            cp.write_text(md)
        except OSError:
            pass
    return _clean_md(md), engine
