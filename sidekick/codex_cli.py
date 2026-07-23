"""Codex via the installed `codex` CLI — current local Codex auth/config.

This is the Codex-native sibling of ``sidekick.claude_cli``. It lets Sidekick use
the user's already-installed Codex CLI (ChatGPT login, configured provider, model
defaults, plugins/auth) without requiring an OpenAI API key in Sidekick.

The Codex CLI is a full coding agent, so this path is intentionally conservative:
it runs in a neutral scratch directory, uses a read-only sandbox, and sends the
SolveIt/Polya persona as prompt text. When the Sidekick web app is running, it
can expose the same loopback notebook-cell MCP tools as the Claude path; those
tools edit visible cells only and never run code.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from dataclasses import dataclass

from .claude_cli import (
    _CELL_EDIT_TURN_REMINDER,
    _DIAGRAM_GUIDANCE,
    _TOOLS_GUIDANCE,
    _wants_cell_edit,
    _wants_diagram,
    system,
)


_CODEX_MODEL_CONFIG = {
    "codex-gpt-5.5-high": ("gpt-5.5", "high"),
    "codex-gpt-5.5-xhigh": ("gpt-5.5", "xhigh"),
    "codex-gpt-5.6-luna-low": ("gpt-5.6-luna", "low"),
}
CLI_MODELS = set(_CODEX_MODEL_CONFIG)

MISSING = ("[Codex CLI: the `codex` binary isn't on PATH. Install Codex CLI "
           "and run `codex` once to sign in/configure it.]")

_CLI_CWD: str | None = None
CODEX_SESSIONS: dict[str, dict] = {}
_SESSION_VERSION = 1

_ALLOWED_TOOLS = ["list_cells", "read_cell", "update_cell", "str_replace", "insert_cell"]


@dataclass(frozen=True)
class _Cmd:
    argv: list[str]
    resume: bool
    session_id: str | None
    context: str
    mode: str | None
    model: str | None
    effort: str | None
    tools: bool


def codex_bin() -> str | None:
    return shutil.which("codex")


def _cli_cwd() -> str:
    """A neutral scratch dir so Codex doesn't auto-load this repo as a workspace."""
    global _CLI_CWD
    if _CLI_CWD is None:
        import tempfile
        _CLI_CWD = tempfile.mkdtemp(prefix="sidekick-codex-")
    return _CLI_CWD


def _cell_tools_enabled() -> bool:
    """Cell tools are live only inside the web-app process.

    The app publishes a loopback URL and shared token at startup. Reuse
    SIDEKICK_CELL_TOOLS so one switch controls both Claude and Codex tool paths.
    """
    if os.environ.get("SIDEKICK_CELL_TOOLS", "1") == "0":
        return False
    return bool(os.environ.get("SIDEKICK_MCP_TOKEN") and os.environ.get("SIDEKICK_APP_URL"))


def _prompt(content: str, context: str = "", mode: str | None = None,
            tools: bool = False) -> str:
    """Combine the shared SolveIt persona, notebook context, and user turn."""
    user_msg = content or ""
    if _wants_diagram(user_msg):
        user_msg += _DIAGRAM_GUIDANCE
    sysmsg = system(context, mode)
    if tools:
        sysmsg += _TOOLS_GUIDANCE
        boundary = (
            "You are answering inside SolveIt Sidekick. Do not run shell commands, "
            "create scratch files, or inspect unrelated filesystem state. You may "
            "use the provided notebook-cell MCP tools only when the user explicitly "
            "asks for a visible cell edit. The user still runs code cells."
        )
    else:
        boundary = (
            "You are answering inside SolveIt Sidekick, not acting as an autonomous "
            "coding agent. Do not run shell commands, edit files, create scratch files, "
            "or inspect the filesystem. Use only the notebook context below and answer "
            "in the visible small-steps SolveIt style."
        )
    return (
        f"{boundary}\n\n"
        "System / notebook instructions:\n"
        f"{sysmsg}\n\n"
        "User prompt:\n"
        f"{user_msg}"
    )


def _toml(value: object) -> str:
    """Enough TOML literal formatting for codex -c values used here."""
    return json.dumps(value)


