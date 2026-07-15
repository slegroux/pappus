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
    assert ids == ["claude", "claude-cli", "claude-cli-fast", "glm", "codex"]
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


# ---- tunnel lifecycle (fake Popen, no real ssh) -----------------------------
import io as _io
import types as _types


class _FakePopen:
    """Stand-in for the ssh subprocess. `poll` is the exit code (None = running)."""
    def __init__(self, poll=None, stderr=b""):
        self._poll = poll
        self.stderr = _io.BytesIO(stderr)
        self.terminated = self.killed = False
        self.wait_raises = False

    def poll(self): return self._poll
    def terminate(self): self.terminated = True
    def kill(self): self.killed = True

    def wait(self, timeout=None):
        if self.wait_raises:
            raise tunnel.subprocess.TimeoutExpired("ssh", timeout)
        return 0


def _fast_clock(monkeypatch, step=0.2):
    """Swap tunnel.time for an advancing fake so the wait-loop runs with no delay."""
    clock = {"t": 0.0}

    def _time():
        clock["t"] += step
        return clock["t"]
    monkeypatch.setattr(tunnel, "time",
                        _types.SimpleNamespace(time=_time, sleep=lambda *_: None))


def test_open_tunnel_returns_proc_when_port_comes_up(monkeypatch):
    _fast_clock(monkeypatch)
    proc = _FakePopen(poll=None)                        # stays alive
    monkeypatch.setattr(tunnel.subprocess, "Popen", lambda *a, **k: proc)
    seq = iter([False, True])                           # in-use guard False, then live
    monkeypatch.setattr(tunnel, "port_open", lambda *a, **k: next(seq, True))
    assert tunnel.open_tunnel(targets.get_target("h100"), wait=1.0) is proc


def test_open_tunnel_raises_when_ssh_exits_early(monkeypatch):
    _fast_clock(monkeypatch)
    proc = _FakePopen(poll=255, stderr=b"permission denied")   # died immediately
    monkeypatch.setattr(tunnel.subprocess, "Popen", lambda *a, **k: proc)
    monkeypatch.setattr(tunnel, "port_open", lambda *a, **k: False)
    with pytest.raises(RuntimeError, match="exited early"):
        tunnel.open_tunnel(targets.get_target("h100"), wait=1.0)


def test_open_tunnel_times_out_and_terminates(monkeypatch):
    _fast_clock(monkeypatch)
    proc = _FakePopen(poll=None)                        # alive, but port never opens
    monkeypatch.setattr(tunnel.subprocess, "Popen", lambda *a, **k: proc)
    monkeypatch.setattr(tunnel, "port_open", lambda *a, **k: False)
    with pytest.raises(TimeoutError):
        tunnel.open_tunnel(targets.get_target("h100"), wait=0.6)
    assert proc.terminated                              # spawned ssh was cleaned up


def test_open_tunnel_refuses_when_local_port_busy(monkeypatch):
    monkeypatch.setattr(tunnel, "port_open", lambda *a, **k: True)   # already in use
    with pytest.raises(RuntimeError, match="already in use"):
        tunnel.open_tunnel(targets.get_target("h100"))


def test_close_tunnel_escalates_to_kill_on_timeout():
    proc = _FakePopen(); proc.wait_raises = True
    tunnel.close_tunnel(proc)
    assert proc.terminated and proc.killed


def test_close_tunnel_terminates_cleanly():
    proc = _FakePopen()
    tunnel.close_tunnel(proc)
    assert proc.terminated and not proc.killed


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


def test_send_scrolls_to_the_new_cell():
    # /send full-reloads the page (returns Page()), which would jump to the top;
    # it flags the new cell so Stream() scrolls it into view instead.
    import sidekick.app as app
    from fasthtml.common import to_xml
    app.STATE["dialog"] = "test/scroll"
    bk = app.STATE["backend"]; bk.messages("test/scroll")
    page = to_xml(app.send(content="a note", msg_type="note"))   # returns Page() (full reload)
    new_id = bk.messages("test/scroll")[-1].id
    assert f"cell-{new_id}" in page and "scrollHeight" in page    # scrolls the stream to the new cell
    assert app.STATE.get("scroll_to") is None                     # one-shot (consumed by render)


def test_heading_level_detects_section_headers():
    import sidekick.app as app
    assert app._heading_level("# Title\ntext") == 1
    assert app._heading_level("\n\n### Method\nbody") == 3       # leading blanks ignored
    assert app._heading_level("just prose, no heading") == 0
    assert app._heading_level("text then\n## later heading") == 0  # heading must be first
    assert app._heading_level("####### too deep") == 0           # >6 isn't a heading
    assert app._heading_level("") == 0


def test_section_header_note_renders_collapse_caret():
    import sidekick.app as app
    from fasthtml.common import to_xml
    from sidekick.client import Msg
    h = to_xml(app.MsgRow(Msg(id="_s", msg_type="note", content="## Method\n\ndetails")))
    assert "sec-caret" in h and 'data-sec="_s"' in h and 'data-sec-level="2"' in h
    plain = to_xml(app.MsgRow(Msg(id="_p", msg_type="note", content="just a note")))
    assert "sec-caret" not in plain                              # non-heading note → no caret
    code = to_xml(app.MsgRow(Msg(id="_c", msg_type="code", content="# not markdown\nx=1")))
    assert "sec-caret" not in code                              # a code comment isn't a section


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


def test_answer_save_route_edits_answer_without_touching_question():
    """The AI answer is editable in place (SolveIt's 'n'): saving the answer keeps
    the prompt's question and only rewrites its output — no re-ask, no wipe."""
    import sidekick.app as app
    app.STATE["dialog"] = "ans/save"
    b = app.STATE["backend"]
    b.messages("ans/save")
    m = b.add("ans/save", "what is 2+2?", "prompt", model="claude-cli")
    b.exec("ans/save", m.id)                      # gives it an answer to edit
    app.cell_answer_save(id=m.id, output="It is 4. (edited)")
    saved = b.messages("ans/save")[-1]
    assert saved.content == "what is 2+2?"        # question untouched
    assert saved.output == "It is 4. (edited)"    # answer rewritten in place


def test_answer_edit_route_returns_editor_with_output():
    import sidekick.app as app
    from fasthtml.common import to_xml
    app.STATE["dialog"] = "ans/edit"
    b = app.STATE["backend"]
    b.messages("ans/edit")
    m = b.add("ans/edit", "q?", "prompt", model="claude-cli")
    b.update_output("ans/edit", m.id, "the answer text")
    html = to_xml(app.cell_answer_edit(id=m.id))
    assert "<textarea" in html and "the answer text" in html and "Save" in html


def test_update_does_not_offer_answer_edit_for_non_prompt():
    """A code/note cell has no AI answer to edit; the answer routes no-op for them."""
    import sidekick.app as app
    app.STATE["dialog"] = "ans/guard"
    b = app.STATE["backend"]
    b.messages("ans/guard")
    m = b.add("ans/guard", "x = 1", "code")
    m.output = "stale"
    app.cell_answer_save(id=m.id, output="hacked")
    assert b.messages("ans/guard")[-1].output == "stale"   # unchanged for non-prompt


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
                is_error=False, stdout=None, usage=None, cost=None):
    """Build a fake _run_cli that records the argv/env and returns a JSON result.

    `usage`/`cost` mirror the `usage` block and `total_cost_usd` the real CLI emits
    on its terminal result, so tests can exercise the running-cost accrual."""
    import json as _json
    from types import SimpleNamespace

    def run(cmd, cwd, env, timeout):
        capture.append({"cmd": cmd, "cwd": cwd, "env": env, "timeout": timeout})
        obj = {"result": result, "session_id": session_id, "is_error": is_error}
        if usage is not None:
            obj["usage"] = usage
        if cost is not None:
            obj["total_cost_usd"] = cost
        body = _json.dumps(obj)
        return SimpleNamespace(returncode=returncode,
                               stdout=body if stdout is None else stdout, stderr="")
    return run


def _mk_cli_popen(capture, deltas, session_id="sid", is_error=False,
                  usage=None, cost=None):
    """Build a fake _popen whose stdout emits stream-json lines for `deltas`.

    `usage`/`cost` ride on the terminal `result` event, as the real CLI emits
    them, so tests can exercise the running-cost accrual on the streaming path."""
    import json as _json
    from types import SimpleNamespace

    def popen(cmd, cwd, env):
        capture.append({"cmd": cmd, "cwd": cwd, "env": env})
        lines = []
        for d in deltas:
            lines.append(_json.dumps({"type": "stream_event",
                "event": {"type": "content_block_delta",
                          "delta": {"type": "text_delta", "text": d}}}) + "\n")
        result_ev = {"type": "result", "session_id": session_id,
                     "is_error": is_error, "result": "".join(deltas)}
        if usage is not None:
            result_ev["usage"] = usage
        if cost is not None:
            result_ev["total_cost_usd"] = cost
        lines.append(_json.dumps(result_ev) + "\n")
        # stream() reads via iter(stdout.readline, "") — return "" at EOF.
        it = iter(lines)

        def readline():
            return next(it, "")
        return SimpleNamespace(stdout=SimpleNamespace(readline=readline),
                               wait=lambda timeout=None: 0)
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
    assert cc.CLI_SESSIONS["cli/d1"] == {"id": "sid-1", "sent": "<code>x=1</code>", "mode": None}


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
    assert cc.CLI_SESSIONS["cli/d3"] == {"id": "sid-new", "sent": edited, "mode": None}


# ---- AI modes (learning / concise / standard personas) ---------------------
def test_system_prompt_carries_mode_directive():
    import sidekick.claude_cli as cc
    for mode, needle in (("learning", "MODE — Learning"),
                         ("concise", "MODE — Concise"),
                         ("standard", "MODE — Standard")):
        s = cc.system("<code>x=1</code>", mode)
        assert needle in s and "<code>x=1</code>" in s        # mode + context both present


def test_system_prompt_defaults_to_learning():
    import sidekick.claude_cli as cc
    assert "MODE — Learning" in cc.system("", None)           # unset -> learning
    assert "MODE — Learning" in cc.system("", "bogus")        # unknown -> learning


