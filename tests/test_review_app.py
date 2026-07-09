"""Tests for the security/robustness review changes (W3, W4, S8, SEC-*).

Mirrors tests/test_sidekick.py: no SolveIt server, no network — the Starlette
TestClient's default Host is "testserver", which the localhost guard allows.
"""
import importlib
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from starlette.testclient import TestClient


def _client():
    import sidekick.app as app
    app.STATE["dialog"] = "guard/test"
    app.STATE["backend"].messages("guard/test")   # ensure the dialog exists
    return app, TestClient(app.app)


# ---- W3: Host / Origin defense ---------------------------------------------
def test_foreign_host_rejected():
    _app, client = _client()
    # A foreign Host (a DNS-rebinding / cross-origin drive-by) is refused …
    r = client.post("/send", data={"content": "", "msg_type": "note"},
                    headers={"Host": "evil.com"})
    assert r.status_code == 403
    # … while the default (testserver) Host is allowed through.
    ok = client.post("/send", data={"content": "", "msg_type": "note"})
    assert ok.status_code != 403


def test_cross_origin_post_rejected():
    _app, client = _client()
    r = client.post("/send", data={"content": "", "msg_type": "note"},
                    headers={"Origin": "http://evil.com"})
    assert r.status_code == 403
    # A POST with no Origin header (same-origin / non-browser) still passes.
    ok = client.post("/send", data={"content": "", "msg_type": "note"})
    assert ok.status_code != 403


def test_cross_site_fetch_rejected():
    # Sec-Fetch-Site catches the browser cross-site cases the Origin check misses:
    # an absent-Origin POST and state-changing GET routes.
    _app, client = _client()
    r = client.post("/send", data={"content": "", "msg_type": "note"},
                    headers={"Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 403
    g = client.get("/new", headers={"Sec-Fetch-Site": "cross-site"})
    assert g.status_code == 403
    # Same-origin browser requests carry same-origin/none and pass through.
    ok = client.post("/send", data={"content": "", "msg_type": "note"},
                     headers={"Sec-Fetch-Site": "same-origin"})
    assert ok.status_code != 403


# ---- W4: audio rich output --------------------------------------------------
def test_audio_rich_view():
    import sidekick.app as app
    from fasthtml.common import to_xml
    html = to_xml(app._rich_view({"type": "audio/wav", "data": "AAA="}))
    assert "<audio" in html
    assert "data:audio/wav;base64,AAA=" in html


# ---- S8: import performs no network probe -----------------------------------
def test_import_no_network(monkeypatch):
    import sidekick.doctor as doctor
    import sidekick.app
    calls = []

    def spy(t):
        calls.append(t)
        return (False, "AUTH", "patched — no real probe")

    monkeypatch.setattr(doctor, "check_test_route", spy)
    importlib.reload(sidekick.app)                 # re-run module top level
    assert calls == [], "importing sidekick.app performed a network probe"


# ---- SEC-sanitize: publish path strips scripts ------------------------------
def test_blog_sanitizes_html():
    from types import SimpleNamespace
    from sidekick import blog
    m = SimpleNamespace(
        msg_type="code", content="df", output="",
        rich=[{"type": "text/html",
               "data": "<script>alert(1)</script><table><tr><td>ok</td></tr></table>"}])
    nb = blog.dialog_to_post([m], title="t")
    dumped = __import__("json").dumps(nb)
    assert "alert(1)" not in dumped
    assert "<script" not in dumped
    assert "<table" in dumped                       # legit markup survives


# ---- SEC-path: sibling-prefix traversal is rejected -------------------------
def test_path_guard_sibling_prefix(tmp_path, monkeypatch):
    import sidekick.app as app
    base = tmp_path / "vendor"
    base.mkdir()
    sibling = tmp_path / "vendor-evil"          # shares the "vendor" prefix
    sibling.mkdir()
    (sibling / "secret.js").write_text("stolen")
    monkeypatch.setattr(app, "_VENDOR_DIR", base)
    # startswith() would have served this; is_relative_to() rejects it.
    resp = app.vendor(fname="../vendor-evil/secret.js")
    assert getattr(resp, "status_code", None) == 404


# ---- SEC-lan: refuse an unauthenticated non-loopback bind -------------------
def test_lan_bind_refused(monkeypatch):
    import sidekick.cli as cli
    monkeypatch.setenv("SIDEKICK_HOST", "0.0.0.0")
    monkeypatch.delenv("SIDEKICK_APP_TOKEN", raising=False)
    with pytest.raises(SystemExit):
        cli.cmd_serve(None)
    # The refusal is unconditional: a token must NOT unlock a non-loopback bind,
    # since the app enforces no auth (that would be false security).
    monkeypatch.setenv("SIDEKICK_APP_TOKEN", "anything")
    with pytest.raises(SystemExit):
        cli.cmd_serve(None)
