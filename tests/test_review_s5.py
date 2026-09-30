"""S5 — per-tab UI state is session-scoped, so two browser tabs no longer clobber
each other.

The failure mode this guards against is silent cross-wiring: tab A switches to
dialog X, tab B switches to dialog Y, and — because ALL of STATE used to be
process-global — a mutation from A would land in Y (editing/deleting the wrong
notebook). These tests drive the REAL routes with two separate Starlette
TestClients. Each TestClient keeps its own cookie jar, so two clients get two
distinct `sk_sid` cookies → two independent sessions.

What must hold:
  1. dialog is PER-TAB: A on X, B on Y; A keeps seeing X and B keeps seeing Y.
  2. a per-tab transient (editing) set in A does not appear in B, and never
     touches the shared global STATE.
  3. backend / target ARE shared: both sessions see the same target and the same
     notebook data.
  4. a caller with no session (direct handler call / cookie-less request) falls
     back to the shared global STATE — the back-compat path the rest of the
     suite relies on.
"""
import re

import pytest
from starlette.testclient import TestClient

import pappus.app as app


# The current dialog is uniquely marked by the rename box (exactly one per page):
#   <input name="new" value="<current dialog>" ... class="title-edit" ...>
# Sidebar links also carry data-dialog=<every dialog>, so we anchor on this
# single current-dialog element instead.
def _current_dialog(html: str) -> str | None:
    m = re.search(r'<input name="new" value="([^"]*)"[^>]*class="title-edit"', html)
    return m.group(1) if m else None


@pytest.fixture
def two_clients():
    """Two independent browser sessions (separate cookie jars) + a couple of
    dialogs that exist on the shared backend."""
    bk = app.STATE["backend"]
    for d in ("s5/alpha", "s5/beta", "s5/shared"):
        bk.messages(d)                       # touch → create on the shared backend
    a, b = TestClient(app.app), TestClient(app.app)
    a.get("/")                               # first request mints each session's sk_sid
    b.get("/")
    assert a.cookies.get("sk_sid") and b.cookies.get("sk_sid")
    assert a.cookies.get("sk_sid") != b.cookies.get("sk_sid")   # genuinely two sessions
    return a, b


def test_two_sessions_keep_independent_current_dialog(two_clients):
    a, b = two_clients
    # Drive the real /open route (what clicking a dialog in the sidebar hits).
    a.get("/open", params={"dialog": "s5/alpha"})
    b.get("/open", params={"dialog": "s5/beta"})

    ha, hb = a.get("/").text, b.get("/").text
    # Each tab sees ITS OWN current dialog — no cross-wiring.
    assert _current_dialog(ha) == "s5/alpha"
    assert _current_dialog(hb) == "s5/beta"

    # And switching B a second time must not disturb A.
    b.get("/open", params={"dialog": "s5/shared"})
    assert _current_dialog(a.get("/").text) == "s5/alpha"
    assert _current_dialog(b.get("/").text) == "s5/shared"


def test_per_tab_transient_does_not_leak_between_sessions(two_clients):
    a, b = two_clients
    bk = app.STATE["backend"]
    anchor = bk.add("s5/shared", "seed", "code")     # a cell to insert relative to
    a.get("/open", params={"dialog": "s5/shared"})
    b.get("/open", params={"dialog": "s5/shared"})   # both tabs on the SAME notebook

    app.STATE.pop("editing", None)                   # clean global baseline
    before = {m.id for m in bk.messages("s5/shared")}
    # A inserts a cell → the freshly-inserted empty cell opens in edit mode for A
    # (the one-shot `editing` transient). This is a real POST route.
    ra = a.post("/cell/insert", data={"id": anchor.id, "msg_type": "code", "where": "below"})
    new_id = next(m.id for m in bk.messages("s5/shared") if m.id not in before)

    # A got its edit-mode textarea (transient delivered to the session that set it).
    assert f"ta-{new_id}" in ra.text
    # The transient never touched the shared global STATE — so it cannot bleed to
    # other tabs. (Pre-S5 this write hit the global and every tab opened the cell.)
    assert app.STATE.get("editing") is None
    # B, viewing the same shared notebook, sees the new cell but NOT in edit mode.
    hb = b.get("/").text
    assert f"cell-{new_id}" in hb or new_id in hb    # the shared cell is present…
    assert f"ta-{new_id}" not in hb                  # …but not opened for editing in B


def test_backend_and_target_are_shared_across_sessions(two_clients):
    a, b = two_clients
    # Same shared backend object regardless of session.
    ha, hb = a.get("/").text, b.get("/").text
    tgt = app.STATE["target_name"]
    assert tgt in ha and tgt in hb                   # both see the same target
    # A dialog created through one session is visible to the other (shared data).
    a.get("/new")
    created = app.STATE["backend"].list_dialogs()
    hb2 = b.get("/").text
    assert any(d in hb2 for d in created)            # B's sidebar reflects shared dialogs


def test_no_session_falls_back_to_global_state():
    # (a) Direct accessor contract with no request in scope (how the rest of the
    #     suite calls handlers): cur reads global, set_cur writes global.
    app.STATE["dialog"] = "s5/global-direct"
    assert app.cur("dialog") == "s5/global-direct"
    app.set_cur("dialog", "s5/global-write")
    assert app.STATE["dialog"] == "s5/global-write"

    # (b) Over HTTP: a client that presents no cookie gets a fresh empty overlay,
    #     so per-tab reads fall through to the shared global default.
    app.STATE["backend"].messages("s5/global-http")
    app.STATE["dialog"] = "s5/global-http"
    c = TestClient(app.app)
    c.cookies.clear()                                # present no session cookie
    assert _current_dialog(c.get("/").text) == "s5/global-http"
