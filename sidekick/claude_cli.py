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
import re
import sys
import uuid

from . import tools_config

_CLI_MODEL_CONFIG = {
    f"claude-{model}-{effort}": (model, effort)
    for model in ("fable", "sonnet", "opus")
    for effort in ("low", "medium", "high", "xhigh", "max")
}
CLI_MODELS = set(_CLI_MODEL_CONFIG)
CLI_SESSIONS: dict[str, dict] = {}
# Running token/cost totals per dialog, folded in from each turn's terminal
# `result` event (see _accrue). The `claude` CLI reports a usage breakdown — and,
# on most setups, total_cost_usd — even under subscription auth, so this is an
# honest tally of what the session would bill at API rates. In-memory and
# per-dialog, same lifecycle as CLI_SESSIONS (both rebuilt on restart).
CLI_COST: dict[str, dict] = {}
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
    "- Diagrams as Mermaid. When a picture would make a step clearer — a flowchart, "
    "sequence, graph, tree, or state diagram — express it as a ```mermaid fenced code "
    "block, which this notebook renders as a real SVG diagram. Never draw diagrams as "
    "ASCII art. Keep node labels short.\n"
    "- Reflect. When a step works, note briefly what was learned and suggest the next "
    "small step, so the user stays in control of the direction.\n\n"
    "Be concise, concrete, and encouraging. Default to the smallest helpful next step."
)

# AI modes (SolveIt's learning / concise / standard). The base persona above is
# the always-on small-steps foundation; a mode appends a short directive that
# tunes how much the AI questions, explains, and hands over. `learning` is the
# default — it's the point of the tool (help the user reach their own
# understanding), and the persona already leans that way.
DEFAULT_MODE = "learning"
_MODE_DIRECTIVES = {
    "learning": (
        "\n\nMODE — Learning. Favour the user's understanding over a finished answer. "
        "Before writing code, check they've thought it through: ask one short guiding "
        "question, or offer a hint, and leave room for them to try. When they're stuck "
        "or ask directly, show the smallest piece that unblocks them and explain the "
        "idea behind it — never hand over a whole solution they could have reached "
        "themselves. Prefer a leading question to a declaration."
    ),
    "concise": (
        "\n\nMODE — Concise. Minimal prose. Give the next step or answer directly: "
        "compact, runnable code, no boilerplate, no preamble or recap. At most one "
        "sentence of reasoning, and only when it's needed."
    ),
    "standard": (
        "\n\nMODE — Standard. Answer helpfully and completely. Explain your reasoning, "
        "and when asked for code give a complete, correct version with enough "
        "explanation to follow it. Still build on the notebook's existing state, and "
        "still leave the running of code to the user."
    ),
}
# (id, label) for the UI selector — order is display order.
AI_MODES = [("learning", "Learning"), ("concise", "Concise"), ("standard", "Standard")]


def _mode_block(mode: str | None) -> str:
    return _MODE_DIRECTIVES.get(mode or DEFAULT_MODE, _MODE_DIRECTIVES[DEFAULT_MODE])


# Appended after the persona when the dialog has prior cells.
_CONTEXT_INTRO = (
    "\n\nHere is the dialog so far (code, output, notes, and prior Q&A), in order. "
    "Treat it as shared state to answer the user's question.\n\n"
)

MISSING = ("[Claude (Max plan): the `claude` CLI isn't on PATH. Install Claude Code "
           "and run `claude` once to sign in to your subscription.]")

# Just-in-time guidance, appended to the *user turn* (not the persona) only when that
# turn asks for a diagram — see `_wants_diagram`. Kept out of the always-on system
# prompt on purpose: it's irrelevant to most turns, and on a resumed session the persona
# isn't re-sent, so injecting it here puts the conventions right where they're needed —
# fresh and salient on the turn that actually wants the picture, not buried at turn 1.
_DIAGRAM_GUIDANCE = (
    "\n\n[Guidance for any Mermaid diagram in this reply — adapt to what was asked. The "
    "diagram is shown in a narrow reading column, so WIDTH is the scarce dimension: a "
    "diagram wider than the column is scaled down and its labels get small. Always grow "
    "DOWNWARD, never sideways. For a neural-net / model architecture ALWAYS use "
    "`flowchart TD` (top-down, data-flow order) — never `LR`/`RL`. Keep each rank to a "
    "single node where you can: stack stages vertically rather than placing sibling nodes "
    "side by side, and avoid wide rows. Wrap each stage or module in its own `subgraph`; "
    "inside a subgraph, keep it vertical with `direction TB` (NOT `direction TD` — the "
    "subgraph parser rejects the TD alias) or just omit the direction line; "
    "put the module's key hyperparameters inside its node (dims, heads, kernel, layer "
    "count) with `<br/>` for extra lines; show how shapes evolve by labelling the edge "
    "between blocks with the running tensor shape, e.g. `A -->|\"(B, N, 384)\"| B`; "
    "distinguish inputs/outputs from internal blocks; collapse repeated blocks as "
    "`N× …` rather than drawing each one. Keep node labels short so columns stay narrow.]"
)

