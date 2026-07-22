"""SolveIt Sidekick — a clean, Claude-desktop-style interface for SolveIt.

Run:
    pip install python-fasthtml pyyaml solveit_client
    python -m sidekick.app          # then open http://localhost:8000

The whole point: the target switcher in the top-right flips between your laptop
('local') and the H100 ('h100'). Everything else stays identical.
"""
from __future__ import annotations

import contextvars
import hmac
import json
import os
import re
import secrets
import threading
from urllib.parse import quote

from fasthtml.common import *
from starlette.datastructures import UploadFile

from .targets import get_target, list_targets, list_models, default_model
from .client import (connect, build_context, est_tokens, _InMemoryBackend, MockBackend,
                     HttpKernelBackend)
from .claude_cli import (stream as stream_claude, call as call_claude,
                         cost_for, CLI_MODELS, AI_MODES, DEFAULT_MODE)
from .codex_cli import stream as stream_codex, CLI_MODELS as CODEX_CLI_MODELS
from . import secrets_store, export, libraries, nbdev_export, scaffold
from . import paper as paperlib
# Shared allowlist sanitizer (drops <script>, event handlers, javascript: URLs
# while keeping tables/plots) — the same one the blog publisher uses. Rich cell
# outputs are now untrusted (dialogs sync across machines via data/), so the live
# view sanitizes too, not just published posts.
from .blog import _sanitize as _sanitize_rich


def _dbg(msg):
    """Log to stderr only when SIDEKICK_DEBUG is truthy, so the broad
    `except ... # noqa: BLE001` handlers that silently degrade become
    diagnosable without changing default (flag-unset) behavior."""
    if os.environ.get("SIDEKICK_DEBUG"):
        import sys
        print(f"[sidekick] {msg}", file=sys.stderr)


# ---- code highlighting (server-side; works offline, no CDN) -----------------
try:
    from pygments import highlight as _pyg_highlight
    from pygments.lexers import get_lexer_by_name, PythonLexer
    from pygments.formatters import HtmlFormatter
    from pygments.styles import get_style_by_name
    from pygments.token import Error as _ErrorToken
    from pygments.util import ClassNotFound

    # Drop the red box Pygments draws around Token.Error glyphs. A lexer emits
    # Error for any char it can't tokenise (box-drawing │└, math ·×−, arrows →▼),
    # which is exactly what ASCII-art diagrams in an answer are made of — they'd
    # otherwise each get an ugly red outline.
    def _no_error_border(style):
        return {**style.styles, _ErrorToken: ""}

    class _DarkCodeStyle(get_style_by_name("monokai")):
        styles = _no_error_border(get_style_by_name("monokai"))

    # Two palettes, so the *background* alone tells you whether code can run:
    #  • runnable code cells   → dark monokai (matches --code-bg)
    #  • illustrative code in an AI answer / note → a light style on the app's own
    #    light background, so it reads as prose rather than an executable cell.
    # noclasses=True inlines the token colors, so no separate stylesheet is needed.
    _pyg_fmt = HtmlFormatter(noclasses=True, style=_DarkCodeStyle)

    class _LightCodeStyle(get_style_by_name("friendly")):
        # Keep this hex in sync with --md-code-bg in the CSS below.
        background_color = "#EAE6DA"
        styles = _no_error_border(get_style_by_name("friendly"))

    _pyg_fmt_light = HtmlFormatter(noclasses=True, style=_LightCodeStyle)

    def _highlight(src: str, lang: str = "", fmt=None) -> str:
        """Highlight `src` with Pygments. No language → Python (this is a Python
        notebook); an unknown language → plain text. `fmt` selects the palette,
        defaulting to the dark monokai used by runnable code cells."""
        try:
            lexer = get_lexer_by_name(lang) if lang else PythonLexer()
        except ClassNotFound:
            lexer = get_lexer_by_name("text")
        return _pyg_highlight(src or "", lexer, fmt or _pyg_fmt)

    def render_code(src: str):
        return NotStr(_highlight(src, "python"))

    def _highlight_md(src: str, lang: str = "") -> str:
        """Highlight a markdown-embedded (non-runnable) code block on the light
        palette, so it's visually distinct from a runnable code cell. An untagged
        fence (``` with no language) is treated as plain text rather than Python —
        it's usually ASCII art or console output, not code to tokenise."""
        return _highlight(src, lang or "text", _pyg_fmt_light)
except Exception as e:  # noqa: BLE001 — degrade to a plain code block if pygments is missing
    _dbg(f"pygments unavailable; plain code blocks: {e}")

    def _highlight(src: str, lang: str = "", fmt=None) -> str | None:
        return None

    def _highlight_md(src: str, lang: str = "") -> str | None:
        return None

    def render_code(src: str):
        return Pre(src or "", cls="code")


# A bare `direction TD` statement only ever appears *inside* a subgraph (the
# top-level uses `flowchart TD`). mermaid's subgraph grammar accepts TB/BT/LR/RL
# but NOT the `TD` alias, so an AI-drawn diagram that writes `direction TD` in a
# subgraph fails to parse and renders the "Syntax error" bomb. TB is the exact
# equivalent, so normalise it — this heals both already-cached dialogs and future
# AI output regardless of whether the model follows the diagram guidance.
_MERMAID_SUBGRAPH_TD = re.compile(r"(?im)^([ \t]*direction[ \t]+)TD([ \t]*)$")


def _normalize_mermaid(src: str) -> str:
    return _MERMAID_SUBGRAPH_TD.sub(r"\1TB\2", src)


# ---- markdown (server-side; works offline, no CDN) --------------------------
try:
    import mistune

    class _MdRenderer(mistune.HTMLRenderer):
        """Markdown HTML renderer that syntax-highlights fenced code blocks
        (```python …```) via Pygments — on the light palette, so illustrative code
        in an answer/note reads differently from a runnable code cell.
        A ```mermaid fence is emitted as a raw <pre class="mermaid"> that mermaid.js
        turns into a diagram client-side (see renderMermaid in the page JS)."""
        def block_code(self, code, info=None):
            lang = (info or "").strip().split(None, 1)[0] if (info or "").strip() else ""
            if lang == "mermaid":
                # mermaid reads the element's textContent, so escape the source
                # rather than highlighting it; the browser decodes it back.
                return f'<pre class="mermaid">{mistune.util.escape(_normalize_mermaid(code or ""))}</pre>'
            html = _highlight_md(code, lang)
            return html if html else super().block_code(code, info)

    # escape=True neutralises raw HTML in the source, so rendering a note or an
    # AI answer can't inject <script> — markdown syntax still renders.
    # 'math' extracts $…$ / $$…$$ before markdown can mangle underscores etc.,
    # emitting \(…\) (inline) and $$…$$ (block) for KaTeX to render client-side.
    _md = mistune.create_markdown(renderer=_MdRenderer(escape=True),
                                  plugins=["strikethrough", "table", "math"])
except Exception as e:  # noqa: BLE001 — degrade to plain text if mistune is missing
    _dbg(f"mistune unavailable; plain-text markdown: {e}")
    _md = None


# Message-ID links (SolveIt's #_msgid). A cell id is "_" + 8 hex chars, so
# `#_deadbeef` links to a cell in THIS dialog and `#folder/dialog/_deadbeef`
# links across dialogs. We autolink these in rendered notes and AI answers.
_MSGID_RE = re.compile(r"#(?:(?P<dlg>[\w][\w./-]*)/)?(?P<mid>_[0-9a-f]{6,})\b")
# Split out <pre>/<code> so we never linkify inside a code block, and split tags
# so we never rewrite inside an attribute (e.g. an existing href="#_…").
_CODE_SPLIT_RE = re.compile(r"(<pre\b.*?</pre>|<code\b.*?</code>)", re.DOTALL | re.IGNORECASE)
_TAG_SPLIT_RE = re.compile(r"(<[^>]+>)")


def _msgid_anchor(m) -> str:
    dlg, mid, label = m.group("dlg"), m.group("mid"), m.group(0)
    if dlg:                                   # cross-dialog: open that dialog, then scroll
        return (f'<a class="msglink xdlg" href="/open?dialog={quote(dlg)}#{mid}" '
                f'title="Open {dlg} at this cell">{label}</a>')
    # same-dialog: intercepted by JS to scroll (no navigation), href is a fallback
    return (f'<a class="msglink" href="#cell-{mid}" data-mid="{mid}" '
            f'title="Jump to this cell">{label}</a>')


# A standard markdown link whose target is a msgid anchor — `[label](#_id)` or
# `[label](#folder/dlg/_id)` — renders as a plain <a href="#…">. Upgrade those to
# our msglink so they get the same scroll/flash/no-edit behavior, keeping the
# author's own label. This lets people use ordinary markdown links, not just the
# bare `#_id` autolink.
_MSGID_HREF_RE = re.compile(
    r'<a href="#(?:(?P<dlg>[\w][\w./-]*)/)?(?P<mid>_[0-9a-f]{6,})"(?P<rest>[^>]*)>')


def _upgrade_anchor(m) -> str:
    dlg, mid, rest = m.group("dlg"), m.group("mid"), m.group("rest")
    if dlg:
        return f'<a class="msglink xdlg" href="/open?dialog={quote(dlg)}#{mid}"{rest}>'
    return f'<a class="msglink" href="#cell-{mid}" data-mid="{mid}"{rest}>'


def _upgrade_msgid_anchors(html: str) -> str:
    """Rewrite markdown-rendered `<a href="#_id">` links into msglinks (add the
    class + data-mid, and the /open href for cross-dialog ones)."""
    if '"#' not in html:
        return html
    return _MSGID_HREF_RE.sub(_upgrade_anchor, html)


def _linkify_msgids(html: str) -> str:
    """Turn bare `#_msgid` / `#dialog/_msgid` references in rendered HTML into
    links, skipping code blocks and tag attributes so only visible text is
    rewritten. (Markdown `[label](#_id)` links are handled by _upgrade_msgid_anchors.)"""
    if "#" not in html:                       # no anchor syntax at all → cheap exit
        return html
    out = []
    for i, block in enumerate(_CODE_SPLIT_RE.split(html)):
        if i % 2:                             # odd => a <pre>/<code> block: leave as-is
            out.append(block)
            continue
        out.append("".join(
            piece if j % 2 else _MSGID_RE.sub(_msgid_anchor, piece)   # odd => an HTML tag
            for j, piece in enumerate(_TAG_SPLIT_RE.split(block))))
    return "".join(out)


def render_md(text: str):
    """Render markdown to safe HTML, or fall back to escaped plain text."""
    text = text or ""
    if not _md:
        return text
    # markdown links [label](#_id) first, then bare #_id text references.
    return NotStr(_linkify_msgids(_upgrade_msgid_anchors(_md(text))))


def _initial_target() -> str:
    """Honor SIDEKICK_TARGET / config default, falling back to the first target."""
    t = get_target()  # resolves SIDEKICK_TARGET env, then config 'default'
    return t.name

# ---- app state (single-user prototype: module-level is fine) ----------------
STATE = {
    "target_name": None,
    "backend": None,
    "warning": None,
    "dialog": "demo/welcome",
    "model": default_model(),
    "ai_mode": DEFAULT_MODE,   # AI persona for Ask AI: learning/concise/standard
    "msg_type": "prompt",
    "pending_stream": None,   # (dialog, msg_id) whose answer is being streamed live
    "pending_run": {},        # {(dialog, msg_id): run_id} code cells whose exec is streaming
    "paper": None,            # {name, status, md, engine} for the reading panel
    "editing": None,          # cell id to render in edit mode once (just-inserted cell)
    "focus_start": None,      # cell id whose editor should focus at the beginning once
    "scroll_to": None,        # cell id to scroll into view once (e.g. after a composer send)
    "cells_dirty": False,     # set when the AI's MCP tools edit cells mid-stream → reload
}

# STATE is process-global (single-user prototype). The one genuinely concurrent
# access is the AI's MCP tools flipping `cells_dirty` from the internal-route
# thread WHILE the SSE generator streams an answer on another thread — a plain
# bool read-modify-write that can lose the "a cell was edited" signal. This lock
# guards those cross-thread transitions (cells_dirty set/consume, pending_stream
# set/clear, dialog switch). It is deliberately NOT a full session-scoping of
# STATE — that larger refactor is out of scope here.
_STATE_LOCK = threading.Lock()

# ---- per-tab (session-scoped) UI state --------------------------------------
# Two browser tabs used to clobber each other because ALL of STATE is process-
# global. A handful of keys are genuinely PER-TAB (which notebook this tab is
# viewing, and its one-shot render transients); the rest are genuinely shared
# (the backend connection, target, warning banner, user model/mode prefs, and
# the AI/kernel streaming coordination flags written from cookie-less threads).
#
# The per-tab keys get an additive session overlay: each browser session (keyed
# by an `sk_sid` cookie, see _SessionScope below) has its own dict in SESSIONS,
# and `cur/set_cur/pop_cur` read/write that overlay WHEN a session is in scope,
# else fall back to the module-global STATE. That fallback is what keeps the
# existing suite working unchanged: tests call handlers directly (no request →
# no session in scope) and keep seeing/setting global STATE exactly as before.
PER_TAB = ("dialog", "editing", "focus_start", "scroll_to", "flash")
SESSIONS: dict[str, dict] = {}
# The current request's session id, or None when no request is in scope (import
# time, background threads, direct test calls). Set by _SessionScope per request.
_CUR_SID: contextvars.ContextVar[str | None] = contextvars.ContextVar("sk_sid", default=None)


def _overlay() -> dict | None:
    """The current session's per-tab overlay dict, or None when no session is in
    scope (→ callers fall back to the shared global STATE)."""
    sid = _CUR_SID.get()
    if sid is None:
        return None
    return SESSIONS.setdefault(sid, {})


def cur(key: str, default=None):
    """Read a per-tab key: the session overlay if it holds this key, else the
    shared global STATE (which is also the no-session/back-compat path)."""
    ov = _overlay()
    if ov is not None and key in ov:
        return ov[key]
    return STATE.get(key, default)


def set_cur(key: str, value) -> None:
    """Write a per-tab key into the current session overlay; with no session in
    scope, write the shared global STATE (back-compat for direct calls)."""
    ov = _overlay()
    with _STATE_LOCK:
        if ov is not None:
            ov[key] = value
        else:
            STATE[key] = value


def pop_cur(key: str, default=None):
    """Read-and-clear a one-shot per-tab transient. In a session, pop only from
    that session's overlay (never the global — so a transient set in one tab
    can't leak into another). With no session, pop the global STATE."""
    ov = _overlay()
    with _STATE_LOCK:
        if ov is not None:
            return ov.pop(key, default)
        return STATE.pop(key, default)


def _mark_cells_dirty() -> None:
    """Record that the AI edited a cell (called from the internal-route thread while
    a prompt streams elsewhere)."""
    with _STATE_LOCK:
        STATE["cells_dirty"] = True


def _reset_cells_dirty() -> None:
    with _STATE_LOCK:
        STATE["cells_dirty"] = False


def _consume_cells_dirty() -> bool:
    """Atomically read-and-clear cells_dirty — the stream generator's end-of-turn
    check, so a concurrent _mark_cells_dirty isn't lost between read and reset."""
    with _STATE_LOCK:
        was = STATE.get("cells_dirty", False)
        STATE["cells_dirty"] = False
        return was


def _set_pending_stream(dialog: str, mid: str) -> None:
    with _STATE_LOCK:
        STATE["pending_stream"] = (dialog, mid)


def _clear_pending_stream(dialog: str, mid: str) -> None:
    with _STATE_LOCK:
        if STATE.get("pending_stream") == (dialog, mid):
            STATE["pending_stream"] = None


def _set_pending_run(dialog: str, mid: str, run_id: str) -> None:
    with _STATE_LOCK:
        STATE["pending_run"][(dialog, mid)] = run_id


def _pending_run_id(dialog: str, mid: str) -> str | None:
    with _STATE_LOCK:
        return STATE["pending_run"].get((dialog, mid))


def _clear_pending_run(dialog: str, mid: str) -> None:
    with _STATE_LOCK:
        STATE["pending_run"].pop((dialog, mid), None)


def _exec_streams(backend) -> bool:
    """Stream a code cell's execution (exec_start → poll → done) only for the real
    kernel backend. The in-process mock runs synchronously, so it keeps the plain
    blocking path (its exec_start would just complete in one poll anyway)."""
    return isinstance(backend, HttpKernelBackend) and hasattr(backend, "exec_start")


def _start_streamed_exec(backend, dialog: str, mid: str) -> None:
    """Kick off an async run on the kernel and mark the cell pending so it renders
    the live-exec view (SSE-wired). If the kernel is unreachable (run_id is None),
    fall back to the blocking exec path so the cell still shows a readable error."""
    run_id = backend.exec_start(dialog, mid)
    if run_id:
        _set_pending_run(dialog, mid, run_id)
        set_cur("scroll_to", mid)
    else:
        backend.exec(dialog, mid)             # kernel down → sync degrade


def _set_dialog(name: str) -> None:
    set_cur("dialog", name)


def _init_cell_tools() -> None:
    """Publish a loopback URL + one-shot token so the `claude -p` path can spawn an
    MCP server that edits the live notebook (see claude_cli + server/mcp_cells).
    Loopback only, single-user: the token just stops other local procs poking in."""
    import secrets
    os.environ.setdefault("SIDEKICK_MCP_TOKEN", secrets.token_hex(16))
    port = os.environ.get("SIDEKICK_PORT", "8000")
    os.environ.setdefault("SIDEKICK_APP_URL", f"http://127.0.0.1:{port}")
    STATE["mcp_token"] = os.environ["SIDEKICK_MCP_TOKEN"]


_init_cell_tools()


def _can_stream(backend, model) -> bool:
    """Stream local CLI-backed prompt answers on in-process backends."""
    local_cli = model in (CLI_MODELS | CODEX_CLI_MODELS) \
        or (isinstance(model, str) and (model.startswith("codex-") or model.startswith("claude-")))
    return local_cli and isinstance(backend, _InMemoryBackend)


def use_target(name: str):
    t = get_target(name)
    backend, warning = connect(t)
    STATE.update(target_name=name, backend=backend, warning=warning)
    if cur("dialog") not in (backend.list_dialogs() or [cur("dialog")]):
        set_cur("dialog", backend.list_dialogs()[0] if backend.list_dialogs() else "demo/welcome")


def _ensure_target() -> None:
    """Resolve the configured target for real (config read + network probe),
    once. Deferred out of import so `import sidekick.app` does no network I/O and
    doesn't require targets.yaml; run on app startup (see the startup hook below)."""
    if STATE.get("_target_ready"):
        return
    STATE["_target_ready"] = True
    use_target(_initial_target())


# Import-time default: a working in-memory backend so the module imports with no
# network call and no targets.yaml. The real target is resolved on startup (or by
# the first server request) via _ensure_target(); until then this mock keeps the
# UI and the (non-served) test suite functional.
STATE["backend"] = MockBackend()
try:
    STATE["target_name"] = _initial_target()   # config-only (no network); best-effort
except Exception:  # noqa: BLE001 — no config yet; resolved for real on startup
    _dbg("initial target unresolved at import; deferring to startup")

# ---- bundled static assets (JS/CSS) ----------------------------------------
# Large front-end JS/CSS blocks live as files under sidekick/static and are
# served from /static (see the /static route). Their text is loaded back into
# the module-level constants so they stay importable (tests) and so the file
# served over HTTP is byte-identical to what the page references.
_STATIC_DIR = Path(__file__).parent / "static"


def _static_text(rel: str) -> str:
    """Read a bundled static asset's text (keeps the JS/CSS constants in sync
    with the files served at /static)."""
    return (_STATIC_DIR / rel).read_text(encoding="utf-8")


# ---- styling: Claude desktop look ------------------------------------------
CSS = _static_text("css/app.css")


# ---- view helpers -----------------------------------------------------------
def Led():
    live = getattr(STATE["backend"], "live", False)
    cls = "live" if live else "mock"
    label = "live" if live else "mock"
    return Span(Span(cls=f"led {cls}"), label, style="display:inline-flex;align-items:center;gap:7px")


def TargetSwitcher():
    opts = [
        Option(n, value=n, selected=(n == STATE["target_name"]))
        for n in list_targets()
    ]
    return Div(
        Led(),
        Form(
            Select(*opts, name="target", cls="tsel",
                   onchange="this.form.submit()"),
            method="post", action="/switch",
        ),
        cls="target",
    )


