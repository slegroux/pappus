"""S1: the large front-end JS/CSS blocks are extracted to sidekick/static and
served from /static — a behaviour-preserving refactor.

These tests lock byte-identity: the module-level constant, the file on disk, and
the bytes served over HTTP must all be identical. Like the sibling suites, they
run with no SolveIt server and no network (TestClient's default Host is
"testserver", which the localhost guard allows).
"""
import sys
from pathlib import Path

from starlette.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import sidekick.app as app

STATIC_DIR = ROOT / "sidekick" / "static"

# (constant name, static url path). Only the pure-static, non-interpolated blocks
# that are NOT re-delivered via htmx partial swaps were extracted.
EXTRACTED = [
    ("CSS", "/static/css/app.css", "text/css"),
    ("SIDEBAR_JS", "/static/js/sidebar.js", "javascript"),
    ("COMMENT_JS", "/static/js/comment.js", "javascript"),
    ("COMPLETE_JS", "/static/js/complete.js", "javascript"),
    ("STREAM_SEL_JS", "/static/js/stream_sel.js", "javascript"),
    ("COMPOSER_JS", "/static/js/composer.js", "javascript"),
    ("MSGLINK_JS", "/static/js/msglink.js", "javascript"),
    ("TOC_JS", "/static/js/toc.js", "javascript"),
]


def _client():
    app.STATE["dialog"] = "guard/test"
    app.STATE["backend"].messages("guard/test")
    return TestClient(app.app)


def test_constant_equals_file_on_disk():
    """Each extracted constant is loaded from its file — byte-identical."""
    for name, url, _ct in EXTRACTED:
        rel = url[len("/static/"):]
        disk = (STATIC_DIR / rel).read_text(encoding="utf-8")
        assert getattr(app, name) == disk, f"{name} != {rel}"


def test_static_route_serves_byte_identical_content():
    """GET /static/... returns 200 with a body byte-identical to the constant."""
    client = _client()
    for name, url, ct in EXTRACTED:
        r = client.get(url)
        assert r.status_code == 200, f"{url} -> {r.status_code}"
        assert ct in r.headers.get("content-type", ""), (
            f"{url} content-type {r.headers.get('content-type')!r} lacks {ct}")
        assert r.text == getattr(app, name), f"served {url} != {name}"


def test_index_still_references_static_assets():
    """The rendered page links the external assets in place of inline blocks."""
    client = _client()
    html = client.get("/").text
    assert html.startswith("<!doctype html>") or "<html" in html
    assert '<link rel="stylesheet" href="/static/css/app.css">' in html
    for _name, url, _ct in EXTRACTED:
        if url.endswith(".js"):
            assert f'src="{url}"' in html, f"missing script ref {url}"


def test_static_route_path_traversal_guarded():
    """The /static route rejects path-traversal like the /vendor route."""
    client = _client()
    r = client.get("/static/../app.py")
    assert r.status_code == 404


def test_interpolated_and_swapped_blocks_kept_inline():
    """Blocks that are interpolated per-render or re-delivered via htmx swaps are
    intentionally NOT extracted; they remain full JS string literals."""
    # Interpolated at render time via .replace("__MID__", ...).
    assert "__MID__" in app._FOCUS_JS
    assert "__MID__" in app._CODE_EDITOR_JS
    # Re-delivered inside htmx #stream / #paperPanel partial swaps.
    assert "/cell/insert" in app.STREAM_JS and len(app.STREAM_JS) > 100
    assert len(app.PAPER_JS) > 100
    # None of these leaked out to a /static file.
    client = _client()
    assert client.get("/static/js/stream.js").status_code == 404
    assert client.get("/static/js/paper.js").status_code == 404
