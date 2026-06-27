"""Runnable test suite for SolveIt Sidekick.

These tests need no SolveIt server and no network — they exercise the real
project logic (target resolution, model routing, mock fallback, doctor checks,
tunnel guards, and the web send-route) using the in-memory mock backend.

Run:  python -m pytest -q     (from the project root)
"""
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sidekick import targets, doctor, tunnel
from sidekick.client import MockBackend, connect


# ---- targets & models -------------------------------------------------------
def test_targets_present():
    assert {"local", "h100", "kernel"} <= set(targets.list_targets())


def test_local_target_is_local():
    t = targets.get_target("local")
    assert t.url == "http://localhost:5001"
    assert t.is_remote is False
    assert t.token == "dummy"          # localhost accepts dummy


def test_h100_target_is_remote_with_tunnel_ports():
    t = targets.get_target("h100")
    assert t.is_remote is True
    assert t.ssh.remote_port == 5001
    assert t.ssh.local_port == 5101
    assert t.url.endswith(":5101")      # client points at the local tunnel end


def test_missing_token_does_not_crash(monkeypatch):
    monkeypatch.delenv("SOLVEIT_H100_TOKEN", raising=False)
    t = targets.get_target("h100")      # must not raise
    assert t.token == ""


def test_token_from_env(monkeypatch):
    monkeypatch.setenv("SOLVEIT_H100_TOKEN", "secret-cookie")
    assert targets.get_target("h100").token == "secret-cookie"


def test_models_and_default():
    ids = [m["id"] for m in targets.list_models()]
    assert ids == ["claude", "glm", "codex"]
    assert targets.default_model() == "claude"


def test_unknown_target_raises():
    with pytest.raises(KeyError):
        targets.get_target("nope")


# ---- mock backend & model routing ------------------------------------------
def test_mock_add_and_exec_code():
    b = MockBackend()
    m = b.add("d/x", "1+1", "code")
    out = b.exec("d/x", m.id)
    assert "mock" in out.output and out.model is None


def test_mock_prompt_records_model():
    b = MockBackend()
    m = b.add("d/x", "hi", "prompt", model="glm")
    out = b.exec("d/x", m.id)
    assert m.model == "glm"
    assert "glm" in out.output           # model surfaces in the answer label


def test_connect_falls_back_to_mock_when_no_server():
    backend, warning = connect(targets.get_target("local"))
    assert backend.live is False         # nothing listening on 5001 in tests
    assert warning                       # and we say why


# ---- doctor -----------------------------------------------------------------
def test_doctor_parses_host_port():
    assert doctor._host_port("http://localhost:5001") == ("localhost", 5001)
    assert doctor._host_port("https://x.solve.it.com/") == ("x.solve.it.com", 443)


def test_doctor_returns_structured_checks():
    checks = doctor.run_checks(targets.get_target("local"))
    assert all(len(c) == 3 for c in checks)
    labels = {c[1] for c in checks}
    assert "DNS" in labels and "PORT" in labels


def test_doctor_remote_reports_tunnel_down():
    checks = doctor.run_checks(targets.get_target("h100"))
    assert any(label == "TUNNEL" for _, label, _ in checks)


# ---- tunnel guards ----------------------------------------------------------
def test_open_tunnel_rejects_local_target():
    with pytest.raises(ValueError):
        tunnel.open_tunnel(targets.get_target("local"))


def test_port_open_false_on_unused_port():
    assert tunnel.port_open("127.0.0.1", 59999, timeout=0.3) is False


# ---- web send route ---------------------------------------------------------
def test_send_route_adds_message_and_remembers_model():
    import sidekick.app as app
    app.STATE["dialog"] = "test/route"
    app.STATE["backend"].messages("test/route")        # ensure dialog exists
    app.send(content="explain this", msg_type="prompt", model="codex")
    msgs = app.STATE["backend"].messages("test/route")
    assert msgs[-1].content == "explain this"
    assert msgs[-1].model == "codex"
    assert app.STATE["model"] == "codex"               # sticky selection


def test_send_route_ignores_empty():
    import sidekick.app as app
    app.STATE["dialog"] = "test/empty"
    app.STATE["backend"].messages("test/empty")
    before = len(app.STATE["backend"].messages("test/empty"))
    app.send(content="   ", msg_type="note")
    assert len(app.STATE["backend"].messages("test/empty")) == before


