# Refactor brief — split `sidekick/app.py` (Q1) + decompose `_cell_edit` (Q2)

Hand this to Claude Code, running on the host (real venv, `just test`, live server).
Goal: make `app.py` maintainable without changing behavior. This is the P1 structural
debt from the audit in `CLAUDE.md` (items Q1, Q2). No feature work, no fixes bundled in —
pure structure, so the diff stays reviewable and the test suite stays the oracle.

## Ground rules

- **Behavior-preserving.** Every route, response, and rendered fragment must be
  byte-identical where practical. The suite (`test_review_app.py`, `_backend`, `_s1`,
  `_scaffold`, …) is the oracle: `just test` must stay green at every commit.
- **Small commits, one concern each.** Prefix `refactor:`. Land the package skeleton +
  one module at a time; run `just test` and `just lint` between each. Never one giant move.
- **No CDN, no new deps, offline rendering stays** (per `CLAUDE.md` conventions).
- Keep the S1/S2 posture intact: `_LocalGuard`, `_SessionScope`, peer-address gating on
  `/internal/*`, and `_sanitize_rich` on rich output must survive the move unchanged.

## The import problem (read first — this dictates the layout)

FastHTML registers routes as a side effect of `@rt(...)` at import time, and `rt` is
created by `fast_app(...)` (currently `app.py:1991`). Naively splitting creates circular
imports (routes need `rt` + renderers; renderers need state; `app.py` needs routes).

Solve it with an app-factory core that everyone imports from, no cycles:

```
sidekick/
  app.py                 # thin wiring: build app, import route modules (for @rt side effects), expose `app`
  webapp/
    __init__.py
    server.py            # fast_app(...) -> app, rt ; _LOCAL_HDRS ; nothing else imports UP from here
    state.py             # STATE, _STATE_LOCK, PER_TAB, cur/set_cur/pop_cur, dirty/pending-stream, target helpers
    middleware.py        # _LocalGuard, _SessionScope, _host_only, _ALLOWED_HOSTS, _LOOPBACK_PEERS
    render/
      md.py              # _md setup, render_md, _msgid/_anchor/_linkify helpers, CSS/JS static loaders
      cell.py            # _cell_textarea, _head, _rich_view, _output_views, _rendered_content, MsgRow, _ctx_buttons…
      cell_edit.py       # _cell_edit + the Q2 per-type builders (see below)
      page.py            # Sidebar, TargetSwitcher, Led, Stream, Composer, Model/ModeSelect, SettingsPage,
                         #   LibrariesPage, PaperPanel, TitleEditor, Page
    routes/
      dialog.py          # /  /open /new /rename /dialog/* /send /switch
      cell.py            # /cell/* (run, stream, exec_stream, save, edit, view, delete, type, fade, check, move…)
      library.py         # /libraries /library/* /cell/export-to /export/*
      paper.py           # /paper/* /recall
      blog.py            # /blog/{path} /publish/blog
      settings.py        # /settings /kernel/restart
      internal.py        # /internal/* + _mcp_ok/_loopback_req/_mcp_guard/_json
```

Dependency direction is strictly downward: `server.py` and `state.py` at the bottom,
`render/*` imports `state` + `md`, `routes/*` imports `server.rt`, `render`, `state`,
`middleware`. `app.py` at the top imports every `routes/*` module purely to trigger
registration, then exposes `app`. No module imports `app.py`.

Keep the package name out of the way of the existing `sidekick/app.py` during the move:
create `sidekick/webapp/` alongside, migrate into it, and at the end either keep `app.py`
as the thin wiring shim (recommended — `python -m sidekick.cli serve` and tests import it)
or update the entrypoints. Check what imports `sidekick.app` first: `cli.py`, tests,
`mcp_cells`? Grep before moving so the public import path (`from sidekick.app import app`)
still resolves.

## Suggested sequence (each step = one green commit)

1. **Skeleton, no moves.** Create `webapp/server.py` with `app, rt = fast_app(...)` and
   `_LOCAL_HDRS`; have `app.py` import them. `just test` green.
2. **state.py.** Move the `STATE` dict, `_STATE_LOCK`, `PER_TAB`, and all the
   `cur/set_cur/pop_cur/_overlay`, `_mark/_reset/_consume_cells_dirty`,
   `_set/_clear_pending_*`, `_set_dialog`, `_initial_target/use_target/_ensure_target/
   _init_cell_tools` helpers. These are self-contained. Re-export from `app.py` for any
   test that reaches in.
3. **middleware.py.** Move `_LocalGuard`, `_SessionScope`, `_host_only`, host/peer
   allowlists. Register them in `server.py` or `app.py` wiring exactly as today.
4. **render/md.py + render/cell.py + render/page.py.** Move component builders. These are
   the bulk of the lines but low-risk (pure functions returning FT components). Watch the
   module-level `_static_text(...)` JS/CSS constants — move each next to its sole user.
5. **render/cell_edit.py — do Q2 here (below).**
6. **routes/*.py, one file per commit.** Move handlers; each module does
   `from ..server import rt` and re-imports the render/state helpers it calls.
7. **Delete dead code** while you're in the neighborhood: the `if not backend.list_dialogs(): pass`
   no-op (audit Q3, ~`app.py:392`).
8. **Final `app.py`** is ~30 lines: build/import wiring + `app` export.

## Q2 — decompose `_cell_edit` (currently `app.py:928–1421`, ~500 lines)

One function builds the edit view for every cell type. Split by type, sharing scaffolding:

```
def _cell_edit(m, num=None):
    kind = m["type"]                      # note | code | prompt
    if kind == "code":   body = _edit_code(m)
    elif kind == "prompt": body = _edit_prompt(m)
    else:                body = _edit_note(m)
    return _edit_shell(m, num, body)      # shared header/toolbar/textarea wrapper
```

- `_edit_shell(m, num, body)` — the common frame: `_head(...)`, action row, textarea
  container, the `_CODE_EDITOR_JS`/`_FOCUS_JS`/`COMPLETE_JS` wiring shared across types.
- `_edit_code` — code-only bits (run/stop controls, completion hookup, exec output views).
- `_edit_prompt` — Ask-AI bits (model/mode selectors, stream target).
- `_edit_note` — the markdown/note path.
Pull the branch-specific blocks out of the current monolith verbatim; don't rewrite logic.
Because output is component trees, `test_review_app.py` will catch any drift immediately —
lean on it, add a snapshot assertion per cell type if coverage feels thin.

## Verification (must all pass before calling it done)

- `just test` green after **every** commit (not just at the end).
- `just lint` clean (`ruff` — note the repo ignores nothing exotic; fix, don't `# noqa`).
- `just dev` boots; smoke-test by hand: open a dialog, run a code cell, edit a note cell,
  send an Ask-AI prompt, switch target, open Settings. These exercise every moved surface.
- `git diff --stat` should show moves, not rewrites — large deletions in `app.py`
  balanced by additions in `webapp/`. If a moved function's body changed, justify why.
- Confirm the public import path still works: `python -c "from sidekick.app import app"`
  and `python -m sidekick.cli serve` both start.

## Explicitly out of scope

Ship this as pure structure. Do **not** fold in the security/error items from the audit
(S1 auth gate, S2/S3, E1, C1, T1/T2). Land the split first — it's what makes those fixes
individually testable — then tackle them in separate PRs against the new module boundaries.
```