def _dialog_tree(names):
    """Nest names on '/' into {'folders': {seg: node}, 'leaves': [(label, full)]}."""
    root = {"folders": {}, "leaves": []}
    for n in names:
        parts = [seg for seg in n.split("/") if seg]   # drop empties: "/a", "a//b", "a/"
        if not parts:
            continue                                   # a name that is only slashes
        node = root
        for seg in parts[:-1]:
            node = node["folders"].setdefault(seg, {"folders": {}, "leaves": []})
        node["leaves"].append((parts[-1], n))
    return root


def _dialog_leaf(label, full, active):
    """A dialog row: the open link + a ⋯ menu (Sync-toggle, Duplicate, Delete)."""
    backend = STATE["backend"]
    private = bool(getattr(backend, "is_private", lambda _d: False)(full))
    # Only meaningful when the store is on disk (has an is_private that can be True/
    # False and a store to move between); hidden for ephemeral/live backends.
    sync_item = None
    if hasattr(backend, "set_private") and getattr(backend, "_store_key", None):
        priv_label = "☁  Sync this dialog" if private else "🔒  Keep local only"
        priv_title = ("Currently local-only — click to include it in the committed/"
                      "synced store" if private
                      else "Currently synced — click to keep it on this machine only "
                      "(out of git)")
        sync_item = Form(Input(type="hidden", name="dialog", value=full),
                         Button(priv_label, type="submit", cls="conv-priv",
                                title=priv_title),
                         method="post", action="/dialog/private", style="margin:0")
    return Div(
        A(label, href=f"/open?dialog={quote(full)}",
          cls=f"conv{' active' if full == active else ''}"),
        Span("🔒", cls="conv-priv-dot", title="Local only — not synced") if private else None,
        Details(
            Summary("⋯", cls="conv-dots", title="Dialog actions"),
            Div(sync_item,
                Form(Input(type="hidden", name="dialog", value=full),
                     Button("⧉  Duplicate", type="submit", cls="conv-dup",
                            title="Copy all cells into a new '<name> copy' dialog"),
                     method="post", action="/dialog/duplicate", style="margin:0"),
                A("⬇  Export .ipynb", href=f"/dialog/export/ipynb?dialog={quote(full)}",
                  cls="conv-dup", title="Download this dialog as a Jupyter notebook"),
                Button("🗑  Delete", type="button", cls="conv-del", **{"data-dialog": full}),
                cls="conv-menu"),
            cls="conv-actions"),
        cls="conv-row")


def _render_dialog_nodes(node, active):
    """Recursively render a dialog-tree node: folders (collapsible) then leaves."""
    out = []
    for seg in sorted(node["folders"]):
        out.append(
            Details(
                Summary(seg, cls="folder-label"),
                *_render_dialog_nodes(node["folders"][seg], active),
                cls="folder",
                open=True,
            )
        )
    for label, full in sorted(node["leaves"]):
        out.append(_dialog_leaf(label, full, active))
    return out


# Shift/⌘-click to multi-select dialog rows, then bulk-delete. Plain click still
# opens a dialog (and the full-page nav resets the ephemeral selection).
SIDEBAR_JS = _static_text("js/sidebar.js")


def Sidebar():
    backend = STATE["backend"]
    names = backend.list_dialogs() or [cur("dialog")]
    tree = _dialog_tree(names)
    sel_bar = Div(
        Span("0 selected", id="selCount", cls="sel-count"),
        Button("🗑 Delete", type="button", cls="sel-del", onclick="window.__deleteDialogSel()"),
        Button("Clear", type="button", cls="sel-clear", onclick="window.__clearDialogSel()"),
        id="selBar", cls="sel-bar", style="display:none")
    # A small "Recent" section on top of the (alphabetical) tree: the most
    # recently created/touched dialogs first, so active work stays one click away.
    # list_dialogs() is in store order (newest appended last) → reverse it.
    recent = list(reversed(names))[:5]
    recent_section = (
        [Div("Recent", cls="seclabel"),
         *[_dialog_leaf(r.rsplit("/", 1)[-1], r, cur("dialog")) for r in recent]]
        if len(names) > 1 else [])
    return Div(
        Div(Span("S", cls="dot"), "SolveIt Sidekick", cls="brand"),
        A("✎  New dialog", href="/new", cls="newbtn"),
        *recent_section,
        Div("Dialogs", cls="seclabel", title="Shift/⌘-click to select several, then Delete"),
        sel_bar,
        *_render_dialog_nodes(tree, cur("dialog")),
        Div(f"target: {STATE['target_name']}", cls="side-foot"),
        Script(src="/static/js/sidebar.js"),
        cls="side",
    )


def _cell_textarea(m, code=False):
    """The editable source of a cell. One line tall by default; JS auto-grows it."""
    attrs = {"name": "content", "id": f"ta-{m.id}", "rows": "1",
             "cls": "cell-edit code-edit" if code else "cell-edit"}
    if code:
        attrs["spellcheck"] = "false"
    return Textarea(m.content, **attrs)


_PRIMARY = {"note": "Save", "code": "Run", "prompt": "Ask"}
_TAG = {"note": "note", "code": "code", "prompt": "Ask AI"}


def _stream_btn(label, post, *, cls="cell-btn", vals=None, title=None, confirm=None,
                onclick=None):
    """A cell-action button: POST `post` and swap the whole #stream (the shape every
    Type/Insert/Export/Mute/Pin/Delete/Run button shares)."""
    a = {"type": "button", "cls": cls, "hx_post": post,
         "hx_target": "#stream", "hx_swap": "outerHTML"}
    if vals:
        a["hx_vals"] = json.dumps(vals)
    if title:
        a["title"] = title
    if confirm:
        a["hx_confirm"] = confirm
    if onclick:
        a["onclick"] = onclick                # client-side feedback before the swap lands
    return Button(label, **a)


def _dropdown(summary, *children, title=None, menu_cls="ins-menu"):
    """A hover-toolbar dropdown: a `.cell-btn` summary over an absolute-positioned
    menu. Shared by the ＋ insert, ⇆ type, and paper Import… menus."""
    return Details(Summary(summary, cls="cell-btn", title=title),
                   Div(*children, cls=menu_cls), cls="ins")


def _insert_item(mid, msg_type, where, label):
    return _stream_btn(label, "/cell/insert", cls="ins-item",
                       vals={"id": mid, "msg_type": msg_type, "where": where})


def _insert_menu(mid):
    """＋ dropdown: insert Code/Note/Ask above or below this cell (a / b shortcuts)."""
    col = lambda where, head: Div(
        Span(head, cls="ins-col-head"),
        _insert_item(mid, "code", where, "Code"),
        _insert_item(mid, "note", where, "Note"),
        _insert_item(mid, "prompt", where, "Ask AI"),
        cls="ins-col")
    return _dropdown("＋", col("above", "↑ Above"), col("below", "↓ Below"),
                     title="Insert a cell  ·  a = above, b = below")


def _type_menu(m):
    """⇆ dropdown: convert this cell to Code / Note / Ask AI (y / m / i shortcuts)."""
    mid = m.id
    def item(t, label):
        cur = (t == m.msg_type)
        return _stream_btn(label, "/cell/type", cls="ins-item" + (" cur" if cur else ""),
                           vals={"id": mid, "msg_type": t})
    return _dropdown("⇆", Span("Cell type", cls="ins-col-head"),
                     item("code", "Code"), item("note", "Note"), item("prompt", "Ask AI"),
                     title="Change cell type  ·  y = Code, m = Note, i = Ask AI",
                     menu_cls="type-menu")


def _copy_menu(mid):
    """⧉ dropdown: copy this cell into another dialog (appended at its end). Lists
    every other dialog; hidden entirely when there's nowhere to copy to."""
    backend = STATE["backend"]
    others = [d for d in (backend.list_dialogs() or []) if d != cur("dialog")]
    if not others or not hasattr(backend, "copy_cell"):
        return None
    items = [_stream_btn(d, "/cell/copy", cls="ins-item", vals={"id": mid, "target": d})
             for d in others]
    return _dropdown("⧉", Span("Copy to dialog", cls="ins-col-head"), *items,
                     title="Copy this cell into another dialog", menu_cls="type-menu copy-menu")


def _current_export_target(content: str) -> tuple[str | None, str]:
    """The (lib, module) a code cell's `#| export lib:module` points at, if any."""
    for name, arg in export._directives(content)[0]:
        if name == "export":
            return nbdev_export.parse_lib_target(arg)
    return None, "core"


def _lib_menu(m):
    """Per-cell dropdown to tag a code cell into a registered library:module.
    Hidden when no libraries exist yet (create one on the Libraries page)."""
    libs = libraries.load()
    if not libs:
        return None
    cur_lib, cur_mod = _current_export_target(m.content)
    opts = [Option(lib["name"], value=lib["name"], selected=(lib["name"] == cur_lib))
            for lib in libs]
    form = Form(
        Span("Export to library", cls="ins-col-head"),
        Select(*opts, name="lib", cls="msel"),
        Input(name="module", value=(cur_mod if cur_lib else "core"),
              placeholder="module", cls="keyinput", style="width:120px"),
        Input(type="hidden", name="id", value=m.id),
        Button("Tag", cls="cell-btn run", type="submit"),
        hx_post="/cell/export-to", hx_target="#stream", hx_swap="outerHTML",
        cls="lib-form")
    label = f"Lib: {cur_lib}" if cur_lib else "Lib ▾"
    return _dropdown(label, form, title="Tag this cell into a library", menu_cls="ins-menu")


def _ctx_buttons(m):
    """Type / Insert / Copy / Export / Lib / Mute / Pin / Delete — in both rendered
    and edit modes. Code cells also get an Export toggle (`#| export`) and, when
    libraries exist, a Lib picker (`#| export <lib>:<module>`)."""
    mid = m.id
    exported = m.msg_type == "code" and export.has_export(m.content)
    btns = []
    if m.msg_type == "code":
        btns.append(_stream_btn(
            "Exported" if exported else "Export", "/cell/export",
            cls="cell-btn exp" + (" on" if exported else ""), vals={"id": mid},
            title="Toggle whether this cell is tangled into the exported package (#| export)"))
        lib_menu = _lib_menu(m)
        if lib_menu is not None:
            btns.append(lib_menu)
    copy_menu = _copy_menu(mid)
    return [
        _type_menu(m),
        _insert_menu(mid),
    ] + ([copy_menu] if copy_menu else []) + btns + [
        _stream_btn("Muted" if m.muted else "In context", "/cell/mute",
                    cls="cell-btn ctx" + (" off" if m.muted else ""), vals={"id": mid},
                    title="Toggle whether this cell is sent to the AI as notebook context"),
        _stream_btn("Pinned" if m.pinned else "Pin", "/cell/pin",
                    cls="cell-btn pin" + (" on" if m.pinned else ""), vals={"id": mid},
                    title="Pin this cell so it stays in context even when older cells are trimmed"),
        _stream_btn("Delete", "/cell/delete", cls="cell-btn del", vals={"id": mid},
                    confirm="Delete this cell?"),
    ]


def _model_label(mid: str | None, default: str | None = None) -> str:
    """Friendly label for a model id. Model-less answers use the configured default."""
    if not mid:
        if default is not None:
            return default
        try:
            mid = default_model()
        except Exception:  # noqa: BLE001
            return "Codex · GPT-5.5 high"
    try:
        for m in list_models():
            if m["id"] == mid:
                return m["label"]
    except Exception as e:  # noqa: BLE001 — config issue: show the id rather than crash
        _dbg(f"_model_label({mid!r}) fell back to raw id: {e}")
    return default or mid


def _rich_view(item):
    """Render one rich kernel output (plot/image/dataframe)."""
    t, data = item.get("type", ""), item.get("data", "")
    if t in ("image/png", "image/jpeg"):
        return Img(src=f"data:{t};base64,{data}", cls="cell-img")
    if t == "image/svg+xml":
        # Inline the SVG markup directly so it stays crisp/scalable (vector
        # diagrams from conv_arch, plots saved as SVG, etc.). Sanitize first: cached
        # outputs are persisted to data/ and synced across machines, so a crafted
        # dialog is untrusted input — strip <script>/event-handlers before NotStr.
        return Div(NotStr(_sanitize_rich(data)), cls="cell-svg")
    if t == "text/html":
        return Div(NotStr(_sanitize_rich(data)), cls="cell-html")
    if t == "audio/wav":
        # Kernel-emitted audio (e.g. IPython.display.Audio) — an inline player.
        return Audio(controls=True, src=f"data:audio/wav;base64,{data}", cls="cell-audio")
    return Div(data, cls="out")


def _tok_badge(m):
    return Span(f"~{est_tokens(m.content) + est_tokens(m.output)}t",
                cls="muted small tok", title="estimated tokens this cell adds to AI context")


def _rowcls(m):
    return (f"row {m.msg_type}" + (" muted" if m.muted else "")
            + (" pinned" if m.pinned else ""))


def _heading_level(content: str) -> int:
    """The markdown heading level a note STARTS with (1–6), or 0 if it doesn't begin
    with a heading. A note that starts with a heading is a collapsible section."""
    for line in (content or "").lstrip().splitlines():
        s = line.strip()
        if not s:
            continue
        mt = re.match(r"(#{1,6})\s", s)
        return len(mt.group(1)) if mt else 0
    return 0


def _head(m, primary, num=None, show_actions=False):
    bits = []
    if num is not None:                           # the cell's 1-based number (matches AI context)
        bits.append(Span(str(num), cls="cell-num", title=f"Cell {num}"))
    lvl = _heading_level(m.content) if m.msg_type == "note" else 0
    if lvl:                                       # a section header → collapse caret + count pill
        bits.append(Span("▾", cls="sec-caret", title="Collapse / expand this section",
                         **{"data-sec": m.id, "data-sec-level": str(lvl)}))
        bits.append(Span("", cls="sec-count", style="display:none"))
    bits += [Span("⠿", cls="drag-handle", title="Drag to reorder"),
             Span(_TAG[m.msg_type], cls="tag")]
    if m.msg_type == "code":
        bits.append(Span(m.id, cls="muted small"))
    bits.append(_tok_badge(m))
    bits.append(Div(*primary, _link_btn(m), *_ctx_buttons(m),
                    cls="cell-actions" + (" show" if show_actions else "")))
    return Div(*bits, cls="who")


def _link_btn(m):
    """🔗 Copy a #reference to this cell — SolveIt's `#_msgid` anchor. It's the exact
    string you paste into a note or prompt (same dialog) to render a clickable link
    here; prefix a dialog path (`#folder/dlg/_id`) to reference it from elsewhere."""
    return Button("🔗", type="button", cls="cell-btn link", title="Copy a #link to this cell",
                  onclick="_copyMsgLink(this)", **{"data-anchor": f"#{m.id}"})


def _output_views(m):
    """Code output + plots/images, or the rendered AI answer for a prompt."""
    out = []
    if m.msg_type == "code":
        run_id = _pending_run_id(cur("dialog"), m.id)
        if run_id:
            # A live exec: a vanilla EventSource (see STREAM_JS) connects to
            # /exec_stream and replaces this <pre>'s content as stdout arrives, with
            # a Stop button to interrupt a runaway loop. On done it reloads #stream
            # so the finished cell (rich plots, normal Run button) renders.
            url = (f"/exec_stream?dialog={quote(cur('dialog'))}"
                   f"&id={m.id}&run_id={quote(run_id)}")
            stop = Button("■ Stop", type="button", cls="cell-btn del",
                          title="Interrupt this running cell",
                          hx_post="/cell/stop", hx_vals=json.dumps({"id": m.id}),
                          hx_swap="none")
            out.append(Div(
                Div(Span(Span(cls="spinner"), "Running…", cls="thinking"), stop, cls="who"),
                Pre(m.output or "", cls="out", id=f"exec-{m.id}",
                    **{"data-exec-url": url}),
                cls="exec-live"))
            return out
        if m.output:
            out.append(Div(m.output, cls="out"))
        out += [_rich_view(it) for it in m.rich]
    elif m.msg_type == "prompt":
        pending = STATE.get("pending_stream") == (cur("dialog"), m.id)
        if pending:
            # Live answer: a vanilla EventSource (see STREAM_JS) connects to /stream
            # and replaces this bubble's innerHTML as tokens arrive. We show this
            # whenever the cell is pending — including a re-ask, where m.output still
            # holds the previous answer; the spinner replaces it until fresh tokens
            # arrive, otherwise a mid-notebook re-run looks like nothing happened.
            who = _model_label(m.model)
            out.append(Div(
                Div(Span(who, cls="tag"), cls="who"),
                Div(Span(Span(cls="spinner"), "Thinking…", cls="thinking"),
                    cls="bubble md", id=f"ans-{m.id}",
                    **{"data-stream-url": f"/stream?dialog={quote(cur('dialog'))}&id={m.id}"}),
                cls="answer"))
        elif m.output:
            out.append(_answer_view(m))
    return out


def _can_edit_answer() -> bool:
    """In-process backends (mock/kernel) can edit a prompt's answer in place; the
    remote SolveIt LiveBackend has no such hook, so its answers stay read-only."""
    return hasattr(STATE["backend"], "update_output")


def _answer_view(m):
    """A prompt's AI answer, rendered read-only with click-to-edit (editing the
    output in place — SolveIt's editable AI response). The stable `answer-<id>`
    wrapper is the swap target for the edit/cancel/save round-trip."""
    who = _model_label(m.model, default="SolveIt AI")
    has = (m.output or "").strip()
    inner = render_md(m.output) if has else Span("Empty answer — click to edit", cls="muted")
    if _can_edit_answer():
        edit = dict(hx_get=f"/cell/answer/edit?id={m.id}", hx_target=f"#answer-{m.id}",
                    hx_swap="outerHTML", title="click to edit the AI's answer")
        bubble = Div(inner, cls="bubble md clickedit", **edit)
    else:
        bubble = Div(inner, cls="bubble md")
    return Div(Div(Span(who, cls="tag"), cls="who"), bubble,
               cls="answer", id=f"answer-{m.id}")


def _answer_edit(m):
    """The prompt answer switched into edit mode: a textarea over `m.output` with
    Save/Cancel. Save writes the output back without touching the question or
    re-asking the AI; Cancel restores the rendered answer."""
    mid = m.id
    ta = Textarea(m.output, name="output", id=f"ta-ans-{mid}", rows="1", cls="cell-edit")
    save = Button("Save", type="button", cls="cell-btn run",
                  hx_post="/cell/answer/save", hx_include=f"#ta-ans-{mid}",
                  hx_vals=json.dumps({"id": mid}),
                  hx_target=f"#answer-{mid}", hx_swap="outerHTML")
    cancel = Button("Cancel", type="button", cls="cell-btn",
                    hx_get=f"/cell/answer/view?id={mid}",
                    hx_target=f"#answer-{mid}", hx_swap="outerHTML")
    js = _focus_js(f"ans-{mid}")
    return Div(Div(Span("Edit answer", cls="tag"), save, cancel, cls="who"),
               ta, Script(js), cls="answer", id=f"answer-{mid}")


def _rendered_content(m):
    """Read-only, click-to-edit rendering of a cell's source: markdown for notes,
    syntax-highlighted code for code cells, plain text for prompts."""
    edit = dict(hx_get=f"/cell/edit?id={m.id}", hx_target=f"#cell-{m.id}",
                hx_swap="outerHTML", title="click to edit")
    has = (m.content or "").strip()
    if m.msg_type == "note":
        inner = render_md(m.content) if has else Span("Empty note — click to edit", cls="muted")
        return Div(inner, cls="note-view md clickedit", **edit)
    if m.msg_type == "code":
        inner = render_code(m.content) if has else Span("Empty cell — click to edit", cls="muted")
        return Div(inner, cls="code-view clickedit", **edit)
    inner = m.content if has else Span("Empty prompt — click to edit", cls="muted")
    return Div(inner, cls="prompt-view clickedit", **edit)


