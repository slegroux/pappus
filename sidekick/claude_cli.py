"""Claude via the `claude` CLI — subscription auth, per-dialog sessions.

Shelling out to Claude Code's ``claude -p`` authenticates with your logged-in
subscription (e.g. Max) instead of an ANTHROPIC_API_KEY / prepaid API credits.

One persistent CLI session per dialog, so each turn re-sends only the *new*
notebook cells. The notebook — not the chat log — is the source of truth, so a
session is only safe to resume while the context we already sent is still a
prefix of the current context (cells were only appended). Any edit / delete /
mute above the prompt breaks that prefix, so we start a fresh session and
re-send the full context. State: ``dialog -> {"id": session-uuid, "sent": ctx}``.

Two entry points share the session/command logic:
    call(dialog, content, context)   -> str            (blocking; kernel server)
    stream(dialog, content, context) -> Iterator[str]  (token deltas; web app SSE)
"""
from __future__ import annotations

import json
import os
import sys
import uuid

CLI_MODELS = {"claude-cli"}
CLI_SESSIONS: dict[str, dict] = {}
_CLI_CWD: str | None = None

# The SolveIt persona. SolveIt (fast.ai / Answer.AI) is built on George Pólya's
# "How to Solve It" and a "small steps" philosophy: the human is the agent and the
# AI is a thinking partner, not an autopilot that emits finished solutions. The exact
# production prompt is proprietary; this is a faithful reconstruction of that method.
_PERSONA = (
    "You are the AI collaborator inside SolveIt, a computational notebook where a "
    "person solves problems with code in small, deliberate steps. SolveIt follows "
    "George Pólya's \"How to Solve It\": understand the problem, devise a plan, carry "
    "it out one step at a time, then look back and reflect.\n\n"
    "Your job is to help the user think — not to think for them. The human is the "
    "agent; you are a partner who helps them reach their own understanding, not an "
    "autopilot that produces the whole solution. Working in small steps and writing "
    "code themselves is how they build real understanding, so protect that.\n\n"
    "Principles:\n"
    "- Small steps. Move one short step at a time. Prefer a few lines the user can run "
    "and understand over a large block that does everything. After a step, stop so they "
    "can run it and see the result before the next.\n"
    "- Understand first. Make sure the problem and goal are clear before proposing code. "
    "If something is ambiguous, ask a brief question rather than guessing.\n"
    "- Build on what's here. The notebook is shared state — refer to the variables, "
    "outputs, and notes already present; don't re-derive or re-import what exists.\n"
    "- Explain briefly. Give the reason a step makes sense in a sentence or two — enough "
    "to teach, not a lecture. Keep prose tight.\n"
    "- Keep code runnable. When you give code, make it minimal and ready to paste into "
    "the next cell of this kernel. Avoid pseudo-code and avoid dumping several unrelated "
    "cells at once.\n"
    "- Reflect. When a step works, note briefly what was learned and suggest the next "
    "small step, so the user stays in control of the direction.\n\n"
    "Be concise, concrete, and encouraging. Default to the smallest helpful next step."
)

# Appended after the persona when the dialog has prior cells.
_CONTEXT_INTRO = (
    "\n\nHere is the dialog so far (code, output, notes, and prior Q&A), in order. "
    "Treat it as shared state to answer the user's question.\n\n"
)

MISSING = ("[Claude (Max plan): the `claude` CLI isn't on PATH. Install Claude Code "
           "and run `claude` once to sign in to your subscription.]")

# Appended after the persona whenever the cell-editing MCP tools are live. Keeps
# the default Ask-AI experience unchanged: the model only touches cells on
# request — like SolveIt's dialoghelper, which acts only when you ask it to.
_TOOLS_GUIDANCE = (
    "\n\nYou also have MCP tools to edit this notebook directly: list_cells, "
    "update_cell, str_replace, and insert_cell. Use them ONLY when the user "
    "explicitly asks you to change, fix, refactor, complete, or add a cell. When "
    "you do edit, call list_cells first to get the exact cell id and current "
    "source, make the change, then briefly say what you changed. For an ordinary "
    "question, answer in text — never modify cells unasked."
)