# ---- rename -----------------------------------------------------------------
def test_mock_backend_rename_moves_messages():
    b = MockBackend()
    b.add("old/name", "x = 1", "code")
    b.rename("old/name", "new/name")
    assert "new/name" in b.list_dialogs() and "old/name" not in b.list_dialogs()
    assert b.messages("new/name")[0].content == "x = 1"


def test_rename_rejects_duplicate():
    b = MockBackend()
    b.add("a", "1", "code")
    b.add("b", "2", "code")
    with pytest.raises(ValueError):
        b.rename("a", "b")


# ---- editable cells: update / delete ---------------------------------------
def test_update_edits_content_and_clears_stale_output():
    b = MockBackend()
    m = b.add("d/x", "1+1", "code")
    b.exec("d/x", m.id)
    assert m.output                              # has output after a run
    b.update("d/x", m.id, "2+2")
    assert m.content == "2+2"
    assert m.output == ""                        # stale output cleared on edit


def test_update_unknown_id_is_noop():
    b = MockBackend()
    assert b.update("d/x", "nope", "x") is None


def test_delete_removes_the_cell():
    b = MockBackend()
    a = b.add("d/x", "a = 1", "code")
    keep = b.add("d/x", "b = 2", "code")
    b.delete("d/x", a.id)
    ids = [m.id for m in b.messages("d/x")]
    assert a.id not in ids and keep.id in ids


# ---- editable cells: web routes --------------------------------------------
def test_cell_run_route_saves_edit_and_executes():
    import sidekick.app as app
    app.STATE["dialog"] = "cell/run"
    b = app.STATE["backend"]
    b.messages("cell/run")
    m = b.add("cell/run", "old", "code")
    app.cell_run(id=m.id, content="new code")
    edited = b.messages("cell/run")[-1]
    assert edited.content == "new code"          # edit persisted
    assert edited.output                         # and it was executed


def test_cell_save_route_edits_without_executing():
    import sidekick.app as app
    app.STATE["dialog"] = "cell/save"
    b = app.STATE["backend"]
    b.messages("cell/save")
    m = b.add("cell/save", "draft", "note")
    app.cell_save(id=m.id, content="# done")
    saved = b.messages("cell/save")[-1]
    assert saved.content == "# done" and saved.output == ""


def test_cell_delete_route_removes_cell():
    import sidekick.app as app
    app.STATE["dialog"] = "cell/del"
    b = app.STATE["backend"]
    b.messages("cell/del")
    m = b.add("cell/del", "x", "code")
    before = len(b.messages("cell/del"))
    app.cell_delete(id=m.id)
    assert len(b.messages("cell/del")) == before - 1


# ---- LiveBackend <-> solveit_client mapping --------------------------------
# These lock how we map onto solveit_client.core without needing a live SolveIt
# server (or the package installed): a fake Dialog/Message mirrors the real one,
# where message fields live in `m.data` with keys id/msg_type/content/output.
class _FakeMsg:
    def __init__(self, data):
        self.data = data
        self.exec_called = self.deleted = False
        self.updated = None

    def exec(self):
        self.exec_called = True
        self.data["output"] = "42"          # server fills output on run
        return self

    def update(self, **kw):
        self.data.update(kw)
        self.updated = kw
        return ("msg", "diff")              # real API returns a MsgDiff

    def delete(self):
        self.deleted = True
        return self


class _FakeDlg:
    def __init__(self):
        self._msgs = [_FakeMsg({"id": "m1", "msg_type": "code", "content": "6*7", "output": ""})]

    @property
    def messages(self):
        return self._msgs

    def read_msg(self, n=0, id=None):
        return next(m for m in self._msgs if m.data["id"] == id)

    def add_msg(self, content, msg_type="code"):   # NB: no `model` kwarg, like the real API
        m = _FakeMsg({"id": "m2", "msg_type": msg_type, "content": content, "output": ""})
        self._msgs.append(m)
        return m


def _live_backend_with(fake):
    from sidekick.client import LiveBackend
    b = LiveBackend.__new__(LiveBackend)   # bypass __init__: no solveit_client / no network
    b._dlg = lambda dialog: fake
    return b


def test_live_backend_reads_fields_from_message_data():
    m = _live_backend_with(_FakeDlg()).messages("d")[0]
    assert (m.id, m.msg_type, m.content) == ("m1", "code", "6*7")


