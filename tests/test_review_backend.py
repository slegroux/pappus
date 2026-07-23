"""Backend hardening tests for the review-fixes batch.

Covers, with no live server / subprocess / network:
  - HttpKernelBackend.exec degrading gracefully when the kernel is unreachable
  - build_context budget correctness (pinned-over-budget, newest-cell survival)
  - stream() reaping an abandoned `claude` subprocess (GeneratorExit path)
  - claude_cli.drop() evicting a dialog's in-memory session/cost state

Run:  python -m pytest -q tests/test_review_backend.py
"""
import json
import sys
import urllib.error
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def test_effective_default_model_falls_back_when_codex_missing():
    """A Codex default stays configured, but resolves to Claude Opus high when the
    `codex` binary isn't installed on this machine — so a fresh session never
    starts on a model that can't run here. When codex IS present, keep the codex
    default untouched."""
    from unittest.mock import patch
    from sidekick import targets

    if not targets.default_model().startswith("codex-"):
        import pytest
        pytest.skip("configured default is not a codex model")
    with patch("sidekick.codex_cli.codex_bin", return_value=None):
        assert targets.effective_default_model() == "claude-opus-high"
    with patch("sidekick.codex_cli.codex_bin", return_value="/bin/codex"):
        assert targets.effective_default_model() == targets.default_model()


# ---- W1: exec degrades when the kernel is down ------------------------------
def test_exec_kernel_down_degrades():
    from sidekick.client import HttpKernelBackend

    b = HttpKernelBackend.__new__(HttpKernelBackend)     # skip __init__ (no live target)
    b._dialogs, b._undo, b._store_key = {}, [], None

    def boom(path, body, timeout=None):
        raise urllib.error.URLError("kernel down")

    b._post = boom
    m = b.add("d", "1/0", "code")
    out = b.exec("d", m.id)                               # must NOT raise
    assert out is m
    assert "kernel error" in out.output.lower()          # readable degraded output
    assert "kernel down" in out.output
    assert out.rich == []


# ---- S2: build_context budget correctness -----------------------------------
def test_build_context_pinned_over_budget():
    # Pinned content larger than the whole budget must be trimmed so the returned
    # context never exceeds max_chars (the old code shipped it whole and negative).
    from sidekick.client import build_context, Msg

    pinned = Msg(id="p", msg_type="note", content="P" * 5000, pinned=True)
    ctx = build_context([pinned], max_chars=200)
    assert len(ctx) <= 200
    assert "P" in ctx                                     # some pinned content survives


def test_build_context_keeps_newest():
    # A single newest cell larger than the budget must be truncated to fit, not
    # dropped into a near-empty context.
    from sidekick.client import build_context, Msg

    big = Msg(id="n", msg_type="note", content="Z" * 5000)
    ctx = build_context([big], max_chars=200)
    assert "Z" in ctx                                     # present (truncated), not dropped
    assert "omitted" not in ctx                           # it was kept, nothing dropped
    assert len(ctx) <= 200


# ---- W2: stream() reaps an abandoned subprocess -----------------------------
class _FakeProc:
    """A stand-in for the streaming Popen: emits stream-json delta lines and
    records terminate()/kill() so an abandoned generator can be observed."""

    def __init__(self, lines):
        self._it = iter(lines)
        self.terminated = False
        self.killed = False
        self._returncode = None
        self.stdout = SimpleNamespace(readline=lambda: next(self._it, ""))

    def poll(self):
        return self._returncode

    def wait(self, timeout=None):
        self._returncode = 0
        return 0

    def terminate(self):
        self.terminated = True
        self._returncode = 0                              # simulate a clean exit

    def kill(self):
        self.killed = True
        self._returncode = 0


def _delta_line(text):
    return json.dumps({"type": "stream_event",
                       "event": {"type": "content_block_delta",
                                 "delta": {"type": "text_delta", "text": text}}}) + "\n"