def test_claude_cli_threads_mode_into_system_prompt(monkeypatch):
    import sidekick.claude_cli as cc
    cc.CLI_SESSIONS.pop("cli/mode1", None)
    monkeypatch.setattr(cc, "claude_bin", lambda: "/bin/claude")
    cap = []
    monkeypatch.setattr(cc, "_run_cli", _mk_cli_run(cap, result="ok", session_id="sid-m"))
    cc.call("cli/mode1", "q", context="<code>x=1</code>", mode="concise")
    cmd = cap[-1]["cmd"]
    sys_arg = cmd[cmd.index("--append-system-prompt") + 1]
    assert "MODE — Concise" in sys_arg                        # the chosen persona shipped
    assert cc.CLI_SESSIONS["cli/mode1"]["mode"] == "concise"  # recorded for resume checks


def test_mode_change_forces_a_fresh_session(monkeypatch):
    import sidekick.claude_cli as cc
    # A resumable session (context stays a prefix) but recorded under a different mode.
    cc.CLI_SESSIONS["cli/mode2"] = {"id": "sid-old", "sent": "<code>x=1</code>",
                                    "mode": "learning"}
    monkeypatch.setattr(cc, "claude_bin", lambda: "/bin/claude")
    cap = []
    monkeypatch.setattr(cc, "_run_cli", _mk_cli_run(cap, result="ok", session_id="sid-2"))
    cc.call("cli/mode2", "q", context="<code>x=1</code>\n<code>y=2</code>", mode="standard")
    cmd = cap[-1]["cmd"]
    assert "--session-id" in cmd and "--resume" not in cmd    # switch -> fresh, not resumed
    assert "MODE — Standard" in cmd[cmd.index("--append-system-prompt") + 1]


def test_run_prompt_passes_mode_to_api_caller(monkeypatch):
    # Non-CLI (API) models get the mode through run_prompt -> caller -> _system.
    import server.kernel_server as ks
    import sidekick.secrets_store as ss
    seen = {}

    def fake_caller(key, content, context, mode):
        seen["mode"] = mode
        return "ok"

    monkeypatch.setattr(ks, "CALLERS", {"claude": fake_caller})
    monkeypatch.setattr(ks, "SDK_MODULE", {"claude": "json"})   # a module that imports fine
    monkeypatch.setattr(ss, "key_for_model", lambda m: "key")   # run_prompt re-imports this
    monkeypatch.setattr(ss, "PROVIDERS", {"claude": ("Claude", "ANTHROPIC_API_KEY")})
    out = ks.run_prompt("d", "q", "claude", context="c", mode="concise")
    assert out == "ok" and seen["mode"] == "concise"


def test_send_route_stores_ai_mode_on_prompt():
    import sidekick.app as app
    app.STATE["dialog"] = "mode/route"
    app.STATE["backend"].messages("mode/route")
    app.send(content="hello?", msg_type="prompt", model="claude-cli", ai_mode="concise")
    assert app.STATE["ai_mode"] == "concise"                 # sticky selection
    assert app.STATE["backend"].messages("mode/route")[-1].ai_mode == "concise"


def test_mode_select_renders_current_selection():
    import sidekick.app as app
    from fasthtml.common import to_xml
    app.STATE["ai_mode"] = "standard"
    html = to_xml(app.ModeSelect())
    assert 'name="ai_mode"' in html and 'value="standard" selected' in html
    app.STATE["ai_mode"] = app.DEFAULT_MODE                  # restore default for other tests


# ---- message-ID links (#_msgid anchors) ------------------------------------
def test_linkify_same_dialog_msgid():
    import sidekick.app as app
    out = app._linkify_msgids("see #_deadbeef now")
    assert 'class="msglink"' in out and 'data-mid="_deadbeef"' in out
    assert 'href="#cell-_deadbeef"' in out and "xdlg" not in out   # scrolls, no navigation


def test_linkify_cross_dialog_msgid():
    import sidekick.app as app
    out = app._linkify_msgids("compare #activation/snake/_abc12345 here")
    assert 'class="msglink xdlg"' in out                            # navigates to the dialog
    assert 'href="/open?dialog=activation/snake#_abc12345"' in out


def test_linkify_skips_code_blocks():
    import sidekick.app as app
    out = app._linkify_msgids('<p>#_aaaa1111</p><pre>x = "#_bbbb2222"</pre>')
    assert 'href="#cell-_aaaa1111"' in out                          # prose linked
    assert '<pre>x = "#_bbbb2222"</pre>' in out                     # code left verbatim


def test_linkify_skips_existing_href_attributes():
    import sidekick.app as app
    out = app._linkify_msgids('<a href="#_cccc3333">x</a> bare #_dddd4444')
    assert '<a href="#_cccc3333">x</a>' in out                      # attribute untouched
    assert 'data-mid="_dddd4444"' in out                            # bare text linked


def test_linkify_noop_without_hash():
    import sidekick.app as app
    assert app._linkify_msgids("plain prose, no anchors") == "plain prose, no anchors"


def test_render_md_autolinks_msgid():
    import sidekick.app as app
    if app._md is None:
        import pytest
        pytest.skip("mistune not installed")
    assert 'class="msglink"' in str(app.render_md("See #_deadbeef for the setup"))


def test_markdown_link_to_msgid_is_upgraded():
    # A standard markdown link [label](#_id) becomes a msglink, keeping the label.
    import sidekick.app as app
    out = app._upgrade_msgid_anchors('<a href="#_a321d2a1">the computation</a>')
    assert 'class="msglink"' in out and 'data-mid="_a321d2a1"' in out
    assert ">the computation</a>" in out                    # author's label preserved


def test_markdown_link_cross_dialog_is_upgraded():
    import sidekick.app as app
    out = app._upgrade_msgid_anchors('<a href="#folder/dlg/_b2c3d4e5">x</a>')
    assert 'class="msglink xdlg"' in out
    assert 'href="/open?dialog=folder/dlg#_b2c3d4e5"' in out


def test_upgrade_leaves_ordinary_anchors_alone():
    # External links and heading anchors must NOT be turned into msglinks.
    import sidekick.app as app
    out = app._upgrade_msgid_anchors(
        '<a href="https://example.com">x</a> <a href="#section-title">y</a>')
    assert "msglink" not in out


def test_render_md_upgrades_markdown_msgid_link():
    import sidekick.app as app
    if app._md is None:
        import pytest
        pytest.skip("mistune not installed")
    out = str(app.render_md("Back to [the setup](#_deadbeef) now"))
    assert 'class="msglink"' in out and ">the setup</a>" in out


def test_link_button_copies_msgid_anchor():
    # SolveIt's 🔗 "Copy URL anchor" — the button copies the #_msgid string you
    # paste inline to link here, not a full URL.
    import sidekick.app as app
    from fasthtml.common import to_xml
    from sidekick.client import Msg
    html = to_xml(app._link_btn(Msg(id="_ff00ff00", msg_type="code", content="x=1")))
    assert 'data-anchor="#_ff00ff00"' in html
    assert "_copyMsgLink(this)" in html


def test_msgrow_includes_link_button():
    import sidekick.app as app
    from fasthtml.common import to_xml
    from sidekick.client import Msg
    app.STATE["dialog"] = "d"
    html = to_xml(app.MsgRow(Msg(id="_1234abcd", msg_type="note", content="hi"), num=1))
    assert 'cls="cell-btn link"' in html or 'class="cell-btn link"' in html


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
    assert cc.CLI_SESSIONS["cli/s1"] == {"id": "sid-s1", "sent": "<code>x=1</code>", "mode": None}


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


def test_claude_cli_call_accrues_running_cost(monkeypatch):
    import sidekick.claude_cli as cc
    cc.CLI_SESSIONS.pop("cli/c1", None)
    cc.CLI_COST.pop("cli/c1", None)
    monkeypatch.setattr(cc, "claude_bin", lambda: "/bin/claude")
    usage = {"input_tokens": 300, "output_tokens": 600,
             "cache_read_input_tokens": 20000, "cache_creation_input_tokens": 2000}
    monkeypatch.setattr(cc, "_run_cli",
                        _mk_cli_run([], usage=usage, cost=0.012, session_id="s"))

    cc.call("cli/c1", "q1", context="<code>x=1</code>")
    cc.call("cli/c1", "q2", context="<code>x=1</code>\n<code>y=2</code>")
    agg = cc.cost_for("cli/c1")
    assert agg["turns"] == 2
    assert agg["usd"] == pytest.approx(0.024)              # summed across turns
    assert agg["input"] == 600 and agg["output"] == 1200
    assert agg["cache_read"] == 40000 and agg["cache_write"] == 4000


def test_claude_cli_stream_accrues_cost_and_skips_errors(monkeypatch):
    import sidekick.claude_cli as cc
    cc.CLI_SESSIONS.pop("cli/c2", None)
    cc.CLI_COST.pop("cli/c2", None)
    monkeypatch.setattr(cc, "claude_bin", lambda: "/bin/claude")
    usage = {"input_tokens": 100, "output_tokens": 200, "cache_read_input_tokens": 5000}
    monkeypatch.setattr(cc, "_popen",
                        _mk_cli_popen([], ["hi"], usage=usage, cost=0.003))
    list(cc.stream("cli/c2", "q", context="<code>x=1</code>"))
    agg = cc.cost_for("cli/c2")
    assert agg["turns"] == 1 and agg["usd"] == pytest.approx(0.003)
    assert agg["input"] == 100 and agg["cache_read"] == 5000

    # an errored turn must not accrue (we'd be billing for a failure)
    monkeypatch.setattr(cc, "_popen",
                        _mk_cli_popen([], [], is_error=True, usage=usage, cost=0.003))
    list(cc.stream("cli/c2", "q2", context="<code>x=1</code>\n<code>z=3</code>"))
    assert cc.cost_for("cli/c2")["turns"] == 1               # unchanged


def test_ctx_meter_shows_running_cost(monkeypatch):
    import sidekick.app as app
    import sidekick.claude_cli as cc
    from sidekick.client import Msg
    cc.CLI_COST["cli/m1"] = {"usd": 0.0123, "turns": 3, "input": 900,
                             "output": 1700, "cache_read": 60000, "cache_write": 5000}
    msgs = [Msg(id="_a", msg_type="prompt", content="hi", output="there")]
    text = app._ctx_meter_text(msgs, "cli/m1")
    assert "$0.01 this session · 3 turns" in text            # reported dollar figure
    assert "AI context" in text                              # still shows context size

    # subscription that reports no dollars -> token-based estimate, marked with ~
    cc.CLI_COST["cli/m2"] = {"usd": 0.0, "turns": 1, "input": 1000, "output": 2000,
                             "cache_read": 0, "cache_write": 0}
    assert "~$" in app._ctx_meter_text(msgs, "cli/m2")


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