# Matches a turn that's asking for a drawn diagram (mermaid / flowchart / architecture
# picture). Deliberately broad-but-cheap: a miss just falls back to the base persona
# line (still a valid mermaid diagram), so false negatives are low-cost; false positives
# only add a short note to a turn that wasn't going to draw anything.
_DIAGRAM_RE = re.compile(
    r"\b(mermaid|flowchart|diagram|architecture|sequence diagram|state diagram|"
    r"graph it|draw|sketch|visuali[sz]e|schematic)\b",
    re.IGNORECASE,
)


def _wants_diagram(content: str) -> bool:
    return bool(content) and bool(_DIAGRAM_RE.search(content))


# Appended after the persona whenever the cell-editing MCP tools are live. Keeps
# the default Ask-AI experience unchanged: the model only touches cells on
# request — like SolveIt's dialoghelper, which acts only when you ask it to.
_TOOLS_GUIDANCE = (
    "\n\nYou also have MCP tools to edit this notebook directly: list_cells, "
    "read_cell, update_cell, str_replace, and insert_cell. Use them ONLY when the "
    "user explicitly asks you to change, fix, refactor, complete, or add a cell. "
    "list_cells truncates very long cells; call read_cell for a single cell's full "
    "source (e.g. before a str_replace on a long cell). Every "
    "cell above carries n=\"<number>\" (matching the number the user sees) and "
    "id=\"<id>\"; the edit tools target a cell by its id. So when the user says "
    "\"fix cell 3\" or names a function, find that cell's id from the context and "
    "edit it directly. When they say \"previous cell\", \"cell above\", \"last "
    "cell\", or \"the cell before this Ask AI prompt\", they mean the immediately "
    "preceding context cell: use that cell's id directly. Only call list_cells if "
    "the target id isn't already clear. If the user explicitly asks for a cell "
    "edit, actually call update_cell, str_replace, or insert_cell; do not merely "
    "describe the edit. After editing, briefly say what you changed. For an "
    "ordinary question, answer in text — never modify cells unasked.\n"
    "These tools don't change the small-steps contract: edit the one cell the user "
    "pointed at, in the smallest change that does the job, and stop so they can run "
    "it. Don't spray a finished multi-cell solution across the notebook with "
    "insert_cell — that's the autopilot behavior small steps exists to prevent. The "
    "user still runs every cell; you never execute code."
)

_CELL_EDIT_ACTION_RE = re.compile(
    r"\b(change|fix|refactor|rewrite|update|modify|replace|insert|add|append|"
    r"prepend|complete|fill\s+in|clean\s+up|rename|remove|delete|edit)\b",
    re.IGNORECASE,
)
_CELL_EDIT_TARGET_RE = re.compile(
    r"\b(cell|above|below|previous|prev|last|preceding|prior|earlier|notebook)\b",
    re.IGNORECASE,
)

_CELL_EDIT_TURN_REMINDER = (
    "\n\nNotebook-cell edit reminder: if this prompt explicitly asks for a visible "
    "cell edit, call update_cell, str_replace, or insert_cell instead of only "
    "describing the change. \"Previous cell\", \"cell above\", \"last cell\", and "
    "\"the cell before this Ask AI prompt\" mean the immediately preceding context "
    "cell; use that cell's id directly when it is clear."
)


def _wants_cell_edit(content: str) -> bool:
    return bool(content) and bool(_CELL_EDIT_ACTION_RE.search(content)) \
        and bool(_CELL_EDIT_TARGET_RE.search(content))

# The MCP tool names Claude must be allowed to call non-interactively in `-p`
# mode (server key "cells" + tool name → mcp__cells__<tool>).
_ALLOWED_TOOLS = [f"mcp__cells__{t}"
                  for t in ("list_cells", "read_cell", "update_cell",
                            "str_replace", "insert_cell")]

# Which tools the agent may/may not use is declarative — see sidekick.tools_config
# (allow: web research etc.; deny: the executor's hands Write/Edit/Bash). The
# cell-editing MCP tools are wired separately below (they need the MCP server).


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


def system(context: str, mode: str | None = None) -> str:
    """The SolveIt persona + the active mode directive, plus the serialized notebook
    context when there is any.

    The persona always applies — including the first prompt in an empty dialog — so
    the assistant works in the SolveIt small-steps style from the very first turn.
    `mode` tunes how much it questions/explains (learning/concise/standard). The
    notebook context is appended only when present.
    """
    return _PERSONA + _mode_block(mode) + (_CONTEXT_INTRO + context if context else "")


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