def test_live_backend_add_keeps_model_but_does_not_pass_it_to_add_msg():
    # add_msg() has no `model` param; forwarding one would TypeError. add() must
    # keep model on our Msg (for display) without sending it to SolveIt.
    m = _live_backend_with(_FakeDlg()).add("d", "x=1", "prompt", model="glm")
    assert m.content == "x=1" and m.model == "glm"


def test_live_backend_exec_update_delete_call_real_methods():
    fake = _FakeDlg()
    b = _live_backend_with(fake)
    out = b.exec("d", "m1")
    assert out.output == "42" and fake._msgs[0].exec_called
    b.update("d", "m1", "7*6")
    assert fake._msgs[0].data["content"] == "7*6" and fake._msgs[0].updated == {"content": "7*6"}
    b.delete("d", "m1")
    assert fake._msgs[0].deleted


# ---- notebook context for the AI (kernel backend) --------------------------
from sidekick.client import build_context, Msg, HttpKernelBackend


def _m(id, t, content, output="", muted=False):
    return Msg(id, t, content, output, muted=muted)


def test_build_context_includes_prior_cells_and_stops_at_upto():
    msgs = [_m("a", "code", "x=1", "1"), _m("b", "note", "hello"), _m("c", "prompt", "q?")]
    ctx = build_context(msgs, upto_id="c")
    assert "x=1" in ctx and "hello" in ctx
    assert "q?" not in ctx              # the prompt being answered is not its own context


def test_build_context_truncates_long_output():
    ctx = build_context([_m("a", "code", "run", "Z" * 5000)], out_trunc=100)
    assert "…" in ctx and len(ctx) < 600


def test_build_context_skips_muted_cells():
    ctx = build_context([_m("a", "note", "SECRET", muted=True), _m("b", "note", "kept")])
    assert "SECRET" not in ctx and "kept" in ctx


def test_build_context_drops_oldest_when_over_budget():
    msgs = [_m("old", "note", "A" * 200), _m("new", "note", "B" * 200)]
    ctx = build_context(msgs, max_chars=250)      # only the newest cell fits
    assert "B" * 200 in ctx and "A" * 200 not in ctx
    assert "omitted" in ctx


def test_set_muted_toggles_and_sets():
    b = MockBackend()
    m = b.add("d", "x", "code")
    b.set_muted("d", m.id)
    assert m.muted is True
    b.set_muted("d", m.id)
    assert m.muted is False
    b.set_muted("d", m.id, muted=True)
    assert m.muted is True


def test_kernel_backend_exec_prompt_sends_notebook_context():
    b = HttpKernelBackend.__new__(HttpKernelBackend)   # bypass __init__ (no network)
    b._dialogs = {}
    posts = []
    b._post = lambda path, body: (posts.append((path, body)) or {"output": "ok"})
    b.add("d", "import numpy as np", "code")
    b._dialogs["d"][0].output = "imported"
    p = b.add("d", "what did we import?", "prompt")
    b.exec("d", p.id)
    path, body = posts[-1]
    assert path == "/prompt"
    assert body["content"] == "what did we import?"
    assert "import numpy as np" in body["context"]   # prior code cell is in the context
    assert "imported" in body["context"]             # ...with its output


def test_cell_mute_route_toggles():
    import sidekick.app as app
    app.STATE["dialog"] = "cell/mute"
    b = app.STATE["backend"]
    b.messages("cell/mute")
    m = b.add("cell/mute", "x", "code")
    app.cell_mute(id=m.id)
    assert m.muted is True
    app.cell_mute(id=m.id)
    assert m.muted is False


def test_run_prompt_surfaces_context_when_no_key(monkeypatch, tmp_path):
    import server.kernel_server as ks
    monkeypatch.setenv("SIDEKICK_SECRETS", str(tmp_path / "none.json"))
    for env in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "ZHIPU_API_KEY"):
        monkeypatch.delenv(env, raising=False)
    out = ks.run_prompt("d", "q", "claude", context="<note>hi</note>")
    assert "no API key" in out and "notebook context:" in out


# ---- rich output (plots / images / dataframes) -----------------------------
def test_run_code_returns_text_and_rich_tuple():
    import server.kernel_server as ks
    text, rich = ks.run_code("rich/plain", "1 + 1")
    assert text == "2" and rich == []


def test_run_code_captures_repr_html_as_rich():
    import server.kernel_server as ks
    code = ("class T:\n"
            "    def _repr_html_(self): return '<b>rich!</b>'\n"
            "T()")
    text, rich = ks.run_code("rich/html", code)
    assert text == ""                                   # rich repr replaces the text repr
    assert any(r["type"] == "text/html" and "rich!" in r["data"] for r in rich)