def test_run_code_concurrent_same_dialog_no_lost_updates():
    """Threads exec'ing into ONE dialog are serialized by the per-dialog lock, so a
    read-modify-write spanning statements never loses an increment. The sleep
    between read and write releases the GIL to widen the race window — without the
    lock the final count would fall short of N."""
    import threading
    import server.kernel_server as ks
    ks.run_code("conc/same", "acc = 0")
    N = 20

    def bump():
        ks.run_code("conc/same", "_t = acc\nimport time\ntime.sleep(0.001)\nacc = _t + 1")

    threads = [threading.Thread(target=bump) for _ in range(N)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    text, _ = ks.run_code("conc/same", "acc")
    assert text == str(N)                               # every increment landed


def test_run_code_concurrent_different_dialogs_stay_isolated():
    """Concurrent execs in different dialogs keep separate namespaces — one
    dialog's variable never bleeds into another's."""
    import threading
    import server.kernel_server as ks
    errors: list = []

    def work(dialog, val):
        for _ in range(20):
            ks.run_code(dialog, f"who = {val!r}")
            text, _ = ks.run_code(dialog, "who")
            if text != repr(val):
                errors.append((dialog, text))

    ta = threading.Thread(target=work, args=("conc/a", "A"))
    tb = threading.Thread(target=work, args=("conc/b", "B"))
    ta.start()
    tb.start()
    ta.join()
    tb.join()
    assert errors == []


# ---- variable / expression injection ($`expr` in prompts) ------------------
def test_inject_vars_evaluates_against_namespace():
    import server.kernel_server as ks
    ks.run_code("inj/ns", "x = 41\nrows = [1, 2, 3]")
    out, warns = ks.inject_vars("inj/ns", "x+1 is $`x + 1`, $`len(rows)` rows")
    assert out == "x+1 is 42, 3 rows" and warns == []


def test_inject_vars_missing_name_is_marked_not_fatal():
    import server.kernel_server as ks
    ks.run_code("inj/miss", "a = 1")
    out, warns = ks.inject_vars("inj/miss", "value $`nope`")
    assert "[unresolved `nope`: NameError]" in out         # visible, prompt not aborted
    assert warns and "NameError" in warns[0]


def test_inject_vars_passthrough_without_marker():
    import server.kernel_server as ks
    out, warns = ks.inject_vars("inj/none", "plain text, cost is $5")
    assert out == "plain text, cost is $5" and warns == []  # lone $ is left alone


def test_inject_vars_truncates_oversized_value():
    import server.kernel_server as ks
    ks.run_code("inj/big", "blob = 'z' * 10000")
    out, _ = ks.inject_vars("inj/big", "$`blob`")
    assert len(out) < 10000 and "truncated" in out


def test_resolve_injections_noop_without_eval_backend():
    # Mock backend has no kernel namespace -> $`…` is left literal, never crashes.
    import sidekick.app as app
    b = MockBackend()
    assert app._resolve_injections(b, "d", "keep $`x` literal") == "keep $`x` literal"


def test_eval_exprs_backend_resolves_via_server(monkeypatch):
    # The app's streaming path resolves through the kernel backend's /eval call.
    b = HttpKernelBackend.__new__(HttpKernelBackend)
    b._post = lambda path, body: {"content": "got 7", "warnings": []}
    resolved, warns = b.eval_exprs("d", "got $`3 + 4`")
    assert resolved == "got 7" and warns == []


def test_inject_vars_broken_str_degrades_to_marker():
    # A value whose __repr__/__str__ raises must not crash the whole prompt.
    import server.kernel_server as ks
    ks.run_code("inj/broken",
                "class Boom:\n"
                "    def __repr__(self): raise ValueError('nope')\n"
                "b = Boom()")
    out, warns = ks.inject_vars("inj/broken", "value $`b` here")
    assert "[unresolved `b`: ValueError]" in out           # marker, not an exception
    assert warns and "ValueError" in warns[0]


def test_eval_exprs_falls_back_on_server_error():
    # If the /eval round-trip throws, the prompt is sent as-is — never blocked.
    b = HttpKernelBackend.__new__(HttpKernelBackend)
    def boom(path, body): raise ConnectionError("kernel down")
    b._post = boom
    resolved, warns = b.eval_exprs("d", "keep $`x`")
    assert resolved == "keep $`x`" and warns == []


def test_stream_path_resolves_injection_before_reaching_ai(monkeypatch):
    """End-to-end for the real streaming path: /stream must send the AI the
    *resolved* prompt (kernel values filled in), not the raw $`…` source."""
    import asyncio
    import sidekick.app as app

    # A kernel backend whose /eval simulates the server resolving against a live
    # namespace (x -> 42); everything else is the genuine _InMemoryBackend.
    b = HttpKernelBackend.__new__(HttpKernelBackend)
    b._dialogs = {}
    b._post = lambda path, body: {"content": body["content"].replace("$`x`", "42"),
                                  "warnings": []} if path == "/eval" else {}
    m = b.add("inj/stream", "what is $`x`?", "prompt", model="claude-cli")

    seen = {}
    def fake_stream(dialog, content, context, model=None, mode=None):
        seen["content"] = content                          # capture what the AI receives
        return iter(())                                    # no tokens
    monkeypatch.setattr(app, "stream_claude", fake_stream)
    monkeypatch.setitem(app.STATE, "backend", b)

    resp = app.stream_answer("inj/stream", m.id)
    async def drain():
        async for _ in resp.body_iterator:
            pass
    asyncio.run(drain())

    assert seen.get("content") == "what is 42?"            # resolved, not "$`x`"
    assert m.content == "what is $`x`?"                    # stored source stays raw


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


def test_run_code_captures_repr_svg_as_rich():
    import server.kernel_server as ks
    code = ("class V:\n"
            "    def _repr_svg_(self): return '<svg><rect/></svg>'\n"
            "V()")
    text, rich = ks.run_code("rich/svg", code)
    assert text == ""                                   # rich repr replaces the text repr
    assert any(r["type"] == "image/svg+xml" and "<svg" in r["data"] for r in rich)


def test_rich_view_renders_svg_inline():
    import sidekick.app as app
    from fasthtml.common import to_xml
    svg = to_xml(app._rich_view({"type": "image/svg+xml", "data": "<svg><rect/></svg>"}))
    assert "cell-svg" in svg and "<svg>" in svg     # inlined, not wrapped in an <img>


def test_conv_arch_emits_inline_svg():
    pytest.importorskip("matplotlib")
    from sidekick.conv_arch import conv_arch, PRESETS
    out = conv_arch(PRESETS["oobleck"], title="Oobleck VAE")  # fmt="svg" default
    svg = out._repr_svg_()
    assert svg.startswith("<svg") and "<?xml" not in svg[:20]


def test_spatial_label_formats_hxw_and_area():
    from sidekick.conv_arch import _spatial
    assert _spatial((112, 112)) == (12544, "112×112")   # explicit dims -> readable
    assert _spatial(12544) == (12544, "12,544")          # scalar area -> comma form
    assert _spatial((65536,)) == (65536, "65,536")       # 1-D spatial


def test_conv_arch_renders_hxw_dims_in_svg():
    pytest.importorskip("matplotlib")
    from sidekick.conv_arch import conv_arch, PRESETS
    svg = conv_arch(PRESETS["resnet"], title="ResNet-50")._repr_svg_()
    assert "112" in svg and "×" in svg                   # H×W dims reach the label layer


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
    monkeypatch.setattr(app, "stream_claude", lambda d, c, ctx, model=None, mode=None: iter(["4", "2"]))

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


# ---- copy a cell across dialogs ---------------------------------------------
def test_copy_cell_clones_into_target_and_leaves_source():
    b = MockBackend()
    src = b.add("d/src", "print(1)", "code")
    src.rich = [{"type": "image/png", "data": "AAAA"}]
    b.messages("d/dst")                                   # target exists, empty
    new = b.copy_cell("d/src", src.id, "d/dst")
    # source is untouched...
    assert [m.id for m in b.messages("d/src")] == [src.id]
    # ...and the clone is appended to the target with a fresh, independent identity
    assert [m.id for m in b.messages("d/dst")] == [new.id]
    assert new.id != src.id and new.content == src.content
    assert new.rich == src.rich and new.rich is not src.rich   # not the same list object


def test_copy_cell_missing_source_returns_none():
    b = MockBackend()
    assert b.copy_cell("d/src", "_nope", "d/dst") is None
    assert b.list_dialogs() == ["demo/welcome"]           # no target dialog conjured


def test_cell_copy_route_copies_and_flashes():
    import sidekick.app as app
    from fasthtml.common import to_xml
    app.STATE["dialog"] = "cell/copy-src"
    b = app.STATE["backend"]
    m = b.add("cell/copy-src", "x = 1", "code")
    b.messages("cell/copy-dst")
    html = to_xml(app.cell_copy(id=m.id, target="cell/copy-dst"))
    dst = b.messages("cell/copy-dst")
    assert len(dst) == 1 and dst[0].content == "x = 1" and dst[0].id != m.id
    # the route re-renders the (unchanged) current dialog with a one-shot flash banner
    assert 'class="flash"' in html and "cell/copy-dst" in html


def test_cell_copy_route_ignores_self_target():
    import sidekick.app as app
    from fasthtml.common import to_xml
    app.STATE["dialog"] = "cell/copy-self"
    b = app.STATE["backend"]
    m = b.add("cell/copy-self", "y = 2", "code")
    app.STATE.pop("flash", None)
    html = to_xml(app.cell_copy(id=m.id, target="cell/copy-self"))  # onto itself: no-op
    assert [c.id for c in b.messages("cell/copy-self")] == [m.id]
    assert 'class="flash"' not in html


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


def test_split_sections_accepts_precomputed_blocks():
    from sidekick import paper as pl
    md = "# A\n\na1\n\n## B\n\nb1\n\nb2\n\n# C\n\nc1"
    blocks = pl.split_blocks(md)
    assert pl.split_sections(md, blocks=blocks) == pl.split_sections(md)


def test_paper_split_is_memoized_on_state(monkeypatch):
    import sidekick.app as app
    calls = {"blocks": 0, "sections": 0}
    real_b, real_s = app.paperlib.split_blocks, app.paperlib.split_sections
    monkeypatch.setattr(app.paperlib, "split_blocks",
                        lambda md: (calls.__setitem__("blocks", calls["blocks"] + 1) or real_b(md)))
    monkeypatch.setattr(app.paperlib, "split_sections",
                        lambda md, blocks=None: (calls.__setitem__("sections", calls["sections"] + 1)
                                                 or real_s(md, blocks)))
    app.STATE["paper"] = {"name": "p.pdf", "status": "ready",
                          "md": "# A\n\na\n\n# B\n\nb", "engine": "pypdf"}
    from fasthtml.common import to_xml
    to_xml(app.PaperPanel()); to_xml(app.PaperPanel()); to_xml(app.PaperPanel())   # 3 renders
    assert calls["blocks"] == 1 and calls["sections"] == 1      # split once, not per render
    app.STATE["paper"] = None


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


def test_arxiv_url_rewrites_to_pdf():
    from sidekick import paper as pl
    assert pl._arxiv_pdf("https://arxiv.org/abs/2305.18247") == "https://arxiv.org/pdf/2305.18247"
    assert pl._arxiv_pdf("https://arxiv.org/abs/2305.18247v2") == "https://arxiv.org/pdf/2305.18247v2"
    assert pl._arxiv_pdf("https://arxiv.org/pdf/2305.18247.pdf") == "https://arxiv.org/pdf/2305.18247"
    assert pl._arxiv_pdf("https://example.com/post") is None        # not arXiv


def test_normalize_url_adds_scheme_to_bare_hosts():
    from sidekick import paper as pl
    # A bare host (what users actually paste) gets https:// — this is the whole
    # reason the URL box works without typing the scheme.
    assert pl.normalize_url("arxiv.org/abs/1706.03762") == "https://arxiv.org/abs/1706.03762"
    assert pl.normalize_url("example.com") == "https://example.com"
    assert pl.normalize_url("www.example.com/p") == "https://www.example.com/p"
    assert pl.normalize_url("  example.com  ") == "https://example.com"   # trimmed
    # An explicit scheme is left untouched (ftp:// still reaches the SSRF guard).
    assert pl.normalize_url("http://x.com") == "http://x.com"
    assert pl.normalize_url("https://y.com") == "https://y.com"
    assert pl.normalize_url("ftp://z.com") == "ftp://z.com"
    assert pl.normalize_url("") == ""


def test_convert_url_normalizes_bare_host(monkeypatch, tmp_path):
    # A scheme-less URL must reach _fetch as https://… (previously it was refused
    # by the SSRF guard as scheme "(none)").
    monkeypatch.setenv("SIDEKICK_DATA", str(tmp_path))
    from sidekick import paper as pl
    seen = {}
    monkeypatch.setattr(pl, "_fetch",
                        lambda u: (seen.setdefault("url", u), (b"<h1>Hi</h1>", "text/html"))[1])
    monkeypatch.setattr(pl, "_extract_article", lambda html, url: ("# Hi", "bs4"))
    md, engine = pl.convert_url("example.com/post")
    assert seen["url"] == "https://example.com/post"
    assert md.strip() == "# Hi"


def test_fetch_guard_rejects_nonhttp_and_private_hosts(monkeypatch):
    from sidekick import paper as pl
    import pytest as _pt
    # non-http(s) scheme (file:// local-file read) — refused before any I/O
    with _pt.raises(pl._BlockedURLError):
        pl._check_url_allowed("file:///etc/passwd")
    # loopback and cloud-metadata link-local (numeric IPs → no DNS needed)
    with _pt.raises(pl._BlockedURLError):
        pl._check_url_allowed("http://127.0.0.1/internal")
    with _pt.raises(pl._BlockedURLError):
        pl._check_url_allowed("http://169.254.169.254/latest/meta-data/")
    # _fetch itself must refuse before opening a connection
    with _pt.raises(pl._BlockedURLError):
        pl._fetch("file:///etc/passwd")


def test_fetch_guard_allows_public_host_and_honors_escape_hatch(monkeypatch):
    from sidekick import paper as pl
    # a host that resolves to a public IP passes (getaddrinfo mocked — no live network)
    monkeypatch.setattr(pl.socket, "getaddrinfo",
                        lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 443))])
    pl._check_url_allowed("https://example.com/paper.pdf")          # does not raise
    # the escape hatch permits private hosts (legitimate intranet papers)
    monkeypatch.setenv("SIDEKICK_ALLOW_PRIVATE_URLS", "1")
    pl._check_url_allowed("http://127.0.0.1/intranet")              # allowed now


