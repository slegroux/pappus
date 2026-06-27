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
    assert ids == ["claude", "claude-cli", "glm", "codex"]
    assert targets.default_model() == "claude-cli"


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


def test_fallback_model_is_subscription_not_api():
    # A model-less prompt must default to the configured default (Claude Max via
    # the CLI), never silently to the paid Anthropic API ('claude').
    from sidekick.client import _fallback_model
    assert _fallback_model() == "claude-cli"


def test_model_label_resolves_id_to_friendly_name():
    import sidekick.app as app
    assert app._model_label("claude-cli") == "Claude (Max)"
    assert app._model_label("claude") == "Claude"
    assert app._model_label(None) == "Claude (Max)"      # model-less -> default label
    assert app._model_label("nope") == "nope"            # unknown id passes through


def test_kernel_prompt_without_model_routes_to_subscription():
    from sidekick.client import HttpKernelBackend, Msg
    b = HttpKernelBackend.__new__(HttpKernelBackend)     # skip __init__ (no live target)
    b._dialogs, b._undo, b._store_key = {}, [], None
    sent = {}

    def fake_post(path, body):
        sent["path"], sent["body"] = path, body
        return {"output": "ok"}

    b._post, b._save = fake_post, lambda: None
    b._dialogs["d"] = [Msg(id="_x", msg_type="prompt", content="hi", model=None)]
    b.exec("d", "_x")
    assert sent["path"] == "/prompt"
    assert sent["body"]["model"] == "claude-cli"          # not "claude" (the API path)


def test_set_type_converts_and_clears_stale_output():
    b = MockBackend()
    m = b.add("t/x", "df.head()", "code")
    m.output = "old result"
    b.set_type("t/x", m.id, "note")
    got = b.messages("t/x")[0]
    assert got.msg_type == "note"
    assert got.content == "df.head()"     # source text kept
    assert got.output == "" and got.rich == []   # stale output dropped


def test_set_type_rejects_unknown_type():
    b = MockBackend()
    m = b.add("t/y", "x", "code")
    b.set_type("t/y", m.id, "bogus")
    assert b.messages("t/y")[0].msg_type == "code"   # unchanged


def test_cell_type_route_converts_in_place():
    import sidekick.app as app
    app.STATE["dialog"] = "t/route"
    bk = app.STATE["backend"]
    bk.messages("t/route")
    m = bk.add("t/route", "2 + 2", "code")
    app.cell_type(id=m.id, msg_type="prompt")
    cells = bk.messages("t/route")
    assert len(cells) == 1                 # same cell, not a new one
    assert cells[0].id == m.id and cells[0].msg_type == "prompt"


def test_undo_restores_deleted_cell_at_its_position():
    b = MockBackend()
    a = b.add("u/x", "a = 1", "code")
    mid = b.add("u/x", "m = 2", "code")
    z = b.add("u/x", "z = 3", "code")
    b.delete("u/x", mid.id)
    assert [m.id for m in b.messages("u/x")] == [a.id, z.id]
    restored = b.undo("u/x")
    assert restored.id == mid.id
    assert [m.id for m in b.messages("u/x")] == [a.id, mid.id, z.id]   # back in the middle


def test_undo_lands_after_anchor_even_after_reorder():
    # Undo must restore relative to the cell it followed, not a stale absolute index.
    b = MockBackend()
    a = b.add("u/r", "a", "code")
    mid = b.add("u/r", "mid", "code")
    z = b.add("u/r", "z", "code")
    b.delete("u/r", mid.id)                       # [a, z]; mid had followed a
    b.reorder("u/r", [z.id, a.id])                # [z, a] — old index 1 now means something else
    b.undo("u/r")
    assert [m.id for m in b.messages("u/r")] == [z.id, a.id, mid.id]   # after its anchor a


def test_undo_appends_when_anchor_also_deleted():
    b = MockBackend()
    a = b.add("u/g", "a", "code")
    mid = b.add("u/g", "mid", "code")
    b.delete("u/g", mid.id)                       # mid anchored on a
    b.delete("u/g", a.id)                         # anchor gone -> []
    b.undo("u/g")                                 # undo the mid delete (LIFO: a first)
    # LIFO: first undo restores a (most recent delete)
    assert [m.id for m in b.messages("u/g")] == [a.id]
    b.undo("u/g")                                 # now mid; its anchor a exists again
    assert [m.id for m in b.messages("u/g")] == [a.id, mid.id]


def test_undo_is_lifo_and_empty_is_noop():
    b = MockBackend()
    x = b.add("u/y", "x", "code")
    y = b.add("u/y", "y", "code")
    b.delete("u/y", x.id)
    b.delete("u/y", y.id)
    assert b.undo("u/y").id == y.id          # most-recent delete comes back first
    assert b.undo("u/y").id == x.id
    assert b.undo("u/y") is None             # nothing left to undo


