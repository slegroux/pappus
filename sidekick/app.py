"""SolveIt Sidekick — a clean, Claude-desktop-style interface for SolveIt.

Run:
    pip install python-fasthtml pyyaml solveit_client
    python -m sidekick.app          # then open http://localhost:8000

The whole point: the target switcher in the top-right flips between your laptop
('local') and the H100 ('h100'). Everything else stays identical.
"""
from __future__ import annotations

import json
import os
import threading
from urllib.parse import quote

from fasthtml.common import *

from .targets import get_target, list_targets, list_models, default_model
from .client import connect, build_context, est_tokens, _InMemoryBackend
from .claude_cli import stream as stream_claude, CLI_MODELS
from . import secrets_store
from . import paper as paperlib


# ---- code highlighting (server-side; works offline, no CDN) -----------------
try:
    from pygments import highlight as _pyg_highlight
    from pygments.lexers import get_lexer_by_name, PythonLexer
    from pygments.formatters import HtmlFormatter
    from pygments.util import ClassNotFound
    # noclasses=True inlines the token colors, so no separate stylesheet is needed.
    _pyg_fmt = HtmlFormatter(noclasses=True, style="monokai")

    def _highlight(src: str, lang: str = "") -> str:
        """Highlight `src` with Pygments (monokai). No language → Python (this is
        a Python notebook); an unknown language → plain text."""
        try:
            lexer = get_lexer_by_name(lang) if lang else PythonLexer()
        except ClassNotFound:
            lexer = get_lexer_by_name("text")
        return _pyg_highlight(src or "", lexer, _pyg_fmt)

    def render_code(src: str):
        return NotStr(_highlight(src, "python"))
except Exception:  # noqa: BLE001 — degrade to a plain code block if pygments is missing
    def _highlight(src: str, lang: str = "") -> str | None:
        return None

    def render_code(src: str):
        return Pre(src or "", cls="code")


# ---- markdown (server-side; works offline, no CDN) --------------------------
try:
    import mistune

    class _MdRenderer(mistune.HTMLRenderer):
        """Markdown HTML renderer that syntax-highlights fenced code blocks
        (```python …```) via Pygments — same palette as the code cells."""
        def block_code(self, code, info=None):
            lang = (info or "").strip().split(None, 1)[0] if (info or "").strip() else ""
            html = _highlight(code, lang)
            return html if html else super().block_code(code, info)

    # escape=True neutralises raw HTML in the source, so rendering a note or an
    # AI answer can't inject <script> — markdown syntax still renders.
    # 'math' extracts $…$ / $$…$$ before markdown can mangle underscores etc.,
    # emitting \(…\) (inline) and $$…$$ (block) for KaTeX to render client-side.
    _md = mistune.create_markdown(renderer=_MdRenderer(escape=True),
                                  plugins=["strikethrough", "table", "math"])
except Exception:  # noqa: BLE001 — degrade to plain text if mistune is missing
    _md = None


def render_md(text: str):
    """Render markdown to safe HTML, or fall back to escaped plain text."""
    text = text or ""
    return NotStr(_md(text)) if _md else text


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
    "msg_type": "prompt",
    "pending_stream": None,   # (dialog, msg_id) whose answer is being streamed live
    "paper": None,            # {name, status, md, engine} for the reading panel
}


def _can_stream(backend, model) -> bool:
    """Stream a prompt's answer only for the subscription CLI model on an in-process
    backend (mock/kernel) — its answer doesn't need the remote SolveIt server."""
    return model in CLI_MODELS and isinstance(backend, _InMemoryBackend)


def use_target(name: str):
    t = get_target(name)
    backend, warning = connect(t)
    STATE.update(target_name=name, backend=backend, warning=warning)
    if not backend.list_dialogs():
        pass
    if STATE["dialog"] not in (backend.list_dialogs() or [STATE["dialog"]]):
        STATE["dialog"] = backend.list_dialogs()[0] if backend.list_dialogs() else "demo/welcome"


use_target(_initial_target())