def _mcp_overrides(dialog: str) -> list[str]:
    """Return `codex exec -c ...` args registering the Sidekick cell MCP server."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env = {
        "PYTHONPATH": root,
        "SIDEKICK_APP_URL": os.environ["SIDEKICK_APP_URL"],
        "SIDEKICK_MCP_TOKEN": os.environ["SIDEKICK_MCP_TOKEN"],
        "SIDEKICK_DIALOG": dialog,
    }
    pairs: list[tuple[str, object]] = [
        ("mcp_servers.cells.command", sys.executable),
        ("mcp_servers.cells.args", ["-m", "server.mcp_cells"]),
        ("mcp_servers.cells.enabled_tools", _ALLOWED_TOOLS),
        # Non-interactive exec cannot pause for approval. The MCP server itself is
        # loopback-token guarded and the prompt restricts use to explicit cell edits.
        ("mcp_servers.cells.default_tools_approval_mode", "approve"),
        ("mcp_servers.cells.startup_timeout_sec", 10),
        ("mcp_servers.cells.tool_timeout_sec", 60),
    ]
    pairs.extend((f"mcp_servers.cells.env.{k}", v) for k, v in env.items())
    out: list[str] = []
    for key, value in pairs:
        out += ["-c", f"{key}={_toml(value)}"]
    return out


def _model_config(model_id: str | None) -> tuple[str, str] | tuple[None, None]:
    """Map Sidekick model ids to concrete Codex CLI model + reasoning flags."""
    if model_id in _CODEX_MODEL_CONFIG:
        return _CODEX_MODEL_CONFIG[model_id]
    env = os.environ.get("SIDEKICK_CODEX_CLI_MODEL")
    if env:
        return env, os.environ.get("SIDEKICK_CODEX_CLI_REASONING", "high")
    return _CODEX_MODEL_CONFIG["codex-gpt-5.5-high"]


def _session_ok(st: dict | None, context: str, mode: str | None,
                model: str | None, effort: str | None, tools: bool) -> bool:
    return bool(
        st
        and context.startswith(st.get("sent", ""))
        and st.get("mode") == mode
        and st.get("model") == model
        and st.get("effort") == effort
        and st.get("tools") == tools
        and st.get("version") == _SESSION_VERSION
        and st.get("id")
    )


def _delta_prompt(content: str, context: str, sent: str) -> str:
    delta = context[len(sent):].strip()
    if not delta:
        return content
    return f"New notebook cells since my last message:\n{delta}\n\n{content}"


def _fresh_args(codex: str, stream: bool) -> list[str]:
    cmd = [
        codex,
        "exec",
        "--sandbox",
        "read-only",
        "--cd",
        _cli_cwd(),
        "--skip-git-repo-check",
        "--color",
        "never",
    ]
    if stream:
        cmd.append("--json")
    return cmd


def _resume_args(codex: str, stream: bool) -> list[str]:
    # `codex exec resume` has a narrower flag surface than fresh `exec`: no
    # --sandbox/--cd/--color. Preserve read-only intent through config and run
    # the subprocess from the same neutral cwd used to create the session.
    cmd = [
        codex,
        "exec",
        "resume",
        "-c",
        'sandbox_mode="read-only"',
        "--skip-git-repo-check",
    ]
    if stream:
        cmd.append("--json")
    return cmd


def _build_cmd_info(dialog: str, content: str, context: str = "",
                    mode: str | None = None, stream: bool = False,
                    model_id: str | None = None) -> _Cmd | None:
    codex = codex_bin()
    if not codex:
        return None
    tools = _cell_tools_enabled()
    model, effort = _model_config(model_id)
    st = CODEX_SESSIONS.get(dialog)
    resume = _session_ok(st, context, mode, model, effort, tools)
    sid = None
    if resume:
        sid = str(st["id"])
        cmd = _resume_args(codex, stream)
    else:
        cmd = _fresh_args(codex, stream)
    if tools:
        cmd += _mcp_overrides(dialog)
    if model:
        cmd += ["--model", model]
    if effort:
        cmd += ["-c", f"model_reasoning_effort={_toml(effort)}"]
    prompt = (_delta_prompt(content, context, str(st.get("sent", "")))
              if resume else _prompt(content, context, mode, tools=tools))
    if resume and tools and _wants_cell_edit(content):
        prompt += _CELL_EDIT_TURN_REMINDER
    if resume:
        cmd.append(sid)
    cmd.append(prompt)
    return _Cmd(cmd, resume, sid, context, mode, model, effort, tools)


def _build_cmd(dialog: str, content: str, context: str = "", mode: str | None = None,
               stream: bool = False, model: str | None = None) -> list[str] | None:
    info = _build_cmd_info(dialog, content, context, mode, stream, model_id=model)
    return None if info is None else info.argv


def _run_cli(cmd, cwd, env, timeout):
    """Blocking subprocess.run (one place for tests to patch)."""
    import subprocess
    return subprocess.run(cmd, capture_output=True, text=True,
                          cwd=cwd, env=env, timeout=timeout)


def _popen(cmd, cwd, env):
    """Streaming subprocess.Popen (one place for tests to patch)."""
    import subprocess
    return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            stdin=subprocess.DEVNULL, text=True, bufsize=1, cwd=cwd, env=env)


def _event_text(ev: dict) -> str:
    """Extract visible assistant text from a Codex JSONL event if present."""
    item = ev.get("item") or {}
    if item.get("type") != "agent_message":
        return ""
    # Current docs show item.completed with `item.text`. Be tolerant of possible
    # streamed/update shapes so the UI benefits if Codex emits partial messages.
    return item.get("text") or ev.get("text") or ""


def _session_id_from_event(ev: dict) -> str | None:
    for key in ("session_id", "thread_id", "conversation_id"):
        value = ev.get(key)
        if value:
            return str(value)
    item = ev.get("item") or {}
    for key in ("session_id", "thread_id", "conversation_id"):
        value = item.get(key)
        if value:
            return str(value)
    return None


def _record_session(dialog: str, info: _Cmd, session_id: str | None) -> None:
    sid = session_id or info.session_id
    if not sid:
        return
    CODEX_SESSIONS[dialog] = {
        "id": sid,
        "sent": info.context,
        "mode": info.mode,
        "model": info.model,
        "effort": info.effort,
        "tools": info.tools,
        "version": _SESSION_VERSION,
    }


def drop(dialog: str) -> None:
    """Evict a dialog's in-memory Codex session state."""
    CODEX_SESSIONS.pop(dialog, None)