# The MCP tool names Claude must be allowed to call non-interactively in `-p`
# mode (server key "cells" + tool name → mcp__cells__<tool>).
_ALLOWED_TOOLS = [f"mcp__cells__{t}"
                  for t in ("list_cells", "update_cell", "str_replace", "insert_cell")]


def _cell_tools_enabled() -> bool:
    """Cell-editing tools are live only inside the web-app process, which sets the
    shared token + loopback URL when it boots (see app._init_cell_tools). The
    kernel server imports this module but never sets them, so its path stays lean.
    Set SIDEKICK_CELL_TOOLS=0 to opt out (e.g. for the leanest TTFT)."""
    if os.environ.get("SIDEKICK_CELL_TOOLS", "1") == "0":
        return False
    return bool(os.environ.get("SIDEKICK_MCP_TOKEN") and os.environ.get("SIDEKICK_APP_URL"))


def _write_mcp_config(dialog: str) -> str:
    """Write a one-server MCP config (our stdio cell-editing server) for this turn
    and return its path. The server reaches back to the app to edit `dialog`, so
    the config carries the dialog + shared token in the spawned server's env."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # project root
    cfg = {"mcpServers": {"cells": {
        "command": sys.executable,
        "args": ["-m", "server.mcp_cells"],
        "env": {
            "PYTHONPATH": root,
            "SIDEKICK_APP_URL": os.environ["SIDEKICK_APP_URL"],
            "SIDEKICK_MCP_TOKEN": os.environ["SIDEKICK_MCP_TOKEN"],
            "SIDEKICK_DIALOG": dialog,
        },
    }}}
    path = os.path.join(_cli_cwd(), f"mcp-cells-{uuid.uuid4().hex[:8]}.json")
    with open(path, "w") as f:
        json.dump(cfg, f)
    return path


def system(context: str) -> str:
    """The SolveIt persona, plus the serialized notebook context when there is any.

    The persona always applies — including the first prompt in an empty dialog — so
    the assistant works in the SolveIt small-steps style from the very first turn.
    The notebook context is appended only when present.
    """
    return _PERSONA + (_CONTEXT_INTRO + context if context else "")


def _cli_cwd() -> str:
    """A neutral scratch dir so `claude` doesn't auto-load this repo's CLAUDE.md."""
    global _CLI_CWD
    if _CLI_CWD is None:
        import tempfile
        _CLI_CWD = tempfile.mkdtemp(prefix="sidekick-claude-")
    return _CLI_CWD


def claude_bin() -> str | None:
    import shutil
    return shutil.which("claude")


def _env() -> dict:
    env = dict(os.environ)
    env.pop("ANTHROPIC_API_KEY", None)     # force subscription auth, not API credits
    return env


def _build_cmd(dialog: str, content: str, context: str, stream: bool):
    """Build the argv and the session id we'll record. Returns (cmd, sid), or
    (None, None) if the `claude` binary isn't installed.

    Resumes the dialog's session when the context we already sent is still a
    prefix of the current context (append-only), sending just the new cells;
    otherwise starts a fresh session with the full context as a system preamble.
    """
    claude = claude_bin()
    if not claude:
        return None, None
    st = CLI_SESSIONS.get(dialog)
    resume = bool(st) and context.startswith(st["sent"])
    fmt = "stream-json" if stream else "json"
    cmd = [claude, "-p", "--output-format", fmt, "--disable-slash-commands",
           # Lean mode: skip MCP servers and *user* settings (hooks, auto-memory,
           # plugins). On a heavy global config these add ~1s+ of per-turn latency,
           # and a notebook assistant needs none of them. Subscription auth is
           # unaffected (it's credentials, not a setting source). Big TTFT win.
           "--strict-mcp-config", "--setting-sources", "project"]
    if stream:                              # stream-json needs these to emit deltas
        cmd += ["--include-partial-messages", "--verbose"]
    tools = _cell_tools_enabled()
    if resume:
        sid = st["id"]
        delta = context[len(st["sent"]):].strip()
        user_msg = (f"New notebook cells since my last message:\n{delta}\n\n{content}"
                    if delta else content)
        cmd += ["--resume", sid]
    else:
        sid = str(uuid.uuid4())
        user_msg = content
        cmd += ["--session-id", sid]
        sysmsg = system(context)            # persona + full notebook context
        if tools:                           # teach the tools once, on the fresh turn
            sysmsg += _TOOLS_GUIDANCE
        cmd += ["--append-system-prompt", sysmsg]
    if tools:                               # register our cell-editing MCP server
        cmd += ["--mcp-config", _write_mcp_config(dialog),
                "--allowedTools", *_ALLOWED_TOOLS]
    model = os.environ.get("SIDEKICK_CLAUDE_CLI_MODEL")
    if model:                               # else inherit the subscription default
        cmd += ["--model", model]
    if tools:                               # `--allowedTools` is variadic; `--` stops
        cmd.append("--")                    # it from swallowing the prompt positional
    cmd.append(user_msg)                     # prompt is the trailing positional
    return cmd, sid


def _run_cli(cmd, cwd, env, timeout):
    """Blocking subprocess.run (one place for tests to patch)."""
    import subprocess
    return subprocess.run(cmd, capture_output=True, text=True,
                          cwd=cwd, env=env, timeout=timeout)


def call(dialog: str, content: str, context: str = "") -> str:
    """Non-streaming: one `claude -p` call, full text back. Used by the kernel server."""
    cmd, sid = _build_cmd(dialog, content, context, stream=False)
    if cmd is None:
        return MISSING
    try:
        r = _run_cli(cmd, _cli_cwd(), _env(), 180)
    except Exception as e:  # noqa: BLE001 — incl. TimeoutExpired
        return f"[Claude (Max plan) failed to run: {e}]"
    if r.returncode != 0:
        msg = (r.stderr or r.stdout or "").strip()[:400]
        return f"[Claude (Max plan) error: {msg or f'exit {r.returncode}'}]"
    try:
        data = json.loads(r.stdout)
    except (ValueError, TypeError):
        return (r.stdout or "").strip() or "[Claude (Max plan): empty response]"
    if data.get("is_error"):
        return f"[Claude (Max plan) error: {data.get('result') or 'unknown'}]"
    CLI_SESSIONS[dialog] = {"id": data.get("session_id") or sid, "sent": context}
    return data.get("result", "")


def _popen(cmd, cwd, env):
    """Streaming subprocess.Popen (one place for tests to patch)."""
    import subprocess
    return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            stdin=subprocess.DEVNULL, text=True, cwd=cwd, env=env)


def stream(dialog: str, content: str, context: str = ""):
    """Streaming: yield text deltas as Claude generates them (for the app's SSE route).

    Parses `claude -p --output-format stream-json` events, yielding each
    `content_block_delta` text fragment. Records the session id from the terminal
    `result` event so the next turn can resume + send only the delta — same
    contract as `call`.
    """
    cmd, sid = _build_cmd(dialog, content, context, stream=True)
    if cmd is None:
        yield MISSING
        return
    try:
        p = _popen(cmd, _cli_cwd(), _env())
    except Exception as e:  # noqa: BLE001
        yield f"[Claude (Max plan) failed to run: {e}]"
        return

    final_sid, got_any = sid, False
    for line in p.stdout:
        try:
            ev = json.loads(line)
        except (ValueError, TypeError):
            continue
        kind = ev.get("type")
        if kind == "stream_event":
            d = ev.get("event") or {}
            if d.get("type") == "content_block_delta":
                txt = (d.get("delta") or {}).get("text")
                if txt:
                    got_any = True
                    yield txt
        elif kind == "result":
            final_sid = ev.get("session_id") or sid
            if ev.get("is_error") and not got_any:
                yield f"[Claude (Max plan) error: {ev.get('result') or 'unknown'}]"
    try:
        p.wait(timeout=5)
    except Exception:  # noqa: BLE001 — reaping is best-effort
        pass
    # Advance the session: remember its id and the full context it now knows.
    CLI_SESSIONS[dialog] = {"id": final_sid, "sent": context}