# ---- styling: Claude desktop look ------------------------------------------
CSS = """
:root{
  --bg:#F0EEE6; --panel:#FAF9F5; --sidebar:#F0EEE6; --ink:#2B2A27; --muted:#73706A;
  --line:#E4E1D8; --accent:#D97757; --accent-ink:#fff; --code-bg:#2B2A27; --code-ink:#EFE9DD;
  --bubble-user:#F5E9E2; --chip:#EDEAE1;
}
*{box-sizing:border-box} html,body{margin:0;height:100%}
body{font-family:'Styrene B','Segoe UI',system-ui,-apple-system,sans-serif;
  background:var(--bg);color:var(--ink);font-size:15px;line-height:1.55}
.app{display:grid;grid-template-columns:264px 1fr;height:100vh}
.app.toc-open{grid-template-columns:264px 1fr 244px}
.app.paper-open{grid-template-columns:264px minmax(300px,36%) 1fr}
.app.paper-open.toc-open{grid-template-columns:264px minmax(280px,32%) 1fr 244px}
/* paper reading panel (left column, toggled open when a paper is loaded) */
.paper{display:none;background:var(--panel);border-right:1px solid var(--line);overflow:auto;height:100vh;padding:16px 18px}
.app.paper-open .paper{display:flex;flex-direction:column}
.paper-head{display:flex;align-items:center;justify-content:space-between;margin-bottom:4px}
.paper-name{font-weight:600;font-size:13px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.paper-converting{margin-top:14px;color:var(--muted);font-size:13px;font-style:italic}
.paper-body{margin-top:10px;font-size:13.5px;line-height:1.6}
.paper-body img{max-width:100%}
.ask-sel-btn{position:absolute;z-index:60;background:var(--accent);color:#fff;border:none;border-radius:8px;
  padding:5px 11px;font-size:12px;cursor:pointer;box-shadow:0 4px 12px rgba(0,0,0,.20);display:none}
.paper-form{display:flex;flex-direction:column;gap:6px;position:absolute;right:0;top:28px;z-index:20;
  background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:10px;min-width:240px;
  box-shadow:0 6px 18px rgba(0,0,0,.10)}
.paper-path{border:1px solid var(--line);border-radius:8px;padding:7px 10px;font:inherit;font-size:13px;outline:none}
.paper-path:focus{border-color:var(--accent)}
/* table of contents (right column, toggleable, full-height so it stays in view) */
.toc{display:none;background:var(--sidebar);border-left:1px solid var(--line);
  padding:16px 14px;overflow:auto;height:100vh}
.app.toc-open .toc{display:flex;flex-direction:column}
.toc-head{font-size:11px;letter-spacing:.04em;text-transform:uppercase;color:var(--muted);margin-bottom:10px}
.toc-list{display:flex;flex-direction:column;gap:1px}
.toc-link{display:block;text-decoration:none;color:var(--ink);font-size:13px;padding:4px 8px;border-radius:7px;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis;cursor:pointer}
.toc-link:hover{background:#E8E5DB}
.toc-h1{font-weight:600}
.toc-h2{padding-left:18px}
.toc-h3{padding-left:30px;color:var(--muted)}
.toc-h4,.toc-h5,.toc-h6{padding-left:42px;color:var(--muted);font-size:12px}
.toc-empty{font-size:12px;color:var(--muted);font-style:italic;padding:4px 8px}
.toc-toggle{cursor:pointer}
/* sidebar */
.side{background:var(--sidebar);border-right:1px solid var(--line);display:flex;flex-direction:column;padding:14px 12px}
.brand{display:flex;align-items:center;gap:9px;font-weight:600;padding:6px 8px 14px}
.brand .dot{width:22px;height:22px;border-radius:6px;background:var(--accent);display:grid;place-items:center;color:#fff;font-size:13px}
.newbtn{display:flex;align-items:center;gap:8px;width:100%;border:1px solid var(--line);background:var(--panel);
  color:var(--ink);border-radius:10px;padding:9px 12px;cursor:pointer;font-size:14px;margin-bottom:12px}
.newbtn:hover{border-color:#d4d0c4}
.seclabel{font-size:11px;letter-spacing:.04em;text-transform:uppercase;color:var(--muted);padding:8px 8px 4px}
.conv{display:block;padding:8px 10px;border-radius:8px;color:var(--ink);text-decoration:none;font-size:14px;cursor:pointer}
.conv:hover{background:#E8E5DB} .conv.active{background:#E3DFD3;font-weight:500}
.side-foot{margin-top:auto;font-size:12px;color:var(--muted);padding:8px}
/* main */
.main{display:flex;flex-direction:column;min-width:0;min-height:0}  /* min-height:0 lets .stream scroll, not .main */
.topbar{display:flex;align-items:center;justify-content:space-between;padding:12px 22px;border-bottom:1px solid var(--line)}
.title{font-weight:600}
.title-form{margin:0}
.title-edit{font-weight:600;font-size:15px;font-family:inherit;color:var(--ink);
  border:1px solid transparent;background:transparent;border-radius:7px;padding:4px 8px;
  min-width:240px;outline:none}
.title-edit:hover{border-color:var(--line)}
.title-edit:focus{border-color:var(--accent);background:var(--panel)}
.gear{text-decoration:none;font-size:18px;color:var(--muted);line-height:1}
.gear:hover{color:var(--ink)}
/* export dropdown */
.export{position:relative}
.export>summary{list-style:none;cursor:pointer}
.export>summary::-webkit-details-marker{display:none}
.export-menu{position:absolute;right:0;top:28px;z-index:20;display:flex;flex-direction:column;gap:2px;
  background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:6px;min-width:188px;
  box-shadow:0 6px 18px rgba(0,0,0,.10)}
.export-menu a{text-decoration:none;color:var(--ink);font-size:13px;padding:6px 10px;border-radius:7px;white-space:nowrap}
.export-menu a:hover{background:#E8E5DB}
/* settings */
.settings-page{min-height:100vh;background:var(--bg);overflow:auto}
.settings-wrap{max-width:640px;margin:0 auto;padding:28px 22px 60px}
.settings-top{display:flex;align-items:center;gap:14px;margin-bottom:6px}
.back{text-decoration:none;color:var(--muted);font-size:14px}
.back:hover{color:var(--ink)}
.settings-intro{color:var(--muted);font-size:13px;margin:8px 0 20px;line-height:1.5}
.saved{background:#E6F2EA;border:1px solid #BFE0CC;color:#2C6B45;padding:8px 12px;border-radius:8px;margin-bottom:14px;font-size:14px}
.prov-card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:14px 16px;margin-bottom:14px}
.prov-head{margin-bottom:4px}
.prov-status{font-size:13px;margin-bottom:9px}
.prov-status.ok{color:#2C6B45}.prov-status.off{color:var(--muted)}
.keyinput{width:100%;border:1px solid var(--line);border-radius:8px;padding:9px 11px;font:inherit;background:#fff;color:var(--ink);outline:none}
.keyinput:focus{border-color:var(--accent)}
.muted{color:var(--muted)}.small{font-size:12px;margin-top:6px}
.pill-default{background:var(--accent);color:#fff;font-size:11px;border-radius:999px;padding:2px 8px;margin-left:8px}
.save-btn{background:var(--accent);border:none;color:#fff;border-radius:10px;padding:10px 22px;font-size:14px;cursor:pointer;margin-top:4px}
.save-btn:hover{filter:brightness(1.05)}
.target{display:flex;align-items:center;gap:8px}
.target form{display:inline}
.pill{display:inline-flex;align-items:center;gap:7px;background:var(--chip);border:1px solid var(--line);
  border-radius:999px;padding:6px 12px;font-size:13px;cursor:pointer}
.led{width:8px;height:8px;border-radius:50%}
.led.live{background:#3FA66A;box-shadow:0 0 0 3px rgba(63,166,106,.18)}
.led.mock{background:#C9A227;box-shadow:0 0 0 3px rgba(201,162,39,.18)}
select.tsel{appearance:none;background:var(--chip);border:1px solid var(--line);border-radius:999px;
  padding:6px 30px 6px 12px;font-size:13px;cursor:pointer;color:var(--ink)}
.banner{background:#FBF3E7;border-bottom:1px solid #EAD9BE;color:#7A5B1E;padding:8px 22px;font-size:13px}
/* conversation */
.stream{flex:1;overflow:auto;padding:26px 0}
.wrap{max-width:760px;margin:0 auto;padding:0 22px}
.row{margin-bottom:22px}
.who{font-size:12px;color:var(--muted);margin-bottom:6px;display:flex;align-items:center;gap:7px}
.tag{font-size:11px;border:1px solid var(--line);border-radius:6px;padding:1px 6px;color:var(--muted)}
.bubble{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:13px 15px}
.bubble.user{background:var(--bubble-user);border-color:#EBD9CD}
.bubble.note{background:transparent;border:none;padding:2px 0}
pre.code{background:var(--code-bg);color:var(--code-ink);border-radius:10px;padding:13px 15px;overflow:auto;
  font-family:'SF Mono',ui-monospace,Menlo,monospace;font-size:13px;margin:0}
.out{margin-top:9px;border-left:2px solid var(--line);padding:4px 0 4px 12px;color:var(--muted);
  font-family:ui-monospace,Menlo,monospace;font-size:13px;white-space:pre-wrap}
.empty{color:var(--muted);text-align:center;margin-top:60px}
/* composer */
.composer{padding:0 0 22px} .composer .wrap{padding:0 22px}
.box{background:var(--panel);border:1px solid var(--line);border-radius:16px;padding:10px 12px;
  box-shadow:0 1px 2px rgba(0,0,0,.03)}
.box textarea{width:100%;border:none;outline:none;resize:none;background:transparent;font:inherit;color:var(--ink);min-height:46px}
.row2{display:flex;align-items:center;justify-content:space-between;margin-top:6px}
.modes{display:flex;gap:6px}
.mode{font-size:12px;border:1px solid var(--line);background:#fff;border-radius:8px;padding:5px 10px;cursor:pointer;color:var(--muted)}
.mode input{display:none}
.mode.sel{background:var(--accent);border-color:var(--accent);color:#fff}
select.msel{appearance:none;background:#fff;border:1px solid var(--line);border-radius:8px;padding:5px 24px 5px 10px;
  font-size:12px;cursor:pointer;color:var(--ink)}
.send{background:var(--accent);border:none;color:#fff;border-radius:10px;width:34px;height:34px;cursor:pointer;font-size:15px}
.send:hover{filter:brightness(1.05)}
.hint{font-size:11px;color:var(--muted);margin-top:8px;text-align:center}
/* editable cells */
.cell-edit{width:100%;border:1px solid var(--line);border-radius:11px;padding:11px 13px;font:inherit;
  background:var(--panel);color:var(--ink);resize:none;outline:none;overflow:hidden;min-height:42px;display:block;
  field-sizing:content}     /* auto-grows to fit content (Chrome/Edge/Safari); JS fallback below */
.cell-edit:focus{border-color:var(--accent)}
.cell-edit.code-edit{font-family:'SF Mono',ui-monospace,Menlo,monospace;font-size:13px;
  background:var(--code-bg);color:var(--code-ink);border-color:#3a3933}
.cell-actions{display:flex;gap:6px;margin-left:auto;opacity:0;transition:opacity .12s}
.row:hover .cell-actions,.cell-actions:focus-within,.cell-actions.show{opacity:1}
/* rendered cells are click-to-edit; hint it on hover */
.clickedit{cursor:text;border-radius:9px;transition:outline-color .12s}
.clickedit:hover{outline:1px dashed var(--line);outline-offset:4px}
.note-view{padding:1px 0}.prompt-view{white-space:pre-wrap}
.note-view .muted,.prompt-view .muted,.code-view .muted{font-style:italic}
/* pygments code block (server-side highlight, inline colors) */
.code-view .highlight{margin:0;border-radius:11px;overflow:auto}
.code-view .highlight pre{margin:0;padding:11px 14px;border-radius:11px;
  font-family:'SF Mono',ui-monospace,Menlo,monospace;font-size:13px;line-height:1.5}
/* CodeMirror: highlight-while-editing for code cells (matches the rendered look) */
.CodeMirror{height:auto;border:1px solid #3a3933;border-radius:11px;
  font-family:'SF Mono',ui-monospace,Menlo,monospace;font-size:13px;line-height:1.5}
.CodeMirror-scroll{min-height:auto}
.CodeMirror-lines{padding:9px 0}
/* Re-map CodeMirror's monokai onto Pygments' exact monokai palette, so a code
   cell looks IDENTICAL rendered (Pygments) vs being edited (CodeMirror). CM's
   own theme uses `span.cm-*` selectors, so we match that specificity to win. */
.cm-s-monokai.CodeMirror{color:#f8f8f2}
.cm-s-monokai span.cm-keyword{color:#66d9ef}
.cm-s-monokai span.cm-operator{color:#ff4689}
.cm-s-monokai span.cm-def{color:#a6e22e}
.cm-s-monokai span.cm-variable,.cm-s-monokai span.cm-variable-2,
.cm-s-monokai span.cm-property{color:#f8f8f2}
.cm-s-monokai span.cm-builtin,.cm-s-monokai span.cm-variable-3,
.cm-s-monokai span.cm-type{color:#f8f8f2}
.cm-s-monokai span.cm-string,.cm-s-monokai span.cm-string-2{color:#e6db74}
.cm-s-monokai span.cm-number,.cm-s-monokai span.cm-atom{color:#ae81ff}
.cm-s-monokai span.cm-comment{color:#959077}
.cm-s-monokai span.cm-meta,.cm-s-monokai span.cm-qualifier{color:#a6e22e}
.cell-btn{font-size:12px;border:1px solid var(--line);background:#fff;border-radius:7px;padding:3px 11px;
  cursor:pointer;color:var(--muted);line-height:1.6}
.cell-btn:hover{border-color:#d4d0c4;color:var(--ink)}
.cell-btn.run{background:var(--accent);color:#fff;border-color:var(--accent)}
.cell-btn.run:hover{filter:brightness(1.05);color:#fff}
.cell-btn.del:hover{border-color:#C0584B;color:#C0584B}
.cell-btn.ctx.off{color:#B0784F;border-color:#E3C7AE;background:#FBF3E7}
.cell-btn.pin.on{color:#2C6B45;border-color:#BFE0CC;background:#E6F2EA}
.tok{margin-left:2px;font-variant-numeric:tabular-nums}
/* drag-to-reorder handle (hover-revealed, like the toolbar) */
.drag-handle{cursor:grab;color:var(--muted);opacity:0;transition:opacity .12s;
  user-select:none;font-size:14px;line-height:1;padding:0 3px;margin-left:-6px}
.row:hover .drag-handle{opacity:.55}
.drag-handle:hover{opacity:1}
.drag-handle:active{cursor:grabbing}
.sortable-ghost{opacity:.35}
.sortable-chosen{background:#EDEAE1;border-radius:10px}
/* a cell muted out of the AI's context: dim it, but keep it usable */
.row.muted .cell-edit,.row.muted .bubble,.row.muted .out,.row.muted .cell-img{opacity:.5}
.row.muted .tag{opacity:.6}
/* a pinned cell: a small accent rail on the left */
.row.pinned{border-left:2px solid var(--accent);margin-left:-12px;padding-left:10px}
/* rich kernel output: plots, images, dataframes */
.cell-img{max-width:100%;height:auto;border:1px solid var(--line);border-radius:8px;margin-top:9px;display:block;background:#fff}
.cell-html{margin-top:9px;overflow-x:auto;font-size:13px}
.cell-html table{border-collapse:collapse}
.cell-html th,.cell-html td{border:1px solid var(--line);padding:4px 9px;text-align:right}
.cell-html th{background:var(--chip)}
/* live context meter at the foot of the stream */
.ctx-meter{margin:18px auto 4px;text-align:center;font-size:12px;color:var(--muted);
  border-top:1px dashed var(--line);padding-top:12px}
.answer{margin-top:9px}
.md>*:first-child{margin-top:0}.md>*:last-child{margin-bottom:0}
.md p{margin:.5em 0}.md ul,.md ol{margin:.5em 0;padding-left:1.4em}
.md h1,.md h2,.md h3{margin:.7em 0 .35em;line-height:1.3}
.md table{border-collapse:collapse;margin:.5em 0}.md th,.md td{border:1px solid var(--line);padding:4px 9px}
.md pre{background:var(--code-bg);color:var(--code-ink);border-radius:10px;padding:12px 14px;overflow:auto;
  font-family:'SF Mono',ui-monospace,Menlo,monospace;font-size:13px}
/* Pygments-highlighted fenced code blocks in markdown (the .highlight div owns the bg) */
.md .highlight{border-radius:10px;overflow:auto;margin:.5em 0;position:relative}
.md .highlight pre{background:transparent;margin:0;padding:12px 14px}
/* hover Copy button on code snippets */
.copy-btn{position:absolute;top:7px;right:7px;font-size:11px;line-height:1.4;
  border:1px solid #3a3933;background:rgba(40,40,36,.75);color:#cfcabb;border-radius:6px;
  padding:2px 9px;cursor:pointer;opacity:0;transition:opacity .12s}
.md .highlight:hover .copy-btn,.copy-btn:focus{opacity:1}
.copy-btn:hover{color:#fff;border-color:#6b675c}
.md code{font-family:'SF Mono',ui-monospace,Menlo,monospace;font-size:.92em}
.md :not(pre)>code{background:var(--chip);border-radius:5px;padding:1px 5px}
"""


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