def _model_config(model_id: str | None) -> tuple[str | None, str | None]:
    """Map Sidekick model ids to Claude Code --model/--effort flags."""
    if model_id in _CLI_MODEL_CONFIG:
        return _CLI_MODEL_CONFIG[model_id]
    env_model = os.environ.get("SIDEKICK_CLAUDE_CLI_MODEL")
    if env_model:
        return env_model, os.environ.get("SIDEKICK_CLAUDE_CLI_EFFORT", "high")
    return None, None


def _build_cmd(dialog: str, content: str, context: str, stream: bool,
               model: str | None = None, mode: str | None = None):
    """Build the argv and the session id we'll record. Returns (cmd, sid), or
    (None, None) if the `claude` binary isn't installed.

    Resumes the dialog's session when the context we already sent is still a
    prefix of the current context (append-only), sending just the new cells;
    otherwise starts a fresh session with the full context as a system preamble.
    """
    claude = claude_bin()
    if not claude:
        return None, None
    model_flag, effort_flag = _model_config(model)
    st = CLI_SESSIONS.get(dialog)
    # Resume only while the context is still a prefix AND the mode is unchanged —
    # the mode directive lives in the session's system prompt, which a resume can't
    # rewrite, so switching mode must start a fresh session to actually take effect.
    resume = bool(st) and context.startswith(st["sent"]) and st.get("mode") == mode \
        and st.get("model") == model_flag and st.get("effort") == effort_flag
    fmt = "stream-json" if stream else "json"
    cmd = [claude, "-p", "--output-format", fmt, "--disable-slash-commands",
           # Lean mode: skip MCP servers and *user* settings (hooks, auto-memory,
           # plugins). On a heavy global config these add ~1s+ of per-turn latency,
           # and a notebook assistant needs none of them. Subscription auth is
           # unaffected (it's credentials, not a setting source). Big TTFT win.
           "--strict-mcp-config", "--setting-sources", "project"]
    # Off-screen tools stay off. `claude -p` is the full agent; left alone it also
    # has Write/Edit/Bash and would write the whole solution to a scratchpad and run
    # it off-screen — taking the executor's seat the human is supposed to hold. The
    # deny list (default Write/Edit/Bash, see tools_config) keeps its only move the
    # visible, in-notebook kind: the SolveIt contract.
    deny = tools_config.deny_list()
    if deny:
        cmd += ["--disallowed-tools", *deny]
    if stream:                              # stream-json needs these to emit deltas
        cmd += ["--include-partial-messages", "--verbose"]
    tools = _cell_tools_enabled()
    if resume:
        sid = st["id"]
        delta = context[len(st["sent"]):].strip()
        user_msg = (f"New notebook cells since my last message:\n{delta}\n\n{content}"
                    if delta else content)
        if tools and _wants_cell_edit(content):
            user_msg += _CELL_EDIT_TURN_REMINDER
        cmd += ["--resume", sid]
    else:
        sid = str(uuid.uuid4())
        user_msg = content
        cmd += ["--session-id", sid]
        sysmsg = system(context, mode)      # persona + mode + full notebook context
        if tools:                           # teach the tools once, on the fresh turn
            sysmsg += _TOOLS_GUIDANCE
        cmd += ["--append-system-prompt", sysmsg]
    allow = list(tools_config.allow_list())
    if tools:                               # register our cell-editing MCP server
        cmd += ["--mcp-config", _write_mcp_config(dialog)]
        allow += _ALLOWED_TOOLS
    if allow:
        cmd += ["--allowedTools", *allow]
    if model_flag:
        cmd += ["--model", model_flag]
    if effort_flag:
        cmd += ["--effort", effort_flag]
    if _wants_diagram(content):             # just-in-time: attach diagram conventions to
        user_msg += _DIAGRAM_GUIDANCE       # the turn that asks, not the global persona
    if allow:                               # `--allowedTools` is variadic; `--` stops
        cmd.append("--")                    # it from swallowing the prompt positional
    cmd.append(user_msg)                     # prompt is the trailing positional
    return cmd, sid


def _run_cli(cmd, cwd, env, timeout):
    """Blocking subprocess.run (one place for tests to patch)."""
    import subprocess
    return subprocess.run(cmd, capture_output=True, text=True,
                          cwd=cwd, env=env, timeout=timeout)