def rename(old: str, new: str) -> None:
    """Carry a dialog's in-memory Codex session state across a dialog rename."""
    if old in CODEX_SESSIONS and new:
        CODEX_SESSIONS[new] = CODEX_SESSIONS.pop(old)


def call(dialog: str, content: str, context: str = "", model: str | None = None,
         mode: str | None = None) -> str:
    """Run one non-streaming Codex CLI turn and return the final stdout text."""
    info = _build_cmd_info(dialog, content, context, mode, stream=True, model_id=model)
    if info is None:
        return MISSING
    try:
        r = _run_cli(info.argv, _cli_cwd(), dict(os.environ), 180)
    except Exception as e:  # noqa: BLE001 — incl. TimeoutExpired
        return f"[Codex CLI failed to run: {e}]"
    if r.returncode != 0:
        msg = (r.stderr or r.stdout or "").strip()[:600]
        return f"[Codex CLI error: {msg or f'exit {r.returncode}'}]"

    session_id = None
    last_text = ""
    saw_json = False
    for line in (r.stdout or "").splitlines():
        try:
            ev = json.loads(line)
        except (ValueError, TypeError):
            continue
        saw_json = True
        session_id = _session_id_from_event(ev) or session_id
        if ev.get("type") in {"turn.failed", "error"}:
            return f"[Codex CLI error: {ev.get('message') or ev.get('error') or 'unknown'}]"
        text = _event_text(ev)
        if text:
            last_text = text

    if saw_json:
        _record_session(dialog, info, session_id)
        return last_text.strip() or "[Codex CLI: empty response]"
    _record_session(dialog, info, session_id)
    return (r.stdout or "").strip() or "[Codex CLI: empty response]"


def stream(dialog: str, content: str, context: str = "", model: str | None = None,
           mode: str | None = None):
    """Yield Codex CLI assistant text as it appears in `codex exec --json` output."""
    info = _build_cmd_info(dialog, content, context, mode, stream=True, model_id=model)
    if info is None:
        yield MISSING
        return
    try:
        p = _popen(info.argv, _cli_cwd(), dict(os.environ))
    except Exception as e:  # noqa: BLE001
        yield f"[Codex CLI failed to run: {e}]"
        return

    last_text = ""
    got_any = False
    session_id = None
    terminal_error = False
    try:
        for line in iter(p.stdout.readline, ""):
            try:
                ev = json.loads(line)
            except (ValueError, TypeError):
                continue
            session_id = _session_id_from_event(ev) or session_id
            if ev.get("type") in {"turn.failed", "error"} and not got_any:
                yield f"[Codex CLI error: {ev.get('message') or ev.get('error') or 'unknown'}]"
                got_any = True
                terminal_error = True
                continue
            if ev.get("type") in {"turn.failed", "error"}:
                terminal_error = True
                continue
            text = _event_text(ev)
            if not text:
                continue
            if text.startswith(last_text):
                delta = text[len(last_text):]
            else:
                delta = text
            last_text = text
            if delta:
                got_any = True
                yield delta
        try:
            p.wait(timeout=5)
        except Exception:  # noqa: BLE001
            pass
        returncode = getattr(p, "returncode", 0)
        if not got_any and returncode:
            terminal_error = True
            yield f"[Codex CLI error: exit {p.returncode}]"
        if not terminal_error and not returncode:
            _record_session(dialog, info, session_id)
    finally:
        poll = getattr(p, "poll", None)
        if poll is not None and poll() is None:
            p.terminate()
            try:
                p.wait(timeout=5)
            except Exception:  # noqa: BLE001
                p.kill()