def Sidebar():
    from urllib.parse import quote
    backend = STATE["backend"]
    names = backend.list_dialogs() or [STATE["dialog"]]
    convs = [
        A(n.split("/")[-1], href=f"/open?dialog={quote(n)}",
          cls=f"conv{' active' if n == STATE['dialog'] else ''}")
        for n in names
    ]
    return Div(
        Div(Span("S", cls="dot"), "SolveIt Sidekick", cls="brand"),
        A("✎  New dialog", href="/new", cls="newbtn"),
        Div("Dialogs", cls="seclabel"),
        *convs,
        Div(f"target: {STATE['target_name']}", cls="side-foot"),
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


def _ctx_buttons(m):
    """Mute / Pin / Delete — available in both rendered and edit modes."""
    mid = m.id
    return [
        Button("Muted" if m.muted else "In context", type="button",
               cls="cell-btn ctx" + (" off" if m.muted else ""),
               title="Toggle whether this cell is sent to the AI as notebook context",
               hx_post="/cell/mute", hx_vals=json.dumps({"id": mid}),
               hx_target="#stream", hx_swap="outerHTML"),
        Button("Pinned" if m.pinned else "Pin", type="button",
               cls="cell-btn pin" + (" on" if m.pinned else ""),
               title="Pin this cell so it stays in context even when older cells are trimmed",
               hx_post="/cell/pin", hx_vals=json.dumps({"id": mid}),
               hx_target="#stream", hx_swap="outerHTML"),
        Button("Delete", type="button", cls="cell-btn del",
               hx_post="/cell/delete", hx_vals=json.dumps({"id": mid}),
               hx_confirm="Delete this cell?",
               hx_target="#stream", hx_swap="outerHTML"),
    ]


def _rich_view(item):
    """Render one rich kernel output (plot/image/dataframe)."""
    t, data = item.get("type", ""), item.get("data", "")
    if t in ("image/png", "image/jpeg"):
        return Img(src=f"data:{t};base64,{data}", cls="cell-img")
    if t == "text/html":
        return Div(NotStr(data), cls="cell-html")
    return Div(data, cls="out")


def _tok_badge(m):
    return Span(f"~{est_tokens(m.content) + est_tokens(m.output)}t",
                cls="muted small tok", title="estimated tokens this cell adds to AI context")


def _rowcls(m):
    return "row" + (" muted" if m.muted else "") + (" pinned" if m.pinned else "")


def _head(m, primary, show_actions=False):
    bits = [Span("⠿", cls="drag-handle", title="Drag to reorder"),
            Span(_TAG[m.msg_type], cls="tag")]
    if m.msg_type == "code":
        bits.append(Span(m.id, cls="muted small"))
    bits.append(_tok_badge(m))
    bits.append(Div(*primary, *_ctx_buttons(m),
                    cls="cell-actions" + (" show" if show_actions else "")))
    return Div(*bits, cls="who")


def _output_views(m):
    """Code output + plots/images, or the rendered AI answer for a prompt."""
    out = []
    if m.msg_type == "code":
        if m.output:
            out.append(Div(m.output, cls="out"))
        out += [_rich_view(it) for it in m.rich]
    elif m.msg_type == "prompt":
        pending = STATE.get("pending_stream") == (STATE["dialog"], m.id)
        if pending and not (m.output or "").strip():
            # Live answer: a vanilla EventSource (see STREAM_JS) connects to /stream
            # and replaces this bubble's innerHTML as tokens arrive.
            who = m.model or "Claude (Max)"
            out.append(Div(
                Div(Span(who, cls="tag"), cls="who"),
                Div(NotStr("▌"), cls="bubble md", id=f"ans-{m.id}",
                    **{"data-stream-url": f"/stream?dialog={quote(STATE['dialog'])}&id={m.id}"}),
                cls="answer"))
        elif m.output:
            who = m.model or "SolveIt AI"
            out.append(Div(Div(Span(who, cls="tag"), cls="who"),
                           Div(render_md(m.output), cls="bubble md"), cls="answer"))
    return out


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


def MsgRow(m):
    """A cell in its default rendered (read-only, click-to-edit) state."""
    primary = [] if m.msg_type == "note" else [
        Button(_PRIMARY[m.msg_type], type="button", cls="cell-btn run",
               title="Re-run this cell", hx_post="/cell/exec",
               hx_vals=json.dumps({"id": m.id}), hx_target="#stream", hx_swap="outerHTML")]
    return Div(_head(m, primary), _rendered_content(m), *_output_views(m),
               cls=_rowcls(m), id=f"cell-{m.id}")


def _cell_edit(m):
    """A cell switched into edit mode: a raw editor + Save/Run/Ask + Cancel."""
    mid = m.id
    path = "/cell/save" if m.msg_type == "note" else "/cell/run"
    primary = [
        Button(_PRIMARY[m.msg_type], type="button", cls="cell-btn run",
               hx_post=path, hx_include=f"#ta-{mid}", hx_vals=json.dumps({"id": mid}),
               hx_target="#stream", hx_swap="outerHTML"),
        Button("Cancel", type="button", cls="cell-btn",
               hx_get=f"/cell/view?id={mid}", hx_target=f"#cell-{mid}", hx_swap="outerHTML"),
    ]
    # Code cells get CodeMirror (highlight-while-editing); notes/prompts just focus.
    js = (_CODE_EDITOR_JS if m.msg_type == "code" else _FOCUS_JS).replace("__MID__", mid)
    return Div(_head(m, primary, show_actions=True),
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
                   'Shift-Enter': function(){ cm.save(); runCell(); } }   // Jupyter convention
    });
    cm.on('change', function(){ cm.save(); });   // keep textarea current for hx-include
    setTimeout(function(){ cm.refresh(); cm.focus(); cm.setCursor(cm.lineCount(), 0); }, 0);
  } else {
    ta.focus(); var n = ta.value.length; ta.setSelectionRange(n, n);
  }
})();
"""

_FOCUS_JS = """
(function(){
  var t = document.getElementById('ta-__MID__');
  if(t){ t.focus(); var n = t.value.length; t.setSelectionRange(n, n); }
})();
"""


STREAM_JS = """
(function(){
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
    renderMath(el); addCopyButtons(el);
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
      renderMath(el); addCopyButtons(el);              // typeset math + copy buttons as it streams
      var s = el.closest('.stream'); if(s) s.scrollTop = s.scrollHeight;
    });
    es.addEventListener('done', function(){
      renderMath(el); addCopyButtons(el);
      es.close(); el.removeAttribute('data-stream-url'); el.__streaming = false;
    });
    es.onerror = function(){ es.close(); };
  });

  if(window.__sidekickCells) return;                            // bind document listeners once
  window.__sidekickCells = true;
  if(!hasFieldSizing){
    document.addEventListener('input', function(e){
      if(e.target.classList && e.target.classList.contains('cell-edit')) autosize(e.target);
    });
    window.addEventListener('resize', sizeAll);
  }
  document.addEventListener('keydown', function(e){
    if(e.target.classList && e.target.classList.contains('cell-edit')
       && (e.metaKey || e.ctrlKey) && e.key === 'Enter'){
      e.preventDefault();
      var row = e.target.closest('.row');
      var btn = row && row.querySelector('.cell-btn.run');
      if(btn) btn.click();                                      // Cmd/Ctrl+Enter runs the cell
    }
  });
})();
"""


def _ctx_meter(msgs):
    """A live read-out of how much of the notebook the AI would see right now."""
    toks = est_tokens(build_context(msgs))
    n_muted = sum(1 for m in msgs if m.muted)
    n_pin = sum(1 for m in msgs if m.pinned)
    bits = [f"AI context ≈ {toks:,} tokens", f"{len(msgs) - n_muted}/{len(msgs)} cells in"]
    if n_pin:
        bits.append(f"{n_pin} pinned")
    if n_muted:
        bits.append(f"{n_muted} muted")
    return Div(" · ".join(bits), cls="ctx-meter", title="estimated; ~4 chars/token")


def Stream():
    msgs = STATE["backend"].messages(STATE["dialog"])
    if not msgs:
        inner = Div("Start the conversation — write code, ask the AI, or jot a note.",
                    cls="empty")
    else:
        inner = Div(*[MsgRow(m) for m in msgs], _ctx_meter(msgs), cls="wrap")
    return Div(inner, Script(STREAM_JS), cls="stream", id="stream")


COMPOSER_JS = """
var composerCM = null;   // CodeMirror instance while the composer is in Code mode