def _accrue(dialog: str, result: dict) -> None:
    """Fold one turn's terminal `result` object into the dialog's running totals.

    `result` is what the CLI emits at end of turn (the whole JSON in non-stream
    mode, or the final `result` event in stream mode). It carries `total_cost_usd`
    and a `usage` block — both reported even under subscription auth — so we just
    sum them. We only store what the CLI gives us; pricing/formatting is the
    caller's job (see app._cost_label), keeping this a pure measurement.
    """
    u = result.get("usage") or {}
    agg = CLI_COST.setdefault(dialog, {"usd": 0.0, "turns": 0, "input": 0,
                                       "output": 0, "cache_read": 0, "cache_write": 0})
    agg["usd"] += float(result.get("total_cost_usd") or 0.0)
    agg["turns"] += 1
    agg["input"] += int(u.get("input_tokens") or 0)
    agg["output"] += int(u.get("output_tokens") or 0)
    agg["cache_read"] += int(u.get("cache_read_input_tokens") or 0)
    agg["cache_write"] += int(u.get("cache_creation_input_tokens") or 0)


def cost_for(dialog: str) -> dict | None:
    """The dialog's running cost/usage totals, or None if it hasn't run a turn."""
    return CLI_COST.get(dialog)


def drop(dialog: str) -> None:
    """Evict a dialog's in-memory CLI session and running cost totals.

    Called when a dialog is deleted so a later dialog reusing the name can't
    resume a stale `claude` session or inherit an old cost tally. Best-effort:
    a dialog that never ran has no entries, which is fine."""
    CLI_SESSIONS.pop(dialog, None)
    CLI_COST.pop(dialog, None)


def call(dialog: str, content: str, context: str = "", model: str | None = None,
         mode: str | None = None) -> str:
    """Non-streaming: one `claude -p` call, full text back. Used by the kernel server."""
    cmd, sid = _build_cmd(dialog, content, context, stream=False, model=model, mode=mode)
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
    model_flag, effort_flag = _model_config(model)
    CLI_SESSIONS[dialog] = {
        "id": data.get("session_id") or sid, "sent": context, "mode": mode,
        "model": model_flag, "effort": effort_flag}
    _accrue(dialog, data)
    return data.get("result", "")


def _popen(cmd, cwd, env):
    """Streaming subprocess.Popen (one place for tests to patch). bufsize=1 makes
    the pipe line-buffered so each stream-json line is readable the moment Claude
    flushes it — paired with readline() below, tokens arrive without read-ahead lag."""
    import subprocess
    return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            stdin=subprocess.DEVNULL, text=True, bufsize=1, cwd=cwd, env=env)


def stream(dialog: str, content: str, context: str = "", model: str | None = None,
           mode: str | None = None):
    """Streaming: yield text deltas as Claude generates them (for the app's SSE route).

    Parses `claude -p --output-format stream-json` events, yielding each
    `content_block_delta` text fragment. Records the session id from the terminal
    `result` event so the next turn can resume + send only the delta — same
    contract as `call`.
    """
    cmd, sid = _build_cmd(dialog, content, context, stream=True, model=model, mode=mode)
    if cmd is None:
        yield MISSING
        return
    try:
        p = _popen(cmd, _cli_cwd(), _env())
    except Exception as e:  # noqa: BLE001
        yield f"[Claude (Max plan) failed to run: {e}]"
        return

    final_sid, got_any, final_result = sid, False, None
    # readline() (not `for line in p.stdout`) avoids the iterator's read-ahead
    # buffer, so each line surfaces as soon as Claude emits it — real streaming.
    try:
        for line in iter(p.stdout.readline, ""):
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
                final_result = ev                    # carries usage + total_cost_usd
                if ev.get("is_error") and not got_any:
                    yield f"[Claude (Max plan) error: {ev.get('result') or 'unknown'}]"
        try:
            p.wait(timeout=5)
        except Exception:  # noqa: BLE001 — reaping is best-effort
            pass
        # Advance the session: remember its id, the full context it now knows, and the
        # mode it was started with (a mode change forces a fresh session — see _build_cmd).
        model_flag, effort_flag = _model_config(model)
        CLI_SESSIONS[dialog] = {
            "id": final_sid, "sent": context, "mode": mode,
            "model": model_flag, "effort": effort_flag}
        if final_result is not None and not final_result.get("is_error"):
            _accrue(dialog, final_result)            # running token/cost tally
    finally:
        # If the SSE consumer abandons the generator (GeneratorExit on .close())
        # mid-stream, the loop above never finishes and the `claude -p` child would
        # otherwise leak. Reap it so an abandoned request never orphans a process.
        # (getattr guard: tolerate lightweight fake processes that omit poll().)
        poll = getattr(p, "poll", None)
        if poll is not None and poll() is None:
            p.terminate()
            try:
                p.wait(timeout=5)
            except Exception:  # noqa: BLE001
                p.kill()