def test_capture_figs_empty_without_matplotlib():
    import server.kernel_server as ks
    assert ks._capture_figs() == []                     # no matplotlib imported -> no cost


def test_run_code_trailing_semicolon_suppresses_value():
    import server.kernel_server as ks
    text, rich = ks.run_code("rich/semi", "5 + 5;")
    assert text == "" and rich == []                    # Jupyter-style suppression


def test_kernel_backend_exec_stores_rich_output():
    b = HttpKernelBackend.__new__(HttpKernelBackend)
    b._dialogs = {}
    b._post = lambda path, body: {"output": "", "rich": [{"type": "image/png", "data": "AAAA"}]}
    m = b.add("d", "plot()", "code")
    b.exec("d", m.id)
    assert m.rich and m.rich[0]["type"] == "image/png"


def test_rich_view_renders_image_and_html():
    import sidekick.app as app
    from fasthtml.common import to_xml
    img = to_xml(app._rich_view({"type": "image/png", "data": "XYZ"}))
    assert "<img" in img and "data:image/png;base64,XYZ" in img
    html = to_xml(app._rich_view({"type": "text/html", "data": "<b>hi</b>"}))
    assert "<b>hi</b>" in html


# ---- token counting + pinned cells -----------------------------------------
def test_est_tokens_rough():
    from sidekick.client import est_tokens
    assert est_tokens("") == 0
    assert est_tokens("a" * 40) == 10                   # ~4 chars/token


def test_build_context_keeps_pinned_over_budget():
    msgs = [_m("old", "note", "A" * 200), _m("new", "note", "B" * 200)]
    msgs[0].pinned = True                               # pin the oldest, over-budget cell
    ctx = build_context(msgs, max_chars=250)
    assert "A" * 200 in ctx                             # pinned survives trimming
    assert "B" * 200 not in ctx                         # the newer, unpinned cell is dropped


def test_set_pinned_toggles():
    b = MockBackend()
    m = b.add("d", "x", "code")
    b.set_pinned("d", m.id)
    assert m.pinned is True
    b.set_pinned("d", m.id)
    assert m.pinned is False


def test_cell_pin_route_toggles():
    import sidekick.app as app
    app.STATE["dialog"] = "cell/pin"
    b = app.STATE["backend"]
    b.messages("cell/pin")
    m = b.add("cell/pin", "x", "code")
    app.cell_pin(id=m.id)
    assert m.pinned is True
    app.cell_pin(id=m.id)
    assert m.pinned is False


def test_page_includes_htmx_so_cell_buttons_work():
    # Regression guard: we return a full Html document, so FastHTML does NOT
    # auto-inject its headers — the page must carry htmx itself, or every
    # per-cell hx-post button (Run/Mute/Pin/Delete) renders but does nothing.
    import sidekick.app as app
    from fasthtml.common import to_xml
    assert "htmx" in to_xml(app.Page()).lower()
    assert "htmx" in to_xml(app.SettingsPage()).lower()


def test_ctx_meter_reports_token_estimate():
    import sidekick.app as app
    from fasthtml.common import to_xml
    html = to_xml(app._ctx_meter([_m("a", "note", "hello world"), _m("b", "code", "x=1", "1")]))
    assert "AI context" in html and "tokens" in html and "cells in" in html


# ---- secrets store ----------------------------------------------------------
def test_secrets_save_load_and_status(tmp_path, monkeypatch):
    from sidekick import secrets_store
    monkeypatch.setenv("SIDEKICK_SECRETS", str(tmp_path / "s.json"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    secrets_store.save("ANTHROPIC_API_KEY", "sk-ant-123456789")
    assert secrets_store.key_for_model("claude") == "sk-ant-123456789"
    row = next(r for r in secrets_store.status() if r["model"] == "claude")
    assert row["set"] and row["masked"].startswith("sk-")
    # secret file is locked down
    assert oct((tmp_path / "s.json").stat().st_mode)[-3:] == "600"


def test_secrets_env_overrides_file(tmp_path, monkeypatch):
    from sidekick import secrets_store
    monkeypatch.setenv("SIDEKICK_SECRETS", str(tmp_path / "s.json"))
    secrets_store.save("OPENAI_API_KEY", "from-file")
    monkeypatch.setenv("OPENAI_API_KEY", "from-env")
    assert secrets_store.get_key("OPENAI_API_KEY") == "from-env"