function _submitComposer(){
  if(composerCM) composerCM.save();                 // flush editor -> textarea
  var ta = document.getElementById('composerInput');
  if(ta && ta.value.trim()) document.getElementById('composerForm').submit();
}
function _initComposerCM(){
  var ta = document.getElementById('composerInput');
  if(!ta || composerCM || !window.CodeMirror) return;   // offline: stays a plain textarea
  composerCM = CodeMirror.fromTextArea(ta, {
    mode: 'python', theme: 'monokai', lineNumbers: false,
    viewportMargin: Infinity, indentUnit: 4, lineWrapping: true,
    placeholder: '# code…  Shift+Enter to run · Enter for newline · Tab switches mode',
    extraKeys: {
      'Shift-Enter': _submitComposer, 'Cmd-Enter': _submitComposer, 'Ctrl-Enter': _submitComposer,
      'Tab': function(){ cycleMode(1); }, 'Shift-Tab': function(){ cycleMode(-1); }
    }
  });
  composerCM.on('change', function(){ composerCM.save(); });
  setTimeout(function(){ composerCM.refresh(); composerCM.focus(); }, 0);
}
function _destroyComposerCM(){
  if(!composerCM) return;
  composerCM.save();
  composerCM.toTextArea();                            // restore the plain textarea
  composerCM = null;
  var ta = document.getElementById('composerInput'); if(ta) ta.focus();
}
function setMode(v){
  document.getElementById('msgType').value = v;
  document.querySelectorAll('#modeChips .mode').forEach(function(el){
    el.classList.toggle('sel', el.getAttribute('data-val') === v);
  });
  if(v === 'code') _initComposerCM(); else _destroyComposerCM();
}
var COMPOSER_MODES = ['prompt','code','note'];   // order matches the chips: Ask AI / Code / Note
function cycleMode(dir){
  var i = COMPOSER_MODES.indexOf(document.getElementById('msgType').value);
  if(i < 0) i = 0;
  setMode(COMPOSER_MODES[(i + dir + COMPOSER_MODES.length) % COMPOSER_MODES.length]);
}
(function(){
  var ta = document.getElementById('composerInput');
  if(!ta) return;
  // Plain-textarea keys (Ask AI / Note, and the offline Code fallback).
  ta.addEventListener('keydown', function(e){
    if(e.key === 'Tab'){                 // Tab cycles Ask AI -> Code -> Note (Shift+Tab back)
      e.preventDefault();
      cycleMode(e.shiftKey ? -1 : 1);
      return;
    }
    if(e.key === 'Enter'){
      if(document.getElementById('msgType').value === 'code'){
        // Jupyter convention: Shift/Cmd/Ctrl+Enter runs, plain Enter = newline
        if(e.shiftKey || e.metaKey || e.ctrlKey){ e.preventDefault(); _submitComposer(); }
      } else if(!e.shiftKey){            // chat convention: Enter sends, Shift+Enter = newline
        e.preventDefault(); _submitComposer();
      }
    }
  });
  if(document.getElementById('msgType').value === 'code') _initComposerCM();  // sticky Code mode
  else ta.focus();
})();
"""


def ModelSelect():
    opts = [
        Option(m["label"], value=m["id"], selected=(m["id"] == STATE["model"]))
        for m in list_models()
    ]
    # The chosen model is submitted with the send form (name="model").
    return Select(*opts, name="model", cls="msel", title="AI model for Ask AI")


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
                     placeholder="Message SolveIt…  (Enter to send, Shift+Enter for newline)"),
            Div(
                Div(mode("prompt", "Ask AI"), mode("code", "Code"),
                    mode("note", "Note"), cls="modes", id="modeChips"),
                Div(ModelSelect(), Button("↑", cls="send", type="submit"),
                    style="display:flex;align-items:center;gap:8px"),
                cls="row2",
            ),
            method="post", action="/send", id="composerForm", cls="box",
        ),
        Div("Connected to ", Strong(STATE["target_name"]),
            " · switch target top-right to move between laptop and H100", cls="hint"),
        Script(COMPOSER_JS),
        cls="wrap"), cls="composer"))


def TitleEditor():
    """The dialog name, editable in place. Submits on Enter or blur."""
    return Form(
        Input(name="new", value=STATE["dialog"], cls="title-edit",
              title="Rename this dialog — press Enter",
              onblur="this.form.submit()",
              onkeydown="if(event.key==='Enter'){event.preventDefault();this.form.submit();}"),
        Input(type="hidden", name="old", value=STATE["dialog"]),
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
        Head(Title("Settings · SolveIt Sidekick"), *app.hdrs, Style(CSS)),
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


# Table of contents built from note headings (h1–h6 inside .note-view). Lives in
# Page() (not #stream), so it persists across htmx swaps; STREAM_JS calls
# window.buildTOC() on each render to keep it in sync. Toggle state is saved.
TOC_JS = """
window.buildTOC = function(){
  var list = document.getElementById('tocList');
  if(!list) return;
  var heads = document.querySelectorAll(
    '#stream .note-view h1,#stream .note-view h2,#stream .note-view h3,' +
    '#stream .note-view h4,#stream .note-view h5,#stream .note-view h6');
  list.innerHTML = '';
  if(!heads.length){
    var e = document.createElement('div'); e.className = 'toc-empty';
    e.textContent = 'No note headings yet — add a note with # headings.';
    list.appendChild(e); return;
  }
  heads.forEach(function(h, i){
    if(!h.id) h.id = 'toc-h-' + i;
    var a = document.createElement('a');
    a.className = 'toc-link toc-' + h.tagName.toLowerCase();
    a.textContent = h.textContent;
    a.href = '#' + h.id;
    a.addEventListener('click', function(ev){
      ev.preventDefault();
      h.scrollIntoView({behavior: 'smooth', block: 'start'});
    });
    list.appendChild(a);
  });
};
window.toggleTOC = function(){
  var app = document.querySelector('.app');
  if(!app) return;
  var open = app.classList.toggle('toc-open');
  try { localStorage.setItem('sidekick_toc', open ? '1' : '0'); } catch(e){}
};
(function(){
  var app = document.querySelector('.app');
  if(!app) return;
  var saved = null;
  try { saved = localStorage.getItem('sidekick_toc'); } catch(e){}
  if(saved !== '0') app.classList.add('toc-open');   // default open
  window.buildTOC();
})();
"""


def PaperPanel():
    """Left reading column: the open paper as rendered markdown, or a converting
    spinner that polls until ready. Selecting text shows an 'Ask AI' button."""
    p = STATE.get("paper")
    if not p:
        return Div(cls="paper", id="paperPanel")
    head = Div(Span(p["name"], cls="paper-name"),
               A("✕", href="/paper/close", cls="gear", title="Close paper"),
               cls="paper-head")
    if p.get("status") == "converting":
        body = Div("Converting… first time runs the model and can take a bit.",
                   cls="paper-converting",
                   hx_get="/paper/status", hx_trigger="every 2s",
                   hx_target="#paperPanel", hx_swap="outerHTML")
        return Div(head, body, cls="paper", id="paperPanel")
    badge = Span(f"via {p.get('engine', '?')} · select text to ask the AI", cls="muted small")
    body = Div(render_md(p.get("md", "")), cls="paper-body md", id="paperBody")
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
  var btn = null;
  function hide(){ if(btn) btn.style.display = 'none'; }
  function ask(text){
    if(typeof setMode === 'function') setMode('prompt');
    var ta = document.getElementById('composerInput');
    if(ta){
      var quote = text.split('\\n').map(function(l){ return '> ' + l; }).join('\\n');
      ta.value = quote + '\\n\\n';
      ta.focus(); ta.setSelectionRange(ta.value.length, ta.value.length);
      ta.scrollIntoView({block: 'center'});
    }
    hide();
  }
  document.addEventListener('mouseup', function(){
    var paper = document.getElementById('paperBody');
    var sel = window.getSelection();
    var text = sel ? sel.toString().trim() : '';
    if(!paper || !text || !sel.anchorNode || !paper.contains(sel.anchorNode)){ hide(); return; }
    if(!btn){
      btn = document.createElement('button');
      btn.className = 'ask-sel-btn'; btn.textContent = 'Ask AI about this ↗';
      btn.addEventListener('mousedown', function(e){ e.preventDefault(); ask(btn.__t); });
      document.body.appendChild(btn);
    }
    btn.__t = text;
    var r = sel.getRangeAt(0).getBoundingClientRect();
    btn.style.top = (window.scrollY + r.bottom + 6) + 'px';
    btn.style.left = (window.scrollX + r.left) + 'px';
    btn.style.display = 'block';
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
        Head(Title("SolveIt Sidekick"), *app.hdrs, Style(CSS)),
        Body(Div(
            Sidebar(),
            PaperPanel(),
            Div(
                Div(TitleEditor(),
                    Div(Span("☰", cls="gear toc-toggle", title="Toggle table of contents",
                             onclick="toggleTOC()"),
                        Details(Summary("📄", cls="gear", title="Open a paper (PDF)"),
                                Form(Input(name="path", placeholder="/path/to/paper.pdf",
                                           cls="paper-path"),
                                     Button("Open", cls="cell-btn run", type="submit"),
                                     method="post", action="/paper/open", cls="paper-form"),
                                cls="export"),
                        Details(Summary("⬇", cls="gear", title="Export this dialog"),
                                Div(A("Jupyter notebook (.ipynb)", href="/export/ipynb"),
                                    A("Markdown (.md)", href="/export/md"),
                                    cls="export-menu"),
                                cls="export"),
                        A("⚙", href="/settings", cls="gear", title="Settings — API keys"),
                        TargetSwitcher(),
                        style="display:flex;align-items:center;gap:12px"),
                    cls="topbar"),
                banner,
                Stream(),
                Composer(),
                cls="main",
            ),
            Div(Div("Contents", cls="toc-head"), Div(id="tocList", cls="toc-list"),
                cls="toc", id="toc"),
            cls="app" + (" paper-open" if STATE.get("paper") else ""),
        ), Script(TOC_JS)),
    )


# ---- export (.ipynb / .md) --------------------------------------------------
def _lines(s: str) -> list:
    """nbformat wants source/text as a list of lines (keeping newlines)."""
    return (s or "").splitlines(keepends=True)


def _code_outputs(m) -> list:
    outs = []
    if m.output:
        outs.append({"output_type": "stream", "name": "stdout", "text": _lines(m.output)})
    for item in m.rich:
        t, data = item.get("type", ""), item.get("data", "")
        if t in ("image/png", "image/jpeg"):
            outs.append({"output_type": "display_data", "data": {t: data}, "metadata": {}})
        elif t == "text/html":
            outs.append({"output_type": "display_data",
                         "data": {"text/html": _lines(data)}, "metadata": {}})
    return outs


def _prompt_md(m) -> str:
    md = f"**Prompt:** {m.content}"
    if m.output:
        md += f"\n\n**{m.model or 'AI'}:**\n\n{m.output}"
    return md


def to_ipynb(msgs) -> dict:
    """Export cells to a Jupyter notebook: code→code cells (with outputs/plots),
    notes→markdown, prompts→markdown (question + AI answer)."""
    cells = []
    for m in msgs:
        cid = (m.id or "").lstrip("_") or "cell"      # nbformat cell id (no leading _)
        if m.msg_type == "code":
            cells.append({"id": cid, "cell_type": "code", "metadata": {}, "execution_count": None,
                          "source": _lines(m.content), "outputs": _code_outputs(m)})
        elif m.msg_type == "note":
            cells.append({"id": cid, "cell_type": "markdown", "metadata": {},
                          "source": _lines(m.content)})
        else:
            cells.append({"id": cid, "cell_type": "markdown", "metadata": {},
                          "source": _lines(_prompt_md(m))})
    return {"cells": cells, "nbformat": 4, "nbformat_minor": 5,
            "metadata": {"kernelspec": {"name": "python3", "display_name": "Python 3",
                                        "language": "python"},
                         "language_info": {"name": "python"}}}


def to_markdown(msgs) -> str:
    """Export cells to a single Markdown document."""
    out = []
    for m in msgs:
        if m.msg_type == "code":
            out.append(f"```python\n{m.content}\n```")
            if m.output:
                out.append(f"```\n{m.output}\n```")
            for item in m.rich:
                if item.get("type", "").startswith("image/"):
                    out.append(f"![output](data:{item['type']};base64,{item['data']})")
        elif m.msg_type == "note":
            out.append(m.content)
        else:
            out.append(_prompt_md(m))
    return "\n\n".join(out) + "\n"


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
    Script(src="/vendor/codemirror.min.js"),
    Script(src="/vendor/python.min.js"),
    Script(src="/vendor/placeholder.min.js"),
    Link(rel="stylesheet", href="/vendor/katex.min.css"),
    Script(src="/vendor/katex.min.js"),
    Script(src="/vendor/auto-render.min.js"),
    Script(src="/vendor/sortable.min.js"),
)
app, rt = fast_app(pico=False, default_hdrs=False, hdrs=_LOCAL_HDRS)
# FastHTML registers a generic "/{fname:path}.{ext:static}" route that serves
# from cwd and would shadow /vendor (404ing our assets). Drop it — we serve our
# own static files from /vendor below.
app.routes[:] = [r for r in app.routes if getattr(r, "path", "") != "/{fname:path}.{ext:static}"]


@rt("/vendor/{fname:path}")
def vendor(fname: str):
    """Serve a vendored front-end asset (with a path-traversal guard)."""
    from starlette.responses import FileResponse, PlainTextResponse
    p = (_VENDOR_DIR / fname).resolve()
    if str(p).startswith(str(_VENDOR_DIR.resolve())) and p.is_file():
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


@rt("/rename", methods=["post"])
def rename_dialog(old: str, new: str):
    new = (new or "").strip()
    backend = STATE["backend"]
    if new and new != old and hasattr(backend, "rename"):
        try:
            backend.rename(old, new)
            STATE["dialog"] = new
        except ValueError as e:
            STATE["warning"] = str(e)          # surfaced as the banner
    return Page()


@rt("/open")
def open_dialog(dialog: str):
    STATE["dialog"] = dialog
    return Page()


@rt("/new")
def new_dialog():
    n = 1
    existing = set(STATE["backend"].list_dialogs())
    while f"untitled/dialog-{n}" in existing:
        n += 1
    STATE["dialog"] = f"untitled/dialog-{n}"
    STATE["backend"].messages(STATE["dialog"])  # touch -> create
    return Page()


@rt("/send", methods=["post"])
def send(content: str, msg_type: str = "prompt", model: str = None):
    content = (content or "").strip()
    if model:
        STATE["model"] = model           # remember last-used model
    if msg_type:
        STATE["msg_type"] = msg_type     # remember last-used compose mode
    if content:
        backend = STATE["backend"]
        use_model = STATE["model"] if msg_type == "prompt" else None
        m = backend.add(STATE["dialog"], content, msg_type, model=use_model)
        if msg_type == "prompt" and _can_stream(backend, use_model):
            # Defer the AI call: the page renders an SSE-wired answer that streams
            # tokens in (the browser opens /stream), instead of blocking here.
            STATE["pending_stream"] = (STATE["dialog"], m.id)
        elif msg_type in ("code", "prompt"):
            backend.exec(STATE["dialog"], m.id)
    return Page()


def _msg_by_id(backend, dialog: str, mid: str):
    for m in backend.messages(dialog):
        if m.id == mid:
            return m
    return None


@rt("/cell/run", methods=["post"])
def cell_run(id: str, content: str = ""):
    """Save a cell's edited source, then (re)execute it. Returns just the stream."""
    backend = STATE["backend"]
    if hasattr(backend, "update"):
        backend.update(STATE["dialog"], id, content)
    m = _msg_by_id(backend, STATE["dialog"], id)
    if m is not None and m.msg_type == "prompt" and _can_stream(backend, m.model):
        STATE["pending_stream"] = (STATE["dialog"], id)   # re-ask, streamed live
    elif m is not None and m.msg_type in ("code", "prompt"):
        backend.exec(STATE["dialog"], id)
    return Stream()