def test_convert_url_html_is_article_extracted_and_cached(monkeypatch, tmp_path):
    monkeypatch.setenv("SIDEKICK_DATA", str(tmp_path))
    from sidekick import paper as pl
    monkeypatch.setattr(pl, "_fetch", lambda u: (b"<html><body><h1>Post</h1></body></html>", "text/html"))
    monkeypatch.setattr(pl, "_extract_article", lambda html, url: ("# Post\n\nbody", "trafilatura"))
    md, engine = pl.convert_url("https://blog.example.com/post")
    assert engine == "trafilatura" and "Post" in md
    monkeypatch.setattr(pl, "_fetch", lambda u: (_ for _ in ()).throw(AssertionError("refetched!")))
    md2, engine2 = pl.convert_url("https://blog.example.com/post")     # cache hit, no refetch
    assert engine2 == "cache" and md2 == md


def test_convert_url_pdf_routes_to_pdf_pipeline(monkeypatch, tmp_path):
    # An arXiv (or any application/pdf) URL must go through the PDF pipeline, not
    # HTML article extraction — so equations/tables survive.
    monkeypatch.setenv("SIDEKICK_DATA", str(tmp_path))
    from sidekick import paper as pl
    seen = {}
    monkeypatch.setattr(pl, "_fetch", lambda u: (seen.setdefault("url", u), (b"%PDF-1.4 ...", "application/pdf"))[1])
    monkeypatch.setattr(pl, "convert", lambda path: ("# Paper\n\n$E=mc^2$", "marker"))
    md, engine = pl.convert_url("https://arxiv.org/abs/2305.18247")
    assert engine == "marker" and "E=mc^2" in md
    assert seen["url"] == "https://arxiv.org/pdf/2305.18247"          # fetched the PDF, not /abs/


def test_convert_url_empty_not_cached(monkeypatch, tmp_path):
    monkeypatch.setenv("SIDEKICK_DATA", str(tmp_path))
    from sidekick import paper as pl
    monkeypatch.setattr(pl, "_fetch", lambda u: (b"<html></html>", "text/html"))
    monkeypatch.setattr(pl, "_extract_article", lambda html, url: ("   ", "bs4"))
    pl.convert_url("https://empty.example.com")
    assert not pl._url_cache_path("https://empty.example.com").exists()


def test_url_name_is_readable():
    import sidekick.app as app
    assert app._url_name("https://www.fast.ai/posts/2025-11-07-solveit.html") == "2025-11-07-solveit"
    assert app._url_name("https://arxiv.org/abs/2305.18247") == "2305.18247"   # arXiv id intact
    assert app._url_name("https://example.com/paper.pdf") == "paper"
    assert app._url_name("https://example.com/") == "example.com"
    assert app._url_name("https://example.com") == "example.com"


def test_paper_open_route_accepts_url(monkeypatch):
    import time, sidekick.app as app
    app.STATE["paper"] = None
    monkeypatch.setattr(app.paperlib, "convert_url", lambda u: ("# Web\n\npara", "trafilatura"))
    app.paper_open(url="https://blog.example.com/great-post")
    for _ in range(100):
        if (app.STATE["paper"] or {}).get("status") == "ready":
            break
        time.sleep(0.02)
    assert app.STATE["paper"]["md"] == "# Web\n\npara"
    assert app.STATE["paper"]["name"] == "great-post" and app.STATE["paper"]["source"].endswith("great-post")
    app.STATE["paper"] = None


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


def test_cell_export_route_toggles_directive_and_keeps_output():
    import sidekick.app as app
    app.STATE["dialog"] = "cell/exp"
    b = app.STATE["backend"]; b.messages("cell/exp")
    m = b.add("cell/exp", "def f(): return 1", "code"); m.output = "ran"
    app.cell_export(id=m.id)
    got = b.messages("cell/exp")[0]
    assert app.export.has_export(got.content)
    assert got.output == "ran"                          # in-memory toggle must NOT clear output
    app.cell_export(id=m.id)
    assert not app.export.has_export(b.messages("cell/exp")[0].content)


def test_cell_export_route_ignores_non_code():
    import sidekick.app as app
    app.STATE["dialog"] = "cell/exp2"
    b = app.STATE["backend"]; b.messages("cell/exp2")
    m = b.add("cell/exp2", "# just a note", "note")
    app.cell_export(id=m.id)
    assert b.messages("cell/exp2")[0].content == "# just a note"   # unchanged


def test_paper_step_noop_when_not_ready_or_empty():
    import sidekick.app as app
    app.STATE["paper"] = {"name": "x.pdf", "status": "converting"}
    app.paper_step()
    assert "dialog" not in app.STATE["paper"]            # nothing imported while converting
    app.STATE["paper"] = {"name": "x.pdf", "status": "ready", "md": "   "}
    app.paper_step()
    assert "dialog" not in app.STATE["paper"]            # nothing imported from empty md
    app.STATE["paper"] = None


def test_paper_toggle_only_shows_when_a_paper_is_loaded():
    import sidekick.app as app
    from fasthtml.common import to_xml
    app.STATE["paper"] = None
    assert 'id="tgl-paper"' not in to_xml(app.Page())          # no paper -> no 📖 toggle
    app.STATE["paper"] = {"name": "p.pdf", "status": "ready", "md": "# A\n\nx", "engine": "pypdf"}
    assert 'id="tgl-paper"' in to_xml(app.Page())              # paper loaded -> toggle appears
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


