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
import re
import threading
from urllib.parse import quote

from fasthtml.common import *
from starlette.datastructures import UploadFile

from .targets import get_target, list_targets, list_models, default_model
from .client import connect, build_context, est_tokens, _InMemoryBackend
from .claude_cli import stream as stream_claude, cost_for, CLI_MODELS
from . import secrets_store, export
from . import paper as paperlib


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
except Exception:  # noqa: BLE001 — degrade to a plain code block if pygments is missing
    def _highlight(src: str, lang: str = "", fmt=None) -> str | None:
        return None

    def _highlight_md(src: str, lang: str = "") -> str | None:
        return None

    def render_code(src: str):
        return Pre(src or "", cls="code")


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
                return f'<pre class="mermaid">{mistune.util.escape(code or "")}</pre>'
            html = _highlight_md(code, lang)
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
    "editing": None,          # cell id to render in edit mode once (just-inserted cell)
    "scroll_to": None,        # cell id to scroll into view once (e.g. after a composer send)
    "cells_dirty": False,     # set when the AI's MCP tools edit cells mid-stream → reload
}


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
  --md-code-bg:#EAE6DA;  /* non-runnable code in answers/notes; sync with _LightCodeStyle */
  --bubble-user:#F5E9E2; --chip:#EDEAE1;
}
*{box-sizing:border-box} html,body{margin:0;height:100%}
body{font-family:'Styrene B','Segoe UI',system-ui,-apple-system,sans-serif;
  background:var(--bg);color:var(--ink);font-size:15px;line-height:1.55}
/* global top bar over a flex row of columns; any column can be hidden */
.app{display:flex;flex-direction:column;height:100vh;--side-w:264px;--toc-w:244px;--paper-w:50%}
.cols{display:flex;flex:1;min-height:0}
/* drag-to-resize handles between columns: zero-width flex items with a wider
   invisible hit area straddling the seam, so resizing adds no layout gap. */
