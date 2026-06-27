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
import uuid

CLI_MODELS = {"claude-cli"}
CLI_SESSIONS: dict[str, dict] = {}
_CLI_CWD: str | None = None

_PREAMBLE = (
    "You are an AI assistant embedded in a computational notebook — a SolveIt-style "
    "dialog of code, output, notes, and prior Q&A. The notebook so far is below, in "
    "order. Use it as context to answer the user's question; refer to variables, "
    "results, and notes already present.\n\n"
)

MISSING = ("[Claude (Max plan): the `claude` CLI isn't on PATH. Install Claude Code "
           "and run `claude` once to sign in to your subscription.]")


def system(context: str) -> str | None:
    """The system preamble + serialized notebook context (or None if empty)."""
    return (_PREAMBLE + context) if context else None


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
        sysmsg = system(context)            # preamble + full notebook context
        if sysmsg:
            cmd += ["--append-system-prompt", sysmsg]
    model = os.environ.get("SIDEKICK_CLAUDE_CLI_MODEL")
    if model:                               # else inherit the subscription default
        cmd += ["--model", model]
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