def test_delete_dialog_removes_it():
    b = MockBackend()
    b.add("d/keep", "x", "code")
    b.add("d/gone", "y", "code")
    assert b.delete_dialog("d/gone") is True
    assert "d/gone" not in b.list_dialogs() and "d/keep" in b.list_dialogs()
    assert b.delete_dialog("d/gone") is False        # already gone


def test_dialog_delete_route_switches_active_then_falls_back():
    import sidekick.app as app
    bk = app.STATE["backend"]
    a = "del/a"; c = "del/b"
    bk.messages(a); bk.add(a, "1", "code")
    bk.messages(c); bk.add(c, "2", "code")
    app.STATE["dialog"] = a
    app.dialog_delete(dialog=a)                       # delete the active dialog
    assert a not in bk.list_dialogs()
    assert app.STATE["dialog"] != a                  # switched to a survivor
    # deleting every remaining dialog falls back to a fresh demo/welcome
    for d in list(bk.list_dialogs()):
        app.STATE["dialog"] = d
        app.dialog_delete(dialog=d)
    assert app.STATE["dialog"] == "demo/welcome"


def test_dialog_delete_bulk_removes_several():
    import json, sidekick.app as app
    bk = app.STATE["backend"]
    for d in ("bulk/a", "bulk/b", "bulk/c"):
        bk.messages(d); bk.add(d, "x", "code")
    app.STATE["dialog"] = "bulk/a"
    app.dialog_delete_bulk(names=json.dumps(["bulk/a", "bulk/b"]))
    live = bk.list_dialogs()
    assert "bulk/a" not in live and "bulk/b" not in live and "bulk/c" in live
    assert app.STATE["dialog"] != "bulk/a"        # active one was deleted -> switched
    app.dialog_delete_bulk(names="not json")      # malformed input is a no-op, no crash
    assert "bulk/c" in bk.list_dialogs()


def test_sidebar_renders_delete_menu():
    import sidekick.app as app
    from fasthtml.common import to_xml
    h = to_xml(app._dialog_leaf("welcome", "demo/welcome", "demo/welcome"))
    assert "conv-del" in h and "Delete" in h                  # the ⋯ delete action
    assert 'data-dialog="demo/welcome"' in h                  # carries the dialog name for JS


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
    monkeypatch.setenv("SIDEKICK_PAPER_DIR", str(tmp_path))   # allow the tmp path (S5)
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
    import io, sidekick.app as app
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


def test_paper_open_bad_path_reports_not_found(monkeypatch, tmp_path):
    import sidekick.app as app
    monkeypatch.setenv("SIDEKICK_PAPER_DIR", str(tmp_path))   # allow the dir so we
    app.paper_open(path=str(tmp_path / "nope.pdf"))          # reach the not-found path
    assert app.STATE["paper"]["engine"] == "error" and "not found" in app.STATE["paper"]["md"].lower()
    app.STATE["paper"] = None


def test_paper_open_refuses_path_outside_allowed_dir(monkeypatch, tmp_path):
    # S5: a server-side `path` outside home/cache/$SIDEKICK_PAPER_DIR is refused,
    # not opened — even when the file exists.
    import sidekick.app as app
    monkeypatch.setenv("SIDEKICK_PAPER_DIR", str(tmp_path / "allowed"))
    outside = tmp_path / "secret.pdf"; outside.write_bytes(b"%PDF-1.4 secret")
    app.paper_open(path=str(outside))
    assert app.STATE["paper"]["engine"] == "error"
    assert "refused" in app.STATE["paper"]["md"].lower()
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


# ---- AI cell-editing MCP tools (claude -p / Max path) -----------------------
def test_cell_tools_disabled_without_env(monkeypatch):
    from sidekick import claude_cli as cc
    monkeypatch.delenv("SIDEKICK_MCP_TOKEN", raising=False)
    monkeypatch.delenv("SIDEKICK_APP_URL", raising=False)
    assert cc._cell_tools_enabled() is False


def test_cell_tools_env_flag_opt_out(monkeypatch):
    from sidekick import claude_cli as cc
    monkeypatch.setenv("SIDEKICK_MCP_TOKEN", "t")
    monkeypatch.setenv("SIDEKICK_APP_URL", "http://127.0.0.1:8000")
    monkeypatch.setenv("SIDEKICK_CELL_TOOLS", "0")
    assert cc._cell_tools_enabled() is False


def test_build_cmd_registers_mcp_and_keeps_prompt_last(monkeypatch):
    from sidekick import claude_cli as cc
    monkeypatch.setenv("SIDEKICK_MCP_TOKEN", "t")
    monkeypatch.setenv("SIDEKICK_APP_URL", "http://127.0.0.1:8000")
    monkeypatch.delenv("SIDEKICK_CELL_TOOLS", raising=False)
    monkeypatch.setattr(cc, "claude_bin", lambda: "/usr/bin/claude")
    cc.CLI_SESSIONS.pop("mcp/x", None)                     # force a fresh session
    cmd, _ = cc._build_cmd("mcp/x", "fix cell 2", "ctx", stream=True)
    assert "--mcp-config" in cmd and "--allowedTools" in cmd
    # the variadic --allowedTools must not swallow the prompt: `--` separates them,
    # and the prompt is the final positional.
    assert cmd[-1] == "fix cell 2" and cmd[-2] == "--"
    assert "mcp__cells__update_cell" in cmd
    sysmsg = cmd[cmd.index("--append-system-prompt") + 1]
    assert "list_cells" in sysmsg                          # tool guidance taught once


def test_build_cmd_no_mcp_when_disabled(monkeypatch):
    from sidekick import claude_cli as cc
    monkeypatch.delenv("SIDEKICK_MCP_TOKEN", raising=False)
    monkeypatch.setattr(cc, "claude_bin", lambda: "/usr/bin/claude")
    cc.CLI_SESSIONS.pop("mcp/off", None)
    cmd, _ = cc._build_cmd("mcp/off", "q", "ctx", stream=True)
    assert "--mcp-config" not in cmd                       # no cell-editing MCP server
    assert not any("mcp__cells__" in a for a in cmd)       # and no cell tools allowed
    assert cmd[-1] == "q"


def test_build_cmd_allows_web_tools_by_default(monkeypatch, tmp_path):
    # With no tools.json, the ethos-safe defaults apply: web research allowed, the
    # executor's hands (Write/Edit/Bash) denied.
    from sidekick import claude_cli as cc
    monkeypatch.delenv("SIDEKICK_MCP_TOKEN", raising=False)
    monkeypatch.setenv("SIDEKICK_TOOLS", str(tmp_path / "none.json"))   # absent -> defaults
    monkeypatch.setattr(cc, "claude_bin", lambda: "/usr/bin/claude")
    cmd, _ = cc._build_cmd("web/on", "q", "ctx", stream=False)
    assert "WebSearch" in cmd and "WebFetch" in cmd
    assert "--" in cmd and cmd[-1] == "q"                  # variadic allowlist stopped by --
    dis = cmd[cmd.index("--disallowed-tools") + 1:cmd.index("--disallowed-tools") + 4]
    assert dis == ["Write", "Edit", "Bash"]                # research yes, executor's hands no


def test_build_cmd_honors_tools_config(monkeypatch, tmp_path):
    # A tools.json is the single source of truth: here, allow only WebSearch and
    # deny nothing → no WebFetch, no --disallowed-tools.
    import json
    from sidekick import claude_cli as cc
    p = tmp_path / "tools.json"
    p.write_text(json.dumps({"allow": ["WebSearch"], "deny": []}))
    monkeypatch.delenv("SIDEKICK_MCP_TOKEN", raising=False)
    monkeypatch.setenv("SIDEKICK_TOOLS", str(p))
    monkeypatch.setattr(cc, "claude_bin", lambda: "/usr/bin/claude")
    cmd, _ = cc._build_cmd("web/cfg", "q", "ctx", stream=False)
    assert "WebSearch" in cmd and "WebFetch" not in cmd
    assert "--disallowed-tools" not in cmd
    assert cmd[-1] == "q"


def test_tools_config_defaults_and_file(monkeypatch, tmp_path):
    from sidekick import tools_config as tc
    monkeypatch.setenv("SIDEKICK_TOOLS", str(tmp_path / "tools.json"))
    # no file -> ethos-safe defaults
    assert tc.allow_list() == ["WebSearch", "WebFetch"]
    assert tc.deny_list() == ["Write", "Edit", "Bash"]
    # a present key (even []) is honored; a missing key falls back to default
    tc.save(allow=["WebSearch", "Read"], deny=[])
    assert tc.allow_list() == ["WebSearch", "Read"] and tc.deny_list() == []
    import json
    (tmp_path / "tools.json").write_text(json.dumps({"deny": ["Bash"]}))   # allow omitted
    assert tc.allow_list() == ["WebSearch", "WebFetch"]    # default allow
    assert tc.deny_list() == ["Bash"]


def _mcp_client(client_host="127.0.0.1"):
    # The /internal/* routes are loopback-only; present a loopback client host so
    # these tests stand in for the legitimate on-box MCP caller (TestClient's
    # default host is "testclient", which the loopback guard would reject).
    from starlette.testclient import TestClient
    import sidekick.app as app
    tc = TestClient(app.app, client=(client_host, 50000))
    return app, tc, app.STATE["mcp_token"]


def test_internal_cells_forbidden_without_token():
    app, client, _ = _mcp_client()
    app.STATE["dialog"] = "mcp/list"
    app.STATE["backend"].messages("mcp/list")
    assert client.get("/internal/cells", params={"dialog": "mcp/list", "tok": "bad"}).status_code == 403


def test_internal_route_rejects_nonloopback_origin_even_with_valid_token():
    # A correct token from off-box must still be refused — the cell-mutation routes
    # are loopback-only, a second factor beyond the token (guards SIDEKICK_HOST=0.0.0.0).
    app, client, tok = _mcp_client(client_host="10.0.0.5")
    bk = app.STATE["backend"]; d = "mcp/lan"; bk.messages(d)
    m = bk.add(d, "keep", "code")
    r = client.post("/internal/cell/update",
                    data={"dialog": d, "id": m.id, "content": "hijacked", "tok": tok})
    assert r.status_code == 403
    assert bk.messages(d)[-1].content == "keep"        # unchanged