@rt("/stream")
def stream_answer(dialog: str, id: str):
    """Server-Sent-Events: stream a prompt's AI answer token-by-token.

    The browser's EventSource (see STREAM_JS) connects here for a cell whose
    answer is pending. We build the notebook context up to that cell, stream the
    deltas from the `claude` CLI, and emit cumulative rendered markdown as `msg`
    events — finishing with a `done` event so the client closes the connection.
    """
    backend = STATE["backend"]
    m = _msg_by_id(backend, dialog, id)
    context = build_context(backend.messages(dialog), upto_id=id) if m else ""

    def gen():
        if m is None:
            yield sse_message(Div(""), event="done")
            return
        acc = ""
        for delta in stream_claude(dialog, m.content, context):
            acc += delta
            # str() unwraps NotStr -> raw (already-safe) markdown HTML for the data lines
            yield sse_message(str(render_md(acc)), event="msg")
        m.output = acc or m.output                       # persist for reloads
        if STATE.get("pending_stream") == (dialog, id):
            STATE["pending_stream"] = None
        yield sse_message(str(render_md(m.output)), event="msg")   # final state
        yield sse_message(Div(""), event="done")

    return StreamingResponse(gen(), media_type="text/event-stream")


@rt("/cell/save", methods=["post"])
def cell_save(id: str, content: str = ""):
    """Save a cell's edited source without executing (used by note cells)."""
    backend = STATE["backend"]
    if hasattr(backend, "update"):
        backend.update(STATE["dialog"], id, content)
    return Stream()


