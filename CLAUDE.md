# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Claude-desktop-style web UI for [SolveIt](https://solve.it.com) — a notebook of
editable cells (note / code / Ask-AI) that runs against a **local kernel**, a
**remote H100** (over an SSH tunnel), or a real **Answer.AI SolveIt server**,
switchable from a dropdown with no change to how you work. The README is unusually
complete on user-facing behavior; read it for feature specifics.

## Commands

Everything runs through [uv](https://docs.astral.sh/uv/) (manages Python + deps;
never `pip`/venv by hand) and [just](https://github.com/casey/just) (task runner).

```bash
just test                      # run the suite  (== uv run pytest -q)
uv run pytest tests/test_review_backend.py            # single file
uv run pytest tests/test_review_backend.py -k budget  # single test by name
just lint                      # lint  (== uv run ruff check)
just dev                       # kernel (:5055) + UI (:8000) in foreground, Ctrl+C stops both
just start [reload]            # same, backgrounded; `just stop` to kill; `reload` auto-restarts UI on edits
just doctor                    # diagnose the kernel target (DNS/port/token/route)
uv run python -m sidekick.cli serve   # UI only (assumes kernel already up)
```

The kernel server needs the coding stack: `uv run --extra kernel python -m
server.kernel_server --port 5055`. Without `--extra kernel` it won't start and the
UI silently falls back to the in-memory mock (grey LED, no code execution).

Live tests skip unless `SOLVEIT_LIVE_URL` is set, so the suite runs offline with no
server, subprocess, or network. Test files are named `test_review_*.py` by the
subsystem they harden (backend, kernel, app, audio, recall, scaffold, s1/s5/s6…).

## Architecture

Three layers, each swappable:

**UI** — `sidekick/app.py` (~3200 lines, the bulk of the app) is a single FastHTML
server. Cells render server-side (mistune for markdown, Pygments for code — offline,
no CDN); interactions swap only the conversation via htmx, never a full reload.
Client-side behavior lives in `sidekick/static/js/*` (thin, vendored libs in
`static/vendor/`, no build step). `sidekick/cli.py` is the `sidekick` entry point
(`targets` / `doctor` / `up` / `serve` / `library build` / `blog build`).

**Client / backends** — `sidekick/client.py` wraps the notion of a "backend" behind
one `connect()`. Three backends, chosen by `targets.yaml`:
- `solveit` — real Answer.AI server via `solveit_client` (`solveit` extra).
- `kernel` — the bundled `server/kernel_server.py`, a self-hostable SolveIt-compatible
  server that executes real Python in a persistent per-dialog namespace.
- mock — in-memory fallback when nothing is reachable (keeps the UI demoable).

A **target** is just "which server URL the client points at" (`sidekick/targets.py`
resolves profiles from `targets.yaml`; tokens come from env vars, never committed).
The local↔H100 switch is entirely `sidekick/tunnel.py` opening an SSH tunnel so the
remote SolveIt port appears on localhost — the client code and URL stay constant.

**Kernel server** (`server/`) — an HTTP process, deliberately separate from the app
so it can run in its own venv or on the H100. It owns code execution, rich-output
capture (matplotlib figures, `_repr_html_`/`_repr_png_`), jedi completions, and
`$`…`` variable injection against the live namespace. `server/mcp_cells.py` is a
tiny loopback MCP server exposing `list_cells`/`update_cell`/`str_replace`/`insert_cell`.

### Two dependency groups — keep them apart

`pyproject.toml` splits **the server** (base install: FastHTML, uvicorn, renderers,
AI SDKs — light) from **the coding environment** (`kernel` extra: numpy/pandas/torch/…
— what *notebook code* imports). They serve different jobs and can live in different
venvs/machines. Other extras: `paper` (marker-pdf), `web` (trafilatura), `solveit`,
`nbdev`, `all`. When adding a dep, put it in the group that matches where it runs.

### Ask AI — two routes

- **API providers** (`claude`/`glm`/`codex`) go through the SDKs in `client.py`.
- **Subscription** (`claude-cli`, the default) shells out via `sidekick/claude_cli.py`
  to `claude -p` on the user's Max plan. It is the full Claude Code agent, so it is
  launched with `--disallowed-tools Write Edit Bash`: the AI may make visible,
  in-notebook edits (via the cell MCP tools) but never runs code or works off-screen.
  This enforces SolveIt's posture — the human is the agent; the AI is a thinking
  partner. Preserve that boundary when touching this path.

Which tools Ask AI may use is declarative: `~/.config/solveit-sidekick/tools.json`
(defaults allow web research, deny Write/Edit/Bash); cell-editing tools are gated by
`SIDEKICK_CELL_TOOLS`.

## Data & secrets

Dialogs, papers, and blog exports live **in the repo** under `data/` — this repo is
private and doubles as the cross-machine sync + backup channel (`just sync` commits/
pulls/pushes `data/`). `SIDEKICK_DATA` points the app at that dir (the justfile sets
it); without it, dialogs default to `~/.config/solveit-sidekick/dialogs-<target>.json`.

**Secrets stay out of the repo**: API keys go in `~/.config/solveit-sidekick/secrets.json`
(chmod 600, gitignored) via `sidekick/secrets_store.py`; an env var of the same name
always overrides the file. Never commit keys or dialog tokens.

## Library & blog builds

`sidekick/nbdev_export.py` + `libraries.py` project cells tagged `#| export lib:module`
(across *all* dialogs, cell-centric) into a real `.py` package via nbdev — using
`nbdev_create_config` offline rather than `nbdev-new` (which rate-limits on an
unauthenticated GitHub API call). `blog.py`/`export.py` do the analogous dialog→Quarto
projection. Generated code is never hand-edited; edit the source cells and rebuild.

## Conventions

- Commit messages use `type: subject` prefixes (`feat:`, `fix:`, `data:`, `docs:`).
- Broad `except ... # noqa: BLE001` handlers intentionally degrade gracefully (mock
  fallback, missing config); `_dbg()` in `app.py` surfaces them under `SIDEKICK_DEBUG`.
- Rendering is server-side and offline by design — don't reach for a CDN.
