# AGENTS.md

Codex reads this file as the repository-specific operating guide for
SolveIt Sidekick. It is the Codex-native counterpart to `CLAUDE.md`; keep the two
in sync when changing project conventions.

## What this is

A Claude-desktop-style web UI for SolveIt: a notebook of editable cells
(`note` / `code` / `Ask AI`) that runs against a local kernel, a remote H100
through an SSH tunnel, or a real Answer.AI SolveIt server. The README is the
source of truth for user-facing behavior.

## SolveIt / Polya behavior to preserve

This project is explicitly not an autopilot. Its Ask-AI experience should follow
George Pólya's "How to Solve It": understand the problem, devise a plan, carry it
out in small steps, then look back and reflect.

When modifying AI behavior, prompts, models, tools, or notebook-editing flows:

- Preserve the core contract: **the human is the agent; the AI is a thinking
  partner**.
- Prefer small, inspectable notebook-visible steps over hidden off-screen work.
- Do not let the spawned Ask-AI assistant run code, write scratch files, or solve
  whole tasks behind the user's back.
- Keep visible cell editing separate from code execution: AI may edit cells only
  when explicitly asked; the user still runs code cells.
- Treat `sidekick/claude_cli.py` as the canonical home of the SolveIt persona,
  mode directives, diagram guidance, and cell-tool guidance. If adding or changing
  a provider, make it consume the same `system(context, mode)` contract unless
  there is a deliberate, documented reason not to.
- The `learning` mode should guide and unblock; `concise` should be terse;
  `standard` should answer fully while still respecting notebook context.

## Commands

Use `uv` for Python/dependency execution and `just` for common tasks.

```bash
just test                                # uv run pytest -q
uv run pytest tests/test_review_backend.py
uv run pytest tests/test_review_backend.py -k budget
just lint                                # uv run ruff check
just dev                                 # kernel :5055 + UI :8000, foreground
just start [reload]                      # background services; stop with just stop
just doctor                              # diagnose the kernel target
uv run python -m sidekick.cli serve       # UI only; assumes kernel is already up
```

The kernel server needs the coding stack:

```bash
uv run --extra kernel python -m server.kernel_server --port 5055
```

Without the `kernel` extra, the UI can fall back to the in-memory mock
(grey LED, no real code execution).

Live tests skip unless `SOLVEIT_LIVE_URL` is set, so the normal suite should run
offline with no live SolveIt server, subprocess, or network. Test files named
`test_review_*.py` harden specific subsystems.

## Architecture map

- `sidekick/app.py` — FastHTML web app, routes, rendering, state, htmx/SSE wiring.
- `sidekick/static/js/*` — thin client-side behavior; vendored libraries are under
  `sidekick/static/vendor/`. Avoid CDNs.
- `sidekick/client.py` — backend abstraction and notebook context serialization.
- `sidekick/targets.py` / `targets.yaml` — local, kernel, and H100 target profiles.
- `sidekick/tunnel.py` — SSH tunnel lifecycle for remote targets.
- `sidekick/claude_cli.py` — subscription-backed Claude Code path plus the shared
  SolveIt persona/prompt contract.
- `server/kernel_server.py` — self-hostable SolveIt-compatible kernel server,
  provider API calls, code execution, rich outputs, completions, and
  dollar-backtick expression injection.
- `server/mcp_cells.py` — loopback MCP tools for visible notebook cell edits.

## Dependency boundaries

`pyproject.toml` deliberately separates dependency groups:

- Base dependencies run Sidekick itself: FastHTML, uvicorn, renderers, and AI SDKs.
- `kernel` is the notebook coding environment: numpy/pandas/torch/etc.
- Other extras include `paper`, `web`, `solveit`, `nbdev`, and `all`.

When adding dependencies, put them where they actually run. The app and kernel may
live in different virtual environments or on different machines.

## Ask AI routes and tool boundaries

Ask AI has two implementation families:

- API providers (`claude`, `glm`, `codex`) go through SDK/provider code in the
  kernel server.
- The installed Codex CLI model (`codex-cli`, default) shells out through
  `sidekick/codex_cli.py`, using the user's current local `codex` auth/config. It
  should stay conservative: neutral scratch directory, read-only sandbox, streamed
  JSONL output, and the shared SolveIt persona wrapped into the prompt. When the
  web app has published its loopback token, it may use the same MCP cell-editing
  tools as Claude, but only for explicit visible notebook edits.
- Subscription-backed Claude models (`claude-cli`; `claude-cli-fast`) shell out
  through `sidekick/claude_cli.py`.

The subscription path invokes a full Claude Code agent, so it must keep
`Write`, `Edit`, and `Bash` disallowed by default. Its only notebook-editing powers
should be the local MCP cell tools, guarded by `SIDEKICK_CELL_TOOLS` and used only
on explicit user request.

Tool policy for the spawned Ask-AI assistant is configured in
`~/.config/solveit-sidekick/tools.json` via `sidekick/tools_config.py`. Defaults
allow web research and deny hidden execution/editing.

## Data and secrets

- Dialogs, papers, and blog exports are stored under `data/` when `SIDEKICK_DATA`
  points there; the repo is private and may be used for sync/backup.
- API keys live outside the repo in
  `~/.config/solveit-sidekick/secrets.json` via `sidekick/secrets_store.py`.
- Environment variables override stored keys.
- Never commit API keys, SolveIt tokens, cookies, or tunnel credentials.

## Library and blog generation

`sidekick/nbdev_export.py` and `sidekick/libraries.py` generate packages from
cells tagged `#| export lib:module`. `sidekick/blog.py` and `sidekick/export.py`
project dialogs to Quarto/blog artifacts.

Generated code and generated notebooks should not be hand-edited; edit the source
cells and rebuild.

## Engineering conventions

- Prefer existing patterns and small, reversible diffs.
- Broad `except ... # noqa: BLE001` handlers are intentional graceful degradation
  points; use `_dbg()` in `sidekick/app.py` for diagnosability.
- Rendering is server-side and offline by design.
- Keep security boundaries explicit: loopback-only internal routes, token/cookie
  checks for exposed kernel routes, sanitized rich output, and no `shell=True`
  subprocesses unless there is a specific reviewed reason.
- Before claiming completion, run the smallest relevant tests or checks. For
  behavior changes, prefer targeted tests first, then `just test`/`just lint` when
  scope warrants it.

## Known structural debt

`sidekick/app.py` and its cell-editing view are large. Refactor them only with a
plan and regression tests; do not mix broad structural cleanup with unrelated
feature or security changes.
