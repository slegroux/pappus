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


# ---- tailnet peer trust: `tailscale serve` on macOS forwards the tailnet IP -----
def test_peer_gate_loopback_always_ok():
    import sidekick.app as app
    for p in ("127.0.0.1", "::1", "testclient"):
        assert app._peer_ok(p)


def test_peer_gate_rejects_non_loopback_by_default(monkeypatch):
    import sidekick.app as app
    monkeypatch.setattr(app, "_TRUST_TAILNET", False)
    # Off by default: a tailnet IP is treated like any other remote peer → rejected.
    assert not app._peer_ok("100.125.88.8")
    assert not app._peer_ok("fd7a:115c:a1e0::1")


def test_peer_gate_trusts_tailnet_when_enabled(monkeypatch):
    import sidekick.app as app
    monkeypatch.setattr(app, "_TRUST_TAILNET", True)
    # Tailscale CGNAT (v4) and ULA (v6) ranges pass; loopback still passes …
    assert app._peer_ok("100.125.88.8")
    assert app._peer_ok("fd7a:115c:a1e0::1")
    assert app._peer_ok("127.0.0.1")
    # … but public, LAN, garbage and missing peers never do, even with trust on.
    assert not app._peer_ok("8.8.8.8")
    assert not app._peer_ok("192.168.1.50")
    assert not app._peer_ok("not-an-ip")
    assert not app._peer_ok(None)


# ---- convert-in-place opens the cell inline in edit mode (not the composer) --
def test_cell_type_convert_opens_edit_mode():
    """`i` (convert to Ask AI) must open the converted cell in edit mode right
    there — like insert — so `Esc i` lands the cursor in the cell inline instead
    of leaving it read-only and drifting focus down to the bottom composer."""
    app, client = _client()
    m = app.STATE["backend"].add("guard/test", "a note", "note")
    r = client.post("/cell/type", data={"id": m.id, "msg_type": "prompt"})
    assert r.status_code == 200
    html = r.text
    assert app.STATE["backend"].messages("guard/test")[-1].msg_type == "prompt"
    assert f"ta-{m.id}" in html                       # inline editor textarea present
    assert ">Ask<" in html and "Cancel" in html       # edit-mode buttons, not read-only
    assert f"getElementById('cell-{m.id}')" in html   # scrolls the cell into view
    assert "block:'nearest'" in html                  # minimal scroll, no jump to composer


def test_prompt_run_uses_selected_picker_model():
    """An explicit run must use the model the AI-picker shows — the picker is
    authoritative. That covers both a model-less cell (note→Ask-AI convert, older
    dialog) AND a cell carrying a STALE model: e.g. one stamped `codex-…` from the
    default before the user switched the picker to Opus. Only overriding on run
    keeps 'the selector decides' true; filling a blank alone would freeze the cell
    on the stale (possibly uninstalled) provider and keep erroring."""
    app, client = _client()
    app.STATE["model"] = "claude-opus-high"            # picker selection

    def model_of(mid):
        return [x for x in app.STATE["backend"].messages("guard/test") if x.id == mid][0].model

    # model-less cell adopts the picker
    m = app.STATE["backend"].add("guard/test", "explain DDPM vs DDIM", "prompt", model=None)
    client.post("/cell/run", data={"id": m.id, "content": m.content})
    assert model_of(m.id) == "claude-opus-high"

    # a cell stamped with a stale/uninstalled model is overridden to the picker
    m2 = app.STATE["backend"].add("guard/test", "q2", "prompt", model="codex-gpt-5.5-high")
    client.post("/cell/run", data={"id": m2.id, "content": m2.content})
    assert model_of(m2.id) == "claude-opus-high"


def test_model_select_persists_without_send():
    """Changing the picker must update server state on its own — a later re-run
    reads STATE['model'], not the composer form."""
    app, client = _client()
    r = client.post("/model/select", data={"model": "claude-sonnet-xhigh"})
    assert r.status_code == 200
    assert app.STATE["model"] == "claude-sonnet-xhigh"


def test_done_button_is_the_touch_escape_hatch():
    """Esc leaves a code/prompt editor keeping the text and WITHOUT running. A
    phone has no Esc, so edit mode carries a Done button posting to /cell/save
    ("save without executing"). Notes don't need one — their primary IS Save."""
    from fasthtml.common import to_xml
    from sidekick.client import Msg
    import sidekick.app as app

    for t in ("code", "prompt"):
        html = to_xml(app._cell_edit(Msg("_d", t, "x=1"), num=1))
        assert 'cell-btn done' in html, f"{t} edit view lost its Done button"
        assert '/cell/save' in html
    note = to_xml(app._cell_edit(Msg("_n", "note", "hi"), num=1))
    assert 'cell-btn done' not in note

    # Edit mode must force the toolbar visible (.show), or on touch — where
    # there is no :hover — every exit from the editor is unreachable.
    assert 'cell-actions show' in note


def test_done_button_saves_without_executing():
    """The Done path must not run the cell — that's the whole point of it."""
    app, client = _client()
    m = app.STATE["backend"].add("touch/esc", "1+1", "code")
    app.STATE["dialog"] = "touch/esc"
    r = client.post("/cell/save", data={"id": m.id, "content": "2+2"})
    assert r.status_code == 200
    saved = [x for x in app.STATE["backend"].messages("touch/esc") if x.id == m.id][0]
    assert saved.content == "2+2"        # text kept
    assert not saved.output              # and never executed