def _answer_code_blocks(md: str) -> list[tuple[str, str]]:
    """Fenced code blocks from an AI answer as (lang, code) pairs, in order — the
    source for "split to code" (SolveIt's `W`). The language tag decides where a
    block lands on split: python/unlabeled become runnable code cells, while
    `mermaid` becomes a note (which renders the fence as a diagram)."""
    blocks, cur, fence, lang = [], None, None, ""
    for line in (md or "").splitlines():
        s = line.lstrip()
        if fence is None:
            mt = re.match(r"(`{3,}|~{3,})\s*([\w+-]*)", s)   # opening fence + lang
            if mt:
                fence, lang, cur = s[0], (mt.group(2) or "").lower(), []
        elif re.match(r"(`{3,}|~{3,})\s*$", s) and s[0] == fence:
            blocks.append((lang, "\n".join(cur)))            # closing fence (same char)
            fence, cur, lang = None, None, ""
        else:
            cur.append(line)
    return [(lg, b) for lg, b in blocks if b.strip()]


def MsgRow(m, num=None):
    """A cell in its default rendered (read-only, click-to-edit) state."""
    primary = [] if m.msg_type == "note" else [
        _stream_btn(_PRIMARY[m.msg_type], "/cell/exec", cls="cell-btn run",
                    vals={"id": m.id}, title="Re-run this cell",
                    onclick=(f"_showCellSpinner('{m.id}')" if m.msg_type == "prompt" else None))]
    if m.msg_type == "prompt" and _answer_code_blocks(m.output):
        primary.append(_stream_btn(
            "Split to code", "/cell/split", vals={"id": m.id},
            title="Extract the answer's code blocks into runnable code cells below"))
    if m.msg_type == "code":
        primary += _fade_buttons(m)
    return Div(_head(m, primary, num=num), _rendered_content(m), *_output_views(m),
               cls=_rowcls(m), id=f"cell-{m.id}")


def _fade_buttons(m):
    """Faded-scaffolding (F6) cell-toolbar affordance, mirroring "Split to code".

    A worked code cell gets "Fade to exercise" (→ level-1 exercise below). A cell
    that is already a faded exercise gets a fade stepper (Show worked / Fill-in /
    From scratch) plus "Ask AI to check" — all re-run `/cell/fade` (or `/cell/check`)."""
    if _is_exercise(m.content):
        lvl = lambda label, n, title: _stream_btn(
            label, "/cell/fade", cls="cell-btn fade", vals={"id": m.id, "level": n}, title=title)
        return [
            lvl("Show worked", 0, "Reveal the full worked solution (level 0)"),
            lvl("Fill-in", 1, "Blank the load-bearing lines to complete (level 1)"),
            lvl("From scratch", 2, "Collapse to a goal + stub; write it yourself (level 2)"),
            _stream_btn("Ask AI to check", "/cell/check", cls="cell-btn", vals={"id": m.id},
                        title="Ask the AI to compare your attempt to the worked solution and hint"),
        ]
    return [_stream_btn(
        "Fade to exercise", "/cell/fade", cls="cell-btn fade", vals={"id": m.id, "level": 1},
        title="Turn this worked code into a fill-in-the-blank exercise below")]


def _cell_edit(m, num=None, focus_start: bool = False):
    """A cell switched into edit mode: a raw editor + Save/Run/Ask + Cancel."""
    mid = m.id
    path = "/cell/save" if m.msg_type == "note" else "/cell/run"
    run_attrs = dict(type="button", cls="cell-btn run",
                     hx_post=path, hx_include=f"#ta-{mid}", hx_vals=json.dumps({"id": mid}),
                     hx_target="#stream", hx_swap="outerHTML")
    if m.msg_type == "prompt":
        run_attrs["onclick"] = f"_showCellSpinner('{mid}')"   # instant wheel before the swap
    primary = [
        Button(_PRIMARY[m.msg_type], **run_attrs),
        Button("Cancel", type="button", cls="cell-btn",
               hx_get=f"/cell/view?id={mid}", hx_target=f"#cell-{mid}", hx_swap="outerHTML"),
    ]
    # Code cells get CodeMirror (highlight-while-editing); notes/prompts just focus.
    js = (_code_editor_js(mid, focus_start)
          if m.msg_type == "code" else _focus_js(mid, focus_start))
    return Div(_head(m, primary, num=num, show_actions=True),
               _cell_textarea(m, code=(m.msg_type == "code")),
               *_output_views(m), Script(js),
               cls=_rowcls(m), id=f"cell-{mid}")


# Editor init for an edit-mode cell. CodeMirror highlights Python as you type and
# keeps the underlying textarea synced (so htmx hx-include still posts the source);
# if CodeMirror didn't load, fall back to focusing the plain textarea.
_CODE_EDITOR_JS = """
(function(){
  var ta = document.getElementById('ta-__MID__');
  if(!ta) return;
  var focusStart = __FOCUS_START__;
  function runCell(){
    var row = ta.closest('.row');
    var btn = row && row.querySelector('.cell-btn.run');
    if(btn) btn.click();
  }
  if(window.CodeMirror){
    var cm = CodeMirror.fromTextArea(ta, {
      mode: 'python', theme: 'monokai', lineNumbers: false,
      viewportMargin: Infinity, indentUnit: 4, lineWrapping: true,
      extraKeys: { 'Cmd-Enter': function(){ cm.save(); runCell(); },
                   'Ctrl-Enter': function(){ cm.save(); runCell(); },
                   'Shift-Enter': function(){ cm.save(); runCell(); },   // Jupyter convention
                   'Cmd-/': function(){ window.__toggleComment(cm); },   // Jupyter convention
                   'Ctrl-/': function(){ window.__toggleComment(cm); },
                   'Ctrl-Space': function(){ if(window.__showCompletions) window.__showCompletions(cm); } }
    });
    cm.on('change', function(){ cm.save(); });   // keep textarea current for hx-include
    if(window.__autocompleteOnType) window.__autocompleteOnType(cm);   // dynamic completion
    setTimeout(function(){
      cm.refresh(); cm.focus();
      cm.setCursor(focusStart ? 0 : cm.lineCount(), 0);
    }, 0);
  } else {
    setTimeout(function(){
      ta.focus({preventScroll:true});   // let scroll_to own the scroll, not focus
      var n = focusStart ? 0 : ta.value.length;
      ta.setSelectionRange(n, n);
    }, 0);
  }
})();
"""

_FOCUS_JS = """
(function(){
  var t = document.getElementById('ta-__MID__');
  var focusStart = __FOCUS_START__;
  if(t) setTimeout(function(){
    // preventScroll: the browser's own focus-scroll otherwise races the scroll_to
    // scrollIntoView (setTimeout vs rAF ordering), landing the cell in a slightly
    // different spot each time. Let scroll_to be the single source of truth so the
    // beginning of a freshly inserted Ask-AI/note cell lands consistently.
    t.focus({preventScroll:true});
    var n = focusStart ? 0 : t.value.length;
    t.setSelectionRange(n, n);
  }, 0);
})();
"""


def _with_focus_options(js: str, mid: str, focus_start: bool = False) -> str:
    return js.replace("__MID__", mid).replace(
        "__FOCUS_START__", "true" if focus_start else "false")


def _focus_js(mid: str, focus_start: bool = False) -> str:
    return _with_focus_options(_FOCUS_JS, mid, focus_start)


def _code_editor_js(mid: str, focus_start: bool = False) -> str:
    return _with_focus_options(_CODE_EDITOR_JS, mid, focus_start)

# Cmd/Ctrl+/ toggles Python line comments on the selected lines, the way Jupyter
# (and most editors) do. Self-contained — we don't vendor CodeMirror's comment
# addon. Comments at the shallowest indentation of the block; if every non-blank
# line is already commented, it uncomments instead. Defined once; the cell and
# composer editors bind it in their extraKeys.
COMMENT_JS = _static_text("js/comment.js")

# Ctrl+Space completion: an async CodeMirror hint that asks /complete (which
# introspects the kernel's live namespace via jedi). Defined once on the page;
# the cell editor's extraKeys calls it. Best-effort — any failure shows nothing.
COMPLETE_JS = _static_text("js/complete.js")