.gutter{flex:0 0 0;position:relative;z-index:6}
.gutter::before{content:"";position:absolute;top:0;bottom:0;left:-3px;width:7px;cursor:col-resize}
.gutter:hover::before,.gutter.dragging::before{background:var(--accent);opacity:.5}
.app.no-side .gutter-side{display:none}
.gutter-paper,.gutter-toc{display:none}
.app.paper-open .gutter-paper{display:block}
.app.paper-collapsed .gutter-paper{display:none}
.app.toc-open .gutter-toc{display:block}
body.col-resizing{cursor:col-resize;user-select:none}
.app.no-side .side{display:none}
.topbar-left{display:flex;align-items:center;gap:12px;min-width:0}
.topbar-right{display:flex;align-items:center;gap:12px}
/* paper reading panel (left column, toggled open when a paper is loaded) */
.paper{display:none;background:var(--panel);border-right:1px solid var(--line);overflow:auto;padding:16px 18px;min-width:0}
.app.paper-open .paper{display:flex;flex-direction:column;flex:0 0 var(--paper-w)}
.app.paper-open.no-paper .paper,.app.no-paper .gutter-paper{display:none}   /* topbar 📖 toggle */
.paper-head{display:flex;align-items:center;justify-content:space-between;gap:8px;margin-bottom:4px}
.paper-actions{display:flex;align-items:center;gap:8px;flex-shrink:0}
.paper-import{margin:0;display:flex;align-items:center;gap:5px}
.paper-name{font-weight:600;font-size:13px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.paper-converting{margin-top:14px;color:var(--muted);font-size:13px;font-style:italic}
.paper-body{margin-top:10px;font-size:13px;line-height:1.6}
.paper-body img{max-width:100%}
/* collapsed paper: hide the text, keep the header (incl. Next section) in a
   compact strip so the notebook reclaims the width. */
.paper-toggle{cursor:pointer;transition:transform .12s}
.app.paper-collapsed .paper-toggle{transform:rotate(-90deg)}
.app.paper-collapsed .paper-body,.app.paper-collapsed .paper-badge{display:none}
.app.paper-collapsed .paper{flex:0 0 auto}
.app.paper-collapsed .paper-name{max-width:150px}
/* secondary import options (stepper + bulk), behind a small "Import…" menu */
.paper-import-menu{position:absolute;right:0;top:28px;z-index:30;display:flex;flex-direction:column;gap:6px;
  background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:10px;min-width:210px;
  box-shadow:0 6px 18px rgba(0,0,0,.12)}
.paper-import-menu .ins-col-head{margin:2px 0 0}
/* floating toolbar shown when you highlight text in the paper */
.sel-tools{position:absolute;z-index:60;display:none;gap:6px;box-shadow:0 4px 12px rgba(0,0,0,.20);
  border-radius:8px}
.sel-btn{border:none;border-radius:8px;padding:5px 11px;font-size:12px;cursor:pointer;white-space:nowrap}
.sel-btn.import{background:var(--accent);color:#fff;font-weight:600}
.sel-btn:not(.import){background:var(--panel);color:var(--ink);border:1px solid var(--line)}
/* the 📄 topbar icon IS the file picker: a label wrapping a hidden file input */
.paper-file{display:none}
/* "open a source" dropdown: a PDF file pick or a web-page URL */
.src-form{position:absolute;right:0;top:28px;z-index:30;display:flex;flex-direction:column;gap:8px;
  background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:12px;min-width:248px;
  box-shadow:0 6px 18px rgba(0,0,0,.12)}
.src-file-label{font-size:13px;font-weight:600;color:var(--accent);cursor:pointer}
.src-url{border:1px solid var(--line);border-radius:8px;padding:7px 10px;font:inherit;font-size:13px;outline:none}
.src-url:focus{border-color:var(--accent)}
/* table of contents (right column, toggleable, full-height so it stays in view) */
.toc{display:none;background:var(--sidebar);border-left:1px solid var(--line);
  padding:16px 14px;overflow:auto}
.app.toc-open .toc{display:flex;flex-direction:column;flex:0 0 var(--toc-w)}
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
.side{flex:0 0 var(--side-w);background:var(--sidebar);border-right:1px solid var(--line);display:flex;flex-direction:column;padding:14px 12px;overflow:auto}
.brand{display:flex;align-items:center;gap:9px;font-weight:600;padding:6px 8px 14px}
.brand .dot{width:22px;height:22px;border-radius:6px;background:var(--accent);display:grid;place-items:center;color:#fff;font-size:13px}
.newbtn{display:flex;align-items:center;gap:8px;width:100%;border:1px solid var(--line);background:var(--panel);
  color:var(--ink);border-radius:10px;padding:9px 12px;cursor:pointer;font-size:14px;margin-bottom:12px}
.newbtn:hover{border-color:#d4d0c4}
.seclabel{font-size:11px;letter-spacing:.04em;text-transform:uppercase;color:var(--muted);padding:8px 8px 4px}
.conv{display:block;padding:8px 10px;border-radius:8px;color:var(--ink);text-decoration:none;font-size:14px;cursor:pointer;
  flex:1 1 auto;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.conv:hover{background:#E8E5DB} .conv.active{background:#E3DFD3;font-weight:500}
/* a dialog row: open-link + a ⋯ actions menu revealed on hover */
.conv-row{display:flex;align-items:center;gap:2px;position:relative}
.conv-actions{position:relative;flex:0 0 auto}
.conv-dots{list-style:none;cursor:pointer;color:var(--muted);opacity:0;padding:2px 7px;border-radius:6px;
  font-size:16px;line-height:1;transition:opacity .12s}
.conv-dots::-webkit-details-marker{display:none}
.conv-row:hover .conv-dots,.conv-actions[open] .conv-dots{opacity:.65}
.conv-dots:hover{opacity:1;background:#E8E5DB}
.conv-menu{position:absolute;right:0;top:26px;z-index:30;background:var(--panel);border:1px solid var(--line);
  border-radius:8px;padding:4px;box-shadow:0 6px 18px rgba(0,0,0,.14);min-width:120px}
.conv-del-form{margin:0}
.conv-del{display:block;width:100%;text-align:left;border:none;background:none;color:#B3402F;cursor:pointer;
  font:inherit;font-size:13px;padding:6px 10px;border-radius:6px}
.conv-del:hover{background:#F6E8E4}
.folder>.conv-row,.folder>.folder{margin-left:10px}
/* multi-select (shift/⌘-click) + bulk-delete bar */
.conv-row.sel>.conv{background:#E6F2EA;box-shadow:inset 2px 0 0 var(--accent)}
.sel-bar{display:none;align-items:center;gap:6px;margin:4px 2px 6px;padding:5px 8px;border-radius:8px;
  background:#FBF3E7;border:1px solid #E3C7AE}
.sel-count{flex:1 1 auto;font-size:12px;color:var(--muted)}
.sel-del{border:none;background:none;color:#B3402F;font:inherit;font-size:12px;font-weight:600;cursor:pointer;
  padding:3px 7px;border-radius:6px}
.sel-del:hover{background:#F6E8E4}
.sel-clear{border:none;background:none;color:var(--muted);font:inherit;font-size:12px;cursor:pointer;padding:3px 5px}
.sel-clear:hover{color:var(--ink)}
.folder-label{list-style:none;cursor:pointer;font-size:11px;letter-spacing:.04em;text-transform:uppercase;
  color:var(--muted);padding:8px 8px 4px;user-select:none}
.folder-label::-webkit-details-marker{display:none}
.folder-label::before{content:"▸ ";font-size:9px;display:inline-block;transition:transform .12s}
.folder[open]>.folder-label::before{transform:rotate(90deg)}
.side-foot{margin-top:auto;font-size:12px;color:var(--muted);padding:8px}
/* main */
.main{flex:1 1 0;display:flex;flex-direction:column;min-width:0;min-height:0}  /* min-height:0 lets .stream scroll, not .main */
.topbar{flex-shrink:0;display:flex;align-items:center;justify-content:space-between;gap:14px;padding:12px 22px;border-bottom:1px solid var(--line)}
.title{font-weight:600}
.title-form{margin:0}
.title-edit{font-weight:600;font-size:15px;font-family:inherit;color:var(--ink);
  border:1px solid transparent;background:transparent;border-radius:7px;padding:4px 8px;
  min-width:240px;outline:none}
.title-edit:hover{border-color:var(--line)}
.title-edit:focus{border-color:var(--accent);background:var(--panel)}
.gear{text-decoration:none;font-size:18px;color:var(--muted);line-height:1}
.gear:hover{color:var(--ink)}
.gear.tgl{cursor:pointer;border-radius:7px;padding:2px 4px;transition:opacity .12s,background .12s}
/* a panel-toggle whose panel is hidden: dimmed, so what's collapsed is obvious */
.gear.tgl.off{opacity:.34}
.gear.tgl.off:hover{opacity:1;background:var(--chip)}
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
.stream{flex:1;overflow:auto;padding:14px 0}
.wrap{max-width:760px;margin:0 auto;padding:0 22px}
.row{margin-bottom:9px}
.who{font-size:11px;color:var(--muted);margin-bottom:2px;display:flex;align-items:center;gap:6px}
.tag{font-size:10px;border:1px solid var(--line);border-radius:6px;padding:0 5px;color:var(--muted)}
/* collapsible heading sections */
.sec-caret{cursor:pointer;color:var(--ink);opacity:.6;font-size:18px;line-height:1;user-select:none;
  transition:transform .12s,opacity .12s;display:inline-flex;align-items:center;
  width:20px;height:20px;justify-content:center;border-radius:6px}
.sec-caret:hover{opacity:1;background:var(--chip)}
.sec-caret.collapsed{transform:rotate(-90deg)}
.sec-count{font-size:11px;color:var(--muted);background:var(--chip);border-radius:10px;padding:0 7px}
#stream .row.sec-hidden{display:none}
.bubble{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:8px 12px}
.bubble.user{background:var(--bubble-user);border-color:#EBD9CD}
.bubble.note{background:transparent;border:none;padding:2px 0}
/* AI "working" indicators: a spinning wheel while we wait for the first token,
   then a blinking caret at the tail of the answer while it streams in. */
.thinking{display:inline-flex;align-items:center;gap:8px;color:var(--muted);font-size:13px}
.spinner{display:inline-block;width:13px;height:13px;border:2px solid var(--line);
  border-top-color:var(--accent);border-radius:50%;animation:spin .7s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
.bubble.streaming::after{content:'▌';color:var(--accent);margin-left:1px;
  animation:caret-blink 1s step-start infinite}
@keyframes caret-blink{50%{opacity:0}}
pre.code{background:var(--code-bg);color:var(--code-ink);border-radius:10px;padding:10px 14px;overflow:auto;
  font-family:'SF Mono',ui-monospace,Menlo,monospace;font-size:13px;margin:0}
.out{margin-top:4px;border-left:2px solid var(--line);padding:3px 0 3px 12px;color:var(--muted);
  font-family:'SF Mono',ui-monospace,Menlo,monospace;font-size:13px;white-space:pre-wrap}
.empty{color:var(--muted);text-align:center;margin-top:60px}
/* composer */
.composer{padding:0 0 22px} .composer .wrap{padding:0 22px}
.box{background:var(--panel);border:1px solid var(--line);border-radius:16px;padding:10px 12px;
  box-shadow:0 1px 2px rgba(0,0,0,.03)}
.box textarea{width:100%;border:none;outline:none;resize:none;background:transparent;font:inherit;color:var(--ink);min-height:46px}
.row2{display:flex;align-items:center;justify-content:space-between;margin-top:6px}
.modes{display:flex;gap:6px}
.mode{font-size:12px;border:1px solid var(--line);background:#fff;border-radius:8px;height:32px;padding:0 12px;
  display:inline-flex;align-items:center;cursor:pointer;color:var(--muted)}
.mode input{display:none}
.mode.sel{background:var(--accent);border-color:var(--accent);color:#fff}
select.msel{appearance:none;background:#fff;border:1px solid var(--line);border-radius:8px;height:32px;padding:0 24px 0 12px;
  font-size:12px;cursor:pointer;color:var(--ink)}
.send{background:var(--accent);border:none;color:#fff;border-radius:8px;width:32px;height:32px;cursor:pointer;font-size:15px}
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
.code-view .highlight pre{margin:0;padding:8px 13px;border-radius:11px;
  font-family:'SF Mono',ui-monospace,Menlo,monospace;font-size:13px;line-height:1.45}
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
.cell-btn.ins{color:#2C6B45;font-weight:600}
.cell-btn.ins:hover{border-color:#BFE0CC;background:#E6F2EA}
.ins{position:relative;display:inline-block}
.ins>summary{list-style:none;cursor:pointer}
.ins>summary::-webkit-details-marker{display:none}
.ins-menu{position:absolute;right:0;top:26px;z-index:30;display:flex;gap:10px;
  background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:8px;
  box-shadow:0 6px 18px rgba(0,0,0,.12)}
.ins-col{display:flex;flex-direction:column;gap:3px;min-width:80px}
.ins-col-head{font-size:10px;text-transform:uppercase;letter-spacing:.04em;color:var(--muted);margin-bottom:2px}
.ins-item{text-align:left;font-size:12px;border:1px solid var(--line);background:#fff;border-radius:7px;
  padding:4px 9px;cursor:pointer;color:var(--ink)}
.ins-item:hover{border-color:var(--accent);background:#FBF3E7}
.type-menu{position:absolute;right:0;top:26px;z-index:30;display:flex;flex-direction:column;gap:3px;
  min-width:96px;background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:8px;
  box-shadow:0 6px 18px rgba(0,0,0,.12)}
.ins-item.cur{border-color:var(--accent);background:#FBF3E7;font-weight:600}   /* the current type */
/* Copy-to-dialog menu: same drop as the type menu, but names can be long, so cap
   the width and keep the list scrollable rather than letting it run off-screen. */
.copy-menu{min-width:140px;max-width:240px;max-height:280px;overflow:auto}
.copy-menu .ins-item{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
/* one-shot confirmation banner after a cross-dialog copy */
.flash{position:sticky;bottom:10px;align-self:center;margin:8px auto 0;width:fit-content;
  background:#2C6B45;color:#fff;font-size:13px;border-radius:8px;padding:7px 14px;
  box-shadow:0 6px 18px rgba(0,0,0,.18);animation:flashin .15s ease-out}
@keyframes flashin{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:none}}
/* the cell a/b will target (last hovered) */
#stream .row:hover{box-shadow:inset 2px 0 0 var(--line)}
/* the selected cell in Jupyter-style command mode (Esc / j / k) */
#stream .row.selected{box-shadow:inset 3px 0 0 var(--accent);border-radius:10px;
  background:rgba(44,107,69,.04)}
.cell-btn.ctx.off{color:#B0784F;border-color:#E3C7AE;background:#FBF3E7}
.cell-btn.pin.on{color:#2C6B45;border-color:#BFE0CC;background:#E6F2EA}
.cell-btn.exp.on{color:#4A5BA6;border-color:#C3CBEB;background:#ECEFF9}
.tok{margin-left:2px;font-variant-numeric:tabular-nums}
/* cell number badge: a quiet gutter index the user can say "fix cell 3" by, and
   the same number the AI sees as n="…" in its context. */
.cell-num{color:var(--muted);font-size:11px;font-variant-numeric:tabular-nums;
  min-width:15px;text-align:right;user-select:none;flex:0 0 auto}
.row.muted .cell-num{opacity:.5}
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
.ctx-meter{margin:10px auto 4px;text-align:center;font-size:12px;color:var(--muted);
  border-top:1px dashed var(--line);padding-top:8px}
.answer{margin-top:5px}
/* An Ask AI cell = question + answer as ONE bordered card, so it's unmistakably a
   single cell (not two). A thin divider separates the question from the answer. */
#stream .row.prompt{border:1px solid var(--line);border-radius:14px;background:var(--panel);
  padding:7px 13px 9px}
#stream .row.prompt .prompt-view{font-weight:500;color:var(--ink)}     /* the question */
#stream .row.prompt .answer{margin-top:7px;border-top:1px solid var(--line);padding-top:7px}
#stream .row.prompt .answer .bubble{background:transparent;border:none;padding:0}  /* card bounds it */
#stream .row.prompt .answer .who .tag{border:none;padding:0;font-weight:600;color:var(--accent)}
.md>*:first-child{margin-top:0}.md>*:last-child{margin-bottom:0}
.md p{margin:.35em 0}.md ul,.md ol{margin:.35em 0;padding-left:1.4em}
.md h1,.md h2,.md h3{margin:.6em 0 .3em;line-height:1.3}
.md table{border-collapse:collapse;margin:.5em 0}.md th,.md td{border:1px solid var(--line);padding:4px 9px}
/* Non-runnable code inside an answer/note: light background (not the dark code-cell
   palette), so it's unmistakably illustrative rather than executable. Covers the
   plain-<pre> fallback when Pygments is unavailable. */
.md pre{background:var(--md-code-bg);color:var(--ink);border:1px solid var(--line);border-radius:10px;
  padding:12px 14px;overflow:auto;font-family:'SF Mono',ui-monospace,Menlo,monospace;font-size:13px}
/* Pygments-highlighted fenced code blocks in markdown (the .highlight div owns the bg) */
.md .highlight{border:1px solid var(--line);border-radius:10px;overflow:auto;margin:.5em 0;position:relative}
.md .highlight pre{background:transparent;margin:0;padding:12px 14px}
/* hover Copy button on code snippets (light theme, to match the light code bg) */
.copy-btn{position:absolute;top:7px;right:7px;font-size:11px;line-height:1.4;
  border:1px solid var(--line);background:rgba(250,249,245,.85);color:var(--muted);border-radius:6px;
  padding:2px 9px;cursor:pointer;opacity:0;transition:opacity .12s}
.md .highlight:hover .copy-btn,.copy-btn:focus{opacity:1}
.copy-btn:hover{color:var(--ink);border-color:var(--muted)}
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
    """A dialog row: the open link + a ⋯ menu (currently just Delete)."""
    return Div(
        A(label, href=f"/open?dialog={quote(full)}",
          cls=f"conv{' active' if full == active else ''}"),
        Details(
            Summary("⋯", cls="conv-dots", title="Dialog actions"),
            Div(Button("🗑  Delete", type="button", cls="conv-del", **{"data-dialog": full}),
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
SIDEBAR_JS = """
(function(){
  if(window.__sidebarSel) return; window.__sidebarSel = true;
  var sel = new Set(), anchor = null;
  function links(){ return Array.prototype.slice.call(
    document.querySelectorAll('.side .conv-row a.conv')); }
  function nameOf(a){ return decodeURIComponent(
    (a.getAttribute('href') || '').replace('/open?dialog=', '')); }
  function paint(){
    links().forEach(function(a){ a.closest('.conv-row').classList.toggle('sel', sel.has(nameOf(a))); });
    var bar = document.getElementById('selBar');
    if(bar){ bar.style.display = sel.size ? 'flex' : 'none';
             var c = document.getElementById('selCount');
             if(c) c.textContent = sel.size + ' selected'; }
  }
  function deleteDialogs(targets){
    if(!targets.length) return;
    if(!confirm('Delete ' + targets.length + ' dialog(s) and all their cells? This cannot be undone.')) return;
    var f = document.createElement('form'); f.method = 'POST'; f.action = '/dialog/delete-bulk';
    var i = document.createElement('input'); i.type = 'hidden'; i.name = 'names';
    i.value = JSON.stringify(targets);
    f.appendChild(i); document.body.appendChild(f); f.submit();
  }
  window.__clearDialogSel = function(){ sel.clear(); anchor = null; paint(); };
  window.__deleteDialogSel = function(){ deleteDialogs(Array.from(sel)); };
  document.addEventListener('click', function(e){
    // a row's ⋯ Delete: delete the whole selection if this row is part of it,
    // otherwise just this one.
    var del = e.target.closest && e.target.closest('.conv-del');
    if(del){
      e.preventDefault();
      var name = del.getAttribute('data-dialog');
      deleteDialogs(sel.size && sel.has(name) ? Array.from(sel) : [name]);
      return;
    }
    var a = e.target.closest && e.target.closest('.side .conv-row a.conv');
    if(!a || !(e.shiftKey || e.metaKey || e.ctrlKey)) return;   // plain click navigates
    e.preventDefault();
    var names = links().map(nameOf), nm = nameOf(a), i = names.indexOf(nm);
    if(e.shiftKey && anchor !== null){
      var lo = Math.min(anchor, i), hi = Math.max(anchor, i);
      for(var k = lo; k <= hi; k++) sel.add(names[k]);
    } else {
      if(sel.has(nm)) sel.delete(nm); else sel.add(nm);
      anchor = i;
    }
    paint();
  });
})();
"""


def Sidebar():
    backend = STATE["backend"]
    names = backend.list_dialogs() or [STATE["dialog"]]
    tree = _dialog_tree(names)
    sel_bar = Div(
        Span("0 selected", id="selCount", cls="sel-count"),
        Button("🗑 Delete", type="button", cls="sel-del", onclick="window.__deleteDialogSel()"),
        Button("Clear", type="button", cls="sel-clear", onclick="window.__clearDialogSel()"),
        id="selBar", cls="sel-bar", style="display:none")
    return Div(
        Div(Span("S", cls="dot"), "SolveIt Sidekick", cls="brand"),
        A("✎  New dialog", href="/new", cls="newbtn"),
        Div("Dialogs", cls="seclabel", title="Shift/⌘-click to select several, then Delete"),
        sel_bar,
        *_render_dialog_nodes(tree, STATE["dialog"]),
        Div(f"target: {STATE['target_name']}", cls="side-foot"),
        Script(SIDEBAR_JS),
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
    others = [d for d in (backend.list_dialogs() or []) if d != STATE["dialog"]]
    if not others or not hasattr(backend, "copy_cell"):
        return None
    items = [_stream_btn(d, "/cell/copy", cls="ins-item", vals={"id": mid, "target": d})
             for d in others]
    return _dropdown("⧉", Span("Copy to dialog", cls="ins-col-head"), *items,
                     title="Copy this cell into another dialog", menu_cls="type-menu copy-menu")


def _ctx_buttons(m):
    """Type / Insert / Copy / Export / Mute / Pin / Delete — in both rendered and edit
    modes. Code cells also get an Export toggle (the `#| export` package directive)."""
    mid = m.id
    exported = m.msg_type == "code" and export.has_export(m.content)
    btns = []
    if m.msg_type == "code":
        btns.append(_stream_btn(
            "Exported" if exported else "Export", "/cell/export",
            cls="cell-btn exp" + (" on" if exported else ""), vals={"id": mid},
            title="Toggle whether this cell is tangled into the exported package (#| export)"))
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


def _model_label(mid: str | None, default: str = "Claude (Max)") -> str:
    """Friendly label for a model id (e.g. 'claude-cli' -> 'Claude (Max)'). Falls
    back to the configured default's label so a model-less answer never mislabels."""
    if not mid:
        return default
    try:
        for m in list_models():
            if m["id"] == mid:
                return m["label"]
    except Exception:  # noqa: BLE001 — config issue: show the id rather than crash
        pass
    return mid


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
                    **{"data-stream-url": f"/stream?dialog={quote(STATE['dialog'])}&id={m.id}"}),
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
    js = _FOCUS_JS.replace("__MID__", f"ans-{mid}")
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
    return Div(_head(m, primary, num=num), _rendered_content(m), *_output_views(m),
               cls=_rowcls(m), id=f"cell-{m.id}")


def _cell_edit(m, num=None):
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
    js = (_CODE_EDITOR_JS if m.msg_type == "code" else _FOCUS_JS).replace("__MID__", mid)
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

# Cmd/Ctrl+/ toggles Python line comments on the selected lines, the way Jupyter
# (and most editors) do. Self-contained — we don't vendor CodeMirror's comment
# addon. Comments at the shallowest indentation of the block; if every non-blank
# line is already commented, it uncomments instead. Defined once; the cell and
# composer editors bind it in their extraKeys.
COMMENT_JS = """
window.__toggleComment = function(cm){
  cm.operation(function(){
    cm.listSelections().forEach(function(sel){
      var from = Math.min(sel.anchor.line, sel.head.line);
      var to   = Math.max(sel.anchor.line, sel.head.line);
      var lines = [];
      for(var i = from; i <= to; i++){ if(/\\S/.test(cm.getLine(i))) lines.push(i); }
      if(!lines.length) lines = [from];                 // act on a lone blank line too
      var commented = lines.every(function(i){ return /^\\s*#/.test(cm.getLine(i)); });
      var indent = Infinity;
      lines.forEach(function(i){ indent = Math.min(indent, cm.getLine(i).match(/^\\s*/)[0].length); });
      if(!isFinite(indent)) indent = 0;
      lines.forEach(function(i){
        if(commented){
          var m = cm.getLine(i).match(/^(\\s*)#( ?)/);  // strip the leading '# ' (or '#')
          if(m) cm.replaceRange('', {line:i, ch:m[1].length}, {line:i, ch:m[1].length + 1 + m[2].length});
        } else {
          cm.replaceRange('# ', {line:i, ch:indent});
        }
      });
    });
  });
};
"""

# Ctrl+Space completion: an async CodeMirror hint that asks /complete (which
# introspects the kernel's live namespace via jedi). Defined once on the page;
# the cell editor's extraKeys calls it. Best-effort — any failure shows nothing.
COMPLETE_JS = """
window.__kernelHint = function(cm, callback){
  var cur = cm.getCursor(), line = cm.getLine(cur.line);
  var startCh = cur.ch;                                   // start of the typed identifier
  while(startCh && /[A-Za-z0-9_]/.test(line.charAt(startCh - 1))) startCh--;
  var body = 'code=' + encodeURIComponent(cm.getValue()) +
             '&line=' + (cur.line + 1) + '&col=' + cur.ch;
  fetch('/complete', {method: 'POST',
      headers: {'Content-Type': 'application/x-www-form-urlencoded'}, body: body})
    .then(function(r){ return r.json(); })
    .then(function(data){
      var comps = (data && data.completions) || [];
      if(!comps.length){ callback(null); return; }
      callback({
        list: comps.map(function(c){ return {text: c.name, displayText: c.name}; }),
        from: CodeMirror.Pos(cur.line, startCh),
        to: CodeMirror.Pos(cur.line, cur.ch)
      });
    })
    .catch(function(){ callback(null); });   // never break typing
};
window.__kernelHint.async = true;            // tells CodeMirror it uses a callback

// Open the completion dropdown. Tab (and Enter) accept the highlighted item —
// the SolveIt convention. completeSingle:false so a lone match never auto-inserts
// while you're mid-word.
window.__showCompletions = function(cm){
  if(!window.__kernelHint) return;
  cm.showHint({
    hint: window.__kernelHint,
    completeSingle: false,
    extraKeys: { 'Tab': function(cm, handle){ handle.pick(); } }
  });
};

// Auto-trigger as you type (SolveIt's "dynamic autocomplete"). On a typed word
// char or a '.', open the dropdown after a short debounce — unless one is already
// open (it updates itself). Programmatic edits (e.g. accepting a hint) don't fire
// inputRead, so this never loops. Debounced to stay light over the H100 tunnel.
window.__autocompleteOnType = function(cm){
  cm.on('inputRead', function(cm, change){
    if(cm.state.completionActive) return;             // already open → it self-updates
    var ch = change.text && change.text[0];
    if(!ch || !/[\\w.]/.test(ch)) return;             // only identifier chars and '.'
    clearTimeout(cm.__hintTimer);
    cm.__hintTimer = setTimeout(function(){
      if(!cm.state.completionActive) window.__showCompletions(cm);
    }, 160);
  });
};
"""


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
  // No-op offline / before mermaid loads. Initialised once with manual start so
  // we control *when* it runs (after a render, never mid-stream on partial source).
  function renderMermaid(el){
    if(!el || !window.mermaid) return;
    if(!window.__mermaidInit){
      try { window.mermaid.initialize({ startOnLoad:false, securityLevel:'strict' }); } catch(e){}
      window.__mermaidInit = true;
    }
    var nodes = el.querySelectorAll('pre.mermaid:not([data-processed])');
    if(!nodes.length) return;
    try { var p = window.mermaid.run({ nodes: nodes }); if(p && p.catch) p.catch(function(){}); }
    catch(e){}
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
    if(window.__selCell && window.__selectCell) window.__selectCell(window.__selCell, false);
    if(window.__applyCollapsed) window.__applyCollapsed();     // re-fold sections after a swap
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
      var ans = e.target.closest('.answer');
      if(ans){
        var asave = ans.querySelector('.cell-btn.run');
        if(asave){ window.__selCell = cid; asave.click(); return; }
      }
      // A note (markdown) cell renders on Esc — same as Save — instead of just
      // dropping focus while it keeps showing the raw source. Code/prompt cells
      // only fall back to command mode (Jupyter never runs code on Esc).
      if(row.classList.contains('note')){
        var save = row.querySelector('.cell-btn.run');
        if(save){ window.__selCell = cid; save.click(); return; }  // keep it selected across the swap
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
    cost = cost_for(dialog if dialog is not None else STATE["dialog"])
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
    msgs = STATE["backend"].messages(STATE["dialog"])
    editing = STATE.pop("editing", None)            # a just-inserted cell opens in edit mode (one-shot)
    scroll_to = STATE.pop("scroll_to", None)        # scroll a just-added cell into view (one-shot)
    flash = STATE.pop("flash", None)                # transient confirmation banner (one-shot)
    if not msgs:
        inner = Div("Start the conversation — write code, ask the AI, or jot a note.",
                    cls="empty")
    else:
        rows = [_cell_edit(m, num=i) if m.id == editing else MsgRow(m, num=i)
                for i, m in enumerate(msgs, 1)]
        inner = Div(*rows, _ctx_meter(msgs), cls="wrap")
    extra = ()
    if scroll_to:
        # The #stream swap resets the scroll container to the top; bring the target
        # cell back into view. scrollIntoView (not scrollTop=scrollHeight) handles
        # both a just-added cell at the bottom (composer /send) and a re-run cell in
        # the middle (re-asking a prompt). rAF so it runs after layout settles.
        extra = (Script(f"requestAnimationFrame(function(){{var c="
                        f"document.getElementById('cell-{scroll_to}');"
                        f"if(c)c.scrollIntoView({{block:'center'}});}});"),)
    if flash:
        # A self-removing banner: fades after a couple seconds so a copy lands with
        # visible feedback even though the current dialog's cells don't change.
        extra = extra + (Div(flash, cls="flash", id="flash"),
                         Script("setTimeout(function(){var f=document.getElementById('flash');"
                                "if(f)f.remove();},2400);"))
    return Div(inner, Script(STREAM_JS), *extra, cls="stream", id="stream",
               **{"data-dialog": STATE["dialog"]})


COMPOSER_JS = """
var composerCM = null;   // CodeMirror instance while the composer is in Code mode

// Drop an immediate "Thinking…" wheel into the stream the instant you send an
// Ask-AI prompt — before any server round-trip. The htmx response then swaps all
// of #stream and replaces it: with the streaming answer (Claude Max) or the final
// answer (blocking API models, which otherwise showed no indicator at all). So a
// wheel is always visible while you wait, regardless of model or speed.
function _showPendingSpinner(){
  var stream = document.getElementById('stream');
  if(!stream || document.getElementById('pending-spinner')) return;
  var box = stream.querySelector('.wrap') || stream;
  var d = document.createElement('div');
  d.id = 'pending-spinner'; d.className = 'row';
  d.innerHTML = '<div class="answer"><div class="bubble md"><span class="thinking">' +
                '<span class="spinner"></span>Thinking…</span></div></div>';
  box.appendChild(d);
  stream.scrollTop = stream.scrollHeight;
}
// Re-running an existing Ask-AI cell posts to the server and then swaps #stream,
// but until that lands the stale answer just sits there. If the first streamed
// token arrives quickly the server-rendered spinner only flashes, so a re-ask
// reads as "no spinner". Drop a wheel into the cell's answer bubble the instant
// you click — the same optimistic feedback the composer gets. The #stream swap
// then renders its own identical spinner, so the handoff is seamless.
function _showCellSpinner(id){
  var cell = document.getElementById('cell-' + id);
  if(!cell) return;
  var spin = '<span class="thinking"><span class="spinner"></span>Thinking…</span>';
  var bubble = cell.querySelector('.answer .bubble');
  if(bubble){ bubble.className = 'bubble md'; bubble.innerHTML = spin; return; }
  var ans = document.createElement('div');     // never-answered prompt: add a bubble
  ans.className = 'answer';
  ans.innerHTML = '<div class="bubble md">' + spin + '</div>';
  cell.appendChild(ans);
}
function _submitComposer(){
  if(composerCM) composerCM.save();                 // flush editor -> textarea
  var ta = document.getElementById('composerInput');
  if(!ta || !ta.value.trim()) return;
  var isPrompt = (document.getElementById('msgType').value === 'prompt');
  // requestSubmit() fires the submit event so htmx posts and swaps just #stream
  // (no full-page reload). htmx serializes the form synchronously, so it's safe
  // to clear the composer right after.
  document.getElementById('composerForm').requestSubmit();
  if(isPrompt) _showPendingSpinner();               // instant feedback until the swap lands
  ta.value = '';
  if(composerCM) composerCM.setValue('');
}
// A successful /send swaps #stream and so removes the optimistic spinner; but if
// the request errors (no swap), clear the stray wheel so it can't hang forever.
if(!window.__pendingSpinnerCleanup){
  window.__pendingSpinnerCleanup = true;
  document.addEventListener('htmx:afterRequest', function(){
    var s = document.getElementById('pending-spinner'); if(s) s.remove();
  });
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
      'Cmd-/': function(){ window.__toggleComment(composerCM); },
      'Ctrl-/': function(){ window.__toggleComment(composerCM); },
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
// Shared by the paper-panel toolbar and the dialog-stream selection bubble: drop a
// quoted passage into the composer as an Ask-AI prompt (switches to prompt mode,
// which also tears down the Code editor so the textarea holds the quote).
window.__askComposer = function(text){
  setMode('prompt');
  var ta = document.getElementById('composerInput');
  if(!ta) return;
  var quote = String(text || '').split('\\n').map(function(l){ return '> ' + l; }).join('\\n');
  ta.value = quote + '\\n\\n';
  ta.focus(); ta.setSelectionRange(ta.value.length, ta.value.length);
  ta.scrollIntoView({block: 'center'});
};
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
      // One notebook convention for every cell type: plain Enter = newline,
      // Shift/Cmd/Ctrl+Enter = run/send (Ask AI, Code, and Note all match).
      if(e.shiftKey || e.metaKey || e.ctrlKey){ e.preventDefault(); _submitComposer(); }
    }
  });
  if(document.getElementById('msgType').value === 'code') _initComposerCM();  // sticky Code mode
  else ta.focus();
})();
"""


# A floating "Ask AI ↗" bubble over text selected in the dialog stream — the paper
# panel's selection toolbar, but for the conversation itself. Attaches once at the
# document level so it survives #stream htmx swaps, and reuses __askComposer.
STREAM_SEL_JS = """
(function(){
  if(window.__streamSel) return; window.__streamSel = true;
  var bar = null, curText = '';
  function hide(){ if(bar) bar.style.display = 'none'; }
  document.addEventListener('mouseup', function(ev){
    if(bar && ev.target && bar.contains(ev.target)) return;       // a click on the bubble itself
    var stream = document.getElementById('stream');
    var sel = window.getSelection();
    var text = sel ? sel.toString().trim() : '';
    if(!stream || !text || !sel.anchorNode || !stream.contains(sel.anchorNode)){ hide(); return; }
    // Don't intrude while editing a cell — CodeMirror / the textarea own their selection UX.
    var n = sel.anchorNode, el = n && (n.nodeType === 3 ? n.parentElement : n);
    if(el && el.closest && el.closest('.CodeMirror, .cell-edit')){ hide(); return; }
    if(!bar){
      bar = document.createElement('div'); bar.className = 'sel-tools';
      var b = document.createElement('button');
      b.className = 'sel-btn import'; b.textContent = 'Ask AI ↗';
      b.title = 'Drop the selected text into the composer as an Ask-AI question';
      b.addEventListener('mousedown', function(e){
        e.preventDefault();                                        // keep the selection alive
        if(window.__askComposer) window.__askComposer(curText);
        hide();
      });
      bar.appendChild(b);
      document.body.appendChild(bar);
    }
    curText = text;
    var r = sel.getRangeAt(0).getBoundingClientRect();
    bar.style.top = (window.scrollY + r.bottom + 6) + 'px';
    bar.style.left = (window.scrollX + r.left) + 'px';
    bar.style.display = 'flex';
  });
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
                     placeholder="Message SolveIt…  (Shift+Enter to send, Enter for newline)"),
            Div(
                Div(mode("prompt", "Ask AI"), mode("code", "Code"),
                    mode("note", "Note"), cls="modes", id="modeChips"),
                Div(ModelSelect(), Button("↑", cls="send", type="button",
                                          onclick="_submitComposer()"),
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
  // Scope to ONE #stream via getElementById: during an htmx outerHTML swap the
  // old #stream lingers briefly, and a '#stream …' selector matches headings
  // under BOTH, so the TOC would double. getElementById resolves a single node.
  var stream = document.getElementById('stream');
  var heads = stream ? stream.querySelectorAll(
    '.note-view h1,.note-view h2,.note-view h3,' +
    '.note-view h4,.note-view h5,.note-view h6') : [];
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
// Dim a toggle's icon when its panel is hidden, so what's collapsed is obvious
// at a glance — and one click on the dimmed icon brings the panel back.
window.syncToggles = function(){
  var app = document.querySelector('.app');
  if(!app) return;
  function set(id, hidden){
    var b = document.getElementById(id);
    if(b) b.classList.toggle('off', hidden);
  }
  set('tgl-side', app.classList.contains('no-side'));
  set('tgl-paper', app.classList.contains('no-paper'));
  set('tgl-toc', !app.classList.contains('toc-open'));
};
// Toggle a layout column class on .app and persist it; syncToggles() updates the
// matching icon's dimmed state. (TOC uses cls 'toc-open'; dialogs uses 'no-side'.)
window.toggleCol = function(cls, key){
  var app = document.querySelector('.app');
  if(!app) return;
  var on = app.classList.toggle(cls);
  try { localStorage.setItem(key, on ? '1' : '0'); } catch(e){}
  window.syncToggles();
};
(function(){
  var app = document.querySelector('.app');
  if(!app) return;
  function restore(key, cls, defOn){
    var v = null; try { v = localStorage.getItem(key); } catch(e){}
    if(v === null) v = defOn ? '1' : '0';
    app.classList.toggle(cls, v === '1');
  }
  restore('sidekick_toc', 'toc-open', true);          // TOC default open
  restore('sidekick_noside', 'no-side', false);       // dialogs default shown
  restore('sidekick_nopaper', 'no-paper', false);     // paper viewer default shown
  restore('sidekick_paperhidden', 'paper-collapsed', false);  // paper text default shown
  window.syncToggles();
  window.buildTOC();
})();
// ---- drag-to-resize columns ------------------------------------------------
(function(){
  var app = document.querySelector('.app');
  if(!app) return;
  // For each handle: the CSS var it drives, the column it sizes, and the sign of
  // the drag (side/paper sit left of their handle -> +dx widens; toc sits right
  // of its handle -> -dx widens). min/max clamp the resulting width in px.
  var SPEC = {
    side:  {v:'--side-w',  el:'.side',  sign: 1, min:170, max:520},
    paper: {v:'--paper-w', el:'.paper', sign: 1, min:220, max:900},
    toc:   {v:'--toc-w',   el:'.toc',   sign:-1, min:160, max:520}
  };
  var KEY = 'sidekick_colw';
  function load(){ try { return JSON.parse(localStorage.getItem(KEY)||'{}'); } catch(e){ return {}; } }
  function save(o){ try { localStorage.setItem(KEY, JSON.stringify(o)); } catch(e){} }
  var saved = load();
  Object.keys(SPEC).forEach(function(k){
    if(saved[k]) app.style.setProperty(SPEC[k].v, saved[k] + 'px');
  });
  var drag = null;
  document.addEventListener('mousedown', function(e){
    var g = e.target.closest && e.target.closest('.gutter');
    if(!g) return;
    var s = SPEC[g.getAttribute('data-resize')]; if(!s) return;
    var col = document.querySelector(s.el); if(!col) return;
    drag = {s:s, k:g.getAttribute('data-resize'), x:e.clientX,
            w:col.getBoundingClientRect().width, g:g};
    g.classList.add('dragging');
    document.body.classList.add('col-resizing');
    e.preventDefault();
  });
  document.addEventListener('mousemove', function(e){
    if(!drag) return;
    var w = drag.w + drag.s.sign * (e.clientX - drag.x);
    w = Math.max(drag.s.min, Math.min(drag.s.max, w));
    app.style.setProperty(drag.s.v, w + 'px');
  });
  document.addEventListener('mouseup', function(){
    if(!drag) return;
    var col = document.querySelector(drag.s.el);
    var o = load();
    o[drag.k] = Math.round(col.getBoundingClientRect().width);
    save(o);
    drag.g.classList.remove('dragging');
    document.body.classList.remove('col-resizing');
    drag = null;
  });
})();
"""


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
        Head(Title("SolveIt Sidekick"), *app.hdrs, Style(CSS)),
        Body(Div(
            # global top bar — above all columns, so its toggles stay reachable
            # even when the dialogs panel is hidden.
            Div(
                Div(Span("🗂", cls="gear tgl", id="tgl-side",
                         title="Show/hide the dialogs panel",
                         onclick="toggleCol('no-side','sidekick_noside')"),
                    # only meaningful once a paper is loaded
                    (Span("📖", cls="gear tgl", id="tgl-paper",
                          title="Show/hide the paper (PDF / markdown) viewer",
                          onclick="toggleCol('no-paper','sidekick_nopaper')")
                     if STATE.get("paper") else None),
                    Span("☰", cls="gear tgl toc-toggle", id="tgl-toc",
                         title="Show/hide the table of contents",
                         onclick="toggleCol('toc-open','sidekick_toc')"),
                    TitleEditor(),
                    cls="topbar-left"),
                Div(Details(
                        Summary("📄", cls="gear", title="Open a source — a PDF or a web page"),
                        Form(
                            Label("Choose a PDF…",
                                  Input(type="file", name="pdf", accept="application/pdf,.pdf",
                                        cls="paper-file",
                                        onchange="try{localStorage.removeItem('sidekick_nopaper')}catch(e){};this.form.submit()"),
                                  cls="src-file-label"),
                            Span("or a web page / blog", cls="ins-col-head"),
                            Input(name="url", type="url", placeholder="https://…", cls="src-url"),
                            Button("Open URL", cls="cell-btn run", type="submit"),
                            method="post", action="/paper/open", enctype="multipart/form-data",
                            cls="src-form",
                            onsubmit="try{localStorage.removeItem('sidekick_nopaper')}catch(e){}"),
                        cls="export"),
                    Details(Summary("⬇", cls="gear", title="Export this dialog"),
                            Div(A("Jupyter notebook (.ipynb)", href="/export/ipynb"),
                                A("Markdown (.md)", href="/export/md"),
                                A("Python package (.zip)", href="/export/package"),
                                cls="export-menu"),
                            cls="export"),
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
    Link(rel="stylesheet", href="/vendor/show-hint.min.css"),
    Script(src="/vendor/codemirror.min.js"),
    Script(src="/vendor/python.min.js"),
    Script(src="/vendor/placeholder.min.js"),
    Script(src="/vendor/show-hint.min.js"),     # Ctrl+Space completion dropdown
    Script(COMMENT_JS),                           # defines window.__toggleComment (Cmd/Ctrl+/)
    Script(COMPLETE_JS),                          # defines window.__kernelHint
    Script(STREAM_SEL_JS),                        # Ask-AI bubble over dialog-stream selections
    Link(rel="stylesheet", href="/vendor/katex.min.css"),
    Script(src="/vendor/katex.min.js"),
    Script(src="/vendor/auto-render.min.js"),
    Script(src="/vendor/mermaid.min.js"),         # ```mermaid → diagrams (renderMermaid)
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


def _delete_dialogs(targets: list[str]):
    """Delete each dialog in `targets`; if the active one is among them, fall back
    to a survivor (or a fresh demo/welcome if none remain)."""
    backend = STATE["backend"]
    if hasattr(backend, "delete_dialog"):
        for d in targets:
            backend.delete_dialog(d)
    if STATE["dialog"] in targets:
        remaining = backend.list_dialogs()
        STATE["dialog"] = remaining[0] if remaining else "demo/welcome"
        backend.messages(STATE["dialog"])       # touch -> ensure it exists


@rt("/dialog/delete", methods=["post"])
def dialog_delete(dialog: str):
    """Delete one dialog (the row's ⋯ menu)."""
    _delete_dialogs([dialog])
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
def send(content: str, msg_type: str = "prompt", model: str = None, htmx=None):
    content = (content or "").strip()
    if model:
        STATE["model"] = model           # remember last-used model
    if msg_type:
        STATE["msg_type"] = msg_type     # remember last-used compose mode
    if content:
        backend = STATE["backend"]
        use_model = STATE["model"] if msg_type == "prompt" else None
        m = backend.add(STATE["dialog"], content, msg_type, model=use_model)
        STATE["scroll_to"] = m.id            # render scrolls to the new cell
        if msg_type == "prompt" and _can_stream(backend, use_model):
            # Defer the AI call: the page renders an SSE-wired answer that streams
            # tokens in (the browser opens /stream), instead of blocking here.
            STATE["pending_stream"] = (STATE["dialog"], m.id)
        elif msg_type in ("code", "prompt"):
            backend.exec(STATE["dialog"], m.id)
    # The composer posts via htmx → swap just #stream (no full-page reload, so the
    # answer starts streaming sooner). A no-JS submit gets the whole page.
    return Stream() if (htmx and htmx.request) else Page()


def _msg_by_id(backend, dialog: str, mid: str):
    for m in backend.messages(dialog):
        if m.id == mid:
            return m
    return None


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
        backend.update(STATE["dialog"], id, content)
    m = _msg_by_id(backend, STATE["dialog"], id)
    if m is not None and m.msg_type == "prompt" and _can_stream(backend, m.model):
        STATE["pending_stream"] = (STATE["dialog"], id)   # re-ask, streamed live
        # The #stream swap resets the scroll container to the top, so bring the
        # re-run cell (and its "Thinking…" spinner) back into view — otherwise a
        # mid-notebook re-ask looks frozen because the spinner is below the fold.
        STATE["scroll_to"] = id
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
        STATE["cells_dirty"] = False                     # the AI's tools may flip this
        acc = ""
        for delta in stream_claude(dialog, m.content, context, model=m.model):
            acc += delta
            # str() unwraps NotStr -> raw (already-safe) markdown HTML for the data lines
            yield sse_message(str(render_md(acc)), event="msg")
        edited = STATE.get("cells_dirty", False)
        if not acc and edited:                           # tool-only turn: say something
            acc = "_Updated the notebook cells as requested._"
        m.output = acc or m.output
        if hasattr(backend, "_save"):
            backend._save()                              # flush to disk so a reload keeps the answer
        if STATE.get("pending_stream") == (dialog, id):
            STATE["pending_stream"] = None
        STATE["cells_dirty"] = False
        yield sse_message(str(render_md(m.output)), event="msg")   # final state
        # Refresh the foot-of-stream meter with this turn's accrued cost/usage
        # without a full reload (the client swaps just #ctxMeter's text).
        yield sse_message(_ctx_meter_text(backend.messages(dialog), dialog), event="cost")
        # 'reload' tells the client to refresh #stream so the AI's cell edits show.
        yield sse_message(Div("reload" if edited else ""), event="done")

    return StreamingResponse(gen(), media_type="text/event-stream")


# ---- internal API for the AI's cell-editing MCP tools (server/mcp_cells) -----
# Loopback + shared-token only. The MCP server (spawned by `claude -p`) calls
# these to read and edit the live notebook; mutating routes flag #stream dirty so
# the streamed answer's `done` event reloads it.
def _mcp_ok(tok: str) -> bool:
    want = STATE.get("mcp_token")
    return bool(want) and tok == want


def _json(obj, status: int = 200):
    from starlette.responses import JSONResponse
    return JSONResponse(obj, status_code=status)


@rt("/internal/cells")
def internal_cells(dialog: str, tok: str = ""):
    """List a dialog's cells for the AI (id, type, source)."""
    if not _mcp_ok(tok):
        return _json({"ok": False, "error": "forbidden"}, 403)
    backend = STATE["backend"]
    cells = [{"id": m.id, "type": m.msg_type, "content": m.content or "",
              "output": m.output or ""} for m in backend.messages(dialog)]
    return _json({"ok": True, "cells": cells})


@rt("/internal/cell/update", methods=["post"])
def internal_cell_update(dialog: str, id: str, content: str = "", tok: str = ""):
    if not _mcp_ok(tok):
        return _json({"ok": False, "error": "forbidden"}, 403)
    backend = STATE["backend"]
    if not hasattr(backend, "update") or _msg_by_id(backend, dialog, id) is None:
        return _json({"ok": False, "error": f"no cell {id}"}, 404)
    backend.update(dialog, id, content)
    STATE["cells_dirty"] = True
    return _json({"ok": True, "message": f"updated cell {id}"})


@rt("/internal/cell/str_replace", methods=["post"])
def internal_cell_str_replace(dialog: str, id: str, old: str = "", new: str = "", tok: str = ""):
    if not _mcp_ok(tok):
        return _json({"ok": False, "error": "forbidden"}, 403)
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
    STATE["cells_dirty"] = True
    return _json({"ok": True, "message": f"edited cell {id}"})


@rt("/internal/cell/insert", methods=["post"])
def internal_cell_insert(dialog: str, content: str = "", cell_type: str = "code",
                         after_id: str = "", tok: str = ""):
    if not _mcp_ok(tok):
        return _json({"ok": False, "error": "forbidden"}, 403)
    if cell_type not in ("code", "note", "prompt"):
        return _json({"ok": False, "error": "bad cell_type"}, 400)
    backend = STATE["backend"]
    if after_id and hasattr(backend, "insert") and _msg_by_id(backend, dialog, after_id):
        m = backend.insert(dialog, content, cell_type, after_id, above=False)
    else:
        m = backend.add(dialog, content, cell_type)
    STATE["cells_dirty"] = True
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
            comps = backend.complete(STATE["dialog"], code, int(line), int(col))
        except Exception:  # noqa: BLE001
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
        backend.update(STATE["dialog"], id, content)
    return Stream()


@rt("/cell/answer/edit")
def cell_answer_edit(id: str):
    """Swap a prompt's AI answer into edit mode (textarea over its output)."""
    bk, d = STATE["backend"], STATE["dialog"]
    m = _msg_by_id(bk, d, id)
    if m is None or m.msg_type != "prompt" or not _can_edit_answer():
        return Stream()
    return _answer_edit(m)


@rt("/cell/answer/view")
def cell_answer_view(id: str):
    """Swap a prompt's AI answer back to its rendered view — used by Cancel."""
    bk, d = STATE["backend"], STATE["dialog"]
    m = _msg_by_id(bk, d, id)
    return _answer_view(m) if m is not None and m.msg_type == "prompt" else Stream()


@rt("/cell/answer/save", methods=["post"])
def cell_answer_save(id: str, output: str = ""):
    """Persist an edited AI answer in place (no re-ask), then render it read-only.
    Only prompt cells have an editable answer, so other types are left untouched."""
    bk, d = STATE["backend"], STATE["dialog"]
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
        backend.delete(STATE["dialog"], id)
    return Stream()


@rt("/cell/type", methods=["post"])
def cell_type(id: str, msg_type: str = "code"):
    """Convert a cell to another type in place (y=code, m=note, i=prompt)."""
    backend = STATE["backend"]
    if hasattr(backend, "set_type"):
        backend.set_type(STATE["dialog"], id, msg_type)
    return Stream()


@rt("/cell/undo", methods=["post"])
def cell_undo():
    """Restore the last deleted cell in this dialog (Jupyter's 'z')."""
    backend = STATE["backend"]
    if hasattr(backend, "undo"):
        backend.undo(STATE["dialog"])
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


@rt("/cell/export", methods=["post"])
def cell_export(id: str):
    """Toggle this code cell's `#| export` directive (whether it's tangled into the package)."""
    backend = STATE["backend"]
    m = _msg_by_id(backend, STATE["dialog"], id)
    if m is not None and m.msg_type == "code":
        new = export.toggle_export(m.content)
        if isinstance(backend, _InMemoryBackend):
            m.content = new                  # a directive is a no-op comment — keep the cell's output
        elif hasattr(backend, "update"):
            backend.update(STATE["dialog"], id, new)
    return Stream()


@rt("/export/package")
def export_package():
    """Tangle the current dialog's `#| export` cells into a downloadable package zip."""
    dialog = STATE["dialog"]
    msgs = STATE["backend"].messages(dialog)
    pkg = export.slug(dialog)
    files = export.dialog_to_package(msgs, dialog, dialog_name=dialog)
    blob = export.package_zip(files, pkg)
    return _download(blob, f"{pkg}.zip", "application/zip")


@rt("/cell/edit")
def cell_edit(id: str):
    """Swap a single cell into edit mode (raw textarea)."""
    bk, d = STATE["backend"], STATE["dialog"]
    m = _msg_by_id(bk, d, id)
    return _cell_edit(m, num=_cell_number(bk, d, id)) if m else Stream()


@rt("/cell/view")
def cell_view(id: str):
    """Swap a single cell back to its rendered (read-only) view — used by Cancel."""
    bk, d = STATE["backend"], STATE["dialog"]
    m = _msg_by_id(bk, d, id)
    return MsgRow(m, num=_cell_number(bk, d, id)) if m else Stream()


@rt("/cell/exec", methods=["post"])
def cell_exec(id: str):
    """Re-run a cell's stored source without editing (the rendered-view Run/Ask)."""
    backend = STATE["backend"]
    m = _msg_by_id(backend, STATE["dialog"], id)
    if m is not None and m.msg_type == "prompt" and _can_stream(backend, m.model):
        m.output = ""                                     # clear stale answer to re-stream
        STATE["pending_stream"] = (STATE["dialog"], id)   # re-ask, streamed live
        # The #stream swap resets the scroll container to the top, so a mid-notebook
        # re-run looks frozen — the "Thinking…" spinner is below the fold. Bring the
        # re-run cell back into view (matches /cell/run).
        STATE["scroll_to"] = id
    elif m is not None:
        backend.exec(STATE["dialog"], id)
    return Stream()


@rt("/cell/insert", methods=["post"])
def cell_insert(id: str, msg_type: str = "code", where: str = "below"):
    """Insert a new (empty) cell above/below `id` and open it in edit mode."""
    backend = STATE["backend"]
    if msg_type not in ("code", "note", "prompt"):
        msg_type = "code"
    if hasattr(backend, "insert"):
        m = backend.insert(STATE["dialog"], "", msg_type, anchor_id=id,
                           above=(where == "above"))
        STATE["editing"] = m.id
    return Stream()


@rt("/cell/copy", methods=["post"])
def cell_copy(id: str, target: str = ""):
    """Copy a cell into another dialog (appended at its end). We stay in the current
    dialog — the stream re-renders unchanged but for a one-shot flash confirming where
    the copy landed."""
    backend = STATE["backend"]
    if target and target != STATE["dialog"] and hasattr(backend, "copy_cell"):
        m = backend.copy_cell(STATE["dialog"], id, target)
        STATE["flash"] = f"Copied cell to “{target}”" if m else "Couldn't copy that cell."
    return Stream()


@rt("/cell/split", methods=["post"])
def cell_split(id: str):
    """Split-to-code (SolveIt's `W`): take the fenced code blocks from a prompt's
    answer and insert them as runnable code cells just below it, in order. The
    cells aren't auto-run — the user still runs each one (small-steps contract)."""
    backend = STATE["backend"]
    m = _msg_by_id(backend, STATE["dialog"], id)
    if m is not None and m.msg_type == "prompt" and hasattr(backend, "insert"):
        anchor, last = id, None
        for lang, code in _answer_code_blocks(m.output):
            # mermaid isn't kernel code — land it in a note, which renders the fence
            # as a diagram; everything else becomes a runnable code cell.
            if lang == "mermaid":
                last = backend.insert(STATE["dialog"], f"```mermaid\n{code}\n```",
                                      "note", anchor_id=anchor)
            else:
                last = backend.insert(STATE["dialog"], code, "code", anchor_id=anchor)
            anchor = last.id
        if last is not None:
            STATE["scroll_to"] = last.id
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


def _convert_paper_async(path: str, name: str | None = None):
    """Convert a PDF in a background thread (marker can take a while), updating
    STATE['paper'] from 'converting' to 'ready'/'error'. The panel polls."""
    name = name or os.path.basename(path)
    STATE["paper"] = {"name": name, "status": "converting"}

    def work():
        try:
            md, engine = paperlib.convert(path)
            STATE["paper"] = {"name": name, "status": "ready", "md": md, "engine": engine}
        except Exception as e:  # noqa: BLE001 — surface conversion failures in the panel
            STATE["paper"] = {"name": name, "status": "ready", "md": f"Could not open: {e}",
                              "engine": "error"}

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


def _convert_url_async(url: str, name: str):
    """Fetch + convert a web page to markdown in a background thread (same panel
    lifecycle as a PDF: 'converting' → 'ready'/'error', polled by the panel)."""
    STATE["paper"] = {"name": name, "status": "converting", "source": url}

    def work():
        try:
            md, engine = paperlib.convert_url(url)
            if md.strip():
                STATE["paper"] = {"name": name, "status": "ready", "md": md,
                                  "engine": engine, "source": url}
            else:
                STATE["paper"] = {"name": name, "status": "ready", "engine": "error",
                                  "md": f"**Couldn't extract anything from** `{url}`"}
        except Exception as e:  # noqa: BLE001 — surface fetch/extract failures in the panel
            STATE["paper"] = {"name": name, "status": "ready", "engine": "error",
                              "md": f"Could not open `{url}`:\n\n```\n{e}\n```"}

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


@rt("/paper/open", methods=["post"])
def paper_open(pdf: UploadFile = None, path: str = "", url: str = ""):
    up = _save_upload(pdf)                       # an uploaded file takes precedence
    if up:
        _convert_paper_async(*up)
        return Page()
    if url.strip():                              # a web page / blog URL
        u = url.strip()
        _convert_url_async(u, _url_name(u))
        return Page()
    if path.strip():                             # a local file path (kept for callers/tests)
        src = os.path.expanduser(path.strip())
        if not os.path.exists(src):
            STATE["paper"] = {"name": os.path.basename(src) or src, "status": "ready",
                              "engine": "error", "md": f"**File not found:** `{src}`"}
        else:
            _convert_paper_async(src, os.path.basename(src))
    return Page()


@rt("/paper/status")
def paper_status():
    return PaperPanel()


@rt("/paper/close")
def paper_close():
    STATE["paper"] = None
    return Page()


def _safe_name(s: str) -> str:
    s = "".join(c if (c.isalnum() or c in "-_") else "-" for c in s).strip("-").lower()
    return s or "paper"


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
        base = os.path.splitext(p.get("name", "paper"))[0]
        dialog = _unique_dialog(backend, f"paper/{_safe_name(base)}")
        backend.messages(dialog)
        p["dialog"] = dialog
    return dialog


def _append_passage(p: dict, text: str):
    """Add a passage to the paper's dialog as a note + an empty code cell to
    reimplement it, then open that code cell focused. Shared by the section
    stepper and the highlight-to-import flow."""
    backend = STATE["backend"]
    dialog = _paper_dialog(backend, p)
    backend.add(dialog, text, "note")            # the passage to read…
    code = backend.add(dialog, "", "code")       # …and a cell to reimplement it
    STATE["dialog"] = dialog
    STATE["editing"] = code.id                   # open the code cell, focused & in view


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
    base = os.path.splitext(p.get("name", "paper"))[0]
    name = _unique_dialog(backend, f"paper/{_safe_name(base)}")
    backend.messages(name)                       # create the dialog
    for c in chunks:
        backend.add(name, c, "note")
    STATE["dialog"] = name
    STATE["paper"] = None                        # it's in the notebook now; close the panel
    return Page()


if __name__ == "__main__":
    serve()