@rt("/cell/delete", methods=["post"])
def cell_delete(id: str):
    backend = STATE["backend"]
    if hasattr(backend, "delete"):
        backend.delete(STATE["dialog"], id)
    return Stream()


@rt("/cell/mute", methods=["post"])
def cell_mute(id: str):
    """Toggle whether this cell is included in the AI's notebook context."""
    backend = STATE["backend"]
    if hasattr(backend, "set_muted"):
        backend.set_muted(STATE["dialog"], id)
    return Stream()


@rt("/cell/pin", methods=["post"])
def cell_pin(id: str):
    """Toggle whether this cell is pinned into context (survives trimming)."""
    backend = STATE["backend"]
    if hasattr(backend, "set_pinned"):
        backend.set_pinned(STATE["dialog"], id)
    return Stream()


@rt("/cell/edit")
def cell_edit(id: str):
    """Swap a single cell into edit mode (raw textarea)."""
    m = _msg_by_id(STATE["backend"], STATE["dialog"], id)
    return _cell_edit(m) if m else Stream()


@rt("/cell/view")
def cell_view(id: str):
    """Swap a single cell back to its rendered (read-only) view — used by Cancel."""
    m = _msg_by_id(STATE["backend"], STATE["dialog"], id)
    return MsgRow(m) if m else Stream()