STREAM_JS = """
(function(){
  // Running/editing a cell swaps all of #stream — the scroll container itself —
  // which would snap the view back to the top. Restore the position saved just
  // before the swap (see the htmx:beforeSwap handler) so you stay at the cell
  // you acted on. A pending AI answer still scrolls to follow its stream below.
  (function(){
    var s = document.getElementById('stream');
    if(s && window.__streamScroll != null) s.scrollTop = window.__streamScroll;
  })();

  // CSS field-sizing auto-grows textareas natively; only run the JS fallback
  // (measured after layout settles) where it isn't supported.
  var hasFieldSizing = window.CSS && CSS.supports && CSS.supports('field-sizing','content');
  function autosize(el){ el.style.height='auto'; el.style.height=el.scrollHeight+'px'; }
  function sizeAll(){ document.querySelectorAll('.cell-edit').forEach(autosize); }
  if(!hasFieldSizing) requestAnimationFrame(sizeAll);           // wait for final width

  // KaTeX: render LaTeX in an element (no-op offline / before KaTeX loads).
  // mistune emits \\(…\\) inline and $$…$$ block, so a single $ is left alone.
  function renderMath(el){
    if(!el || !window.renderMathInElement) return;
    try { renderMathInElement(el, { throwOnError:false, delimiters:[
      {left:'$$', right:'$$', display:true},
      {left:'\\\\[', right:'\\\\]', display:true},
      {left:'\\\\(', right:'\\\\)', display:false}
    ]}); } catch(e){}
  }

  // Mermaid: turn <pre class="mermaid"> (from ```mermaid fences) into diagrams.
  // The library (~3.2MB) is lazy-loaded on first use: ensureMermaid() injects the
  // vendored script only when a diagram is actually present, so sessions that never
  // draw one pay nothing. renderMermaid() is the entry point; it bails cheaply when
  // there's no <pre.mermaid> to draw and otherwise loads-then-renders.
  function ensureMermaid(){
    if(window.mermaid) return Promise.resolve(window.mermaid);
    if(window.__mermaidLoading) return window.__mermaidLoading;
    window.__mermaidLoading = new Promise(function(resolve, reject){
      var s = document.createElement('script');
      s.src = '/vendor/mermaid.min.js';
      s.onload = function(){ resolve(window.mermaid); };
      s.onerror = function(){ window.__mermaidLoading = null; reject(new Error('mermaid load failed')); };
      document.head.appendChild(s);
    });
    return window.__mermaidLoading;
  }
  function mermaidScope(el){
    return document.getElementById('stream') || el;
  }
  function renderMermaid(el){
    if(!el) return;
    // Cheap guard: only fetch the library when there's an unprocessed diagram.
    var scope = mermaidScope(el);
    if(!scope || !scope.querySelector('pre.mermaid:not([data-processed])')) return;
    window.__sidekickMermaidRenderQueued = true;
    ensureMermaid().then(function(){ queueMermaidRender(scope); }).catch(function(err){
      window.__sidekickMermaidRenderQueued = false;
      if(window.console && console.warn) console.warn('Sidekick Mermaid load failed', err);
    });
  }
  var MERMAID_MAX_TRIES = 3;
  function mermaidWarn(err){
    if(window.console && console.warn) console.warn('Sidekick Mermaid render failed', err);
  }
  // A node with no layout box (display:none ancestor / detached) can't be measured,
  // so mermaid collapses it — but that's not a real failure: every #stream swap
  // re-renders, so it gets another shot once it's visible. Don't judge/churn on it.
  function mermaidNodeHidden(n){
    return !(n.offsetWidth || n.offsetHeight || (n.getClientRects && n.getClientRects().length));
  }
  // Did this node actually render, or did it collapse? A diagram measured while its
  // container was unmeasurable stacks every node onto one point — the tell is a
  // ~zero-size svg. mermaid records the intrinsic size in the svg's viewBox
  // (visibility-independent, unlike getBoundingClientRect), so read that. Treating a
  // collapsed-but-present svg as success is exactly what made corrupted diagrams
  // stick until a manual re-run; failing it here lets scheduleMermaidRetry recover.
  function mermaidRenderOk(n){
    var svg = n.querySelector('svg');
    if(!svg) return false;
    if(mermaidNodeHidden(n)) return true;    // can't fairly judge a hidden node
    var vb = svg.getAttribute('viewBox');
    if(vb){
      var p = vb.split(/[ ,]+/);
      var w = parseFloat(p[2]), h = parseFloat(p[3]);
      if(isFinite(w) && isFinite(h) && (w < 4 || h < 4)) return false;
    }
    return true;
  }
  function releaseMermaidFailures(nodes, err){
    if(err) mermaidWarn(err);
    nodes.forEach(function(n){
      if(mermaidRenderOk(n)) return;
      if(n.__sidekickMermaidSource) n.textContent = n.__sidekickMermaidSource;
      if((n.__sidekickMermaidTries || 0) < MERMAID_MAX_TRIES){
        n.removeAttribute('data-processed');
      } else {
        n.setAttribute('data-mermaid-error', 'true');
      }
    });
  }
  function scheduleMermaidRetry(el){
    setTimeout(function(){
      if(!el || el.isConnected === false) return;
      Array.prototype.forEach.call(el.querySelectorAll('pre.mermaid[data-processed]'), function(n){
        if(mermaidRenderOk(n)) return;
        // A collapsed render left its (bad) svg in place; drop it back to source so
        // the re-render below starts clean rather than nesting svg-in-svg.
        if((n.__sidekickMermaidTries || 0) < MERMAID_MAX_TRIES){
          if(n.__sidekickMermaidSource) n.textContent = n.__sidekickMermaidSource;
          n.removeAttribute('data-processed');
        }
      });
      if(el.querySelector('pre.mermaid:not([data-processed])')) renderMermaid(el);
    }, 120);
  }
  function mermaidNodeId(n){
    var row = n.closest && n.closest('.row[id]');
    var base = row ? row.id : 'stream';
    var all = row ? row.querySelectorAll('pre.mermaid') : document.querySelectorAll('#stream pre.mermaid');
    var idx = Array.prototype.indexOf.call(all, n);
    if(idx < 0) idx = window.__sidekickMermaidAnonId = (window.__sidekickMermaidAnonId || 0) + 1;
    return ('sidekick-mermaid-' + base + '-' + idx).replace(/[^A-Za-z0-9_-]/g, '-');
  }
  function renderMermaidNode(n){
    if(!n || !window.mermaid) return Promise.resolve();
    var src = n.__sidekickMermaidSource || n.textContent || '';
    n.__sidekickMermaidSource = src;
    n.__sidekickMermaidTries = (n.__sidekickMermaidTries || 0) + 1;
    n.setAttribute('data-processed', 'true');
    n.setAttribute('data-mermaid-id', mermaidNodeId(n));
    try {
      return window.mermaid.render(n.getAttribute('data-mermaid-id'), src, n).then(function(result){
        n.innerHTML = result.svg;
        if(result.bindFunctions) result.bindFunctions(n);
      }).catch(function(err){
        n.textContent = src;
        releaseMermaidFailures([n], err);
      });
    } catch(err){
      n.textContent = src;
      releaseMermaidFailures([n], err);
      return Promise.resolve();
    }
  }
  function queueMermaidRender(el){
    window.__sidekickMermaidRenderScope = mermaidScope(el);
    if(window.__sidekickMermaidRenderActive) return;
    window.__sidekickMermaidRenderActive = true;
    var tick = window.requestAnimationFrame || function(fn){ setTimeout(fn, 0); };
    tick(function(){
      var scope = window.__sidekickMermaidRenderScope;
      window.__sidekickMermaidRenderScope = null;
      var chain = window.__sidekickMermaidRenderChain || Promise.resolve();
      window.__sidekickMermaidRenderChain = chain.then(function(){
        window.__sidekickMermaidRenderQueued = false;
        if(!scope || scope.isConnected === false) scope = document.getElementById('stream');
        if(!scope || !scope.querySelector('pre.mermaid:not([data-processed])')) return null;
        return drawMermaid(scope);
      }, function(){
        window.__sidekickMermaidRenderQueued = false;
        if(!scope || scope.isConnected === false) scope = document.getElementById('stream');
        if(!scope || !scope.querySelector('pre.mermaid:not([data-processed])')) return null;
        return drawMermaid(scope);
      }).then(function(){
        window.__sidekickMermaidRenderActive = false;
        if(window.__sidekickMermaidRenderScope) queueMermaidRender(window.__sidekickMermaidRenderScope);
      }, function(err){
        window.__sidekickMermaidRenderActive = false;
        mermaidWarn(err);
        if(window.__sidekickMermaidRenderScope) queueMermaidRender(window.__sidekickMermaidRenderScope);
      });
    });
  }
  // Initialised once with manual start so we control *when* it runs (after a
  // render, never mid-stream on partial source).
  function drawMermaid(el){
    if(!el || !window.mermaid) return Promise.resolve();
    if(!window.__mermaidInit){
      // Theme mermaid to the app's warm Claude palette (default theme is purple
      // and clashes with the cream/terracotta UI). 'base' + themeVariables lets us
      // pin every colour; values mirror the CSS :root vars.
      try { window.mermaid.initialize({
        startOnLoad:false, securityLevel:'strict',
        theme:'base', fontFamily:"'Styrene B','Segoe UI',system-ui,-apple-system,sans-serif",
        // useMaxWidth:true — a diagram never grows wider than its cell. mermaid emits
        // svg width:100% + max-width:<natural>px, so a diagram that already fits renders
        // at natural size (crisp, unchanged), and only one that WOULD overflow scales
        // down to fit (text shrinks mildly) instead of spilling out or forcing a
        // sideways scroll. We lean on hard top-down (`flowchart TD`) generation — see
        // _DIAGRAM_GUIDANCE — to keep diagrams narrow, so the shrink rarely bites.
        // Moderate spacing — tighter than mermaid's defaults but not crowded.
        // htmlLabels:false — node sizing then comes from SVG <text> getComputedTextLength
        // rather than measuring a <foreignObject> HTML label. foreignObject measurement
        // returns 0 whenever the node is rendered while its container isn't laid out
        // (the transient state during a #stream swap, or a display:none collapsed
        // section), which makes dagre pile every node onto one point — diagrams that
        // had rendered fine "corrupt" into a stack of overlapping boxes on the next
        // unrelated action. SVG text measurement is far more robust to that timing.
        flowchart:{ curve:'basis', htmlLabels:false, padding:12, nodeSpacing:40, rankSpacing:46, useMaxWidth:true },
        sequence:{ useMaxWidth:true, boxMargin:10, mirrorActors:false, actorMargin:50, width:150, height:42 },
        themeVariables:{
          background:'#FAF9F5',
          primaryColor:'#F5E9E2',          // node fill (soft terracotta tint)
          primaryBorderColor:'#D97757',     // accent
          primaryTextColor:'#2B2A27',       // ink
          secondaryColor:'#EDEAE1',
          tertiaryColor:'#F0EEE6',
          lineColor:'#9C988E',              // edges: muted, readable on cream
          textColor:'#2B2A27',
          mainBkg:'#F5E9E2', nodeBorder:'#D97757', clusterBkg:'#F0EEE6',
          clusterBorder:'#E4E1D8', titleColor:'#2B2A27', edgeLabelBackground:'#FAF9F5',
          fontSize:'15px',
          noteBkgColor:'#FBF3E7', noteBorderColor:'#D97757', noteTextColor:'#2B2A27',
          actorBkg:'#F5E9E2', actorBorder:'#D97757', actorTextColor:'#2B2A27',
          signalColor:'#73706A', signalTextColor:'#2B2A27', labelBoxBkgColor:'#FBF3E7',
          labelBoxBorderColor:'#D97757', activationBkgColor:'#EDEAE1'
        }
      }); } catch(e){}
      window.__mermaidInit = true;
    }
    var nodes = Array.prototype.slice.call(el.querySelectorAll('pre.mermaid:not([data-processed])'))
      .filter(function(n){ return (n.__sidekickMermaidTries || 0) < MERMAID_MAX_TRIES; });
    if(!nodes.length) return Promise.resolve();
    return nodes.reduce(function(p, n){
      return p.then(function(){ return renderMermaidNode(n); });
    }, Promise.resolve()).then(function(){ scheduleMermaidRetry(el); });
  }

  // Add a hover Copy button to each highlighted code block in answers/notes.
  function copyText(text, btn){
    function done(){ btn.textContent = 'Copied!'; setTimeout(function(){ btn.textContent = 'Copy'; }, 1200); }
    if(navigator.clipboard && navigator.clipboard.writeText){
      navigator.clipboard.writeText(text).then(done).catch(function(){ done(); });
    } else {
      var t = document.createElement('textarea'); t.value = text;
      t.style.position='fixed'; t.style.opacity='0'; document.body.appendChild(t); t.select();
      try { document.execCommand('copy'); } catch(e){}
      document.body.removeChild(t); done();
    }
  }
  function addCopyButtons(el){
    if(!el) return;
    el.querySelectorAll('.highlight').forEach(function(block){
      if(block.__copy) return; block.__copy = true;
      block.style.position = 'relative';
      var btn = document.createElement('button');
      btn.type = 'button'; btn.className = 'copy-btn'; btn.textContent = 'Copy';
      btn.addEventListener('click', function(e){
        e.stopPropagation();             // don't trigger a note's click-to-edit
        var pre = block.querySelector('pre');
        copyText(pre ? pre.innerText : block.innerText, btn);
      });
      block.appendChild(btn);
    });
  }
  document.querySelectorAll('#stream .md').forEach(function(el){   // already-rendered answers/notes
    renderMath(el); renderMermaid(el); addCopyButtons(el);
  });
  if(window.buildTOC) window.buildTOC();   // refresh the table of contents on every render

  // Drag-to-reorder: SortableJS on the cell list, dragged via each cell's handle.
  // The wrap is a fresh element on every swap, so init once per wrap.
  var wrap = document.querySelector('#stream .wrap');
  if(wrap && window.Sortable && !wrap.__sortable){
    wrap.__sortable = Sortable.create(wrap, {
      draggable: '.row', handle: '.drag-handle', animation: 150,
      ghostClass: 'sortable-ghost', chosenClass: 'sortable-chosen',
      onEnd: function(){
        var ids = Array.prototype.map.call(wrap.querySelectorAll('.row'),
          function(r){ return r.id.replace('cell-', ''); });
        fetch('/cell/move', {
          method: 'POST', headers: {'Content-Type': 'application/x-www-form-urlencoded'},
          body: 'ids=' + encodeURIComponent(ids.join(','))
        }).catch(function(){});            // best-effort; the DOM is already reordered
        if(window.buildTOC) window.buildTOC();
      }
    });
  }

  // Live AI answers: connect a vanilla EventSource for each streaming bubble.
  // 'msg' events carry the cumulative rendered markdown; 'done' closes the stream.
  // Runs on every #stream (re)render so newly-inserted placeholders get wired.
  document.querySelectorAll('[data-stream-url]').forEach(function(el){
    if(el.__streaming) return; el.__streaming = true;
    var es = new EventSource(el.getAttribute('data-stream-url'));
    es.addEventListener('msg', function(e){
      el.innerHTML = e.data;
      el.classList.add('streaming');                   // blinking caret while tokens arrive
      renderMath(el); addCopyButtons(el);              // typeset math + copy buttons as it streams
      var s = el.closest('.stream'); if(s) s.scrollTop = s.scrollHeight;
    });
    es.addEventListener('cost', function(e){
      // Turn finished: refresh the foot-of-stream meter's running cost in place.
      var meter = document.getElementById('ctxMeter');
      if(meter) meter.textContent = e.data;
    });
    es.addEventListener('done', function(e){
      el.classList.remove('streaming');                // generation finished → drop the caret
      renderMath(el); renderMermaid(el); addCopyButtons(el);  // diagrams only once source is complete
      es.close(); el.removeAttribute('data-stream-url'); el.__streaming = false;
      // The AI's tools edited cells this turn — refresh #stream so they appear.
      if(e && e.data && e.data.indexOf('reload') >= 0 && window.htmx){
        window.htmx.ajax('GET', '/stream/refresh', {target:'#stream', swap:'outerHTML'});
      }
    });
    es.onerror = function(){ es.close(); };
  });

  // Live code execution: connect an EventSource for each running code cell.
  // 'msg' events carry the accumulated stdout/stderr (a <pre>) as it streams;
  // 'done' persists server-side and reloads #stream so the finished cell (plots,
  // normal Run button) renders. Mirrors the answer-streaming loop above.
  document.querySelectorAll('[data-exec-url]').forEach(function(el){
    if(el.__execing) return; el.__execing = true;
    var es = new EventSource(el.getAttribute('data-exec-url'));
    es.addEventListener('msg', function(e){
      el.innerHTML = e.data;                           // cumulative output snapshot
      var s = el.closest('.stream'); if(s) s.scrollTop = s.scrollHeight;
    });
    es.addEventListener('done', function(e){
      es.close(); el.removeAttribute('data-exec-url'); el.__execing = false;
      if(window.htmx){                                 // re-render the finished cell
        window.htmx.ajax('GET', '/stream/refresh', {target:'#stream', swap:'outerHTML'});
      }
    });
    es.onerror = function(){ es.close(); };
  });

  // ---- Jupyter-style command mode ---------------------------------------
  // A cell is "selected" (command mode) by id; the highlight is re-applied on
  // every swap since #stream is replaced wholesale. Defined every render (cheap
  // reassign) so the per-render call below and the once-bound listeners share them.
  window.__cellIds = function(){
    return Array.prototype.map.call(document.querySelectorAll('#stream .row'),
      function(r){ return r.id.replace('cell-', ''); });
  };
  window.__inEditor = function(t){
    return !!(t && (t.tagName === 'TEXTAREA' || t.tagName === 'INPUT' || t.tagName === 'SELECT'
                    || t.isContentEditable || (t.closest && t.closest('.CodeMirror'))));
  };
  window.__selectCell = function(id, scroll){
    window.__selCell = id || null;
    document.querySelectorAll('#stream .row.selected').forEach(function(r){
      r.classList.remove('selected'); });
    if(!id) return;
    var row = document.getElementById('cell-' + id);
    if(row){ row.classList.add('selected'); if(scroll) row.scrollIntoView({block:'nearest'}); }
    else window.__selCell = null;
  };
  // Re-apply the selection highlight after this (re)render; drop it if the cell is gone.
  window.__selectCell(window.__selCell, false);

  // ---- collapsible sections (a heading note folds the cells beneath it) -------
  // Per-dialog collapse state in localStorage; re-applied on every render since
  // #stream is rebuilt on each action. A section runs from a heading note down to
  // the next heading of the same-or-higher level (so a # folds its ## subsections).
  function _secKey(){ var s = document.getElementById('stream');
    return 'sidekick_collapsed_' + (s ? s.getAttribute('data-dialog') : ''); }
  function _secLoad(){ try { return new Set(JSON.parse(localStorage.getItem(_secKey()) || '[]')); }
                       catch(e){ return new Set(); } }
  function _secSave(set){ try { localStorage.setItem(_secKey(), JSON.stringify(Array.from(set))); } catch(e){} }
  window.__applyCollapsed = function(){
    var set = _secLoad();
    var rows = Array.prototype.slice.call(document.querySelectorAll('#stream .row'));
    rows.forEach(function(r){ r.classList.remove('sec-hidden'); });
    function levelOf(r){ var c = r.querySelector('.sec-caret');
                         return c ? parseInt(c.getAttribute('data-sec-level'), 10) : 0; }
    for(var i = 0; i < rows.length; i++){
      var caret = rows[i].querySelector('.sec-caret');
      if(!caret) continue;
      var lvl = parseInt(caret.getAttribute('data-sec-level'), 10);
      var collapsed = set.has(caret.getAttribute('data-sec')), count = 0;
      caret.classList.toggle('collapsed', collapsed);
      for(var j = i + 1; j < rows.length; j++){
        var l = levelOf(rows[j]);
        if(l > 0 && l <= lvl) break;             // next same-or-higher heading ends the section
        if(collapsed) rows[j].classList.add('sec-hidden');
        count++;
      }
      var pill = rows[i].querySelector('.sec-count');
      if(pill){ pill.textContent = count + ' hidden';
                pill.style.display = (collapsed && count) ? '' : 'none'; }
    }
  };
  window.__toggleSection = function(id){
    var set = _secLoad();
    if(set.has(id)) set.delete(id); else set.add(id);
    _secSave(set); window.__applyCollapsed();
  };
  window.__applyCollapsed();

  if(window.__sidekickCells) return;                            // bind document listeners once
  window.__sidekickCells = true;
  // Remember the notebook's scroll position right before htmx replaces #stream,
  // so the fresh render (above) can restore it instead of jumping to the top.
  document.addEventListener('htmx:beforeSwap', function(){
    var s = document.getElementById('stream');
    if(s) window.__streamScroll = s.scrollTop;
  });
  // Re-apply the command-mode selection highlight once htmx finishes a swap.
  // (The inline reapply runs too early on script-bearing swaps, so the class
  // gets dropped; afterSettle reliably lands after the new DOM is in place.)
  document.addEventListener('htmx:afterSettle', function(){
    if(window.__escToCell && window.__selectCell){
      // Esc out of an editor: keep the escaped cell in view (scroll:true) instead
      // of letting the post-swap scroll settle at the bottom / composer.
      window.__selectCell(window.__escToCell, true);
      window.__escToCell = null;
    } else if(window.__selCell && window.__selectCell){
      window.__selectCell(window.__selCell, false);
    }
    if(window.__applyCollapsed) window.__applyCollapsed();     // re-fold sections after a swap
    // Partial swaps (answer save/cancel, single-cell view) replace a fragment
    // without re-running STREAM_JS, so the line-1198 render loop never touches
    // them. Re-scan #stream so freshly-swapped diagrams/math/copy buttons render.
    // (e.detail.target is the *detached* old node on outerHTML swaps, so we can't
    // rely on it.) Idempotent: renderMermaid skips [data-processed], addCopyButtons
    // skips __copy, and KaTeX re-typeset is a no-op once delimiters are gone.
    document.querySelectorAll('#stream .md').forEach(function(el){
      renderMath(el); renderMermaid(el); addCopyButtons(el);
    });
  });
  // Click a section caret to fold/unfold the cells beneath its heading.
  document.addEventListener('click', function(e){
    var caret = e.target.closest && e.target.closest('#stream .sec-caret');
    if(caret && window.__toggleSection) window.__toggleSection(caret.getAttribute('data-sec'));
  });
  if(!hasFieldSizing){
    document.addEventListener('input', function(e){
      if(e.target.classList && e.target.classList.contains('cell-edit')) autosize(e.target);
    });
    window.addEventListener('resize', sizeAll);
  }
  document.addEventListener('keydown', function(e){
    if(e.target.classList && e.target.classList.contains('cell-edit')
       && (e.metaKey || e.ctrlKey || e.shiftKey) && e.key === 'Enter'){
      e.preventDefault();
      // Prefer a Save/Run button inside the same answer editor; fall back to the
      // cell's own run button. So Cmd+Enter while editing an AI answer saves the
      // answer rather than re-asking the question.
      var scope = e.target.closest('.answer') || e.target.closest('.row');
      var btn = scope && scope.querySelector('.cell-btn.run');
      if(btn) btn.click();                  // Shift/Cmd/Ctrl+Enter runs; plain Enter = newline
    }
  });
  // Track the cell under the cursor — the fallback target for a/b before anything
  // is selected. Clicking a cell selects it (so Esc later lands on the right one).
  document.addEventListener('mouseover', function(e){
    var row = e.target.closest && e.target.closest('#stream .row');
    if(row) window.__activeRow = row;
  });
  document.addEventListener('click', function(e){
    var row = e.target.closest && e.target.closest('#stream .row');
    if(row) window.__selectCell(row.id.replace('cell-', ''), false);
  });

  // Jupyter-style command mode: Esc leaves the editor; ↑/↓ or j/k move the
  // selection; Enter edits; a/b insert; dd deletes; z undoes the last delete.
  // Single-letter keys only fire when no editor/input is focused.
  function _insert(where){
    var id = window.__selCell ||
             (window.__activeRow && window.__activeRow.id.replace('cell-', ''));
    if(!id || !window.htmx) return;
    htmx.ajax('POST', '/cell/insert', {target: '#stream', swap: 'outerHTML',
      values: {id: id, msg_type: 'note', where: where}});   // a/b default to a note
  }
  document.addEventListener('keydown', function(e){
    if((e.metaKey || e.ctrlKey) && e.key !== 'Enter') return;   // allow Cmd/Ctrl+Enter through
    var inEditor = window.__inEditor(e.target);

    if(e.key === 'Escape'){                        // leave edit mode -> command mode
      if(!inEditor) return;
      var row = e.target.closest('#stream .row');
      if(!row) return;
      e.preventDefault();
      var cid = row.id.replace('cell-', '');
      // An AI answer is just another markdown view: Esc renders it (clicks the
      // answer's OWN Save), same as a note — scoped to .answer so it doesn't hit
      // the prompt row's Ask button. Mirrors the Cmd+Enter handler above.
      // __escToCell: the Esc-triggered Save below swaps all of #stream, and the
      // synchronous scroll-restore can land at the bottom (near the composer)
      // when the freshly-swapped content isn't laid out yet. Re-assert this cell
      // into view once the swap settles (see htmx:afterSettle) so Esc keeps you
      // exactly where you were editing, never yanked down to the composer.
      var ans = e.target.closest('.answer');
      if(ans){
        var asave = ans.querySelector('.cell-btn.run');
        if(asave){ window.__selCell = cid; window.__escToCell = cid; asave.click(); return; }
      }
      // A note (markdown) cell renders on Esc — same as Save — instead of just
      // dropping focus while it keeps showing the raw source. Code/prompt cells
      // only fall back to command mode (Jupyter never runs code on Esc).
      if(row.classList.contains('note')){
        var save = row.querySelector('.cell-btn.run');
        if(save){ window.__selCell = cid; window.__escToCell = cid; save.click(); return; }
      }
      if(e.target.blur) e.target.blur();
      window.__selectCell(cid, false);
      return;
    }
    if(inEditor || e.altKey) return;               // everything below is command-mode only
    if(e.key !== 'd') window.__lastD = 0;           // any other key breaks a pending 'dd'

    var ids = window.__cellIds();
    if(!ids.length) return;
    var idx = ids.indexOf(window.__selCell);

    if(e.key === 'ArrowDown' || e.key === 'j'){
      e.preventDefault();
      window.__selectCell(ids[idx < 0 ? 0 : Math.min(ids.length - 1, idx + 1)], true);
    } else if(e.key === 'ArrowUp' || e.key === 'k'){
      e.preventDefault();
      window.__selectCell(ids[idx < 0 ? ids.length - 1 : Math.max(0, idx - 1)], true);
    } else if(e.key === 'Enter'){
      if(idx < 0) return;
      e.preventDefault();
      var row = document.getElementById('cell-' + window.__selCell);
      if(e.shiftKey || e.metaKey || e.ctrlKey){    // Jupyter: run the cell, don't edit it
        var rb = row && row.querySelector('.cell-btn.run');
        if(rb) rb.click();                         // code/prompt run; a note is already rendered
        if(e.shiftKey && !e.metaKey && !e.ctrlKey) // Shift+Enter also advances to the next cell
          window.__selectCell(ids[Math.min(ids.length - 1, idx + 1)], true);
        return;
      }
      var view = row && row.querySelector('.clickedit');
      if(view){ view.click(); return; }            // plain Enter: rendered cell -> open its editor
      var cm = row && row.querySelector('.CodeMirror');
      if(cm && cm.CodeMirror){ cm.CodeMirror.focus(); return; }
      var ta = row && row.querySelector('textarea');
      if(ta) ta.focus();
    } else if(e.key === 'a' || e.key === 'b'){
      e.preventDefault();
      _insert(e.key === 'a' ? 'above' : 'below');
    } else if(e.key === 'y' || e.key === 'm' || e.key === 'i'){   // convert cell type
      if(idx < 0 || !window.htmx) return;
      e.preventDefault();
      var t = e.key === 'y' ? 'code' : (e.key === 'm' ? 'note' : 'prompt');
      htmx.ajax('POST', '/cell/type', {target: '#stream', swap: 'outerHTML',
        values: {id: ids[idx], msg_type: t}});   // id is unchanged -> selection persists
    } else if(e.key === 'd'){                       // dd within 600ms = delete
      var now = Date.now();
      if(window.__lastD && now - window.__lastD < 600){
        window.__lastD = 0;
        if(idx < 0 || !window.htmx) return;
        e.preventDefault();
        window.__selCell = ids[idx + 1] || ids[idx - 1] || null;   // land on a neighbor
        htmx.ajax('POST', '/cell/delete', {target: '#stream', swap: 'outerHTML',
          values: {id: ids[idx]}});
      } else { window.__lastD = now; }
    } else if(e.key === 'z'){                       // undo last delete
      if(!window.htmx) return;
      e.preventDefault();
      htmx.ajax('POST', '/cell/undo', {target: '#stream', swap: 'outerHTML', values: {}});
    }
  });
})();
"""


# Published API rates ($/1M tokens) for the models the subscription CLI routes
# to, so we can show an estimated session cost when the CLI reports
# total_cost_usd == 0 (some Max setups do). Cache reads bill ~0.1x input, writes
# ~1.25x. Keep in sync with kernel_server.MODEL_NAMES / shared/models.md.
_PRICES = {"opus": (5.0, 25.0), "sonnet": (3.0, 15.0), "haiku": (1.0, 5.0)}


def _est_usd(cost: dict, tier: str = "sonnet") -> float:
    """Estimate session cost from accumulated token usage at published API rates —
    the fallback when the CLI itself doesn't report a dollar figure. Defaults to the
    Sonnet tier (the notebook CLI's default model)."""
    in_rate, out_rate = _PRICES.get(tier, _PRICES["sonnet"])
    return (cost["input"] * in_rate + cost["output"] * out_rate
            + cost["cache_read"] * in_rate * 0.1
            + cost["cache_write"] * in_rate * 1.25) / 1_000_000


def _cost_label(cost: dict) -> str:
    """Compact running-cost segment for the context meter: the dollar figure the
    Max-plan CLI reported this session, or a token-based estimate if it reported
    none, plus the turn count."""
    usd, estimated = cost["usd"], False
    if usd <= 0:                                   # subscription reported no $ — estimate
        usd, estimated = _est_usd(cost), True
    money = f"${usd:.2f}" if usd >= 0.01 else f"${usd:.3f}"
    turns = cost["turns"]
    return f"{'~' if estimated else ''}{money} this session · {turns} turn{'' if turns == 1 else 's'}"


def _ctx_meter_text(msgs, dialog=None):
    """The foot-of-stream read-out: how much of the notebook the AI would see right
    now, plus — once the session has run a turn — its running cost."""
    toks = est_tokens(build_context(msgs))
    n_muted = sum(1 for m in msgs if m.muted)
    n_pin = sum(1 for m in msgs if m.pinned)
    bits = [f"AI context ≈ {toks:,} tokens", f"{len(msgs) - n_muted}/{len(msgs)} cells in"]
    if n_pin:
        bits.append(f"{n_pin} pinned")
    if n_muted:
        bits.append(f"{n_muted} muted")
    cost = cost_for(dialog if dialog is not None else cur("dialog"))
    if cost and cost["turns"]:
        bits.append(_cost_label(cost))
    return " · ".join(bits)


def _ctx_meter(msgs):
    """A live read-out of context size and running cost. `id` so the SSE answer
    stream can refresh just this line (a `cost` event) after each turn."""
    return Div(_ctx_meter_text(msgs), id="ctxMeter", cls="ctx-meter",
               title="context is estimated (~4 chars/token); running cost is what the "
                     "Max-plan CLI reports this session would bill at API rates")


def Stream():
    msgs = STATE["backend"].messages(cur("dialog"))
    editing = pop_cur("editing", None)            # a just-inserted cell opens in edit mode (one-shot)
    focus_start = pop_cur("focus_start", None)    # inserted cells put the cursor at the beginning
    scroll_to = pop_cur("scroll_to", None)        # scroll a just-added cell into view (one-shot)
    flash = pop_cur("flash", None)                # transient confirmation banner (one-shot)
    if not msgs:
        inner = Div("Start the conversation — write code, ask the AI, or jot a note.",
                    cls="empty")
    else:
        rows = [
            _cell_edit(m, num=i, focus_start=(m.id == focus_start))
            if m.id == editing else MsgRow(m, num=i)
            for i, m in enumerate(msgs, 1)
        ]
        inner = Div(*rows, _ctx_meter(msgs), cls="wrap")
    extra = ()
    if scroll_to:
        # The #stream swap resets the scroll container to the top; bring the target
        # cell back into view. scrollIntoView (not scrollTop=scrollHeight) handles
        # both a just-added cell at the bottom (composer /send) and a re-run cell in
        # the middle (re-asking a prompt). rAF so it runs after layout settles.
        #
        # A freshly inserted/edited cell (scroll_to == focus_start) opens in edit
        # mode with the cursor at the beginning, so align to keep that beginning in
        # view with the *least* scroll ('nearest' → no jump if it's already visible)
        # rather than 'center', which yanks the viewport down to mid-screen and
        # buries the top of the cell the user just started typing in.
        block = "nearest" if scroll_to == focus_start else "center"
        extra = (Script(f"requestAnimationFrame(function(){{var c="
                        f"document.getElementById('cell-{scroll_to}');"
                        f"if(c)c.scrollIntoView({{block:'{block}'}});}});"),)
    if flash:
        # A self-removing banner: fades after a couple seconds so a copy lands with
        # visible feedback even though the current dialog's cells don't change.
        extra = extra + (Div(flash, cls="flash", id="flash"),
                         Script("setTimeout(function(){var f=document.getElementById('flash');"
                                "if(f)f.remove();},2400);"))
    return Div(inner, Script(STREAM_JS), *extra, cls="stream", id="stream",
               **{"data-dialog": cur("dialog")})