def test_internal_cell_update_edits_live_backend():
    app, client, tok = _mcp_client()
    bk = app.STATE["backend"]; d = "mcp/upd"; bk.messages(d)
    m = bk.add(d, "return a - b", "code")
    r = client.post("/internal/cell/update",
                    data={"dialog": d, "id": m.id, "content": "return a + b", "tok": tok})
    assert r.json()["ok"] is True
    assert bk.messages(d)[-1].content == "return a + b"
    assert app.STATE["cells_dirty"] is True


def test_internal_str_replace_requires_unique_match():
    app, client, tok = _mcp_client()
    bk = app.STATE["backend"]; d = "mcp/sr"; bk.messages(d)
    m = bk.add(d, "x = 1\nx = 1", "code")
    dup = client.post("/internal/cell/str_replace",
                      data={"dialog": d, "id": m.id, "old": "x = 1", "new": "x = 2", "tok": tok})
    assert dup.status_code == 400 and "unique" in dup.json()["error"]
    miss = client.post("/internal/cell/str_replace",
                       data={"dialog": d, "id": m.id, "old": "zzz", "new": "q", "tok": tok})
    assert miss.status_code == 400


def test_internal_cell_insert_after():
    app, client, tok = _mcp_client()
    bk = app.STATE["backend"]; d = "mcp/ins"; bk.messages(d)
    a = bk.add(d, "first", "code")
    bk.add(d, "third", "code")
    r = client.post("/internal/cell/insert",
                    data={"dialog": d, "content": "second", "cell_type": "code",
                          "after_id": a.id, "tok": tok})
    assert r.json()["ok"] is True
    assert [c.content for c in bk.messages(d)] == ["first", "second", "third"]


def test_cells_dirty_mark_during_stream_survives_to_consume():
    """The stream generator resets cells_dirty at the start of a turn; an MCP cell
    edit on another thread mid-turn must still be seen by the end-of-turn consume."""
    import threading
    import sidekick.app as app
    app._reset_cells_dirty()                            # gen: start of turn
    t = threading.Thread(target=app._mark_cells_dirty)  # the AI edits a cell mid-turn
    t.start(); t.join()
    assert app._consume_cells_dirty() is True           # gen: end of turn — edit seen
    assert app.STATE["cells_dirty"] is False            # and cleared


def test_cells_dirty_consume_atomic_no_lost_update():
    """A mark racing the consume is never silently dropped: it is either observed by
    that consume or left set for the next one. With a non-atomic read-then-reset a
    mark landing between the read and the reset would be lost."""
    import threading
    import sidekick.app as app
    app._reset_cells_dirty()
    observed = 0
    ROUNDS = 200
    for _ in range(ROUNDS):
        go = threading.Event()

        def marker():
            go.wait()
            app._mark_cells_dirty()

        t = threading.Thread(target=marker)
        t.start()
        go.set()                                        # release ~simultaneously with consume
        if app._consume_cells_dirty():
            observed += 1
        t.join()
        if app._consume_cells_dirty():                  # a mark that landed just after
            observed += 1
    assert observed == ROUNDS                           # every mark accounted for, none lost


def test_mcp_server_dispatches_tools(monkeypatch):
    import server.mcp_cells as mc
    calls = []
    def fake_request(method, path, params):
        calls.append((method, path, params))
        if path == "/internal/cells":
            return {"ok": True, "cells": [{"id": "_a", "type": "code", "content": "y=2"}]}
        return {"ok": True, "message": "done"}
    monkeypatch.setattr(mc, "_request", fake_request)
    assert "id=_a" in mc._call_tool("list_cells", {})
    assert mc._call_tool("update_cell", {"cell_id": "_a", "content": "y=3"}) == "done"
    assert calls[-1][1] == "/internal/cell/update"


def test_mcp_server_tool_error_raises(monkeypatch):
    import server.mcp_cells as mc
    monkeypatch.setattr(mc, "_request", lambda *a: {"ok": False, "error": "no cell"})
    with pytest.raises(RuntimeError):
        mc._call_tool("update_cell", {"cell_id": "x", "content": "z"})


# ---- cell numbering: visible badge + AI-context handles ----------------------
def test_build_context_tags_cells_with_number_and_id():
    msgs = [_m("_a", "code", "x=1"), _m("_b", "note", "hi"), _m("_c", "prompt", "q?")]
    ctx = build_context(msgs, upto_id="_c")
    assert '<code n="1" id="_a">' in ctx
    assert '<note n="2" id="_b">' in ctx


def test_build_context_numbering_is_absolute_across_muted():
    # a muted cell still consumes its position, so visible cells keep the number
    # the UI shows (UI numbers every row; context just omits the muted body).
    msgs = [_m("_a", "code", "x=1"), _m("_b", "note", "SECRET", muted=True),
            _m("_c", "code", "y=2")]
    ctx = build_context(msgs)
    assert "SECRET" not in ctx
    assert 'id="_c"' in ctx and 'n="3"' in ctx        # third cell stays cell 3


def test_cell_number_is_one_based_position():
    import sidekick.app as app
    bk = app.STATE["backend"]; d = "num/pos"; bk.messages(d)
    a = bk.add(d, "a", "code"); bk.add(d, "b", "note"); c = bk.add(d, "c", "code")
    assert app._cell_number(bk, d, a.id) == 1
    assert app._cell_number(bk, d, c.id) == 3
    assert app._cell_number(bk, d, "missing") is None


def test_msgrow_shows_cell_number_badge():
    import sidekick.app as app
    from fasthtml.common import to_xml
    from sidekick.client import Msg
    html = to_xml(app.MsgRow(Msg("_z", "code", "x=1"), num=4))
    assert 'class="cell-num"' in html and ">4<" in html


def test_ab_shortcut_inserts_a_note_cell():
    # the a/b keyboard shortcut posts msg_type:'note' so a quick insert is a
    # markdown note (use the ＋ menu for code / Ask AI).
    import sidekick.app as app
    assert "msg_type: 'note'" in app.STREAM_JS
    assert "msg_type: 'code'" not in app.STREAM_JS


def test_cell_insert_route_creates_note_when_asked():
    import sidekick.app as app
    app.STATE["dialog"] = "ins/note"
    bk = app.STATE["backend"]; bk.messages("ins/note")
    a = bk.add("ins/note", "x", "code")
    app.cell_insert(id=a.id, msg_type="note", where="below")
    assert bk.messages("ins/note")[1].msg_type == "note"


def test_build_cmd_prompt_survives_variadic_flags(monkeypatch):
    # `claude -p` carries multiple variadic options (--allowedTools when cell
    # tools are on, --disallowed-tools always). A variadic option greedily eats
    # following bare args, so the prompt positional must stay last in every mode —
    # else the prompt is silently swallowed and Claude gets no input. Guards a
    # bug we already hit once when --allowedTools was added.
    from sidekick import claude_cli as cc
    monkeypatch.setattr(cc, "claude_bin", lambda: "/usr/bin/claude")
    monkeypatch.delenv("SIDEKICK_CLAUDE_CLI_MODEL", raising=False)
    for tools_on in (True, False):
        if tools_on:
            monkeypatch.setenv("SIDEKICK_MCP_TOKEN", "t")
            monkeypatch.setenv("SIDEKICK_APP_URL", "http://127.0.0.1:8000")
            monkeypatch.delenv("SIDEKICK_CELL_TOOLS", raising=False)
        else:
            monkeypatch.setenv("SIDEKICK_CELL_TOOLS", "0")
        for stream in (True, False):
            cc.CLI_SESSIONS.pop("guard/d", None)
            cmd, _ = cc._build_cmd("guard/d", "THE_PROMPT", "ctx", stream=stream)
            assert "--disallowed-tools" in cmd            # the always-on variadic flag
            assert cmd[-1] == "THE_PROMPT", (tools_on, stream, cmd[-4:])


def test_toc_query_is_scoped_to_one_stream():
    # buildTOC must resolve a single #stream via getElementById and query headings
    # within it. A global '#stream .note-view …' selector matches BOTH the old and
    # new #stream during an htmx outerHTML swap, doubling the table of contents.
    import sidekick.app as app
    assert "getElementById('stream')" in app.TOC_JS
    assert "#stream .note-view" not in app.TOC_JS      # the doubling selector is gone


# ---- code completion (Ctrl+Space, kernel-backed) ----------------------------
def test_complete_code_uses_live_namespace():
    # jedi introspects the dialog's executed namespace: after `import numpy as np`
    # and a list var, np.ar -> arange and xs.app -> append.
    pytest.importorskip("jedi")
    pytest.importorskip("numpy")          # np.arange completion needs numpy (kernel extra)
    from server.kernel_server import complete_code, run_code
    run_code("cmpl/d", "import numpy as np\nxs = [1, 2, 3]")
    np_names = [c["name"] for c in complete_code("cmpl/d", "np.ar", 1, 5)]
    assert "arange" in np_names
    xs_names = [c["name"] for c in complete_code("cmpl/d", "xs.app", 1, 6)]
    assert "append" in xs_names


def test_complete_code_never_raises_on_bad_input():
    pytest.importorskip("jedi")
    from server.kernel_server import complete_code
    assert isinstance(complete_code("cmpl/empty", "", 1, 0), list)   # no crash


def test_complete_route_forwards_to_backend():
    import sidekick.app as app
    from starlette.testclient import TestClient
    class FakeKernel:
        def complete(self, dialog, code, line, col):
            return [{"name": "arange", "type": "function"}]
    app.STATE["backend"] = FakeKernel(); app.STATE["dialog"] = "d"
    r = TestClient(app.app).post("/complete", data={"code": "np.ar", "line": 1, "col": 5})
    assert r.json()["completions"][0]["name"] == "arange"


def test_complete_route_empty_when_backend_cannot_complete():
    import sidekick.app as app
    from sidekick.client import MockBackend
    from starlette.testclient import TestClient
    app.STATE["backend"] = MockBackend()           # no .complete method
    r = TestClient(app.app).post("/complete", data={"code": "x", "line": 1, "col": 1})
    assert r.json() == {"completions": []}