def test_stream_abandon_kills_subprocess(monkeypatch):
    import sidekick.claude_cli as cc

    cc.CLI_SESSIONS.pop("cli/abandon", None)
    monkeypatch.setattr(cc, "claude_bin", lambda: "/bin/claude")
    # Plenty of deltas and NO terminal result event, so the stream is still
    # mid-flight when we abandon it (it never reaches normal completion).
    proc = _FakeProc([_delta_line(f"chunk{i}") for i in range(10)])
    monkeypatch.setattr(cc, "_popen", lambda cmd, cwd, env: proc)

    gen = cc.stream("cli/abandon", "hi?", context="<code>x=1</code>")
    assert next(gen) == "chunk0"                          # consume a couple of deltas
    assert next(gen) == "chunk1"
    gen.close()                                           # GeneratorExit mid-stream

    assert proc.terminated                                # child was reaped, not leaked
    # A normal-completion session record must NOT have been written on abandon.
    assert "cli/abandon" not in cc.CLI_SESSIONS


# ---- evict-sessions: drop() clears in-memory session/cost -------------------
def test_drop_evicts_session():
    import sidekick.claude_cli as cc
    import sidekick.codex_cli as cx

    cc.CLI_SESSIONS["dlg/drop"] = {"id": "s", "sent": "", "mode": None}
    cc.CLI_COST["dlg/drop"] = {"usd": 0.5, "turns": 2}
    cx.CODEX_SESSIONS["dlg/drop"] = {"id": "c", "sent": "", "mode": None,
                                     "model": None, "tools": False, "version": 1}
    cc.drop("dlg/drop")
    cx.drop("dlg/drop")
    assert "dlg/drop" not in cc.CLI_SESSIONS
    assert "dlg/drop" not in cc.CLI_COST
    assert "dlg/drop" not in cx.CODEX_SESSIONS
    cc.drop("dlg/never-ran")                              # absent -> best-effort no-op
    cx.drop("dlg/never-ran")


# ---- selective sync: per-dialog local-only toggle ---------------------------
def test_set_private_moves_dialog_between_stores(tmp_path, monkeypatch):
    """set_private(dialog) moves a dialog out of the committed store and into the
    gitignored .local.json overlay (and back), and is_private tracks state."""
    monkeypatch.setenv("SIDEKICK_DATA", str(tmp_path))
    from sidekick.client import (_InMemoryBackend, _read_private_dialogs,
                                 _store_path, _local_store_path)

    b = _InMemoryBackend(store_key="kernel")
    b.add("keep", "public cell", "note")
    b.add("secret", "local-only cell", "note")

    # Both public initially.
    assert not b.is_private("secret")
    public = json.loads(_store_path("kernel").read_text())
    assert {"keep", "secret"} <= set(public)
    assert not _local_store_path("kernel").exists()

    # Flag "secret" local-only: it leaves the committed store for the overlay.
    assert b.set_private("secret") is True
    assert b.is_private("secret")
    assert _read_private_dialogs("kernel") == {"secret"}
    public = json.loads(_store_path("kernel").read_text())
    assert "secret" not in public and "keep" in public
    local = json.loads(_local_store_path("kernel").read_text())
    assert "secret" in local

    # A fresh backend still loads both (public + overlay merged).
    assert set(_InMemoryBackend(store_key="kernel").list_dialogs()) == {"keep", "secret"}

    # Toggle back: "secret" returns to the committed store, overlay is emptied.
    assert b.set_private("secret") is False
    assert not b.is_private("secret")
    assert _read_private_dialogs("kernel") == set()
    assert "secret" in json.loads(_store_path("kernel").read_text())
    assert not _local_store_path("kernel").exists()


def test_private_flag_follows_rename_and_delete(tmp_path, monkeypatch):
    monkeypatch.setenv("SIDEKICK_DATA", str(tmp_path))
    from sidekick.client import _InMemoryBackend, _read_private_dialogs

    b = _InMemoryBackend(store_key="kernel")
    b.add("draft", "x", "note")
    b.set_private("draft")

    b.rename("draft", "draft/final")                      # rename carries the flag
    assert _read_private_dialogs("kernel") == {"draft/final"}
    assert b.is_private("draft/final")

    b.delete_dialog("draft/final")                        # delete cleans it up
    assert _read_private_dialogs("kernel") == set()


def test_set_private_noops_without_store():
    from sidekick.client import _InMemoryBackend

    b = _InMemoryBackend()                                # ephemeral, no store_key
    assert b.set_private("whatever") is False
    assert b.is_private("whatever") is False