COMPOSER_JS = _static_text("js/composer.js")


# A floating "Ask AI ↗" bubble over text selected in the dialog stream — the paper
# panel's selection toolbar, but for the conversation itself. Attaches once at the
# document level so it survives #stream htmx swaps, and reuses __askComposer.
STREAM_SEL_JS = _static_text("js/stream_sel.js")


def ModelSelect():
    opts = [
        Option(m["label"], value=m["id"], selected=(m["id"] == STATE["model"]))
        for m in list_models()
    ]
    # The chosen model is submitted with the send form (name="model").
    return Select(*opts, name="model", cls="msel", title="AI model for Ask AI")


def ModeSelect():
    """The SolveIt AI mode — how the assistant should respond (learning/concise/
    standard). Submitted with the send form (name="ai_mode") and remembered."""
    cur = STATE.get("ai_mode", DEFAULT_MODE)
    opts = [Option(label, value=mid, selected=(mid == cur)) for mid, label in AI_MODES]
    return Select(*opts, name="ai_mode", cls="msel",
                  title="AI mode — learning asks guiding questions; concise is terse; "
                        "standard answers fully")


def Composer():
    cur = STATE.get("msg_type", "prompt")

    def mode(val, label):
        sel = val == cur
        # Clicking a mode sets the hidden msg_type and highlights the chip.
        return Label(label, cls=f"mode{' sel' if sel else ''}",
                     onclick=f"setMode('{val}')", **{"data-val": val})

    return Div(Div(Div(
        Form(
            Input(type="hidden", name="msg_type", value=cur, id="msgType"),
            Textarea(name="content", id="composerInput",
                     placeholder="Message SolveIt…  (Shift+Enter to send, Enter for newline)"),
            Div(
                Div(mode("prompt", "Ask AI"), mode("code", "Code"),
                    mode("note", "Note"), cls="modes", id="modeChips"),
                Div(ModeSelect(), ModelSelect(),
                    Button("↑", cls="send", type="button", onclick="_submitComposer()"),
                    style="display:flex;align-items:center;gap:8px"),
                cls="row2",
            ),
            # htmx swaps just #stream (no full-page reload) so the answer streams
            # sooner; native method/action stays as a no-JS fallback.
            method="post", action="/send", id="composerForm", cls="box",
            hx_post="/send", hx_target="#stream", hx_swap="outerHTML",
        ),
        Div("Connected to ", Strong(STATE["target_name"]),
            " · switch target top-right to move between laptop and H100", cls="hint"),
        Script(src="/static/js/composer.js"),
        cls="wrap"), cls="composer"))


def TitleEditor():
    """The dialog name, editable in place. Submits on Enter or blur."""
    return Form(
        Input(name="new", value=cur("dialog"), cls="title-edit",
              title="Rename this dialog — press Enter",
              onblur="this.form.submit()",
              onkeydown="if(event.key==='Enter'){event.preventDefault();this.form.submit();}"),
        Input(type="hidden", name="old", value=cur("dialog")),
        method="post", action="/rename", cls="title-form",
    )


def SettingsPage(saved=False):
    rows = []
    for s in secrets_store.status():
        is_default = s["model"] == STATE["model"]
        status_txt = (
            (f"✓ set ({s['masked']})" + (" — from env" if s["from_env"] else ""))
            if s["set"] else "not set"
        )
        rows.append(Div(
            Div(Strong(s["label"]),
                Span(f"  ·  {s['model']}", cls="muted"),
                (Span("  default", cls="pill-default") if is_default else ""),
                cls="prov-head"),
            Div(status_txt, cls=f"prov-status {'ok' if s['set'] else 'off'}"),
            Input(type="password", name=s["env"],
                  placeholder=("leave blank to keep current" if s["set"] else f"paste {s['label']} API key"),
                  cls="keyinput"),
            Div(f"env var: {s['env']}", cls="muted small"),
            cls="prov-card",
        ))
    return Html(
        Head(Title("Settings · SolveIt Sidekick"), *app.hdrs, Link(rel="stylesheet", href="/static/css/app.css")),
        Body(Div(
            Div(
                Div(A("←  Back", href="/", cls="back"), Div("Settings", cls="title"),
                    cls="settings-top"),
                (Div("✓ Saved.", cls="saved") if saved else ""),
                Div("API keys for the model providers. Keys are stored locally in "
                    f"{secrets_store.secrets_path()} (chmod 600) and never committed.",
                    cls="settings-intro"),
                Form(
                    *rows,
                    Div(Label("Default model ",
                              Select(*[Option(m["label"], value=m["id"],
                                              selected=(m["id"] == STATE["model"]))
                                       for m in list_models()],
                                     name="default_model")),
                        cls="prov-card"),
                    Button("Save", cls="save-btn", type="submit"),
                    method="post", action="/settings",
                ),
                cls="settings-wrap",
            ),
            cls="settings-page",
        )),
    )


def _lib_result_banner(result):
    if not result:
        return ""
    if result.get("error"):
        return Div("⚠ " + result["error"], cls="banner")
    if result.get("use"):
        pkg = result["pkg"]
        if result.get("ok"):
            return Div(Span(f"'{result['use']}' is on the kernel path — "),
                       Code(f"import {pkg}"),
                       Span(" now works in any dialog (build it first if you haven't)."),
                       cls="saved")
        return Div("⚠ couldn't reach the kernel to add the path.", cls="banner")
    nb = ("✓ built with nbdev" if result.get("nbdev_ok")
          else "notebooks emitted — " + result.get("nbdev_detail", ""))
    mods = ", ".join(result.get("modules") or []) or "no tagged cells"
    return Div(f"Built '{result['name']}': {mods}  ·  {nb}  ·  → {result.get('path','')}",
               cls="saved")


def LibrariesPage(result=None, saved=False):
    backend = STATE["backend"]
    has_kernel = hasattr(backend, "add_syspath")
    # The remote SolveIt LiveBackend can't enumerate dialogs (list_dialogs() -> []),
    # so a cross-dialog build would silently show nothing tagged. Flag it instead.
    live_cliff = (getattr(backend, "live", False) and not backend.list_dialogs())
    cards = []
    for lib in libraries.load():
        mods = nbdev_export.gather(backend, lib["name"])
        ncells = sum(len(v) for v in mods.values())
        summary = (f"{ncells} cells · {len(mods)} modules"
                   + (f" ({', '.join(sorted(mods))})" if mods else " · nothing tagged yet"))
        actions = [Form(Input(type="hidden", name="name", value=lib["name"]),
                        Button("Build", cls="cell-btn run", type="submit"),
                        method="post", action="/library/build", style="display:inline")]
        if has_kernel:
            actions.append(Form(
                Input(type="hidden", name="name", value=lib["name"]),
                Button("Use in kernel", cls="cell-btn", type="submit",
                       title=f"Put this library on the kernel path so `import {lib['pkg']}` "
                             f"works in any dialog"),
                method="post", action="/library/use", style="display:inline"))
        actions.append(Form(
            Input(type="hidden", name="name", value=lib["name"]),
            Button("Remove", cls="cell-btn del", type="submit"),
            method="post", action="/library/remove", style="display:inline",
            onsubmit=f"return confirm('Remove library {lib['name']}? "
                     f"(the registry entry only — no files are deleted)')"))
        cards.append(Div(
            Div(Strong(lib["name"]), Span(f"  ·  pkg {lib['pkg']}", cls="muted"), cls="prov-head"),
            Div(summary, cls="muted small"),
            Div(f"→ {lib['path']}", cls="muted small"),
            Div(*actions, cls="lib-actions", style="display:flex;gap:8px;margin-top:8px"),
            cls="prov-card"))
    if not cards:
        cards = [Div("No libraries yet. Create one below, then tag code cells with the "
                     "Lib picker (or type ", Code("#| export <name>:<module>"), ").",
                     cls="settings-intro")]
    add_form = Form(
        Span("New library", cls="ins-col-head"),
        Input(name="name", placeholder="name — used as #| export <name>:module", cls="keyinput"),
        Input(name="pkg", placeholder="package name (optional; defaults to a slug of name)",
              cls="keyinput"),
        Input(name="path", placeholder="nbdev project dir (optional)", cls="keyinput"),
        Button("Add library", cls="save-btn", type="submit"),
        method="post", action="/library/add")
    return Html(
        Head(Title("Libraries · SolveIt Sidekick"), *app.hdrs, Link(rel="stylesheet", href="/static/css/app.css")),
        Body(Div(
            Div(
                Div(A("←  Back", href="/", cls="back"), Div("Libraries", cls="title"),
                    cls="settings-top"),
                (Div("✓ Saved.", cls="saved") if saved else ""),
                (Div("⚠ Cross-dialog build needs the local kernel backend (the live "
                     "SolveIt backend can't enumerate dialogs yet).", cls="banner")
                 if live_cliff else ""),
                _lib_result_banner(result),
                Div("Build a Python library from cells tagged across your dialogs. Sidekick "
                    "projects them into an nbdev project; nbdev builds the package. See the "
                    "design note for the model.", cls="settings-intro"),
                *cards,
                add_form,
                cls="settings-wrap",
            ),
            cls="settings-page",
        )),
    )


# Table of contents built from note headings (h1–h6 inside .note-view). Lives in
# Page() (not #stream), so it persists across htmx swaps; STREAM_JS calls
# window.buildTOC() on each render to keep it in sync. Toggle state is saved.
MSGLINK_JS = _static_text("js/msglink.js")

TOC_JS = _static_text("js/toc.js")


def _paper_blocks(p: dict) -> list[str]:
    """Block-split of the open paper, memoized on the paper state (md is immutable
    once ready, so the split is computed once instead of on every render)."""
    if "blocks" not in p:
        p["blocks"] = paperlib.split_blocks(p.get("md", ""))
    return p["blocks"]


def _paper_sections(p: dict) -> list[str]:
    """Section-split of the open paper, memoized — reuses the cached blocks so the
    document isn't walked twice."""
    if "sections" not in p:
        p["sections"] = paperlib.split_sections(p.get("md", ""), blocks=_paper_blocks(p))
    return p["sections"]


def PaperPanel():
    """Left reading column: the open paper as rendered markdown, or a converting
    spinner that polls until ready. Selecting text shows an 'Ask AI' button."""
    p = STATE.get("paper")
    if not p:
        return Div(cls="paper", id="paperPanel")
    actions = []
    if p.get("status") == "ready":
        # The primary flow is highlight → import (see the badge + selection toolbar).
        # The sequential stepper and whole-paper bulk import live in this menu so the
        # header stays clean.
        n = len(_paper_sections(p))
        step = p.get("step", 0)
        if step < n:
            stepper = Form(
                Button(f"Next section ▸  ({step}/{n})", cls="cell-btn run", type="submit",
                       title="Bring the next section into the notebook as a note + "
                             "a code cell to reimplement it yourself"),
                method="post", action="/paper/step", cls="paper-import")
        else:
            stepper = Span(f"All {n} sections imported ✓", cls="muted small")
        bulk = Form(
            Span("Whole paper:", cls="muted small"),
            Button("¶", cls="cell-btn", type="submit", name="mode", value="para",
                   title="Import the whole paper at once — one note per paragraph"),
            Button("§", cls="cell-btn", type="submit", name="mode", value="section",
                   title="Import the whole paper at once — one note per section"),
            method="post", action="/paper/import", cls="paper-import")
        actions.append(_dropdown(
            "Import…",
            Span("Step by step", cls="ins-col-head"), stepper,
            Span("Or all at once", cls="ins-col-head"), bulk,
            title="Step through by section, or import the whole paper",
            menu_cls="paper-import-menu"))
        # collapse just the paper text — the header (incl. Import) stays put
        actions.append(Span("▾", cls="gear paper-toggle", title="Show/hide the paper text",
                            onclick="toggleCol('paper-collapsed','sidekick_paperhidden')"))
    actions.append(A("✕", href="/paper/close", cls="gear", title="Close paper"))
    head = Div(Span(p["name"], cls="paper-name"), Div(*actions, cls="paper-actions"),
               cls="paper-head")
    if p.get("status") == "converting":
        body = Div("Converting… first time runs the model and can take a bit.",
                   cls="paper-converting",
                   hx_get="/paper/status", hx_trigger="every 2s",
                   hx_target="#paperPanel", hx_swap="outerHTML")
        return Div(head, body, cls="paper", id="paperPanel")
    badge = Span(f"via {p.get('engine', '?')} · highlight text → import to notebook or ask the AI",
                 cls="muted small paper-badge")
    # Render block-by-block, each block carrying its *source markdown* in data-md,
    # so a highlight can be imported as real markdown (headings/formatting kept)
    # rather than the rendered plain text.
    body = Div(*[Div(render_md(b), cls="pblock", **{"data-md": b}) for b in _paper_blocks(p)],
               cls="paper-body md", id="paperBody")
    return Div(head, badge, body, Script(PAPER_JS), cls="paper", id="paperPanel")


# Select text in the paper → a floating button → prefill the composer (Ask AI)
# with the quoted passage, so the next question carries the paragraph as context.
PAPER_JS = """
(function(){
  // Typeset LaTeX in the paper (marker emits $$/inline math). Runs on each
  // render/swap; mistune already turned $…$ into \\(…\\), so no bare-$ delimiter.
  var body = document.getElementById('paperBody');
  if(body && window.renderMathInElement){
    try { renderMathInElement(body, { throwOnError:false, delimiters:[
      {left:'$$', right:'$$', display:true},
      {left:'\\\\[', right:'\\\\]', display:true},
      {left:'\\\\(', right:'\\\\)', display:false}
    ]}); } catch(e){}
  }
  if(window.__paperSel) return; window.__paperSel = true;
  var bar = null, curText = '', curMd = '';
  function hide(){ if(bar) bar.style.display = 'none'; }
  // Source markdown for the highlighted range: every block the selection touches,
  // in document order, so the imported note keeps headings/formatting. Snaps to
  // whole blocks (partial selection of a block still imports that block's source).
  function selectedMarkdown(sel){
    if(!sel.rangeCount) return '';
    var range = sel.getRangeAt(0), out = [];
    var blocks = document.querySelectorAll('#paperBody .pblock');
    for(var i = 0; i < blocks.length; i++){
      // intersectsNode is reliable at block boundaries (containsNode(.,true) can
      // drop a block the selection only partially covers → silent plain-text loss).
      var hit;
      try { hit = range.intersectsNode(blocks[i]); }
      catch(e){ hit = sel.containsNode(blocks[i], true); }
      if(hit){ var md = blocks[i].getAttribute('data-md'); if(md) out.push(md); }
    }
    return out.join('\\n\\n').trim();
  }
  function ask(text){
    if(window.__askComposer) window.__askComposer(text);   // shared composer prefill
    hide();
  }
  function toNotebook(text){
    // full-page POST so the new code cell opens focused (the Page render keeps
    // STATE['editing'], which an htmx/fetch round-trip would consume early).
    var f = document.createElement('form');
    f.method = 'POST'; f.action = '/paper/import-selection';
    var i = document.createElement('input');
    i.type = 'hidden'; i.name = 'text'; i.value = text;
    f.appendChild(i); document.body.appendChild(f); f.submit();
  }
  document.addEventListener('mouseup', function(){
    var paper = document.getElementById('paperBody');
    var sel = window.getSelection();
    var text = sel ? sel.toString().trim() : '';
    if(!paper || !text || !sel.anchorNode || !paper.contains(sel.anchorNode)){ hide(); return; }
    if(!bar){
      bar = document.createElement('div'); bar.className = 'sel-tools';
      var b1 = document.createElement('button');
      b1.className = 'sel-btn import'; b1.textContent = '→ Notebook';
      b1.title = 'Import this highlighted passage as a note + a code cell to reimplement it';
      b1.addEventListener('mousedown', function(e){ e.preventDefault(); toNotebook(curMd || curText); });
      var b2 = document.createElement('button');
      b2.className = 'sel-btn'; b2.textContent = 'Ask AI ↗';
      b2.title = 'Drop the passage into the composer as an Ask-AI question';
      b2.addEventListener('mousedown', function(e){ e.preventDefault(); ask(curText); });
      bar.appendChild(b1); bar.appendChild(b2);
      document.body.appendChild(bar);
    }
    curText = text;
    curMd = selectedMarkdown(sel) || text;     // source markdown for import, plain for ask
    var r = sel.getRangeAt(0).getBoundingClientRect();
    bar.style.top = (window.scrollY + r.bottom + 6) + 'px';
    bar.style.left = (window.scrollX + r.left) + 'px';
    bar.style.display = 'flex';
  });
})();
"""


def Page():
    banner = (Div("⚠ ", STATE["warning"], " — showing a mock so you can still explore the UI.",
                  cls="banner") if STATE["warning"] else None)
    return Html(
        # *app.hdrs carries htmx (+ fasthtml.js): without it the per-cell
        # hx-post buttons render but do nothing, since we return a full Html
        # document and FastHTML only auto-injects those headers when it wraps
        # body content itself.
        # app.hdrs carries everything (htmx + CodeMirror + KaTeX + Sortable),
        # all served locally from /vendor — see _LOCAL_HDRS. Fully offline.
        Head(Title("SolveIt Sidekick"), *app.hdrs, Link(rel="stylesheet", href="/static/css/app.css")),
        Body(Div(
            # global top bar — above all columns, so its toggles stay reachable
            # even when the dialogs panel is hidden.
            Div(
                Div(Span("🗂", cls="gear tgl", id="tgl-side",
                         title="Show/hide the dialogs panel",
                         onclick="toggleCol('no-side','sidekick_noside')"),
                    # paper open → show/hide toggle; closed but this dialog
                    # remembers its source → reopen it (see /paper/reopen)
                    (Span("📖", cls="gear tgl", id="tgl-paper",
                          title="Show/hide the paper (PDF / markdown) viewer",
                          onclick="toggleCol('no-paper','sidekick_nopaper')")
                     if STATE.get("paper") else
                     (A("📖", href="/paper/reopen", cls="gear tgl", id="tgl-paper",
                        title="Reopen this dialog's paper",
                        onclick="try{localStorage.removeItem('sidekick_nopaper')}catch(e){}")
                      if _paper_source_for(cur("dialog")) else None)),
                    Span("☰", cls="gear tgl toc-toggle", id="tgl-toc",
                         title="Show/hide the table of contents",
                         onclick="toggleCol('toc-open','sidekick_toc')"),
                    TitleEditor(),
                    cls="topbar-left"),
                Div(Details(
                        Summary("📄", cls="gear", title="Open a source — a PDF or a web page"),
                        Form(
                            # Explicit button → input.click() rather than a label
                            # wrapping the hidden input: nested-label + display:none
                            # file inputs fail to open the picker in some browsers.
                            # This triggers the native dialog reliably everywhere.
                            Input(type="file", name="pdf", id="pdfPick",
                                  accept="application/pdf,.pdf", cls="paper-file",
                                  onchange="try{localStorage.removeItem('sidekick_nopaper')}catch(e){};this.form.submit()"),
                            Button("Choose a PDF…", type="button", cls="src-file-label",
                                   onclick="document.getElementById('pdfPick').click()"),
                            Span("or a web page / blog", cls="ins-col-head"),
                            # type="text" (not "url") so the browser doesn't silently
                            # refuse a scheme-less paste like "arxiv.org/abs/1706.03762";
                            # the server adds https:// (paperlib.normalize_url).
                            Input(name="url", type="text", inputmode="url",
                                  placeholder="arxiv.org/abs/…  or  https://…", cls="src-url"),
                            Button("Open URL", cls="cell-btn run", type="submit"),
                            # or a file already on disk — opened in place by its path
                            # (no re-upload). Confined to home / the papers cache /
                            # $SIDEKICK_PAPER_DIR by _paper_path_allowed.
                            Span("or a file on this machine", cls="ins-col-head"),
                            Input(name="path", type="text",
                                  placeholder="~/papers/attention.pdf", cls="src-url",
                                  title="Open a PDF already on disk by its path (under your "
                                        "home dir, the papers cache, or $SIDEKICK_PAPER_DIR)"),
                            Button("Open file", cls="cell-btn run", type="submit"),
                            method="post", action="/paper/open", enctype="multipart/form-data",
                            cls="src-form",
                            onsubmit="try{localStorage.removeItem('sidekick_nopaper')}catch(e){}"),
                        cls="export"),
                    Details(Summary("⬇", cls="gear", title="Export this dialog"),
                            Div(A("Jupyter notebook (.ipynb)", href="/export/ipynb"),
                                A("Markdown (.md)", href="/export/md"),
                                A("Python package (.zip)", href="/export/package"),
                                A("Publish to blog", href="/publish/blog", target="_blank",
                                  title="Build this dialog into your Quarto blog and open it"),
                                A("Recall / Quiz me", href="/recall",
                                  title="Generate retrieval-practice questions from this dialog"),
                                cls="export-menu"),
                            cls="export"),
                    Button("▶▶", cls="gear", type="button",
                           title="Run all code cells in this dialog, top to bottom",
                           hx_post="/cell/run-all", hx_target="#stream", hx_swap="outerHTML"),
                    Button("⟳", cls="gear", type="button",
                           title="Restart the kernel — clear all variables for this dialog",
                           hx_post="/kernel/restart", hx_target="#stream", hx_swap="outerHTML",
                           **{"hx-confirm": "Restart the kernel? This clears all variables "
                                            "defined in this dialog."}),
                    A("📦 Libraries", href="/libraries", cls="gear",
                      title="Build Python packages from tagged cells",
                      style="font-size:13px;font-weight:500;white-space:nowrap"),
                    A("⚙", href="/settings", cls="gear", title="Settings — API keys"),
                    TargetSwitcher(),
                    cls="topbar-right"),
                cls="topbar"),
            # the columns row
            Div(
                Sidebar(),
                Div(cls="gutter gutter-side", data_resize="side"),
                PaperPanel(),
                Div(cls="gutter gutter-paper", data_resize="paper"),
                Div(banner, Stream(), Composer(), cls="main"),
                Div(cls="gutter gutter-toc", data_resize="toc"),
                Div(Div("Contents", cls="toc-head"), Div(id="tocList", cls="toc-list"),
                    cls="toc", id="toc"),
                cls="cols"),
            cls="app" + (" paper-open" if STATE.get("paper") else ""),
        ), Script(src="/static/js/toc.js"), Script(src="/static/js/msglink.js")),
    )