@rt("/cell/exec", methods=["post"])
def cell_exec(id: str):
    """Re-run a cell's stored source without editing (the rendered-view Run/Ask)."""
    backend = STATE["backend"]
    m = _msg_by_id(backend, STATE["dialog"], id)
    if m is not None and m.msg_type == "prompt" and _can_stream(backend, m.model):
        m.output = ""                                     # clear stale answer to re-stream
        STATE["pending_stream"] = (STATE["dialog"], id)   # re-ask, streamed live
    elif m is not None:
        backend.exec(STATE["dialog"], id)
    return Stream()


@rt("/cell/move", methods=["post"])
def cell_move(ids: str = ""):
    """Persist a new cell order after a drag. SortableJS has already reordered the
    DOM, so we just save the order — no re-render needed (returns empty)."""
    backend = STATE["backend"]
    order = [i for i in ids.split(",") if i]
    if order and hasattr(backend, "reorder"):
        backend.reorder(STATE["dialog"], order)
    return ""


def _download(body: str, fname: str, media: str):
    from starlette.responses import Response
    return Response(body, media_type=media,
                    headers={"Content-Disposition": f'attachment; filename="{fname}"'})


@rt("/export/ipynb")
def export_ipynb():
    msgs = STATE["backend"].messages(STATE["dialog"])
    fname = STATE["dialog"].replace("/", "-") + ".ipynb"
    return _download(json.dumps(to_ipynb(msgs), indent=1), fname, "application/x-ipynb+json")