def test_cell_undo_route_restores_last_delete():
    import sidekick.app as app
    app.STATE["dialog"] = "u/route"
    bk = app.STATE["backend"]
    bk.messages("u/route")
    keep = bk.add("u/route", "keep", "code")
    gone = bk.add("u/route", "gone", "note")
    app.cell_delete(id=gone.id)
    assert [m.id for m in bk.messages("u/route")] == [keep.id]
    app.cell_undo()
    assert [m.id for m in bk.messages("u/route")] == [keep.id, gone.id]


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


# ---- Claude via the `claude` CLI (subscription / Max plan) -----------------
# The CLI session/command logic lives in sidekick.claude_cli; the kernel server
# routes the `claude-cli` model to it. Tests drive the module directly, plus one
# that asserts run_prompt delegates.
def _mk_cli_run(capture, result="ok", session_id="sid", returncode=0,
                is_error=False, stdout=None):
    """Build a fake _run_cli that records the argv/env and returns a JSON result."""
    import json as _json
    from types import SimpleNamespace

    def run(cmd, cwd, env, timeout):
        capture.append({"cmd": cmd, "cwd": cwd, "env": env, "timeout": timeout})
        body = _json.dumps({"result": result, "session_id": session_id,
                            "is_error": is_error})
        return SimpleNamespace(returncode=returncode,
                               stdout=body if stdout is None else stdout, stderr="")
    return run


def _mk_cli_popen(capture, deltas, session_id="sid", is_error=False):
    """Build a fake _popen whose stdout emits stream-json lines for `deltas`."""
    import json as _json
    from types import SimpleNamespace

    def popen(cmd, cwd, env):
        capture.append({"cmd": cmd, "cwd": cwd, "env": env})
        lines = []
        for d in deltas:
            lines.append(_json.dumps({"type": "stream_event",
                "event": {"type": "content_block_delta",
                          "delta": {"type": "text_delta", "text": d}}}) + "\n")
        lines.append(_json.dumps({"type": "result", "session_id": session_id,
                                  "is_error": is_error,
                                  "result": "".join(deltas)}) + "\n")
        return SimpleNamespace(stdout=iter(lines), wait=lambda timeout=None: 0)
    return popen