# ---- export (.ipynb / .md) --------------------------------------------------
# The pure notebook/markdown serializers live in sidekick.export_nb (no app
# state). Re-exported here so existing references (incl. tests using app.to_ipynb
# / app.to_markdown) keep resolving.
from .export_nb import to_ipynb, to_markdown  # noqa: E402,F401


# ---- routes -----------------------------------------------------------------
# All front-end assets are vendored under sidekick/static/vendor and served from
# /vendor, so the app is fully offline — no CDN. This REPLACES FastHTML's default
# CDN headers (default_hdrs=False): local htmx + fasthtml.js, then CodeMirror,
# KaTeX, and Sortable.
_VENDOR_DIR = Path(__file__).parent / "static" / "vendor"
_LOCAL_HDRS = (
    Meta(charset="utf-8"),
    Meta(name="viewport", content="width=device-width, initial-scale=1, viewport-fit=cover"),
    Script(src="/vendor/htmx.min.js"),
    Script(src="/vendor/fasthtml.js"),
    Script(src="/vendor/surreal.js"),
    Script(src="/vendor/css-scope.js"),
    Link(rel="stylesheet", href="/vendor/codemirror.min.css"),
    Link(rel="stylesheet", href="/vendor/monokai.min.css"),
    Link(rel="stylesheet", href="/vendor/show-hint.min.css"),
    Script(src="/vendor/codemirror.min.js"),
    Script(src="/vendor/python.min.js"),
    Script(src="/vendor/placeholder.min.js"),
    Script(src="/vendor/show-hint.min.js"),     # Ctrl+Space completion dropdown
    Script(src="/static/js/comment.js"),                           # defines window.__toggleComment (Cmd/Ctrl+/)
    Script(src="/static/js/complete.js"),                          # defines window.__kernelHint
    Script(src="/static/js/stream_sel.js"),                        # Ask-AI bubble over dialog-stream selections
    Link(rel="stylesheet", href="/vendor/katex.min.css"),
    Script(src="/vendor/katex.min.js"),
    Script(src="/vendor/auto-render.min.js"),
    # mermaid.min.js (~3.2MB) is NOT eager-loaded — renderMermaid lazy-loads it on
    # first use via ensureMermaid(), so pages without a diagram never fetch it.
    Script(src="/vendor/sortable.min.js"),
)
app, rt = fast_app(pico=False, default_hdrs=False, hdrs=_LOCAL_HDRS)
# FastHTML registers a generic "/{fname:path}.{ext:static}" route that serves
# from cwd and would shadow /vendor (404ing our assets). Drop it — we serve our
# own static files from /vendor below.
app.routes[:] = [r for r in app.routes if getattr(r, "path", "") != "/{fname:path}.{ext:static}"]


# ---- localhost-only defense (peer address + Host/Origin) -------------------
# The app executes code on the box it runs on and has no auth layer, so every
# request must originate from loopback. The PRIMARY gate is the real TCP peer
# address (scope["client"]), which the client cannot forge — so even if the app
# is bound to 0.0.0.0 by a raw `uvicorn sidekick.app:app --host 0.0.0.0` (the CLI
# refuses this, but the ASGI app can be run directly), a remote box is rejected.
# The Host/Sec-Fetch/Origin checks below remain as defense-in-depth against a
# malicious local web page driving the POST routes cross-origin; they are NOT the
# authentication (the Host header is client-controlled and must never be trusted
# for that — the reason this gate exists).
_ALLOWED_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "testserver"})
# Real peer addresses that count as loopback. "testclient" is what Starlette's
# in-process TestClient reports; no network peer can present it (the OS sets the
# source address), so allowlisting it is safe and keeps the test suite working.
_LOOPBACK_PEERS = frozenset({"127.0.0.1", "::1", "testclient"})


def _host_only(raw: str) -> str:
    """The host part of a Host header, port stripped (handles IPv6 brackets)."""
    raw = (raw or "").strip()
    if raw.startswith("["):                      # [::1] or [::1]:8000
        return raw[1:raw.index("]")] if "]" in raw else raw
    if raw.count(":") == 1:                       # host:port
        return raw.rsplit(":", 1)[0]
    return raw                                     # bare host, or bare IPv6 (no port)


