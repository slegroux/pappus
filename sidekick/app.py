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

from fasthtml.common import *

from .targets import get_target, list_targets, list_models, default_model
from .client import connect
from . import secrets_store


# ---- markdown (server-side; works offline, no CDN) --------------------------
try:
    import mistune
    # escape=True neutralises raw HTML in the source, so rendering a note or an
    # AI answer can't inject <script> — markdown syntax still renders.
    _md = mistune.create_markdown(escape=True, plugins=["strikethrough", "table"])
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
}


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
.main{display:flex;flex-direction:column;min-width:0}
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
.row:hover .cell-actions,.cell-actions:focus-within{opacity:1}
.cell-btn{font-size:12px;border:1px solid var(--line);background:#fff;border-radius:7px;padding:3px 11px;
  cursor:pointer;color:var(--muted);line-height:1.6}
.cell-btn:hover{border-color:#d4d0c4;color:var(--ink)}
.cell-btn.run{background:var(--accent);color:#fff;border-color:var(--accent)}
.cell-btn.run:hover{filter:brightness(1.05);color:#fff}
.cell-btn.del:hover{border-color:#C0584B;color:#C0584B}
.answer{margin-top:9px}
.md>*:first-child{margin-top:0}.md>*:last-child{margin-bottom:0}
.md p{margin:.5em 0}.md ul,.md ol{margin:.5em 0;padding-left:1.4em}
.md h1,.md h2,.md h3{margin:.7em 0 .35em;line-height:1.3}
.md table{border-collapse:collapse;margin:.5em 0}.md th,.md td{border:1px solid var(--line);padding:4px 9px}
.md pre{background:var(--code-bg);color:var(--code-ink);border-radius:10px;padding:12px 14px;overflow:auto;
  font-family:'SF Mono',ui-monospace,Menlo,monospace;font-size:13px}
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


def _cell_actions(mid, run_label="Run", run_path="/cell/run"):
    """Per-cell toolbar. Run posts the (possibly edited) source; Delete removes it.
    Both swap only #stream so running a cell never reloads the whole page."""
    return Div(
        Button(run_label, type="button", cls="cell-btn run",
               hx_post=run_path, hx_include=f"#ta-{mid}",
               hx_vals=json.dumps({"id": mid}),
               hx_target="#stream", hx_swap="outerHTML"),
        Button("Delete", type="button", cls="cell-btn del",
               hx_post="/cell/delete", hx_vals=json.dumps({"id": mid}),
               hx_confirm="Delete this cell?",
               hx_target="#stream", hx_swap="outerHTML"),
        cls="cell-actions",
    )


def MsgRow(m):
    mid = m.id
    if m.msg_type == "note":
        head = Div(Span("note", cls="tag"),
                   _cell_actions(mid, run_label="Save", run_path="/cell/save"), cls="who")
        body = [head, _cell_textarea(m)]
        if (m.content or "").strip():            # rendered markdown preview
            body.append(Div(render_md(m.content), cls="bubble note md"))
        return Div(*body, cls="row", id=f"cell-{mid}")
    if m.msg_type == "code":
        head = Div(Span("code", cls="tag"), Span(mid, cls="muted small"),
                   _cell_actions(mid), cls="who")
        body = [head, _cell_textarea(m, code=True)]
        if m.output:
            body.append(Div(m.output, cls="out"))
        return Div(*body, cls="row", id=f"cell-{mid}")
    # prompt -> editable question + AI answer (labelled with the model used)
    head = Div(Span("Ask AI", cls="tag"), _cell_actions(mid, run_label="Ask"), cls="who")
    body = [head, _cell_textarea(m)]
    if m.output:
        who = m.model or "SolveIt AI"
        body.append(Div(Div(Span(who, cls="tag"), cls="who"),
                        Div(render_md(m.output), cls="bubble md"), cls="answer"))
    return Div(*body, cls="row", id=f"cell-{mid}")


STREAM_JS = """
(function(){
  // CSS field-sizing auto-grows textareas natively; only run the JS fallback
  // (measured after layout settles) where it isn't supported.
  var hasFieldSizing = window.CSS && CSS.supports && CSS.supports('field-sizing','content');
  function autosize(el){ el.style.height='auto'; el.style.height=el.scrollHeight+'px'; }
  function sizeAll(){ document.querySelectorAll('.cell-edit').forEach(autosize); }
  if(!hasFieldSizing) requestAnimationFrame(sizeAll);           // wait for final width

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


def Stream():
    msgs = STATE["backend"].messages(STATE["dialog"])
    if not msgs:
        inner = Div("Start the conversation — write code, ask the AI, or jot a note.",
                    cls="empty")
    else:
        inner = Div(*[MsgRow(m) for m in msgs], cls="wrap")
    return Div(inner, Script(STREAM_JS), cls="stream", id="stream")


COMPOSER_JS = """
function setMode(v){
  document.getElementById('msgType').value = v;
  document.querySelectorAll('#modeChips .mode').forEach(function(el){
    el.classList.toggle('sel', el.getAttribute('data-val') === v);
  });
}
(function(){
  var ta = document.getElementById('composerInput');
  if(!ta) return;
  ta.focus();
  ta.addEventListener('keydown', function(e){
    if(e.key === 'Enter' && !e.shiftKey){
      e.preventDefault();
      if(ta.value.trim()) document.getElementById('composerForm').submit();
    }
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
        Head(Title("Settings · SolveIt Sidekick"), Style(CSS),
             Meta(name="viewport", content="width=device-width, initial-scale=1")),
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


def Page():
    banner = (Div("⚠ ", STATE["warning"], " — showing a mock so you can still explore the UI.",
                  cls="banner") if STATE["warning"] else None)
    return Html(
        Head(Title("SolveIt Sidekick"), Style(CSS),
             Meta(name="viewport", content="width=device-width, initial-scale=1")),
        Body(Div(
            Sidebar(),
            Div(
                Div(TitleEditor(),
                    Div(A("⚙", href="/settings", cls="gear", title="Settings — API keys"),
                        TargetSwitcher(),
                        style="display:flex;align-items:center;gap:12px"),
                    cls="topbar"),
                banner,
                Stream(),
                Composer(),
                cls="main",
            ),
            cls="app",
        )),
    )


# ---- routes -----------------------------------------------------------------
app, rt = fast_app(pico=False)


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
        if msg_type in ("code", "prompt"):
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
    if m is not None and m.msg_type in ("code", "prompt"):
        backend.exec(STATE["dialog"], id)
    return Stream()


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


if __name__ == "__main__":
    serve()