def test_completion_autotrigger_and_tab_accept_wired():
    # SolveIt-style polish: completion fires as you type (inputRead, identifier/dot
    # only) and Tab accepts the highlighted item. Guards the JS wiring.
    import sidekick.app as app
    js = app.COMPLETE_JS
    assert "__showCompletions" in js and "__autocompleteOnType" in js
    assert "inputRead" in js                       # auto-trigger as you type
    assert "'Tab'" in js                           # Tab accepts
    # the cell editor turns both on
    assert "__autocompleteOnType(cm)" in app._CODE_EDITOR_JS
    assert "__showCompletions(cm)" in app._CODE_EDITOR_JS


def test_pending_prompt_shows_thinking_spinner():
    # While an answer is pending (no output yet), the bubble shows a spinner +
    # "Thinking…" and the SSE wiring, so the user sees the AI is working.
    import sidekick.app as app
    from fasthtml.common import to_xml
    from sidekick.client import MockBackend
    b = MockBackend(); app.STATE["backend"] = b; app.STATE["dialog"] = "spin/d"
    b.messages("spin/d"); m = b.add("spin/d", "q?", "prompt", model="claude-cli")
    app.STATE["pending_stream"] = ("spin/d", m.id)
    html = to_xml(app._output_views(m)[0])
    app.STATE["pending_stream"] = None                 # reset shared state
    assert 'class="spinner"' in html and "Thinking" in html
    assert "data-stream-url" in html


def test_stream_js_toggles_streaming_caret():
    # The blinking caret marks an answer as actively streaming, removed on done.
    import sidekick.app as app
    assert "classList.add('streaming')" in app.STREAM_JS
    assert "classList.remove('streaming')" in app.STREAM_JS


# ---- latency reductions: htmx swap on send + fast model ---------------------
def test_send_htmx_returns_stream_fragment_not_full_page():
    # The composer posts via htmx, so /send returns just #stream (fast swap) when
    # HX-Request is set, and the whole page only for a no-JS submit.
    import sidekick.app as app
    from starlette.testclient import TestClient
    from sidekick.client import MockBackend
    app.STATE["backend"] = MockBackend(); app.STATE["dialog"] = "send/htmx"
    c = TestClient(app.app)
    frag = c.post("/send", data={"content": "a note", "msg_type": "note"},
                  headers={"HX-Request": "true"}).text
    full = c.post("/send", data={"content": "b note", "msg_type": "note"}).text
    assert 'id="stream"' in frag and "<html" not in frag.lower()
    assert "<html" in full.lower()


def test_claude_cli_fast_model_uses_haiku(monkeypatch):
    from sidekick import claude_cli as cc
    monkeypatch.setattr(cc, "claude_bin", lambda: "/usr/bin/claude")
    monkeypatch.delenv("SIDEKICK_CLAUDE_CLI_MODEL", raising=False)
    cc.CLI_SESSIONS.pop("fast/d", None)
    cmd, _ = cc._build_cmd("fast/d", "hi", "", stream=True, model="claude-cli-fast")
    assert cmd[cmd.index("--model") + 1] == "haiku"
    # the default CLI model adds no --model flag (inherits the subscription default)
    cc.CLI_SESSIONS.pop("norm/d", None)
    cmd2, _ = cc._build_cmd("norm/d", "hi", "", stream=True, model="claude-cli")
    assert "--model" not in cmd2
    assert "claude-cli-fast" in cc.CLI_MODELS          # routes through the streaming path


def test_answer_code_blocks_extracts_fenced_code_in_order():
    import sidekick.app as app
    md = ("Here you go:\n\n```python\nx = 1\nprint(x)\n```\n\n"
          "and a diagram\n\n```mermaid\nflowchart TD\n  A-->B\n```\n")
    assert app._answer_code_blocks(md) == [
        ("python", "x = 1\nprint(x)"), ("mermaid", "flowchart TD\n  A-->B")]
    assert app._answer_code_blocks("no code here, just prose") == []
    assert app._answer_code_blocks("```\nbare = 1\n```") == [("", "bare = 1")]  # unlabeled
    assert app._answer_code_blocks("```python\n\n```") == []   # empty block dropped


def test_split_to_code_inserts_code_cells_below_prompt():
    import sidekick.app as app
    from fasthtml.common import to_xml
    app.STATE["dialog"] = "test/split"
    bk = app.STATE["backend"]; bk.messages("test/split")
    m = bk.add("test/split", "write a loop", "prompt")
    bk.update_output("test/split", m.id, "Sure:\n\n```python\nfor i in range(3):\n    print(i)\n```\n")
    out = to_xml(app.cell_split(id=m.id))                 # Stream() consumes the one-shot scroll_to
    msgs = bk.messages("test/split")
    i = next(k for k, x in enumerate(msgs) if x.id == m.id)
    assert msgs[i + 1].msg_type == "code"
    assert msgs[i + 1].content == "for i in range(3):\n    print(i)"
    assert f"cell-{msgs[i + 1].id}" in out and "scrollHeight" in out   # scrolled into view


def test_split_to_code_routes_mermaid_into_a_note():
    import sidekick.app as app
    app.STATE["dialog"] = "test/split-mermaid"
    bk = app.STATE["backend"]; bk.messages("test/split-mermaid")
    m = bk.add("test/split-mermaid", "diagram it", "prompt")
    bk.update_output("test/split-mermaid", m.id,
                     "Here:\n\n```mermaid\nflowchart TD\n  A-->B\n```\n")
    app.cell_split(id=m.id)
    msgs = bk.messages("test/split-mermaid")
    i = next(k for k, x in enumerate(msgs) if x.id == m.id)
    assert msgs[i + 1].msg_type == "note"                      # mermaid → note, not code
    assert msgs[i + 1].content == "```mermaid\nflowchart TD\n  A-->B\n```"   # fence kept so it renders


def test_split_button_only_shows_when_answer_has_code():
    import sidekick.app as app
    from fasthtml.common import to_xml
    from sidekick.client import Msg
    has = Msg(id="_p1", msg_type="prompt", content="q", output="```python\nx=1\n```")
    no = Msg(id="_p2", msg_type="prompt", content="q", output="just prose")
    assert "/cell/split" in to_xml(app.MsgRow(has))
    assert "/cell/split" not in to_xml(app.MsgRow(no))


def test_composer_shows_instant_pending_spinner():
    # On an Ask-AI send the composer injects a "Thinking…" wheel immediately
    # (client-side), so a working indicator is visible for every model — including
    # the blocking API ones that stream nothing — until the answer swaps in.
    import sidekick.app as app
    js = app.COMPOSER_JS
    assert "_showPendingSpinner" in js and "pending-spinner" in js
    assert 'class="spinner"' in js and "Thinking" in js
    assert "msgType" in js and "_showPendingSpinner()" in js   # gated to prompt sends
    assert "__pendingSpinnerCleanup" in js                     # stray-spinner cleanup


# ---- cell-centric nbdev libraries (sidekick.nbdev_export) -------------------
def _lib_backend():
    """Two unrelated dialogs whose cells tag into one library `audiolib`."""
    b = MockBackend()
    b.add("conv/conv1d", "#| export audiolib:layers\nclass Conv1d:\n    pass", "code")
    b.add("conv/conv1d", "scratch = 1  # not exported", "code")
    b.add("conv/conv1d", "#| export localmod", "code")        # plain -> dialog-local, NOT a library
    b.add("models/unet", "#| export audiolib:blocks\ndef resblock():\n    return 42", "code")
    b.add("models/unet", "#| export audiolib:layers\nclass Conv2d:\n    pass", "code")
    return b


def test_parse_lib_target():
    from sidekick import nbdev_export as nx
    assert nx.parse_lib_target("audiolib:layers") == ("audiolib", "layers")
    assert nx.parse_lib_target("audiolib:") == ("audiolib", "core")     # empty module -> core
    assert nx.parse_lib_target("layers") == (None, "layers")            # no colon -> dialog-local


def test_gather_collects_across_dialogs_excludes_plain_export():
    from sidekick import nbdev_export as nx
    mods = nx.gather(_lib_backend(), "audiolib")
    assert set(mods) == {"layers", "blocks"}
    # layers drew a cell from EACH dialog — cross-dialog assembly
    assert [e["dialog"] for e in mods["layers"]] == ["conv/conv1d", "models/unet"]
    assert [e["dialog"] for e in mods["blocks"]] == ["models/unet"]
    # the plain `#| export localmod` cell is dialog-local, not in the library
    bodies = "\n".join(e["body"] for v in mods.values() for e in v)
    assert "localmod" not in bodies


def test_module_notebook_has_default_exp_and_provenance():
    from sidekick import nbdev_export as nx
    nb = nx.module_notebook("layers", [{"body": "class C: pass",
                                        "dialog": "conv/conv1d", "id": "_abc12345"}])
    srcs = ["".join(c["source"]) for c in nb["cells"]]
    assert srcs[0] == "#| default_exp layers"                 # nbdev module header
    assert srcs[1].startswith("#| export\n")                  # native nbdev directive
    assert "# source: conv/conv1d #_abc12345" in srcs[1]      # provenance back-link
    assert nb["nbformat"] == 4 and "cells" in nb


def test_library_files_emits_pyproject_and_valid_notebooks():
    import json
    from sidekick import nbdev_export as nx
    files = nx.library_files(_lib_backend(), "audiolib")
    assert "pyproject.toml" in files and "[tool.nbdev]" in files["pyproject.toml"]
    assert "nbs/layers.ipynb" in files and "nbs/blocks.ipynb" in files
    nb = json.loads(files["nbs/layers.ipynb"])                # valid JSON / nbformat
    assert nb["nbformat"] == 4


def test_build_library_minimal_scaffold_and_degrades_without_nbdev(tmp_path, monkeypatch):
    # SIDEKICK_NBDEV_SCAFFOLD=0 skips nbdev-new (no network) -> minimal pyproject.
    from sidekick import nbdev_export as nx
    monkeypatch.setenv("SIDEKICK_NBDEV_SCAFFOLD", "0")
    monkeypatch.setattr(nx, "run_nbdev", lambda dest: (False, "nbdev not installed"))
    res = nx.build_library(_lib_backend(), "audiolib", str(tmp_path), pkg_name="audiolib")
    assert res["pkg"] == "audiolib" and set(res["modules"]) == {"layers", "blocks"}
    assert (tmp_path / "pyproject.toml").exists()             # minimal project written
    assert (tmp_path / "nbs" / "layers.ipynb").exists()       # module notebooks always ours
    assert res["nbdev_ok"] is False                           # soft failure, notebooks still emitted
    assert "minimal pyproject" in res["scaffold"]