class _LocalGuard:
    """Pure-ASGI middleware (kept pure so it never buffers the /stream SSE
    response the way BaseHTTPMiddleware can)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        # PRIMARY auth: the unforgeable TCP peer address. Fail closed if unknown.
        client = scope.get("client")
        peer = client[0] if client else None
        if peer not in _LOOPBACK_PEERS:
            await self._forbid(scope, receive, send, "non-loopback peer")
            return
        headers = {k.decode("latin-1").lower(): v.decode("latin-1")
                   for k, v in scope.get("headers", [])}
        host_hdr = headers.get("host", "")
        if _host_only(host_hdr) not in _ALLOWED_HOSTS:
            await self._forbid(scope, receive, send, "host not allowed")
            return
        # Sec-Fetch-Site is set by browsers and cannot be spoofed from JS, so it
        # catches cross-site requests the Origin check misses (absent Origin on a
        # POST, and state-changing GET routes like /new or /publish/blog). Non-
        # browser clients omit it entirely → treated as trusted (on a loopback
        # bind that means local processes, which already have full access).
        sfs = headers.get("sec-fetch-site")
        if sfs and sfs not in ("same-origin", "none"):
            await self._forbid(scope, receive, send, f"cross-site request ({sfs})")
            return
        if scope["method"] == "POST":
            origin = headers.get("origin")
            if origin:
                from urllib.parse import urlparse
                if urlparse(origin).netloc != host_hdr:   # cross-origin POST
                    await self._forbid(scope, receive, send, "cross-origin POST")
                    return
        await self.app(scope, receive, send)

    async def _forbid(self, scope, receive, send, why):
        from starlette.responses import PlainTextResponse
        _dbg(f"blocked request: {why}")
        await PlainTextResponse(f"forbidden: {why}", status_code=403)(scope, receive, send)


class _SessionScope:
    """Pure-ASGI middleware that gives every browser session its own per-tab UI
    state. Reads the `sk_sid` cookie (minting one on the response when absent),
    and binds it to the _CUR_SID ContextVar for the duration of the request so
    cur/set_cur/pop_cur — used deep inside the render helpers — resolve to THIS
    session's overlay without every handler having to thread a session through.

    The sid is an opaque random key into the in-process SESSIONS dict; it carries
    no authority (the app is already loopback- and same-origin-guarded), it only
    selects which tab's dialog/transients you see. Non-browser / cookie-less
    callers (the MCP subprocess, tests calling handlers directly) never get a sid
    → cur/set_cur fall back to the shared global STATE, exactly as before."""

    _COOKIE = "sk_sid"

    def __init__(self, app):
        self.app = app

    def _read_sid(self, scope) -> str | None:
        for k, v in scope.get("headers", []):
            if k == b"cookie":
                for part in v.decode("latin-1").split(";"):
                    name, _, val = part.strip().partition("=")
                    if name == self._COOKIE and val:
                        return val
        return None

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        sid = self._read_sid(scope)
        minted = sid is None
        if minted:
            sid = secrets.token_urlsafe(16)
        token = _CUR_SID.set(sid)

        async def _send(message):
            if minted and message["type"] == "http.response.start":
                message = dict(message)
                headers = list(message.get("headers", []))
                cookie = (f"{self._COOKIE}={sid}; Path=/; HttpOnly; SameSite=Lax")
                headers.append((b"set-cookie", cookie.encode("latin-1")))
                message["headers"] = headers
            await send(message)

        try:
            await self.app(scope, receive, _send)
        finally:
            _CUR_SID.reset(token)


app.add_middleware(_LocalGuard)
app.add_middleware(_SessionScope)


@app.on_event("startup")
def _resolve_target_on_startup():
    """Resolve the configured target (config + network probe) when the server
    actually starts, so import stays side-effect-free (S8)."""
    _ensure_target()


@rt("/vendor/{fname:path}")
def vendor(fname: str):
    """Serve a vendored front-end asset (with a path-traversal guard)."""
    from starlette.responses import FileResponse, PlainTextResponse
    p = (_VENDOR_DIR / fname).resolve()
    if p.is_relative_to(_VENDOR_DIR.resolve()) and p.is_file():
        return FileResponse(p)
    return PlainTextResponse("not found", status_code=404)


@rt("/static/{fname:path}")
def static(fname: str):
    """Serve a bundled JS/CSS asset from sidekick/static (path-traversal guarded)."""
    from starlette.responses import FileResponse, PlainTextResponse
    p = (_STATIC_DIR / fname).resolve()
    if p.is_relative_to(_STATIC_DIR.resolve()) and p.is_file():
        return FileResponse(p)
    return PlainTextResponse("not found", status_code=404)


@rt("/")
def index():
    return Page()


@rt("/switch", methods=["post"])
def switch(target: str):
    use_target(target)
    return Page()


@rt("/settings", methods=["get"])
def settings_get():
    return SettingsPage()


@rt("/settings", methods=["post"])
def settings_post(ANTHROPIC_API_KEY: str = "", ZHIPU_API_KEY: str = "",
                  OPENAI_API_KEY: str = "", default_model: str = ""):
    incoming = {
        "ANTHROPIC_API_KEY": ANTHROPIC_API_KEY,
        "ZHIPU_API_KEY": ZHIPU_API_KEY,
        "OPENAI_API_KEY": OPENAI_API_KEY,
    }
    for env, val in incoming.items():
        val = (val or "").strip()
        if val:                              # blank = keep current (don't clear)
            secrets_store.save(env, val)
    if default_model:
        STATE["model"] = default_model
    use_target(STATE["target_name"])         # reconnect so new key takes effect
    return SettingsPage(saved=True)


@rt("/libraries", methods=["get"])
def libraries_get():
    return LibrariesPage()


@rt("/library/add", methods=["post"])
def library_add(name: str = "", pkg: str = "", path: str = ""):
    try:
        libraries.add(name, pkg or None, path or None)
    except ValueError as e:
        return LibrariesPage(result={"error": str(e)})
    return LibrariesPage(saved=True)


@rt("/library/remove", methods=["post"])
def library_remove(name: str = ""):
    libraries.remove(name)
    return LibrariesPage()


@rt("/library/build", methods=["post"])
def library_build(name: str = ""):
    lib = libraries.get(name)
    if lib is None:
        return LibrariesPage(result={"error": f"no library named '{name}'"})
    res = nbdev_export.build_library(STATE["backend"], name, lib["path"], lib["pkg"])
    return LibrariesPage(result={"name": name, "path": lib["path"], **res})


@rt("/library/use", methods=["post"])
def library_use(name: str = ""):
    """Make a built library importable in the kernel (put its dir on sys.path)."""
    lib = libraries.get(name)
    if lib is None:
        return LibrariesPage(result={"error": f"no library named '{name}'"})
    fn = getattr(STATE["backend"], "add_syspath", None)
    if fn is None:
        return LibrariesPage(result={"error": "this target has no kernel to import into "
                                              "(switch to the kernel backend)"})
    ok = fn(lib["path"])
    return LibrariesPage(result={"use": name, "pkg": lib["pkg"], "ok": ok})


@rt("/cell/export-to", methods=["post"])
def cell_export_to(id: str, lib: str = "", module: str = "core"):
    """Tag a code cell into `<lib>:<module>` (the per-cell library picker)."""
    backend = STATE["backend"]
    m = _msg_by_id(backend, cur("dialog"), id)
    if m is not None and m.msg_type == "code" and lib.strip():
        target = f"{lib.strip()}:{module.strip() or 'core'}"
        new = export.set_export_target(m.content, target)
        if isinstance(backend, _InMemoryBackend):
            m.content = new
        elif hasattr(backend, "update"):
            backend.update(cur("dialog"), id, new)
    return Stream()


@rt("/rename", methods=["post"])
def rename_dialog(old: str, new: str):
    new = (new or "").strip()
    backend = STATE["backend"]
    if new and new != old and hasattr(backend, "rename"):
        try:
            backend.rename(old, new)
            try:
                from . import codex_cli
                codex_cli.rename(old, new)
            except Exception:  # noqa: BLE001 — session carry is best-effort
                _dbg(f"codex rename({old!r}, {new!r}) failed")
            _set_dialog(new)
        except ValueError as e:
            STATE["warning"] = str(e)          # surfaced as the banner
    return Page()


@rt("/open")
def open_dialog(dialog: str):
    _set_dialog(dialog)
    return Page()


@rt("/new")
def new_dialog():
    n = 1
    existing = set(STATE["backend"].list_dialogs())
    while f"untitled/dialog-{n}" in existing:
        n += 1
    _set_dialog(f"untitled/dialog-{n}")
    STATE["backend"].messages(cur("dialog"))  # touch -> create
    return Page()


def _delete_dialogs(targets: list[str]):
    """Delete each dialog in `targets`; if the active one is among them, fall back
    to a survivor (or a fresh demo/welcome if none remain)."""
    backend = STATE["backend"]
    if hasattr(backend, "delete_dialog"):
        for d in targets:
            backend.delete_dialog(d)
    for d in targets:                              # evict any lingering CLI session/cost
        try:
            from . import claude_cli
            claude_cli.drop(d)
        except Exception:  # noqa: BLE001 — eviction is best-effort cleanup
            _dbg(f"drop({d!r}) failed during delete")
        try:
            from . import codex_cli
            codex_cli.drop(d)
        except Exception:  # noqa: BLE001 — eviction is best-effort cleanup
            _dbg(f"codex drop({d!r}) failed during delete")
    if cur("dialog") in targets:
        remaining = backend.list_dialogs()
        _set_dialog(remaining[0] if remaining else "demo/welcome")
        backend.messages(cur("dialog"))       # touch -> ensure it exists


@rt("/dialog/delete", methods=["post"])
def dialog_delete(dialog: str):
    """Delete one dialog (the row's ⋯ menu)."""
    _delete_dialogs([dialog])
    return Page()


@rt("/dialog/private", methods=["post"])
def dialog_private(dialog: str):
    """Toggle whether a dialog stays local-only (out of the committed/synced store).
    Moves its cells between dialogs-<key>.json and the gitignored .local.json."""
    backend = STATE["backend"]
    if hasattr(backend, "set_private"):
        backend.set_private(dialog)
    return Page()


@rt("/dialog/duplicate", methods=["post"])
def dialog_duplicate(dialog: str):
    """Copy an entire dialog's cells into a fresh '<name> copy' dialog, then open
    it. Reuses the per-cell copy so each clone gets its own id/rich list."""
    backend = STATE["backend"]
    if not hasattr(backend, "copy_cell"):
        return Page()
    existing = set(backend.list_dialogs())
    new = f"{dialog} copy"
    while new in existing:                       # avoid clobbering an earlier copy
        new += " copy"
    for m in list(backend.messages(dialog)):     # snapshot: copy preserves order
        backend.copy_cell(dialog, m.id, new)
    backend.messages(new)                        # touch -> exists even if source was empty
    set_cur("dialog", new)
    return Page()


@rt("/dialog/delete-bulk", methods=["post"])
def dialog_delete_bulk(names: str = "[]"):
    """Delete several dialogs at once (shift/⌘-click multi-select). `names` is a
    JSON array of dialog names."""
    try:
        targets = [d for d in json.loads(names) if isinstance(d, str)]
    except (ValueError, TypeError):
        targets = []
    _delete_dialogs(targets)
    return Page()


@rt("/send", methods=["post"])
def send(content: str, msg_type: str = "prompt", model: str = None,
         ai_mode: str = None, htmx=None):
    content = (content or "").strip()
    if model:
        STATE["model"] = model           # remember last-used model
    if ai_mode:
        STATE["ai_mode"] = ai_mode       # remember last-used AI mode
    if msg_type:
        STATE["msg_type"] = msg_type     # remember last-used compose mode
    if content:
        backend = STATE["backend"]
        use_model = STATE["model"] if msg_type == "prompt" else None
        use_mode = STATE["ai_mode"] if msg_type == "prompt" else None
        m = backend.add(cur("dialog"), content, msg_type, model=use_model, ai_mode=use_mode)
        set_cur("scroll_to", m.id)           # render scrolls to the new cell
        if msg_type == "prompt" and _can_stream(backend, use_model):
            # Defer the AI call: the page renders an SSE-wired answer that streams
            # tokens in (the browser opens /stream), instead of blocking here.
            _set_pending_stream(cur("dialog"), m.id)
        elif msg_type in ("code", "prompt"):
            backend.exec(cur("dialog"), m.id)
    # The composer posts via htmx → swap just #stream (no full-page reload, so the
    # answer starts streaming sooner). A no-JS submit gets the whole page.
    return Stream() if (htmx and htmx.request) else Page()


def _msg_by_id(backend, dialog: str, mid: str):
    for m in backend.messages(dialog):
        if m.id == mid:
            return m
    return None


def _resolve_injections(backend, dialog: str, content: str) -> str:
    """Fill $`expr` in a prompt with live kernel values before it reaches the AI.

    Only the kernel backend can evaluate (the namespace lives in its server); a
    real solveit target injects on its own side, and the mock has no namespace —
    both leave the text untouched. Best-effort by design: never block a prompt.
    """
    if not content or "$`" not in content:
        return content
    fn = getattr(backend, "eval_exprs", None)
    if fn is None:
        return content
    resolved, _warnings = fn(dialog, content)
    return resolved


def _cell_number(backend, dialog: str, mid: str):
    """The cell's 1-based number (its position in the dialog) — so a single-row
    htmx swap shows the same badge as a full render."""
    for i, m in enumerate(backend.messages(dialog), 1):
        if m.id == mid:
            return i
    return None


@rt("/cell/run", methods=["post"])
def cell_run(id: str, content: str = ""):
    """Save a cell's edited source, then (re)execute it. Returns just the stream."""
    backend = STATE["backend"]
    if hasattr(backend, "update"):
        backend.update(cur("dialog"), id, content)
    m = _msg_by_id(backend, cur("dialog"), id)
    if m is not None and m.msg_type == "prompt" and _can_stream(backend, m.model):
        _set_pending_stream(cur("dialog"), id)          # re-ask, streamed live
        # The #stream swap resets the scroll container to the top, so bring the
        # re-run cell (and its "Thinking…" spinner) back into view — otherwise a
        # mid-notebook re-ask looks frozen because the spinner is below the fold.
        set_cur("scroll_to", id)
    elif m is not None and m.msg_type == "code" and _exec_streams(backend):
        _start_streamed_exec(backend, cur("dialog"), id)
    elif m is not None and m.msg_type in ("code", "prompt"):
        backend.exec(cur("dialog"), id)
    return Stream()


@rt("/stream")
def stream_answer(dialog: str, id: str):
    """Server-Sent-Events: stream a prompt's AI answer token-by-token.

    The browser's EventSource (see STREAM_JS) connects here for a cell whose
    answer is pending. We build the notebook context up to that cell, stream the
    deltas from the selected local CLI, and emit cumulative rendered markdown as `msg`
    events — finishing with a `done` event so the client closes the connection.
    """
    backend = STATE["backend"]
    m = _msg_by_id(backend, dialog, id)
    context = build_context(backend.messages(dialog), upto_id=id) if m else ""
    # Resolve $`expr` injections against the live kernel, fresh at send time. The
    # cell's stored source keeps the raw $`…`; only what the AI receives is filled
    # in. A backend with no kernel (mock) leaves them literal.
    content = _resolve_injections(backend, dialog, m.content) if m else ""

    def gen():
        if m is None:
            yield sse_message(Div(""), event="done")
            return
        _reset_cells_dirty()                             # the AI's tools may flip this
        acc = ""
        streamer = stream_codex if m.model in CODEX_CLI_MODELS else stream_claude
        for delta in streamer(dialog, content, context, model=m.model, mode=m.ai_mode):
            acc += delta
            # str() unwraps NotStr -> raw (already-safe) markdown HTML for the data lines
            yield sse_message(str(render_md(acc)), event="msg")
        edited = _consume_cells_dirty()                  # atomic read+clear vs the MCP thread
        if not acc and edited:                           # tool-only turn: say something
            acc = "_Updated the notebook cells as requested._"
        m.output = acc or m.output
        if hasattr(backend, "_save"):
            backend._save()                              # flush to disk so a reload keeps the answer
        _clear_pending_stream(dialog, id)
        yield sse_message(str(render_md(m.output)), event="msg")   # final state
        # Refresh the foot-of-stream meter with this turn's accrued cost/usage
        # without a full reload (the client swaps just #ctxMeter's text).
        yield sse_message(_ctx_meter_text(backend.messages(dialog), dialog), event="cost")
        # 'reload' tells the client to refresh #stream so the AI's cell edits show.
        yield sse_message(Div("reload" if edited else ""), event="done")

    return StreamingResponse(gen(), media_type="text/event-stream")


@rt("/exec_stream")
def exec_stream(dialog: str, id: str, run_id: str):
    """Server-Sent-Events: stream a code cell's execution output as it runs.

    The browser's EventSource (see STREAM_JS) connects here for a code cell whose
    run is pending. We poll the kernel's exec_poll ~4x/sec, emit the accumulated
    stdout/stderr as `msg` events, and — when the run finishes — persist the final
    output + rich outputs on the cell and emit a `done` event carrying 'reload' so
    the client refreshes #stream (rendering plots and the normal Run button)."""
    import time
    backend = STATE["backend"]
    m = _msg_by_id(backend, dialog, id)

    def gen():
        if m is None or not hasattr(backend, "exec_poll"):
            _clear_pending_run(dialog, id)
            yield sse_message(Div("reload"), event="done")
            return
        last = None
        last_beat = time.monotonic()
        while True:
            snap = backend.exec_poll(dialog, run_id)
            out = snap.get("output", "")
            if out != last:                              # push only when it changed
                yield sse_message(Pre(out, cls="out"), event="msg")
                last = out
                last_beat = time.monotonic()
            if snap.get("done"):
                m.output = out
                m.rich = snap.get("rich", []) or []
                if hasattr(backend, "_save"):
                    backend._save()                      # flush to disk
                _clear_pending_run(dialog, id)
                # 'reload' → the client refreshes #stream so the finished cell
                # (rich plots, restored Run button) renders in its resting state.
                yield sse_message(Div("reload"), event="done")
                return
            # Heartbeat: a cell that hangs producing NO output (e.g. `while True:
            # pass`) otherwise never yields, so Starlette can't notice the client
            # disconnected and this generator leaks a threadpool worker forever.
            # A periodic SSE comment forces a send, which raises on a dead client
            # and tears the stream down. EventSource ignores comment lines.
            elif time.monotonic() - last_beat > 15:
                yield ": keepalive\n\n"
                last_beat = time.monotonic()
            time.sleep(0.25)                             # ~4 polls/sec

    return StreamingResponse(gen(), media_type="text/event-stream")


@rt("/cell/stop", methods=["post"])
def cell_stop(id: str):
    """Interrupt a running code cell: raise KeyboardInterrupt in the kernel worker.
    The cell's live EventSource then sees the run finish and reloads #stream."""
    backend = STATE["backend"]
    run_id = _pending_run_id(cur("dialog"), id)
    if run_id and hasattr(backend, "exec_stop"):
        backend.exec_stop(cur("dialog"), run_id)
    return Div("", id=f"stop-{id}")                      # hx-swap="none": nothing to render


@rt("/kernel/restart", methods=["post"])
def kernel_restart():
    """Restart the kernel for the current dialog: drop its server-side namespace via
    the kernel's /reset endpoint, so the next run starts from a clean slate."""
    backend = STATE["backend"]
    dialog = cur("dialog")
    if hasattr(backend, "reset"):
        backend.reset(dialog)
    set_cur("flash", "Kernel restarted — variables cleared.")
    return Stream()


@rt("/cell/run-all", methods=["post"])
def cell_run_all():
    """Run every code cell in the current dialog, top to bottom, sequentially —
    reusing the blocking run path (a clean re-execution of the whole notebook)."""
    backend = STATE["backend"]
    dialog = cur("dialog")
    for m in list(backend.messages(dialog)):
        if m.msg_type == "code":
            backend.exec(dialog, m.id)
    return Stream()


# ---- internal API for the AI's cell-editing MCP tools (server/mcp_cells) -----
# Loopback + shared-token only. The MCP server (spawned by `claude -p`) calls
# these to read and edit the live notebook; mutating routes flag #stream dirty so
# the streamed answer's `done` event reloads it.
def _mcp_ok(tok: str) -> bool:
    want = STATE.get("mcp_token")
    # constant-time compare — these routes rewrite arbitrary notebook cells, so
    # don't leak the token length/prefix through `==` timing.
    return bool(want) and hmac.compare_digest(tok or "", want)


def _loopback_req(request) -> bool:
    """True when the request comes from loopback. The /internal/* routes mutate the
    live notebook; the shared token is a second factor, not the only one, so they
    must stay unreachable off-box even when the UI is bound to 0.0.0.0
    (SIDEKICK_HOST). If we can't determine the client (no request), fail closed."""
    client = getattr(request, "client", None) if request is not None else None
    host = getattr(client, "host", "") if client is not None else None
    return host in ("127.0.0.1", "::1", "localhost", "")


def _mcp_guard(request, tok: str):
    """Shared gate for /internal/* routes: loopback origin AND valid token. Returns
    a 403 response to short-circuit, or None when the request may proceed."""
    if not _loopback_req(request) or not _mcp_ok(tok):
        return _json({"ok": False, "error": "forbidden"}, 403)
    return None


def _json(obj, status: int = 200):
    from starlette.responses import JSONResponse
    return JSONResponse(obj, status_code=status)


@rt("/internal/cells")
def internal_cells(dialog: str, tok: str = "", request=None):
    """List a dialog's cells for the AI (id, type, source)."""
    if (deny := _mcp_guard(request, tok)) is not None:
        return deny
    backend = STATE["backend"]
    cells = [{"id": m.id, "type": m.msg_type, "content": m.content or "",
              "output": m.output or ""} for m in backend.messages(dialog)]
    return _json({"ok": True, "cells": cells})


@rt("/internal/cell/update", methods=["post"])
def internal_cell_update(dialog: str, id: str, content: str = "", tok: str = "", request=None):
    if (deny := _mcp_guard(request, tok)) is not None:
        return deny
    backend = STATE["backend"]
    if not hasattr(backend, "update") or _msg_by_id(backend, dialog, id) is None:
        return _json({"ok": False, "error": f"no cell {id}"}, 404)
    backend.update(dialog, id, content)
    _mark_cells_dirty()
    return _json({"ok": True, "message": f"updated cell {id}"})


@rt("/internal/cell/str_replace", methods=["post"])
def internal_cell_str_replace(dialog: str, id: str, old: str = "", new: str = "",
                              tok: str = "", request=None):
    if (deny := _mcp_guard(request, tok)) is not None:
        return deny
    backend = STATE["backend"]
    m = _msg_by_id(backend, dialog, id)
    if m is None or not hasattr(backend, "update"):
        return _json({"ok": False, "error": f"no cell {id}"}, 404)
    src = m.content or ""
    n = src.count(old)
    if n == 0:
        return _json({"ok": False, "error": "old_str not found"}, 400)
    if n > 1:
        return _json({"ok": False, "error": f"old_str matches {n}× (must be unique)"}, 400)
    backend.update(dialog, id, src.replace(old, new))
    _mark_cells_dirty()
    return _json({"ok": True, "message": f"edited cell {id}"})


@rt("/internal/cell/insert", methods=["post"])
def internal_cell_insert(dialog: str, content: str = "", cell_type: str = "code",
                         after_id: str = "", tok: str = "", request=None):
    if (deny := _mcp_guard(request, tok)) is not None:
        return deny
    if cell_type not in ("code", "note", "prompt"):
        return _json({"ok": False, "error": "bad cell_type"}, 400)
    backend = STATE["backend"]
    if after_id and hasattr(backend, "insert") and _msg_by_id(backend, dialog, after_id):
        m = backend.insert(dialog, content, cell_type, after_id, above=False)
    else:
        m = backend.add(dialog, content, cell_type)
    _mark_cells_dirty()
    return _json({"ok": True, "message": f"inserted cell {m.id}"})


@rt("/complete", methods=["post"])
def complete(code: str = "", line: int = 1, col: int = 0):
    """Code-completion proxy: forward to the kernel's live namespace (Ctrl+Space in
    a code cell). Only the kernel backend introspects a namespace; other backends
    return nothing. Best-effort — never raises into the editor."""
    backend = STATE["backend"]
    comps = []
    if hasattr(backend, "complete"):
        try:
            comps = backend.complete(cur("dialog"), code, int(line), int(col))
        except Exception as e:  # noqa: BLE001
            _dbg(f"kernel completion failed: {e}")
            comps = []
    return _json({"completions": comps})


@rt("/stream/refresh")
def stream_refresh():
    """Re-render #stream — the client swaps this in after a turn whose AI tools
    edited cells, so the edits become visible without a full page reload."""
    return Stream()


@rt("/cell/save", methods=["post"])
def cell_save(id: str, content: str = ""):
    """Save a cell's edited source without executing (used by note cells)."""
    backend = STATE["backend"]
    if hasattr(backend, "update"):
        backend.update(cur("dialog"), id, content)
    return Stream()


@rt("/cell/answer/edit")
def cell_answer_edit(id: str):
    """Swap a prompt's AI answer into edit mode (textarea over its output)."""
    bk, d = STATE["backend"], cur("dialog")
    m = _msg_by_id(bk, d, id)
    if m is None or m.msg_type != "prompt" or not _can_edit_answer():
        return Stream()
    return _answer_edit(m)


@rt("/cell/answer/view")
def cell_answer_view(id: str):
    """Swap a prompt's AI answer back to its rendered view — used by Cancel."""
    bk, d = STATE["backend"], cur("dialog")
    m = _msg_by_id(bk, d, id)
    return _answer_view(m) if m is not None and m.msg_type == "prompt" else Stream()


@rt("/cell/answer/save", methods=["post"])
def cell_answer_save(id: str, output: str = ""):
    """Persist an edited AI answer in place (no re-ask), then render it read-only.
    Only prompt cells have an editable answer, so other types are left untouched."""
    bk, d = STATE["backend"], cur("dialog")
    m = _msg_by_id(bk, d, id)
    if m is None or m.msg_type != "prompt":
        return Stream()
    if hasattr(bk, "update_output"):
        bk.update_output(d, id, output)
    return _answer_view(m)


@rt("/cell/delete", methods=["post"])
def cell_delete(id: str):
    backend = STATE["backend"]
    if hasattr(backend, "delete"):
        backend.delete(cur("dialog"), id)
    return Stream()


@rt("/cell/type", methods=["post"])
def cell_type(id: str, msg_type: str = "code"):
    """Convert a cell to another type in place (y=code, m=note, i=prompt), then
    open it in edit mode right there. Without this, a keyboard convert (`Esc i` to
    turn a just-inserted note into an Ask-AI cell) left the cell read-only, dropped
    focus out of the stream, and drifted the view down to the composer. Mirror
    `/cell/insert`: land the cursor in the converted cell, in view, inline."""
    backend = STATE["backend"]
    if hasattr(backend, "set_type"):
        m = backend.set_type(cur("dialog"), id, msg_type)
        if m is not None:
            set_cur("editing", m.id)
            set_cur("focus_start", m.id)     # cursor at the start; 'nearest' scroll (no jump)
            set_cur("scroll_to", m.id)
    return Stream()


@rt("/cell/undo", methods=["post"])
def cell_undo():
    """Restore the last deleted cell in this dialog (Jupyter's 'z')."""
    backend = STATE["backend"]
    if hasattr(backend, "undo"):
        backend.undo(cur("dialog"))
    return Stream()


@rt("/cell/mute", methods=["post"])
def cell_mute(id: str):
    """Toggle whether this cell is included in the AI's notebook context."""
    backend = STATE["backend"]
    if hasattr(backend, "set_muted"):
        backend.set_muted(cur("dialog"), id)
    return Stream()


@rt("/cell/pin", methods=["post"])
def cell_pin(id: str):
    """Toggle whether this cell is pinned into context (survives trimming)."""
    backend = STATE["backend"]
    if hasattr(backend, "set_pinned"):
        backend.set_pinned(cur("dialog"), id)
    return Stream()


@rt("/cell/export", methods=["post"])
def cell_export(id: str):
    """Toggle this code cell's `#| export` directive (whether it's tangled into the package)."""
    backend = STATE["backend"]
    m = _msg_by_id(backend, cur("dialog"), id)
    if m is not None and m.msg_type == "code":
        new = export.toggle_export(m.content)
        if isinstance(backend, _InMemoryBackend):
            m.content = new                  # a directive is a no-op comment — keep the cell's output
        elif hasattr(backend, "update"):
            backend.update(cur("dialog"), id, new)
    return Stream()


@rt("/export/package")
def export_package():
    """Tangle the current dialog's `#| export` cells into a downloadable package zip."""
    dialog = cur("dialog")
    msgs = STATE["backend"].messages(dialog)
    pkg = export.slug(dialog)
    files = export.dialog_to_package(msgs, dialog, dialog_name=dialog)
    blob = export.package_zip(files, pkg)
    return _download(blob, f"{pkg}.zip", "application/zip")


@rt("/cell/edit")
def cell_edit(id: str):
    """Swap a single cell into edit mode (raw textarea)."""
    bk, d = STATE["backend"], cur("dialog")
    m = _msg_by_id(bk, d, id)
    return _cell_edit(m, num=_cell_number(bk, d, id)) if m else Stream()


@rt("/cell/view")
def cell_view(id: str):
    """Swap a single cell back to its rendered (read-only) view — used by Cancel."""
    bk, d = STATE["backend"], cur("dialog")
    m = _msg_by_id(bk, d, id)
    return MsgRow(m, num=_cell_number(bk, d, id)) if m else Stream()


@rt("/cell/exec", methods=["post"])
def cell_exec(id: str):
    """Re-run a cell's stored source without editing (the rendered-view Run/Ask)."""
    backend = STATE["backend"]
    m = _msg_by_id(backend, cur("dialog"), id)
    if m is not None and m.msg_type == "prompt" and _can_stream(backend, m.model):
        m.output = ""                                     # clear stale answer to re-stream
        _set_pending_stream(cur("dialog"), id)          # re-ask, streamed live
        # The #stream swap resets the scroll container to the top, so a mid-notebook
        # re-run looks frozen — the "Thinking…" spinner is below the fold. Bring the
        # re-run cell back into view (matches /cell/run).
        set_cur("scroll_to", id)
    elif m is not None and m.msg_type == "code" and _exec_streams(backend):
        _start_streamed_exec(backend, cur("dialog"), id)
    elif m is not None:
        backend.exec(cur("dialog"), id)
    return Stream()


@rt("/cell/insert", methods=["post"])
def cell_insert(id: str, msg_type: str = "code", where: str = "below"):
    """Insert a new (empty) cell above/below `id` and open it in edit mode."""
    backend = STATE["backend"]
    if msg_type not in ("code", "note", "prompt"):
        msg_type = "code"
    if hasattr(backend, "insert"):
        m = backend.insert(cur("dialog"), "", msg_type, anchor_id=id,
                           above=(where == "above"))
        set_cur("editing", m.id)
        set_cur("focus_start", m.id)
        set_cur("scroll_to", m.id)
    return Stream()


@rt("/cell/copy", methods=["post"])
def cell_copy(id: str, target: str = ""):
    """Copy a cell into another dialog (appended at its end). We stay in the current
    dialog — the stream re-renders unchanged but for a one-shot flash confirming where
    the copy landed."""
    backend = STATE["backend"]
    if target and target != cur("dialog") and hasattr(backend, "copy_cell"):
        m = backend.copy_cell(cur("dialog"), id, target)
        set_cur("flash", f"Copied cell to “{target}”" if m else "Couldn't copy that cell.")
    return Stream()


@rt("/cell/split", methods=["post"])
def cell_split(id: str):
    """Split-to-code (SolveIt's `W`): take the fenced code blocks from a prompt's
    answer and insert them as runnable code cells just below it, in order. The
    cells aren't auto-run — the user still runs each one (small-steps contract)."""
    backend = STATE["backend"]
    m = _msg_by_id(backend, cur("dialog"), id)
    if m is not None and m.msg_type == "prompt" and hasattr(backend, "insert"):
        anchor, last = id, None
        for lang, code in _answer_code_blocks(m.output):
            # mermaid isn't kernel code — land it in a note, which renders the fence
            # as a diagram; everything else becomes a runnable code cell.
            if lang == "mermaid":
                last = backend.insert(cur("dialog"), f"```mermaid\n{code}\n```",
                                      "note", anchor_id=anchor)
            else:
                last = backend.insert(cur("dialog"), code, "code", anchor_id=anchor)
            anchor = last.id
        if last is not None:
            set_cur("scroll_to", last.id)
    return Stream()


# ── Faded scaffolding (F6) ───────────────────────────────────────────────────
# A "faded exercise" is just an ordinary CODE cell (no new cell type) whose first
# line is this marker comment — that both tells the learner what to do and lets
# us detect the cell so re-fading updates it in place instead of stacking copies.
_EXERCISE_HEADER = "# ✏️ Exercise — fill in the ___ blanks (faded from the worked cell above)"


def _exercise_content(faded: str) -> str:
    """Wrap faded code as an exercise cell (marker header + the faded body)."""
    return f"{_EXERCISE_HEADER}\n{faded}"


def _is_exercise(content: str) -> bool:
    return (content or "").lstrip().startswith(_EXERCISE_HEADER)


def _strip_exercise_marker(content: str) -> str:
    """The learner's attempt without the marker header line."""
    lines = (content or "").splitlines()
    if lines and lines[0].lstrip().startswith(_EXERCISE_HEADER):
        lines = lines[1:]
    return "\n".join(lines)


def _fade_source(msgs, idx: int):
    """The worked code cell an exercise at position `idx` was faded from: the
    nearest preceding code cell that is not itself an exercise."""
    for j in range(idx - 1, -1, -1):
        s = msgs[j]
        if s.msg_type == "code" and not _is_exercise(s.content):
            return s
    return None


@rt("/cell/fade", methods=["post"])
def cell_fade(id: str, level: int = 1):
    """Fade a worked code cell into a graded exercise — a cell TRANSFORM in the
    same spirit as "Split to code" (`/cell/split`).

    From a worked (non-exercise) code cell it inserts ONE derived exercise cell
    below at `level` (reusing the same `backend.insert` split uses); if an
    exercise already sits directly below, it re-fades that in place. Called on an
    existing exercise cell it re-fades in place from its worked source above —
    so `level` 0/1/2 = "show worked answer" / "fill-in" / "from scratch"."""
    backend = STATE["backend"]
    if not hasattr(backend, "insert"):
        return Stream()
    msgs = backend.messages(cur("dialog"))
    idx = next((i for i, m in enumerate(msgs) if m.id == id), None)
    if idx is None or msgs[idx].msg_type != "code":
        return Stream()
    m = msgs[idx]

    if _is_exercise(m.content):
        src = _fade_source(msgs, idx)
        if src is None:
            return Stream()
        content = _exercise_content(scaffold.fade_code(src.content, level))
        if hasattr(backend, "update"):
            backend.update(cur("dialog"), m.id, content)
        set_cur("scroll_to", m.id)
        return Stream()

    content = _exercise_content(scaffold.fade_code(m.content, level))
    nxt = msgs[idx + 1] if idx + 1 < len(msgs) else None
    if nxt is not None and nxt.msg_type == "code" and _is_exercise(nxt.content) \
            and hasattr(backend, "update"):
        backend.update(cur("dialog"), nxt.id, content)
        set_cur("scroll_to", nxt.id)
    else:
        new = backend.insert(cur("dialog"), content, "code", anchor_id=id)
        set_cur("scroll_to", new.id)
    return Stream()


@rt("/cell/check", methods=["post"])
def cell_check(id: str):
    """AI-check step for a faded exercise. This does NOT add any AI wiring: it
    inserts an ordinary prompt cell pre-filled with `scaffold.check_prompt(...)`
    below the exercise, then drops the learner into it so they hit the existing
    "Ask" button — the answer streams through the normal `/cell/run` prompt path
    with full dialog context."""
    backend = STATE["backend"]
    if not hasattr(backend, "insert"):
        return Stream()
    msgs = backend.messages(cur("dialog"))
    idx = next((i for i, m in enumerate(msgs) if m.id == id), None)
    if idx is None:
        return Stream()
    src = _fade_source(msgs, idx)
    original = src.content if src is not None else ""
    attempt = _strip_exercise_marker(msgs[idx].content)
    prompt = scaffold.check_prompt(original, attempt)
    m = backend.insert(cur("dialog"), prompt, "prompt", anchor_id=id)
    set_cur("editing", m.id)         # let the learner review, then click Ask
    set_cur("scroll_to", m.id)
    return Stream()


@rt("/cell/move", methods=["post"])
def cell_move(ids: str = ""):
    """Persist a new cell order after a drag. SortableJS has already reordered the
    DOM, so we just save the order — no re-render needed (returns empty)."""
    backend = STATE["backend"]
    order = [i for i in ids.split(",") if i]
    if order and hasattr(backend, "reorder"):
        backend.reorder(cur("dialog"), order)
    return ""


def _download(body: str, fname: str, media: str):
    from starlette.responses import Response
    return Response(body, media_type=media,
                    headers={"Content-Disposition": f'attachment; filename="{fname}"'})


@rt("/export/ipynb")
def export_ipynb():
    msgs = STATE["backend"].messages(cur("dialog"))
    fname = cur("dialog").replace("/", "-") + ".ipynb"
    return _download(json.dumps(to_ipynb(msgs), indent=1), fname, "application/x-ipynb+json")


@rt("/export/md")
def export_md():
    msgs = STATE["backend"].messages(cur("dialog"))
    fname = cur("dialog").replace("/", "-") + ".md"
    return _download(to_markdown(msgs), fname, "text/markdown; charset=utf-8")


@rt("/dialog/export/ipynb")
def dialog_export_ipynb(dialog: str):
    """Export any named dialog to a notebook without opening it first (the sidebar
    ⋯ menu). Same serializer as /export/ipynb, but keyed by `dialog`."""
    msgs = STATE["backend"].messages(dialog)
    fname = dialog.replace("/", "-") + ".ipynb"
    return _download(json.dumps(to_ipynb(msgs), indent=1), fname, "application/x-ipynb+json")


@rt("/publish/blog")
def publish_blog():
    """Publish the current dialog as a post into your Quarto blog, then open it.

    A dialog is already a literate document (prose + code + outputs), so a blog is
    a per-dialog *publish* — the sibling of the ⬇ file exports, not a cross-dialog
    collection like a library. It builds in place into a persistent blog project
    (`blog.default_blog_dir()`), which accumulates posts across dialogs, and we
    redirect to the freshly-rendered post. If quarto isn't installed the render
    soft-fails and we flash where the source project was written instead."""
    from datetime import date
    from starlette.responses import RedirectResponse
    from . import blog
    dialog = cur("dialog")
    result = blog.publish_dialog(STATE["backend"], dialog, blog.default_blog_dir(),
                                 title="Sidekick Blog", date=date.today().isoformat())
    if not result["render_ok"]:
        set_cur("flash", f"Post written to {blog.default_blog_dir()} — {result['render_detail']}")
        return RedirectResponse("/", status_code=303)
    return RedirectResponse(f"/blog/posts/{result['slug']}.html", status_code=303)


@rt("/recall")
def recall_quiz(n: int = 5):
    """Quiz the current dialog: ask the AI for `n` recall questions from its cells
    and drop them in as unanswered prompt cells for retrieval practice.

    A dialog is a literate record of what you worked through; recall is the
    projection that hides it and asks you to reconstruct it (the testing effect).
    Each question lands as a `prompt` cell so you can answer from memory and then
    Ask AI to check. Smallest working path: reuse `recall.build_quiz_prompt` +
    `parse_questions` (pure) around the existing `call` AI seam, then `backend.add`
    the cells the same way `/send` does. Returns to the dialog with the quiz
    appended."""
    from starlette.responses import RedirectResponse
    from . import recall
    backend = STATE["backend"]
    dialog = cur("dialog")
    prompt = recall.build_quiz_prompt(backend.messages(dialog), n=n)
    # A distinct session key ("recall:<dialog>") so quizzing never disturbs the
    # dialog's own resumable CLI session or its running cost tally.
    ai_text = call_claude(f"recall:{dialog}", prompt, context="", model=STATE["model"])
    questions = recall.parse_questions(ai_text)
    intro = backend.add(dialog, "## Recall quiz\n\nTry to answer each from memory, "
                                "then Ask AI to check.", "note")
    for q in questions:
        backend.add(dialog, q, "prompt")
    set_cur("scroll_to", intro.id)           # bring the fresh quiz into view
    return RedirectResponse("/", status_code=303)


@rt("/blog/{path:path}")
def blog_site(path: str):
    """Serve the rendered blog (its `_site/`), with a path-traversal guard —
    mirrors the /vendor asset route. Bare `/blog/` serves the listing index."""
    from starlette.responses import FileResponse, PlainTextResponse
    from . import blog
    site = Path(blog.default_blog_dir()) / "_site"
    p = (site / (path or "index.html")).resolve()
    if p.is_dir():
        p = p / "index.html"
    if p.resolve().is_relative_to(site.resolve()) and p.is_file():
        return FileResponse(p)
    return PlainTextResponse("not found", status_code=404)


def _convert_paper_async(path: str, name: str | None = None, dialog: str | None = None):
    """Convert a PDF in a background thread (marker can take a while), updating
    STATE['paper'] from 'converting' to 'ready'/'error'. The panel polls.
    `dialog` re-links a reopened paper to its dialog so imports continue there."""
    name = name or os.path.basename(path)
    link = {"dialog": dialog} if dialog else {}
    with _STATE_LOCK:
        STATE["paper"] = {"name": name, "status": "converting", "source": path, **link}

    def work():
        try:
            md, engine = paperlib.convert(path)
            new = {"name": name, "status": "ready", "md": md, "engine": engine,
                   "source": path, **link}
        except Exception as e:  # noqa: BLE001 — surface conversion failures in the panel
            _dbg(f"paper convert failed for {path!r}: {e}")
            new = {"name": name, "status": "ready", "md": f"Could not open: {e}",
                   "engine": "error"}
        with _STATE_LOCK:                        # daemon thread → take the lock
            STATE["paper"] = new

    threading.Thread(target=work, daemon=True).start()


def _url_name(url: str) -> str:
    """A readable panel/dialog name for a URL source: its last path segment, else
    host. Only strips known doc extensions so an arXiv id like '2305.18247' stays
    intact (os.path.splitext would chop it at the dot)."""
    from urllib.parse import urlparse
    u = urlparse(url)
    seg = [s for s in u.path.split("/") if s]
    base = seg[-1] if seg else u.netloc
    for ext in (".pdf", ".html", ".htm"):
        if base.lower().endswith(ext):
            base = base[:-len(ext)]
            break
    return base or u.netloc or "page"


def _convert_url_async(url: str, name: str, dialog: str | None = None):
    """Fetch + convert a web page to markdown in a background thread (same panel
    lifecycle as a PDF: 'converting' → 'ready'/'error', polled by the panel).
    `dialog` re-links a reopened paper to its dialog so imports continue there."""
    link = {"dialog": dialog} if dialog else {}
    with _STATE_LOCK:
        STATE["paper"] = {"name": name, "status": "converting", "source": url, **link}

    def work():
        try:
            md, engine = paperlib.convert_url(url)
            if md.strip():
                new = {"name": name, "status": "ready", "md": md,
                       "engine": engine, "source": url, **link}
            else:
                new = {"name": name, "status": "ready", "engine": "error",
                       "md": f"**Couldn't extract anything from** `{url}`"}
        except Exception as e:  # noqa: BLE001 — surface fetch/extract failures in the panel
            _dbg(f"url convert failed for {url!r}: {e}")
            new = {"name": name, "status": "ready", "engine": "error",
                   "md": f"Could not open `{url}`:\n\n```\n{e}\n```"}
        with _STATE_LOCK:                        # daemon thread → take the lock
            STATE["paper"] = new

    threading.Thread(target=work, daemon=True).start()


def _save_upload(pdf) -> tuple[str, str] | None:
    """Save an uploaded PDF under the papers cache (keyed by content hash so the
    same file re-uses its conversion). Returns (path, display_name) or None."""
    if pdf is None or not getattr(pdf, "filename", ""):
        return None
    data = pdf.file.read()
    if not data:
        return None
    import hashlib
    updir = paperlib._cache_dir() / "uploads"
    updir.mkdir(parents=True, exist_ok=True)
    dst = updir / (hashlib.sha1(data).hexdigest()[:16] + ".pdf")
    if not dst.exists():                         # identical content already saved →
        dst.write_bytes(data)                    # keep it (and its mtime) so the markdown
    return str(dst), pdf.filename                # cache, keyed by path+mtime, still hits


def _paper_path_allowed(src: str) -> bool:
    """Whether a server-side `path` may be opened. The UI opens papers by upload or
    URL; this `path` entry is for callers/tests, and an unrestricted local-file read
    is a disclosure primitive (esp. if the loopback gate is ever bypassed). Confine
    it to the user's home, the papers cache, or SIDEKICK_PAPER_DIR — resolved, so
    `..`/symlinks can't escape."""
    from pathlib import Path
    bases = [Path.home()]
    extra = os.environ.get("SIDEKICK_PAPER_DIR")
    if extra:
        bases.append(Path(extra).expanduser())
    try:
        bases.append(paperlib._cache_dir())
    except Exception:  # noqa: BLE001 — cache dir is best-effort
        pass
    rp = Path(src).resolve()
    for b in bases:
        try:
            if rp == b.resolve() or rp.is_relative_to(b.resolve()):
                return True
        except (OSError, ValueError):
            continue
    return False


# ---- dialog → paper source ---------------------------------------------------
# Remembered when a paper's cells are imported into a dialog, persisted next to
# the conversion cache, so closing the panel (or restarting) isn't one-way: a
# paper/* dialog can reopen its source later via /paper/reopen.

def _paper_sources_file() -> Path:
    return paperlib._cache_dir() / "sources.json"


def _load_paper_sources() -> dict:
    try:
        return json.loads(_paper_sources_file().read_text())
    except Exception:  # noqa: BLE001 — missing/corrupt file → simply no reopen links
        return {}


def _record_paper_source(dialog: str, p: dict) -> None:
    src = (p or {}).get("source")
    if not (dialog and src):
        return
    try:
        f = _paper_sources_file()
        f.parent.mkdir(parents=True, exist_ok=True)
        srcs = _load_paper_sources()
        srcs[dialog] = {"source": src, "name": p.get("name", "")}
        f.write_text(json.dumps(srcs, indent=1))
    except OSError as e:
        _dbg(f"could not record paper source for {dialog!r}: {e}")


def _paper_source_for(dialog: str) -> dict | None:
    return _load_paper_sources().get(dialog)


@rt("/paper/open", methods=["post"])
def paper_open(pdf: UploadFile = None, path: str = "", url: str = ""):
    up = _save_upload(pdf)                       # an uploaded file takes precedence
    if up:
        _convert_paper_async(*up)
        return Page()
    if url.strip():                              # a web page / blog URL
        u = paperlib.normalize_url(url)          # add https:// to a bare host so
        _convert_url_async(u, _url_name(u))      # `arxiv.org/abs/…` just works
        return Page()
    if path.strip():                             # a local file path (kept for callers/tests)
        src = os.path.expanduser(path.strip())
        if not _paper_path_allowed(src):
            with _STATE_LOCK:
                STATE["paper"] = {"name": os.path.basename(src) or src, "status": "ready",
                                  "engine": "error",
                                  "md": f"**Refused:** `{src}` is outside the allowed "
                                        f"directory (home, the papers cache, or "
                                        f"$SIDEKICK_PAPER_DIR)."}
        elif not os.path.exists(src):
            with _STATE_LOCK:
                STATE["paper"] = {"name": os.path.basename(src) or src, "status": "ready",
                                  "engine": "error", "md": f"**File not found:** `{src}`"}
        else:
            _convert_paper_async(src, os.path.basename(src))
    return Page()


@rt("/paper/status")
def paper_status():
    return PaperPanel()


@rt("/paper/asset/{key}/{name}")
def paper_asset(key: str, name: str):
    """Serve a converted paper's extracted figure (see paperlib._store_assets),
    with the same resolve-and-contain traversal guard as /blog and /vendor."""
    from starlette.responses import FileResponse, PlainTextResponse
    base = (paperlib._cache_dir() / "assets").resolve()
    p = (base / key / name).resolve()
    if p.is_relative_to(base) and p.is_file():
        return FileResponse(p)
    return PlainTextResponse("not found", status_code=404)


@rt("/paper/close")
def paper_close():
    STATE["paper"] = None
    return Page()


@rt("/paper/reopen")
def paper_reopen():
    """Reopen the current dialog's source paper in the reading panel. The source
    was remembered when the paper's cells were imported (see _record_paper_source),
    so ✕ or a restart isn't one-way. Conversion re-runs but hits the markdown
    cache, so this is fast."""
    rec = _paper_source_for(cur("dialog")) or {}
    src = rec.get("source", "")
    if src.startswith(("http://", "https://")):
        _convert_url_async(src, rec.get("name") or _url_name(src), dialog=cur("dialog"))
    elif src:
        path = os.path.expanduser(src)
        if _paper_path_allowed(path) and os.path.exists(path):
            _convert_paper_async(path, rec.get("name") or None, dialog=cur("dialog"))
    return Page()


def _safe_name(s: str) -> str:
    s = "".join(c if (c.isalnum() or c in "-_") else "-" for c in s).strip("-").lower()
    return s or "paper"


def _strip_doc_ext(name: str) -> str:
    """Drop a trailing document extension from a paper name for the dialog slug —
    but only a *real* extension, so an arXiv id like '1512.03385' keeps its full id
    (os.path.splitext would chop it at the dot → 'paper/1512')."""
    for ext in (".pdf", ".html", ".htm", ".md", ".txt"):
        if name.lower().endswith(ext):
            return name[:-len(ext)]
    return name


def _paper_title(p: dict) -> str:
    """The dialog-name base for an imported paper: the paper's *title* (its first
    markdown heading — marker/trafilatura put it first), so an arXiv upload becomes
    `paper/attention-is-all-you-need`, not `paper/1706-03762`. Falls back to the
    file/URL name when the conversion has no headings."""
    for line in (p.get("md") or "").splitlines():
        m = re.match(r"#{1,6}\s+(.+)", line.strip())
        if m:
            title = re.sub(r"[*_`]", "", m.group(1)).strip()
            if title:
                return title[:60]
    return _strip_doc_ext(p.get("name", "paper"))


def _unique_dialog(backend, base: str) -> str:
    existing = set(backend.list_dialogs())
    if base not in existing:
        return base
    n = 2
    while f"{base}-{n}" in existing:
        n += 1
    return f"{base}-{n}"


def _paper_dialog(backend, p: dict) -> str:
    """The dialog that the open paper's imported cells go into — a `paper/<name>`
    dialog, created on first use and remembered on the paper state."""
    dialog = p.get("dialog")
    if not dialog:
        dialog = _unique_dialog(backend, f"paper/{_safe_name(_paper_title(p))}")
        backend.messages(dialog)
        p["dialog"] = dialog
        _record_paper_source(dialog, p)
    return dialog


def _append_passage(p: dict, text: str):
    """Add a passage to the paper's dialog as a note + an empty code cell to
    reimplement it, then open that code cell focused. Shared by the section
    stepper and the highlight-to-import flow."""
    backend = STATE["backend"]
    dialog = _paper_dialog(backend, p)
    backend.add(dialog, text, "note")            # the passage to read…
    code = backend.add(dialog, "", "code")       # …and a cell to reimplement it
    _set_dialog(dialog)
    set_cur("editing", code.id)                  # open the code cell, focused & in view


@rt("/paper/step", methods=["post"])
def paper_step():
    """Progressive paper reading (Jeremy Howard's piece-by-piece method): pull the
    *next* section of the open paper into the notebook as a note, plus an empty
    code cell to reimplement it yourself. Advances a cursor so each click brings
    the next piece — small steps, instead of dumping the whole paper at once."""
    p = STATE.get("paper")
    if not p or p.get("status") != "ready" or not (p.get("md") or "").strip():
        return Page()
    sections = _paper_sections(p)                # cached; same split the panel counts
    i = p.get("step", 0)
    if i >= len(sections):
        return Page()                            # nothing left to bring in
    _append_passage(p, sections[i])              # note + code cell, opened focused
    p["step"] = i + 1
    return Page()


@rt("/paper/import-selection", methods=["post"])
def paper_import_selection(text: str = ""):
    """Import a passage the user highlighted in the paper into the notebook as a
    note + a code cell to reimplement it — cherry-pick the parts that matter,
    instead of stepping through the whole paper."""
    text = (text or "").strip()
    p = STATE.get("paper")
    if text and p and p.get("status") == "ready":
        _append_passage(p, text)
    return Page()


@rt("/paper/import", methods=["post"])
def paper_import(mode: str = "para"):
    """Import the open paper into a new dialog as note cells, then switch to it.
    mode='para' → one cell per paragraph/block; mode='section' → one cell per
    heading-and-its-content. Headings become the dialog's table of contents."""
    p = STATE.get("paper")
    if not p or p.get("status") != "ready" or not (p.get("md") or "").strip():
        return Page()
    chunks = (paperlib.split_sections if mode == "section" else paperlib.split_blocks)(p["md"])
    backend = STATE["backend"]
    name = _unique_dialog(backend, f"paper/{_safe_name(_paper_title(p))}")
    backend.messages(name)                       # create the dialog
    for c in chunks:
        backend.add(name, c, "note")
    _set_dialog(name)
    p.setdefault("dialog", name)                 # panel stays open — ✕ closes it, and
    _record_paper_source(name, p)                # further highlight imports join this dialog
    return Page()


if __name__ == "__main__":
    serve()
