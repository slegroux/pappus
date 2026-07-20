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

### Ask AI — three routes

- **API providers** (`claude`/`glm`/`codex`) go through the SDKs in `client.py`.
- **Codex CLI** (`codex-cli`, the default) shells out via `sidekick/codex_cli.py`
  to the user's installed `codex` command and current local auth/config. It runs a
  fresh conservative read-only `codex exec` turn from a neutral scratch directory,
  streams `--json` events in the web UI, and can use the same loopback cell MCP
  tools for explicit visible notebook edits.
- **Claude subscription** (`claude-cli`) shells out via `sidekick/claude_cli.py`
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

## Audit findings & prioritized fix plan (2026-07-15)

Full-codebase audit (security + code quality). Ordered by severity; each item is
independently actionable. Codes: S = security, E = error handling, C = concurrency,
Q = structure, T = tests. Line numbers are approximate — verify before editing.

> **Status (2026-07-15):** all 14 targeted items below are done (see the commit).
> Q1/Q2 (the large `app.py`/`_cell_edit` refactors) were deferred — they need their
> own review pass, not a batch. S1 was fixed via the peer-address gate option.

### P0 — fix first

- [x] **S1 · 0.0.0.0 mode is effectively unauthenticated RCE** (`app.py:1998,2024`).
  The whole app is gated only by a Host-header allowlist (`_ALLOWED_HOSTS`), which the
  client controls. With `SIDEKICK_HOST=0.0.0.0` (`cli.py:76`, the documented network-
  exposure switch), a remote `curl` sending `Host: localhost` reaches every route —
  including `/cell/run` → `backend.exec` → arbitrary Python on the kernel, and
  `/settings` (writes API keys). Internal `/internal/*` routes are safe (they use a peer-
  address check), but the app routes are not. Fix: require a shared session secret on all
  mutating routes, or apply the same peer-address gate `_loopback_req` uses. Don't rely on
  the Host header for auth.

### P1 — high

- [x] **T1 · Base-install test suite fails collection** (`test_review_audio.py:8`,
  `test_review_kernel.py:13`). Both do a top-level `import numpy as np`; numpy lives only
  in the `kernel` extra (`pyproject.toml:35`), so plain `uv run pytest` errors at
  collection instead of skipping — contradicting "the suite runs offline." They already
  guard torch with `pytest.importorskip`; do the same: `np = pytest.importorskip("numpy")`.
- [x] **S2 · Stored XSS via kernel rich output** (`app.py:706-708`). `_rich_view` emits
  `Div(NotStr(data))` for `image/svg+xml` and `text/html` unescaped. Dialogs (with cached
  outputs) are persisted to `data/` and synced across machines, so a crafted dialog runs
  JS in the viewer's browser on open. Fix: sanitize HTML/SVG (allowlist tags/attrs, strip
  `<script>` and event handlers) before `NotStr`.
- [ ] **Q1 · `app.py` is a 3208-line god-module.** Renderers, global state, ~90 route
  handlers, and ASGI middleware share one file with no boundaries. Split into
  `app/state.py`, `app/render.py`, `app/routes_*.py`, leaving `app.py` as wiring. Enables
  every other fix here to be tested in isolation.
- [ ] **Q2 · `_cell_edit` is a ~500-line function** (`app.py:923-1421`). One function
  builds the edit view for every cell type. Decompose per cell-type (code/note/prompt)
  into helper builders.

### P2 — medium

- [x] **S3 · SSRF guard is TOCTOU / DNS-rebinding-vulnerable** (`paper.py:167-208`).
  `_check_url_allowed` resolves and vets the host, but `urllib` re-resolves when it
  connects — a name returning a public IP then `169.254.169.254`/`127.0.0.1` bypasses it.
  Fix: resolve once and pin the vetted IP for the actual connection.
- [x] **E1 · `client.connect()` swallows all errors as "connection issue"**
  (`client.py:636`). `except Exception: return MockBackend(), str(e)` degrades to the mock
  for *any* exception, so a real bug in a backend `__init__` looks identical to "server
  down" (grey LED). Fix: catch connection-shaped errors (URLError/OSError/timeout) narrowly;
  let unexpected exceptions propagate or at least `_dbg` the type.
- [x] **C1 · SSE `exec_stream` generator can leak a thread forever** (`app.py:2455-2471`).
  It yields only when output *changes*, so a cell that hangs producing no output (e.g.
  `while True: pass`) never yields, never detects client disconnect, and loops forever in a
  threadpool worker. Fix: emit a periodic heartbeat yield and/or cap poll duration.
- [x] **T2 · `tunnel.py` SSH lifecycle is essentially untested.** Only `port_open` and the
  reject-local guard are covered; spawn, readiness wait-loop, early-exit, timeout+terminate,
  and terminate→kill escalation have none. This is the local↔H100 switch. Add fake-Popen
  tests for the up/early-exit/timeout branches.

### P3 — low / cleanup

- [x] **S4 · Secrets file has a brief world-readable window** (`secrets_store.py:52-56`).
  `write_text` then `chmod(0o600)` — readable in the gap. Fix: create with `os.open(...,
  0o600)` or write a 0600 temp then rename.
- [x] **S5 · Arbitrary local-file read via `/paper/open` `path` param** (`app.py:3088`).
  Minor under loopback trust, a file-disclosure primitive if S1 is exploited. Restrict to
  an allowed base dir.
- [x] **C2 · Paper state written from daemon threads without `_STATE_LOCK`**
  (`app.py:3024+`). Atomic under the GIL so low-risk, but inconsistent with the module's own
  locking discipline. Route through the lock.
- [x] **C4 · Kernel `_LOCKS` and injected `sys.modules` grow unbounded**
  (`kernel_server.py`). `/reset` and `/rename` leave the per-dialog lock and
  `__solveit_<slug>__` module entry behind. Slow leak. Drop them on `/reset`.
- [x] **E2 · Possible unbound `was_training` in `finally`** (`conv_arch.py:292-300`). If
  `model.training` access raises, the `finally` throws `NameError`, masking the original.
  Initialize `was_training = False` before the `try`.
- [x] **Q3 · Dead no-op branch** (`app.py:392`): `if not backend.list_dialogs(): pass`.
  Delete.
- [x] **T3 · Provider API branches in `kernel_server.run_prompt` untested** — patch the
  Anthropic/OpenAI/Zhipu SDKs and assert argv/shape.

### Reviewed and sound (no action)

Kernel server auth (loopback default, refuses off-loopback bind without `--token`,
`hmac.compare_digest` cookie check), no shell/command injection (list-argv subprocess, no
`shell=True`), `yaml.safe_load` (no unsafe deserialization), CSRF mitigation via
`Sec-Fetch-Site`/`Origin` in `_LocalGuard`, no secret leakage in logs/URLs, static-path
traversal blocked, the subprocess-reaping `finally` in `claude_cli.stream`, per-dialog
kernel locking, and atomic dialog saves with rolling `.bak`. The concurrency-sensitive code
is the most carefully written part of the codebase; the real debt is S1 and structure
(Q1/Q2).