def test_claude_cli_fresh_session_sends_full_context(monkeypatch):
    import sidekick.claude_cli as cc
    cc.CLI_SESSIONS.pop("cli/d1", None)
    monkeypatch.setattr(cc, "claude_bin", lambda: "/bin/claude")
    monkeypatch.delenv("SIDEKICK_CLAUDE_CLI_MODEL", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-be-dropped")
    cap = []
    monkeypatch.setattr(cc, "_run_cli", _mk_cli_run(cap, result="a1", session_id="sid-1"))

    out = cc.call("cli/d1", "what is x?", context="<code>x=1</code>")
    assert out == "a1"
    cmd = cap[-1]["cmd"]
    assert "--session-id" in cmd and "--resume" not in cmd      # brand-new session
    sys_arg = cmd[cmd.index("--append-system-prompt") + 1]
    assert "<code>x=1</code>" in sys_arg                        # full context shipped
    assert cmd[-1] == "what is x?"                              # prompt is the positional
    assert "ANTHROPIC_API_KEY" not in cap[-1]["env"]            # forced onto subscription
    # lean mode: no MCP servers, no user settings (hooks/auto-memory) -> faster TTFT
    assert "--strict-mcp-config" in cmd
    assert cmd[cmd.index("--setting-sources") + 1] == "project"
    assert cc.CLI_SESSIONS["cli/d1"] == {"id": "sid-1", "sent": "<code>x=1</code>"}


def test_claude_cli_appended_cells_resume_with_only_the_delta(monkeypatch):
    import sidekick.claude_cli as cc
    cc.CLI_SESSIONS["cli/d2"] = {"id": "sid-9", "sent": "<code>x=1</code>"}
    monkeypatch.setattr(cc, "claude_bin", lambda: "/bin/claude")
    cap = []
    monkeypatch.setattr(cc, "_run_cli", _mk_cli_run(cap, result="a2", session_id="sid-9"))

    new_ctx = "<code>x=1</code>\n<code>y=2</code>"
    cc.call("cli/d2", "and y?", context=new_ctx)
    cmd = cap[-1]["cmd"]
    assert "--resume" in cmd and "sid-9" in cmd                 # continues the session
    assert "--session-id" not in cmd
    assert "--append-system-prompt" not in cmd                 # no full re-send
    user_msg = cmd[-1]
    assert "y=2" in user_msg and "and y?" in user_msg          # only the new cell + question
    assert "x=1" not in user_msg                               # old cell not re-sent
    assert cc.CLI_SESSIONS["cli/d2"]["sent"] == new_ctx


def test_claude_cli_edit_above_starts_a_fresh_session(monkeypatch):
    import sidekick.claude_cli as cc
    cc.CLI_SESSIONS["cli/d3"] = {"id": "sid-old",
                                 "sent": "<code>x=1</code>\n<code>y=2</code>"}
    monkeypatch.setattr(cc, "claude_bin", lambda: "/bin/claude")
    cap = []
    monkeypatch.setattr(cc, "_run_cli", _mk_cli_run(cap, result="a3", session_id="sid-new"))

    edited = "<code>x=99</code>\n<code>y=2</code>"              # not a prefix-extension
    cc.call("cli/d3", "q", context=edited)
    cmd = cap[-1]["cmd"]
    assert "--session-id" in cmd and "--resume" not in cmd      # reset, not resumed
    assert cc.CLI_SESSIONS["cli/d3"] == {"id": "sid-new", "sent": edited}


def test_claude_cli_missing_binary_is_friendly(monkeypatch):
    import sidekick.claude_cli as cc
    monkeypatch.setattr(cc, "claude_bin", lambda: None)
    out = cc.call("cli/d4", "q", context="")
    assert "claude" in out.lower() and "PATH" in out


def test_claude_cli_surfaces_error_output(monkeypatch):
    import sidekick.claude_cli as cc
    cc.CLI_SESSIONS.pop("cli/d5", None)
    monkeypatch.setattr(cc, "claude_bin", lambda: "/bin/claude")
    monkeypatch.setattr(cc, "_run_cli", _mk_cli_run([], is_error=True,
                                                    result="rate limit reached"))
    out = cc.call("cli/d5", "q", context="<code>x=1</code>")
    assert "error" in out.lower() and "rate limit" in out
    assert "cli/d5" not in cc.CLI_SESSIONS          # failed turn doesn't record a session


def test_run_prompt_routes_claude_cli_to_the_module(monkeypatch):
    # The kernel server delegates the `claude-cli` model to sidekick.claude_cli.
    import server.kernel_server as ks
    import sidekick.claude_cli as cc
    monkeypatch.setattr(cc, "claude_bin", lambda: "/bin/claude")
    monkeypatch.setattr(cc, "_run_cli", _mk_cli_run([], result="routed", session_id="s"))
    assert ks.run_prompt("cli/route", "q", "claude-cli", context="c") == "routed"
    assert ks.CLI_SESSIONS is cc.CLI_SESSIONS        # server shares the one session store


def test_claude_cli_stream_yields_deltas_and_records_session(monkeypatch):
    import sidekick.claude_cli as cc
    cc.CLI_SESSIONS.pop("cli/s1", None)
    monkeypatch.setattr(cc, "claude_bin", lambda: "/bin/claude")
    cap = []
    monkeypatch.setattr(cc, "_popen",
                        _mk_cli_popen(cap, ["Hel", "lo ", "world"], session_id="sid-s1"))

    chunks = list(cc.stream("cli/s1", "hi?", context="<code>x=1</code>"))
    assert chunks == ["Hel", "lo ", "world"]               # streamed in order
    cmd = cap[-1]["cmd"]
    assert "stream-json" in cmd and "--session-id" in cmd   # streaming, fresh session
    assert cc.CLI_SESSIONS["cli/s1"] == {"id": "sid-s1", "sent": "<code>x=1</code>"}


def test_claude_cli_stream_resumes_on_append(monkeypatch):
    import sidekick.claude_cli as cc
    cc.CLI_SESSIONS["cli/s2"] = {"id": "sid-7", "sent": "<code>x=1</code>"}
    monkeypatch.setattr(cc, "claude_bin", lambda: "/bin/claude")
    cap = []
    monkeypatch.setattr(cc, "_popen", _mk_cli_popen(cap, ["ok"], session_id="sid-7"))

    list(cc.stream("cli/s2", "more?", context="<code>x=1</code>\n<code>y=2</code>"))
    cmd = cap[-1]["cmd"]
    assert "--resume" in cmd and "sid-7" in cmd             # streaming resume
    assert "y=2" in cmd[-1] and "x=1" not in cmd[-1]        # only the delta


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


# ---- render-by-default / click-to-edit -------------------------------------
def test_rendered_cells_are_not_raw_textareas():
    import sidekick.app as app
    from fasthtml.common import to_xml
    note = to_xml(app.MsgRow(Msg("n", "note", "# Title")))
    code = to_xml(app.MsgRow(Msg("c", "code", "x = 1")))
    assert "<textarea" not in note and "<h1>" in note          # markdown rendered, not raw
    assert "<textarea" not in code and "highlight" in code      # pygments-highlighted, not raw


def test_cell_edit_route_returns_editor_with_cancel():
    import sidekick.app as app
    from fasthtml.common import to_xml
    app.STATE["dialog"] = "edit/route"
    b = app.STATE["backend"]
    b.messages("edit/route")
    m = b.add("edit/route", "x = 1", "code")
    html = to_xml(app.cell_edit(id=m.id))
    assert "<textarea" in html and "Cancel" in html


def test_cell_view_route_returns_rendered_cell():
    import sidekick.app as app
    from fasthtml.common import to_xml
    app.STATE["dialog"] = "view/route"
    b = app.STATE["backend"]
    b.messages("view/route")
    m = b.add("view/route", "# hi", "note")
    html = to_xml(app.cell_view(id=m.id))
    assert "<textarea" not in html and "<h1>" in html


def test_cell_exec_route_reruns_stored_content():
    import sidekick.app as app
    app.STATE["dialog"] = "exec/route"
    b = app.STATE["backend"]
    b.messages("exec/route")
    m = b.add("exec/route", "2+2", "code")
    app.cell_exec(id=m.id)
    assert b.messages("exec/route")[-1].output                 # re-ran stored source


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
# ---- streaming AI answers (claude-cli + SSE) -------------------------------
def test_send_prompt_with_claude_cli_defers_to_stream():
    import sidekick.app as app
    app.STATE["dialog"] = "stream/send"
    app.STATE["backend"].messages("stream/send")
    app.STATE["pending_stream"] = None
    app.send(content="hello?", msg_type="prompt", model="claude-cli")
    m = app.STATE["backend"].messages("stream/send")[-1]
    assert m.model == "claude-cli"
    assert m.output == ""                                 # NOT executed inline
    assert app.STATE["pending_stream"] == ("stream/send", m.id)
    app.STATE["pending_stream"] = None


def test_msgrow_renders_sse_placeholder_for_pending_prompt():
    import sidekick.app as app
    from fasthtml.common import to_xml
    app.STATE["dialog"] = "stream/row"
    b = app.STATE["backend"]
    b.messages("stream/row")
    m = b.add("stream/row", "q?", "prompt", model="claude-cli")
    app.STATE["pending_stream"] = ("stream/row", m.id)
    html = to_xml(app.MsgRow(m))
    assert "data-stream-url" in html and "/stream?dialog=" in html
    app.STATE["pending_stream"] = None


def test_stream_route_emits_deltas_and_persists_output(monkeypatch):
    import asyncio
    import sidekick.app as app
    app.STATE["dialog"] = "stream/route"
    b = app.STATE["backend"]
    b.messages("stream/route")
    m = b.add("stream/route", "add x and y?", "prompt", model="claude-cli")
    app.STATE["pending_stream"] = ("stream/route", m.id)
    monkeypatch.setattr(app, "stream_claude", lambda d, c, ctx: iter(["4", "2"]))

    resp = app.stream_answer(dialog="stream/route", id=m.id)

    async def collect():
        out = []
        async for chunk in resp.body_iterator:
            out.append(chunk.decode() if isinstance(chunk, bytes) else chunk)
        return "".join(out)
    body = asyncio.run(collect())
    assert "event: msg" in body and "event: done" in body
    assert "42" in body                                  # cumulative render reached "42"
    assert m.output == "42"                              # persisted for reloads
    assert app.STATE["pending_stream"] is None           # cleared on completion


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


# ---- persistence (in-memory backends save/restore dialogs) ------------------
def test_persistence_round_trip_survives_backend_rebuild(monkeypatch, tmp_path):
    monkeypatch.setenv("SIDEKICK_DATA", str(tmp_path))
    from sidekick.client import HttpKernelBackend, _store_path
    from sidekick.targets import Target
    t = Target(name="kernel", url="http://localhost:5055", token="dummy", ssh=None)
    b1 = HttpKernelBackend(t)                       # __init__ is network-free
    m = b1.add("paper/notes", "x = 1", "code")
    b1.update("paper/notes", m.id, "x = 2")
    b1.set_pinned("paper/notes", m.id)
    # a brand-new backend (restart / Settings-save / target-switch) restores it
    b2 = HttpKernelBackend(t)
    msgs = b2.messages("paper/notes")
    assert len(msgs) == 1
    assert msgs[0].content == "x = 2" and msgs[0].pinned is True
    assert _store_path("kernel").exists()


def test_mock_backend_is_ephemeral(monkeypatch, tmp_path):
    monkeypatch.setenv("SIDEKICK_DATA", str(tmp_path))
    from sidekick.client import MockBackend
    MockBackend().add("d/x", "hi", "note")
    assert list(tmp_path.glob("dialogs-*.json")) == []   # nothing written
    assert MockBackend().list_dialogs() == ["demo/welcome"]


def test_persistence_preserves_rich_and_flags(monkeypatch, tmp_path):
    monkeypatch.setenv("SIDEKICK_DATA", str(tmp_path))
    from sidekick.client import _save_dialogs, _load_dialogs, Msg
    _save_dialogs("kernel", {"d": [Msg("m1", "code", "plot()", output="ok",
                                       rich=[{"type": "image/png", "data": "AAAA"}], muted=True)]})
    m = _load_dialogs("kernel")["d"][0]
    assert m.rich == [{"type": "image/png", "data": "AAAA"}]
    assert m.muted is True and m.output == "ok"


# ---- kernel server auth -----------------------------------------------------
def test_kernel_auth_open_when_no_token():
    import server.kernel_server as ks
    assert ks._check_auth(None, "") is True
    assert ks._check_auth(None, "_solveit=whatever") is True


def test_kernel_auth_enforced_when_token_set():
    import server.kernel_server as ks
    assert ks._check_auth("s3cret", "_solveit=s3cret") is True
    assert ks._check_auth("s3cret", "a=1; _solveit=s3cret; b=2") is True
    assert ks._check_auth("s3cret", "_solveit=wrong") is False
    assert ks._check_auth("s3cret", "") is False                 # missing cookie -> denied


def test_kernel_token_from_cookie():
    import server.kernel_server as ks
    assert ks._token_from_cookie("x=1; _solveit=tok; y=2") == "tok"
    assert ks._token_from_cookie("") == ""


def test_kernel_loopback_detection():
    import server.kernel_server as ks
    assert ks._is_loopback("127.0.0.1") and ks._is_loopback("localhost")
    assert not ks._is_loopback("0.0.0.0") and not ks._is_loopback("1.2.3.4")


# ---- drag-to-reorder --------------------------------------------------------
def test_reorder_moves_cells_and_never_drops():
    b = MockBackend()
    a = b.add("d/x", "1", "code")
    bb = b.add("d/x", "2", "code")
    c = b.add("d/x", "3", "code")
    b.reorder("d/x", [c.id, a.id, bb.id])
    assert [m.id for m in b.messages("d/x")] == [c.id, a.id, bb.id]
    b.reorder("d/x", [bb.id])                       # omitted ids are kept, not lost
    ids = [m.id for m in b.messages("d/x")]
    assert ids[0] == bb.id and set(ids) == {a.id, bb.id, c.id}


def test_cell_move_route_reorders():
    import sidekick.app as app
    app.STATE["dialog"] = "cell/move"
    b = app.STATE["backend"]
    b.messages("cell/move")
    a = b.add("cell/move", "1", "code")
    c = b.add("cell/move", "2", "code")
    app.cell_move(ids=f"{c.id},{a.id}")
    assert [m.id for m in b.messages("cell/move")] == [c.id, a.id]


# ---- export (.ipynb / .md) --------------------------------------------------
def test_export_ipynb_structure():
    import json, sidekick.app as app
    from sidekick.client import Msg
    msgs = [Msg("n", "note", "# Title"),
            Msg("c", "code", "print(1)", output="1", rich=[{"type": "image/png", "data": "AAAA"}]),
            Msg("p", "prompt", "what?", output="because", model="codex")]
    nb = app.to_ipynb(msgs)
    assert nb["nbformat"] == 4 and len(nb["cells"]) == 3
    assert nb["cells"][0]["cell_type"] == "markdown"
    code = nb["cells"][1]
    assert code["cell_type"] == "code" and "print(1)" in "".join(code["source"])
    kinds = []
    for o in code["outputs"]:
        kinds.append(o["name"]) if o["output_type"] == "stream" else kinds.extend(o["data"])
    assert "stdout" in kinds and "image/png" in kinds
    assert nb["cells"][2]["cell_type"] == "markdown"      # prompt -> markdown Q&A
    json.dumps(nb)                                         # must be serializable


def test_export_markdown():
    import sidekick.app as app
    from sidekick.client import Msg
    md = app.to_markdown([
        Msg("n", "note", "## Hi"),
        Msg("c", "code", "x=1", output="ok", rich=[{"type": "image/png", "data": "BBBB"}]),
        Msg("p", "prompt", "q", output="a", model="glm"),
    ])
    assert "## Hi" in md
    assert "```python\nx=1\n```" in md and "```\nok\n```" in md
    assert "data:image/png;base64,BBBB" in md
    assert "**Prompt:** q" in md and "**glm:**" in md


def test_export_routes_set_download_headers():
    import sidekick.app as app
    app.STATE["dialog"] = "paper/x"
    b = app.STATE["backend"]
    b.messages("paper/x")
    b.add("paper/x", "# Notes", "note")
    r1, r2 = app.export_ipynb(), app.export_md()
    assert 'filename="paper-x.ipynb"' in r1.headers["content-disposition"]
    assert 'filename="paper-x.md"' in r2.headers["content-disposition"]
    assert r1.media_type.startswith("application/x-ipynb")


# ---- paper reading (PDF -> markdown) ----------------------------------------
def test_paper_convert_caches_and_falls_back(monkeypatch, tmp_path):
    monkeypatch.setenv("SIDEKICK_DATA", str(tmp_path))
    from sidekick import paper as pl
    pdf = tmp_path / "x.pdf"; pdf.write_bytes(b"%PDF-1.4 fake")
    monkeypatch.setattr(pl, "_marker_convert", lambda p: None)       # marker unavailable
    monkeypatch.setattr(pl, "_pypdf_convert", lambda p: "# Extracted\ntext")
    md, engine = pl.convert(str(pdf))
    assert engine == "pypdf" and "Extracted" in md and pl.cache_path(pdf).exists()
    monkeypatch.setattr(pl, "_pypdf_convert", lambda p: "SHOULD NOT RUN")
    md2, engine2 = pl.convert(str(pdf))                              # second call hits cache
    assert engine2 == "cache" and md2 == md


def test_paper_empty_conversion_is_not_cached(monkeypatch, tmp_path):
    # An empty extraction (image-only/scanned PDF) must NOT be cached, or the paper
    # would be blank forever (cache key is path+mtime; uploads are content-deduped).
    monkeypatch.setenv("SIDEKICK_DATA", str(tmp_path))
    from sidekick import paper as pl
    pdf = tmp_path / "scan.pdf"; pdf.write_bytes(b"%PDF-1.4 fake")
    monkeypatch.setattr(pl, "_marker_convert", lambda p: None)
    monkeypatch.setattr(pl, "_pypdf_convert", lambda p: "   ")       # nothing extracted
    md, engine = pl.convert(str(pdf))
    assert engine == "pypdf" and not pl.cache_path(pdf).exists()      # not poisoned
    monkeypatch.setattr(pl, "_pypdf_convert", lambda p: "# Now it works")
    md2, engine2 = pl.convert(str(pdf))                              # retried, not stuck blank
    assert engine2 == "pypdf" and "Now it works" in md2


def test_paper_uses_marker_when_available(monkeypatch, tmp_path):
    monkeypatch.setenv("SIDEKICK_DATA", str(tmp_path))
    from sidekick import paper as pl
    pdf = tmp_path / "y.pdf"; pdf.write_bytes(b"%PDF-1.4 fake")
    monkeypatch.setattr(pl, "_marker_convert", lambda p: r"$$E=mc^2$$")
    md, engine = pl.convert(str(pdf))
    assert engine == "marker" and "E=mc^2" in md


def test_paper_panel_states():
    import sidekick.app as app
    from fasthtml.common import to_xml
    app.STATE["paper"] = None
    assert 'id="paperPanel"' in to_xml(app.PaperPanel())
    app.STATE["paper"] = {"name": "p.pdf", "status": "converting"}
    h = to_xml(app.PaperPanel())
    assert "Converting" in h and "/paper/status" in h               # polling spinner
    app.STATE["paper"] = {"name": "p.pdf", "status": "ready",
                          "md": "# Title\n\nsome **bold** body", "engine": "pypdf"}
    h = to_xml(app.PaperPanel())
    assert "<h1>Title</h1>" in h and "paperBody" in h               # rendered markdown
    # blocks carry their source markdown so a highlight imports real md, not plain text
    assert 'class="pblock"' in h and 'data-md="# Title"' in h
    app.STATE["paper"] = None


def test_paper_step_brings_one_section_at_a_time():
    import sidekick.app as app
    md = "# A\n\nalpha body\n\n# B\n\nbeta body"     # split_sections -> 2 sections
    app.STATE["paper"] = {"name": "x.pdf", "status": "ready", "md": md, "engine": "pypdf"}

    from fasthtml.common import to_xml
    page = to_xml(app.paper_step())                   # step 1
    dlg = app.STATE["paper"]["dialog"]
    cells = app.STATE["backend"].messages(dlg)
    assert app.STATE["paper"]["step"] == 1
    assert [c.msg_type for c in cells] == ["note", "code"]      # section + reimplement cell
    assert "# A" in cells[0].content and "alpha body" in cells[0].content
    assert cells[1].content == ""                              # empty code cell to fill in
    assert app.STATE["dialog"] == dlg
    assert f'ta-{cells[1].id}' in page                        # code cell opened in edit mode

    app.paper_step()                                  # step 2 → same dialog, next section
    cells = app.STATE["backend"].messages(dlg)
    assert app.STATE["paper"]["step"] == 2
    assert [c.msg_type for c in cells] == ["note", "code", "note", "code"]
    assert "# B" in cells[2].content

    app.paper_step()                                  # nothing left → no new cells
    assert len(app.STATE["backend"].messages(dlg)) == 4
    app.STATE["paper"] = None


def test_paper_import_selection_adds_note_and_code():
    import sidekick.app as app
    app.STATE["paper"] = {"name": "sel.pdf", "status": "ready", "md": "# whole\n\nbig paper",
                          "engine": "pypdf"}
    app.paper_import_selection(text="  just this bit I care about  ")
    dlg = app.STATE["paper"]["dialog"]
    cells = app.STATE["backend"].messages(dlg)
    assert [c.msg_type for c in cells] == ["note", "code"]
    assert cells[0].content == "just this bit I care about"      # trimmed selection
    assert cells[1].content == "" and app.STATE["dialog"] == dlg
    # empty / no-paper selections are no-ops
    app.paper_import_selection(text="   ")
    assert len(app.STATE["backend"].messages(dlg)) == 2
    app.STATE["paper"] = None


def test_dialog_tree_nests_and_drops_empty_segments():
    import sidekick.app as app
    t = app._dialog_tree(["a/b", "a/c", "top"])
    assert sorted(t["folders"]) == ["a"]
    assert [lab for lab, _ in t["folders"]["a"]["leaves"]] == ["b", "c"]
    assert ("top", "top") in t["leaves"]
    # leading/trailing/double slashes must not create blank-named folders/leaves
    t2 = app._dialog_tree(["/x", "y/", "p//q"])
    assert "" not in t2["folders"]
    assert ("x", "/x") in t2["leaves"] and ("y", "y/") in t2["leaves"]
    assert "" not in t2["folders"]["p"]["folders"]      # p//q -> p/q, no empty middle
    assert ("q", "p//q") in t2["folders"]["p"]["leaves"]
    assert app._dialog_tree(["/"])["leaves"] == []      # only-slashes name is skipped


def test_paper_panel_shows_stepper_progress():
    import sidekick.app as app
    from fasthtml.common import to_xml
    md = "# A\n\na\n\n# B\n\nb\n\n# C\n\nc"            # 3 sections
    app.STATE["paper"] = {"name": "p.pdf", "status": "ready", "md": md, "engine": "pypdf", "step": 1}
    h = to_xml(app.PaperPanel())
    assert "/paper/step" in h and "Next section" in h and "1/3" in h
    assert "paper-collapsed" in h and "sidekick_paperhidden" in h   # show/hide text toggle
    app.STATE["paper"]["step"] = 3                    # all consumed
    h = to_xml(app.PaperPanel())
    assert "All 3 sections imported" in h and "/paper/step" not in h
    app.STATE["paper"] = None


def test_paper_open_and_close_routes(monkeypatch, tmp_path):
    import time, sidekick.app as app
    monkeypatch.setenv("SIDEKICK_DATA", str(tmp_path))
    monkeypatch.setattr(app.paperlib, "convert", lambda p: ("# Paper", "pypdf"))
    pdf = tmp_path / "a.pdf"; pdf.write_bytes(b"%PDF-1.4 fake")   # must exist now
    app.paper_open(path=str(pdf))
    for _ in range(100):
        if (app.STATE["paper"] or {}).get("status") == "ready":
            break
        time.sleep(0.02)
    assert app.STATE["paper"]["status"] == "ready" and app.STATE["paper"]["md"] == "# Paper"
    app.paper_close()
    assert app.STATE["paper"] is None


def test_paper_open_accepts_upload(monkeypatch, tmp_path):
    import time, sidekick.app as app
    from starlette.testclient import TestClient
    monkeypatch.setenv("SIDEKICK_DATA", str(tmp_path))
    monkeypatch.setattr(app.paperlib, "convert", lambda p: ("# Uploaded", "pypdf"))
    app.STATE["paper"] = None
    c = TestClient(app.app)
    c.post("/paper/open", files={"pdf": ("mypaper.pdf", b"%PDF-1.4 data", "application/pdf")})
    for _ in range(100):
        if (app.STATE["paper"] or {}).get("status") == "ready":
            break
        time.sleep(0.02)
    assert app.STATE["paper"]["name"] == "mypaper.pdf" and app.STATE["paper"]["md"] == "# Uploaded"
    app.STATE["paper"] = None


def test_reupload_keeps_file_so_markdown_cache_hits(monkeypatch, tmp_path):
    # The same PDF re-uploaded must reuse its conversion: identical content lands
    # at the same path AND isn't rewritten, so the path+mtime cache key is stable.
    import os, io, sidekick.app as app
    monkeypatch.setenv("SIDEKICK_DATA", str(tmp_path))

    class FakeUpload:
        def __init__(self, data): self.filename, self.file = "p.pdf", io.BytesIO(data)

    data = b"%PDF-1.4 identical paper bytes"
    p1, _ = app._save_upload(FakeUpload(data))
    os.utime(p1, ns=(111_111_111, 111_111_111))          # stamp a distinctive mtime
    key1 = app.paperlib.cache_path(Path(p1))

    p2, _ = app._save_upload(FakeUpload(data))            # re-upload identical content
    assert p1 == p2                                       # content-hashed path
    assert os.stat(p2).st_mtime_ns == 111_111_111        # not rewritten -> mtime preserved
    assert app.paperlib.cache_path(Path(p2)) == key1     # so the .md cache key still matches


def test_paper_open_bad_path_reports_not_found():
    import sidekick.app as app
    app.paper_open(path="/no/such/file.pdf")
    assert app.STATE["paper"]["engine"] == "error" and "not found" in app.STATE["paper"]["md"].lower()
    app.STATE["paper"] = None


# ---- import paper into notebook (one note cell per paragraph) ---------------
def test_split_blocks_paragraphs_and_fences():
    from sidekick import paper as pl
    md = "# Title\n\nFirst para.\n\nSecond para\nwraps two lines.\n\n```python\na = 1\n\nb = 2\n```\n\n![](fig.png)"
    blocks = pl.split_blocks(md)
    assert blocks[0] == "# Title"
    assert blocks[1] == "First para."
    assert blocks[2] == "Second para\nwraps two lines."          # multi-line para kept together
    assert "a = 1\n\nb = 2" in blocks[3]                          # blank line inside fence preserved
    assert not any("fig.png" in b for b in blocks)               # figure-only block dropped


def test_paper_import_creates_one_note_per_block(monkeypatch):
    import sidekick.app as app
    app.STATE["paper"] = {"name": "attention.pdf", "status": "ready",
                          "md": "# Attention\n\nThe transformer.\n\n## Heads\n\nMulti-head."}
    app.paper_import()
    dlg = app.STATE["dialog"]
    assert dlg == "paper/attention"
    cells = app.STATE["backend"].messages(dlg)
    assert [c.msg_type for c in cells] == ["note", "note", "note", "note"]
    assert cells[0].content == "# Attention" and cells[2].content == "## Heads"
    assert app.STATE["paper"] is None                            # panel closed after import


def test_paper_import_unique_dialog_name():
    import sidekick.app as app
    b = app.STATE["backend"]
    b.messages("paper/dup")                                      # pre-existing
    assert app._unique_dialog(b, "paper/dup").startswith("paper/dup-")


def test_clean_md_strips_marker_html_noise():
    from sidekick import paper as pl
    dirty = ('# Title <span id="page-2-0"></span>\n\nAuthor<sup>*</sup> and X<sub>i</sub>\n\n'
             r"recurrent [\[7\]](#page-10-1) nets and [link](#page-3-0).")
    clean = pl._clean_md(dirty)
    assert "<span" not in clean and "<sup>" not in clean and "<sub>" not in clean
    assert "#page-" not in clean                      # all dead cross-refs gone
    assert "# Title" in clean and "Author*" in clean and "Xi" in clean
    assert r"\[7\]" in clean and "link" in clean      # citation/link text kept


# ---- insert cell below + section import ------------------------------------
def test_insert_below_and_above_anchor():
    b = MockBackend()
    a = b.add("d/x", "1", "code")
    c = b.add("d/x", "3", "code")
    below = b.insert("d/x", "2", "note", anchor_id=a.id)              # default: below
    assert [x.id for x in b.messages("d/x")] == [a.id, below.id, c.id]
    assert below.msg_type == "note"
    above = b.insert("d/x", "0", "code", anchor_id=a.id, above=True)  # above the anchor
    assert [x.id for x in b.messages("d/x")] == [above.id, a.id, below.id, c.id]


def test_cell_insert_route_inserts_and_opens_editor():
    import sidekick.app as app
    from fasthtml.common import to_xml
    app.STATE["dialog"] = "ins/route"
    bk = app.STATE["backend"]
    bk.messages("ins/route")
    a = bk.add("ins/route", "para", "note")
    html = to_xml(app.cell_insert(id=a.id, msg_type="code"))
    cells = bk.messages("ins/route")
    assert len(cells) == 2 and cells[1].msg_type == "code"
    assert f'id="ta-{cells[1].id}"' in html              # new cell rendered as an editor


def test_split_sections_groups_under_headings():
    from sidekick import paper as pl
    md = "intro line\n\n# A\n\npara a1\n\npara a2\n\n## B\n\npara b1"
    secs = pl.split_sections(md)
    assert len(secs) == 3                                # intro, A(+content), B(+content)
    assert secs[0] == "intro line"
    assert secs[1].startswith("# A") and "para a2" in secs[1]
    assert secs[2].startswith("## B") and "para b1" in secs[2]


def test_paper_import_section_mode_fewer_cells_than_para():
    import sidekick.app as app
    md = "# Title\n\np1\n\np2\n\n## Sub\n\np3\n\np4"
    app.STATE["paper"] = {"name": "x.pdf", "status": "ready", "md": md}
    app.paper_import(mode="section")
    sec_cells = len(app.STATE["backend"].messages(app.STATE["dialog"]))
    app.STATE["paper"] = {"name": "x.pdf", "status": "ready", "md": md}
    app.paper_import(mode="para")
    para_cells = len(app.STATE["backend"].messages(app.STATE["dialog"]))
    assert sec_cells == 2 and para_cells == 6 and sec_cells < para_cells
    app.STATE["paper"] = None


def test_cell_insert_route_above_and_below_with_type():
    import sidekick.app as app
    app.STATE["dialog"] = "ins/ab"
    bk = app.STATE["backend"]
    bk.messages("ins/ab")
    anchor = bk.add("ins/ab", "anchor", "note")
    app.cell_insert(id=anchor.id, msg_type="prompt", where="above")
    app.cell_insert(id=anchor.id, msg_type="note", where="below")
    cells = bk.messages("ins/ab")
    types = [c.msg_type for c in cells]
    assert types == ["prompt", "note", "note"]            # above-prompt, anchor-note, below-note
    assert cells[1].id == anchor.id


def test_cell_insert_route_rejects_bad_type():
    import sidekick.app as app
    app.STATE["dialog"] = "ins/bad"
    bk = app.STATE["backend"]
    bk.messages("ins/bad")
    a = bk.add("ins/bad", "x", "code")
    app.cell_insert(id=a.id, msg_type="evil", where="below")
    assert bk.messages("ins/bad")[1].msg_type == "code"   # bad type falls back to code