@rt("/export/md")
def export_md():
    msgs = STATE["backend"].messages(STATE["dialog"])
    fname = STATE["dialog"].replace("/", "-") + ".md"
    return _download(to_markdown(msgs), fname, "text/markdown; charset=utf-8")


def _convert_paper_async(path: str):
    """Convert a PDF in a background thread (marker can take a while), updating
    STATE['paper'] from 'converting' to 'ready'/'error'. The panel polls."""
    name = os.path.basename(path)
    STATE["paper"] = {"name": name, "status": "converting"}

    def work():
        try:
            md, engine = paperlib.convert(path)
            STATE["paper"] = {"name": name, "status": "ready", "md": md, "engine": engine}
        except Exception as e:  # noqa: BLE001 — surface conversion failures in the panel
            STATE["paper"] = {"name": name, "status": "ready", "md": f"Could not open: {e}",
                              "engine": "error"}

    threading.Thread(target=work, daemon=True).start()


@rt("/paper/open", methods=["post"])
def paper_open(path: str = ""):
    path = os.path.expanduser((path or "").strip())
    if path:
        _convert_paper_async(path)
    return Page()


@rt("/paper/status")
def paper_status():
    return PaperPanel()


@rt("/paper/close")
def paper_close():
    STATE["paper"] = None
    return Page()


if __name__ == "__main__":
    serve()