def test_build_library_scaffolds_on_fresh_dir(tmp_path, monkeypatch):
    # A fresh dir triggers nbdev_create_config (stubbed — offline anyway).
    from sidekick import nbdev_export as nx
    calls = {}

    def fake_scaffold(dest, pkg, *a, **k):
        calls["dest"] = dest
        (Path(dest) / "pyproject.toml").write_text("[tool.nbdev]\nlib_path='x'\nnbs_path='nbs'\n")
        return True, "generated nbdev pyproject.toml with nbdev_create_config (offline)"

    monkeypatch.setattr(nx, "scaffold_nbdev", fake_scaffold)
    monkeypatch.setattr(nx, "run_nbdev", lambda dest: (True, "built"))
    res = nx.build_library(_lib_backend(), "audiolib", str(tmp_path), pkg_name="audiolib")
    assert calls["dest"] == str(tmp_path)                     # scaffold was invoked
    assert "nbdev_create_config" in res["scaffold"]
    assert (tmp_path / "nbs" / "index.ipynb").exists()        # index written on success
    assert (tmp_path / "nbs" / "blocks.ipynb").exists()


def test_build_library_reuses_existing_nbdev_project(tmp_path, monkeypatch):
    # A dir that's already an nbdev project is reused — no scaffold attempt.
    from sidekick import nbdev_export as nx
    (tmp_path / "pyproject.toml").write_text("[tool.nbdev]\nlib_path='x'\nnbs_path='nbs'\n")

    def boom(*a, **k):
        raise AssertionError("scaffold_nbdev must not run for an existing project")

    monkeypatch.setattr(nx, "scaffold_nbdev", boom)
    monkeypatch.setattr(nx, "run_nbdev", lambda dest: (True, "built"))
    res = nx.build_library(_lib_backend(), "audiolib", str(tmp_path), pkg_name="audiolib")
    assert "existing nbdev project" in res["scaffold"]
    assert (tmp_path / "nbs" / "layers.ipynb").exists()       # notebooks refreshed


def test_build_library_prunes_orphaned_modules_on_retag(tmp_path, monkeypatch):
    # Retagging every cell to a new module must not leave the old module's notebook
    # (or its tangled .py) behind — the live cells are the source of truth.
    from sidekick import nbdev_export as nx
    monkeypatch.setenv("SIDEKICK_NBDEV_SCAFFOLD", "0")        # minimal, offline
    monkeypatch.setattr(nx, "run_nbdev", lambda dest: (False, "skipped"))
    nx.build_library(_lib_backend(), "audiolib", str(tmp_path), pkg_name="audiolib")
    assert (tmp_path / "nbs" / "layers.ipynb").exists()
    assert (tmp_path / "nbs" / "blocks.ipynb").exists()
    # simulate a prior nbdev tangle + nbdev-owned files in the package dir
    pkgdir = tmp_path / "audiolib"; pkgdir.mkdir(exist_ok=True)
    (pkgdir / "layers.py").write_text("# tangled")
    (pkgdir / "blocks.py").write_text("# tangled")
    (pkgdir / "__init__.py").write_text("__version__ = '0'")  # nbdev-owned, must survive
    # retag: cells now target a single new module
    b = MockBackend()
    b.add("conv/conv1d", "#| export audiolib:newmod\nclass X:\n    pass", "code")
    res = nx.build_library(b, "audiolib", str(tmp_path), pkg_name="audiolib")
    assert not (tmp_path / "nbs" / "layers.ipynb").exists()   # orphan notebooks gone
    assert not (tmp_path / "nbs" / "blocks.ipynb").exists()
    assert not (pkgdir / "layers.py").exists()                # and their tangled .py
    assert not (pkgdir / "blocks.py").exists()
    assert (tmp_path / "nbs" / "newmod.ipynb").exists()       # the current module written
    assert (tmp_path / "nbs" / "index.ipynb").exists()        # index never pruned
    assert (pkgdir / "__init__.py").exists()                  # nbdev-owned file untouched
    assert set(res["pruned"]) == {"nbs/layers.ipynb", "nbs/blocks.ipynb",
                                  "audiolib/layers.py", "audiolib/blocks.py"}


def test_is_nbdev_project(tmp_path):
    from sidekick import nbdev_export as nx
    assert not nx.is_nbdev_project(str(tmp_path))             # empty
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    assert not nx.is_nbdev_project(str(tmp_path))             # pyproject but no [tool.nbdev]
    (tmp_path / "pyproject.toml").write_text("[tool.nbdev]\nlib_path='x'\n")
    assert nx.is_nbdev_project(str(tmp_path))                 # marked


def test_run_nbdev_missing_binary_is_soft(monkeypatch):
    import shutil
    from sidekick import nbdev_export as nx
    monkeypatch.setattr(shutil, "which", lambda name: None)
    ok, detail = nx.run_nbdev("/tmp/whatever")
    assert ok is False and "nbdev" in detail.lower()


# ---- library registry + in-app UI (sidekick.libraries + app routes) --------
def test_library_registry_add_get_names_remove(tmp_path, monkeypatch):
    monkeypatch.setenv("SIDEKICK_DATA", str(tmp_path))
    from sidekick import libraries
    lib = libraries.add("audiolib")
    assert lib["name"] == "audiolib" and lib["pkg"] == "audiolib" and lib["path"]
    assert libraries.names() == ["audiolib"]
    assert libraries.get("audiolib")["pkg"] == "audiolib"
    with pytest.raises(ValueError):
        libraries.add("audiolib")                       # duplicate name rejected
    libraries.remove("audiolib")
    assert libraries.names() == []


def test_library_add_custom_pkg_and_path(tmp_path, monkeypatch):
    monkeypatch.setenv("SIDEKICK_DATA", str(tmp_path))
    from sidekick import libraries
    lib = libraries.add("My Lib", pkg="mylib", path="/tmp/x")
    assert lib["name"] == "My Lib" and lib["pkg"] == "mylib" and lib["path"] == "/tmp/x"


def test_set_export_target_replaces_existing():
    from sidekick import export
    out = export.set_export_target("#| export old:mod\nx = 1", "audiolib:layers")
    assert "#| export audiolib:layers" in out
    assert "old:mod" not in out and "x = 1" in out       # old export gone, body kept


def test_cell_export_to_route_tags_cell(tmp_path, monkeypatch):
    monkeypatch.setenv("SIDEKICK_DATA", str(tmp_path))
    import sidekick.app as app
    app.STATE["dialog"] = "lib/tag"
    b = app.STATE["backend"]
    b.messages("lib/tag")
    m = b.add("lib/tag", "class C:\n    pass", "code")
    app.cell_export_to(id=m.id, lib="audiolib", module="layers")
    assert "#| export audiolib:layers" in b.messages("lib/tag")[-1].content


def test_library_build_route_emits_and_reports(tmp_path, monkeypatch):
    monkeypatch.setenv("SIDEKICK_DATA", str(tmp_path))
    import sidekick.app as app
    from sidekick import libraries, nbdev_export
    from fasthtml.common import to_xml
    dest = tmp_path / "audiolib"
    libraries.add("audiolib", path=str(dest))
    app.STATE["dialog"] = "lib/x"
    b = app.STATE["backend"]
    b.messages("lib/x")
    b.add("lib/x", "#| export audiolib:core\nclass C:\n    pass", "code")
    monkeypatch.setattr(nbdev_export, "run_nbdev", lambda d: (False, "nbdev not installed"))
    html = to_xml(app.library_build(name="audiolib"))
    assert "Built 'audiolib'" in html and "core" in html
    assert (dest / "nbs" / "core.ipynb").exists()


def test_libraries_page_top_bar_link_present():
    import sidekick.app as app
    from fasthtml.common import to_xml
    assert 'href="/libraries"' in to_xml(app.Page())


def test_git_identity_strips_quotes(monkeypatch):
    # Some git configs store the name WITH quotes (`user.name = "Jane Doe"`); those
    # must be stripped or they double up and break the generated pyproject TOML.
    import subprocess
    from sidekick import nbdev_export as nx

    def fake_run(cmd, **kw):
        key = cmd[-1]
        out = '"Jane Doe"\n' if key == "user.name" else "jane@example.com\n"
        class R:
            returncode = 0
            stdout = out
        return R()

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert nx._git_identity() == ("Jane Doe", "jane@example.com")   # no stray quotes


def test_add_syspath_backend_posts_to_kernel():
    from sidekick.client import HttpKernelBackend
    b = HttpKernelBackend.__new__(HttpKernelBackend)
    seen = {}
    b._post = lambda path, body: seen.update(path=path, body=body) or {"ok": True, "added": True}
    assert b.add_syspath("/tmp/mylib") is True
    assert seen["path"] == "/syspath" and seen["body"] == {"path": "/tmp/mylib"}


def test_library_use_route_adds_path_and_reports(tmp_path, monkeypatch):
    monkeypatch.setenv("SIDEKICK_DATA", str(tmp_path))
    import sidekick.app as app
    from sidekick import libraries
    from sidekick.client import HttpKernelBackend
    from fasthtml.common import to_xml
    libraries.add("mylib", path=str(tmp_path / "mylib"))
    b = HttpKernelBackend.__new__(HttpKernelBackend)
    b._dialogs = {}                                          # LibrariesPage gathers over dialogs
    added = {}
    b.add_syspath = lambda p: added.setdefault("p", p) or True
    monkeypatch.setitem(app.STATE, "backend", b)
    html = to_xml(app.library_use(name="mylib"))
    assert added["p"] == str(tmp_path / "mylib")             # library dir put on kernel path
    assert "is on the kernel path" in html and "import mylib" in html


def test_library_use_button_kernel_only(tmp_path, monkeypatch):
    monkeypatch.setenv("SIDEKICK_DATA", str(tmp_path))
    import sidekick.app as app
    from sidekick import libraries
    from sidekick.client import MockBackend
    from fasthtml.common import to_xml
    libraries.add("mylib", path=str(tmp_path / "mylib"))
    monkeypatch.setitem(app.STATE, "backend", MockBackend())   # no add_syspath
    assert "Use in kernel" not in to_xml(app.LibrariesPage())
