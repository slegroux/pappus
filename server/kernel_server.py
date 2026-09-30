"""A lightweight, self-hostable SolveIt-compatible kernel server.

This is NOT Answer.AI's proprietary SolveIt server — it's a minimal stand-in you
can actually run on your laptop or H100 so the Pappus interface has a *real*
backend: it executes Python code in a persistent per-dialog namespace and returns
output, just like a notebook kernel. Prompts get a routed (stubbed, or real if an
API key is present) AI reply.

Auth: on loopback with no --token the server is open (local dev). Off-loopback
it refuses to start without --token, and then every request must carry a matching
`_solveit` cookie — because /exec runs arbitrary code.

Endpoints (simple JSON):
    GET  /test_route            -> "here"          (what doctor/solveit_client probe)
    GET  /health                -> {ok, dialogs}
    POST /exec     {dialog,code}                    -> {output, rich}
    POST /exec_start {dialog,code}                  -> {run_id}          (async)
    GET  /exec_poll?dialog=&run_id=                 -> {output,done,rich,error,interrupted}
    POST /exec_stop  {dialog,run_id}                -> {ok, done}        (KeyboardInterrupt)
    POST /complete {dialog,code,line,col}           -> {completions}
    POST /prompt   {dialog,content,model,context}   -> {output, model}
    POST /eval     {dialog,content}                 -> {content, warnings}
    POST /reset    {dialog}                 -> {ok}

Run:  python -m server.kernel_server --port 5001        # laptop ('local' target)
      python -m server.kernel_server --port 5001        # on the H100, then tunnel
Stdlib only — no dependencies.
"""
from __future__ import annotations

import argparse
import ast
import ctypes
import hmac
import io
import json
import os
import re
import contextlib
import threading
import warnings
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from pappus import claude_cli   # CLI (subscription) Claude + shared notebook preamble
from pappus import codex_cli    # installed Codex CLI path (current local auth/config)

# Request auth. main() sets _AUTH_TOKEN: when it's not None, every request must
# carry a matching `_solveit` cookie (this is what makes binding off-loopback
# safe — /exec runs arbitrary code). On loopback with no token it stays open.
_AUTH_TOKEN: str | None = None


def _dbg(msg) -> None:
    """Print a diagnostic to stderr, but only when PAPPUS_DEBUG is truthy.

    The kernel deliberately swallows errors around exec/capture so a bad cell
    can't take the server down; that makes failures invisible. Set PAPPUS_DEBUG
    to surface them. With the flag unset this is a no-op — default output is
    byte-for-byte unchanged."""
    if os.environ.get("PAPPUS_DEBUG"):
        import sys
        print(f"[pappus-kernel] {msg}", file=sys.stderr)


def _is_loopback(host: str) -> bool:
    return host in ("127.0.0.1", "localhost", "::1", "")


def _token_from_cookie(cookie_header: str) -> str:
    c = SimpleCookie()
    try:
        c.load(cookie_header or "")
    except Exception:  # noqa: BLE001 — a malformed cookie is just no token
        return ""
    m = c.get("_solveit")
    return m.value if m else ""


def _check_auth(expected: str | None, cookie_header: str) -> bool:
    if expected is None:
        return True                                  # open (loopback dev, no token)
    return hmac.compare_digest(_token_from_cookie(cookie_header), expected)

# Per-dialog execution namespaces — this is the "kernel" state.
KERNELS: dict[str, dict] = {}

# Serialize work per dialog. The server is a ThreadingHTTPServer, so two requests
# for the SAME dialog (two /exec, or /exec racing /complete or /reset) would run
# against one shared namespace dict simultaneously — interleaved exec corrupts
# state, and iterating the namespace (completion, the var list in run_prompt) while
# another thread mutates it raises RuntimeError. Each dialog gets its own lock;
# _LOCKS_GUARD guards only the registry lookup in _lock_for and is released before
# any per-dialog lock is held (never nested), so no lock-ordering cycle exists.
# _dialog_locks sorts+dedups its dialogs so concurrent multi-dialog ops (/rename
# a↔b) acquire in the same order and can't deadlock.
_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def _lock_for(dialog: str) -> threading.Lock:
    with _LOCKS_GUARD:
        lk = _LOCKS.get(dialog)
        if lk is None:
            lk = threading.Lock()
            _LOCKS[dialog] = lk
        return lk


@contextlib.contextmanager
def _dialog_locks(*dialogs: str):
    """Hold the per-dialog lock(s) for `dialogs`. Deduped and sorted so two
    concurrent multi-dialog ops (e.g. /rename a↔b) acquire in the same order and
    never deadlock."""
    locks = [_lock_for(d) for d in sorted(set(dialogs))]
    for lk in locks:
        lk.acquire()
    try:
        yield
    finally:
        for lk in reversed(locks):
            lk.release()


def _modname(dialog: str) -> str:
    """The sys.modules key backing a dialog's namespace (see _ns). Deterministic so
    /reset can drop the exact module it created."""
    slug = "".join(c if c.isalnum() else "_" for c in dialog).strip("_") or "default"
    return f"__solveit_{slug}__"


def _ns(dialog: str) -> dict:
    ns = KERNELS.get(dialog)
    if ns is None:
        # Back the namespace with a REAL module registered in sys.modules, the way
        # IPython/Jupyter do. User code then runs under __name__ = this module's
        # name, so anything that resolves sys.modules[cls.__module__] — dataclasses,
        # typing.get_type_hints, pickle, attrs — finds a live module with a real
        # __dict__. Without this, @dataclass eventually raises "'NoneType' object
        # has no attribute '__dict__'" because the import machinery leaves a None
        # at that key, and dataclasses does sys.modules[cls.__module__].__dict__.
        # The per-dialog module name also keeps dialogs from sharing globals.
        import sys, types
        modname = _modname(dialog)
        mod = types.ModuleType(modname)
        sys.modules[modname] = mod
        ns = mod.__dict__
        KERNELS[dialog] = ns
    return ns


def _b64(data: bytes) -> str:
    import base64
    return base64.b64encode(data).decode()


def _capture_figs() -> list:
    """Grab any open matplotlib figures as PNGs, then clear them (inline-plot
    behavior). Returns [] when matplotlib isn't even imported, so there's no cost
    for non-plotting code.

    KNOWN LIMITATION (not fixed by the per-dialog exec lock): pyplot's figure
    registry is process-global, so two dialogs plotting concurrently can cross
    figures — this reads whatever figures are open process-wide, not just this
    dialog's. The per-dialog lock in run_code serializes same-dialog execs but
    cannot isolate the global pyplot state across different dialogs. Fixing it
    needs per-dialog figure isolation, out of scope for the lock change."""
    import sys
    plt = sys.modules.get("matplotlib.pyplot")
    if plt is None:
        return []
    out = []
    for num in plt.get_fignums():
        bio = io.BytesIO()
        try:
            plt.figure(num).savefig(bio, format="png", bbox_inches="tight")
            out.append({"type": "image/png", "data": _b64(bio.getvalue())})
        except Exception as e:  # noqa: BLE001 — a bad figure shouldn't break the cell
            _dbg(f"_capture_figs: figure {num} failed: {type(e).__name__}: {e}")
    plt.close("all")
    return out


def _rich_repr(val) -> dict | None:
    """Map a value to a rich output via the IPython display protocol.

    Covers DataFrames (_repr_html_), PIL/other images (_repr_png_/_repr_jpeg_).
    matplotlib Figures are handled by _capture_figs, so skip them here.
    """
    if type(val).__module__.startswith("matplotlib"):
        return None
    # Audio first: a carrier from pappus.audio.play() advertises a WAV via
    # _repr_audio_wav_, returning a complete base64-encoded WAV file (the kernel
    # side of the audio contract). Checked before the image/html branches.
    fn = getattr(val, "_repr_audio_wav_", None)
    if callable(fn):
        b64 = fn()
        if b64:
            return {"type": "audio/wav", "data": b64}
    for meth, mime in (("_repr_png_", "image/png"), ("_repr_jpeg_", "image/jpeg")):
        fn = getattr(val, meth, None)
        if callable(fn):
            data = fn()
            if data:
                return {"type": mime, "data": data if isinstance(data, str) else _b64(data)}
    # SVG before HTML: an object offering both (e.g. a vector figure) is sharper
    # as inline SVG than as an <img>. svg markup is text, so it's passed through.
    fn = getattr(val, "_repr_svg_", None)
    if callable(fn):
        svg = fn()
        if svg:
            return {"type": "image/svg+xml", "data": svg}
    fn = getattr(val, "_repr_html_", None)
    if callable(fn):
        html = fn()
        if html:
            return {"type": "text/html", "data": html}
    return None


def complete_code(dialog: str, code: str, line: int, col: int) -> list:
    """Jupyter-style completions for `code` at (line, col), against the dialog's
    live namespace. jedi introspects the actual objects in the kernel — so after
    you run `import numpy as np`, `np.ar` offers `arange`; `df.` offers columns —
    without executing the code being typed. `line` is 1-based, `col` 0-based
    (jedi's convention). Returns [] if jedi isn't installed or anything goes wrong
    (completion is best-effort; never break typing)."""
    try:
        import jedi
    except ImportError:
        return []
    try:
        with _dialog_locks(dialog):     # don't introspect a namespace mid-exec
            script = jedi.Interpreter(code, namespaces=[_ns(dialog)])
            comps = script.complete(line, col)
    except Exception:  # noqa: BLE001 — jedi can raise on odd partial source
        return []
    out = []
    for c in comps[:50]:                     # cap: a dropdown needs only so many
        out.append({"name": c.name, "type": c.type})
    return out


def _split_last_expr(code: str, tree: ast.Module):
    """A trailing ';' suppresses the last expression's value, Jupyter-style. The
    statement still runs (side effects, plots) — only its repr is hidden. Returns
    the last-expression AST to eval for its repr, or None. Mutates `tree` (pops the
    trailing Expr) so the exec body no longer double-evaluates it."""
    suppress = code.rstrip().endswith(";")
    if not suppress and tree.body and isinstance(tree.body[-1], ast.Expr):
        return ast.Expression(tree.body.pop().value)
    return None


def _execute(dialog: str, code: str, buf: io.StringIO, rich_out: list) -> str | None:
    """Core execution shared by the sync (`run_code`) and async (`exec_start`)
    paths. Parses `code`, execs it into the dialog namespace, evals a trailing
    expression for its repr, and captures matplotlib figures — writing text output
    incrementally into `buf` (so a poller can see partial output) and rich outputs
    into `rich_out`.

    MUST be called while holding `_dialog_locks(dialog)`. Returns an error string
    (or None on success). A KeyboardInterrupt (the async /exec_stop path) is NOT
    caught here — it propagates to the caller so the async worker can mark the run
    interrupted; this matches the original run_code, which never caught it either.
    """
    try:
        tree = ast.parse(code, mode="exec")
    except SyntaxError as e:
        buf.write(f"SyntaxError: {e}")
        return f"SyntaxError: {e}"
    last_expr = _split_last_expr(code, tree)
    ns = _ns(dialog)
    err = None
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf), \
                warnings.catch_warnings():
            # plt.show() is a no-op under the headless Agg backend, but matplotlib
            # still warns "FigureCanvasAgg is non-interactive…" to stderr — pure
            # noise here, since _capture_figs() renders the figure inline anyway.
            warnings.filterwarnings("ignore", message="FigureCanvasAgg is non-interactive")
            exec(compile(tree, "<dialog>", "exec"), ns)
            if last_expr is not None:
                val = eval(compile(last_expr, "<dialog>", "eval"), ns)
                if val is not None:
                    r = _rich_repr(val)
                    if r:
                        rich_out.append(r)
                    else:
                        print(repr(val), file=buf)
    except Exception as e:  # noqa: BLE001 — surface kernel errors as output
        print(f"{type(e).__name__}: {e}", file=buf)
        _dbg(f"_execute: exec/eval raised: {type(e).__name__}: {e}")
        err = f"{type(e).__name__}: {e}"
    rich_out[:0] = _capture_figs()         # plots created during the cell, newest cell-state
    return err


def run_code(dialog: str, code: str) -> tuple[str, list]:
    """Execute code in the dialog's namespace (synchronous, blocking).

    Returns (text, rich) where text is stdout/stderr plus the last expression's
    repr, and rich is a list of MIME-typed outputs (plots, images, dataframes).
    """
    buf = io.StringIO()
    rich: list = []
    with _dialog_locks(dialog):        # serialize exec/complete against this namespace
        _execute(dialog, code, buf, rich)
    return buf.getvalue().rstrip("\n"), rich


# ---- Async / interruptible execution ----------------------------------------
# The synchronous /exec above is one blocking HTTP call: a long-running cell (a
# training loop on the H100) shows NOTHING until it finishes — tqdm is useless and
# there's no way to stop it. The async path fixes both: /exec_start spawns a worker
# thread that streams stdout/stderr into a per-run buffer, /exec_poll snapshots it
# ~live, and /exec_stop raises KeyboardInterrupt in the worker.
RUNS: dict[str, "RunState"] = {}
_RUNS_GUARD = threading.Lock()


class RunState:
    """One async run for a dialog. The worker thread writes output into `buf`
    incrementally; pollers read a snapshot. `_lock` guards the done/error/rich
    flags for a consistent cross-thread read (the StringIO itself is fine to read
    under the GIL)."""

    def __init__(self, dialog: str, code: str):
        import uuid
        self.run_id = uuid.uuid4().hex[:12]
        self.dialog = dialog
        self.code = code
        self.buf = io.StringIO()
        self.rich: list = []
        self.done = False
        self.error: str | None = None
        self.interrupted = False
        self.thread: threading.Thread | None = None
        self.thread_id: int | None = None
        self._lock = threading.Lock()

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "output": self.buf.getvalue().rstrip("\n"),
                "done": self.done,
                # rich (plots/images) is only complete once the run finishes.
                "rich": list(self.rich) if self.done else [],
                "error": self.error,
                "interrupted": self.interrupted,
            }


def _run_worker(run: "RunState") -> None:
    """Worker-thread body: acquire the dialog lock (cooperating with the sync
    /exec, /complete, /reset paths) and run the code, streaming output into the
    RunState buffer. A KeyboardInterrupt injected by /exec_stop marks the run
    interrupted."""
    run.thread_id = threading.get_ident()
    try:
        with _dialog_locks(run.dialog):
            err = _execute(run.dialog, run.code, run.buf, run.rich)
        with run._lock:
            run.error = err
    except KeyboardInterrupt:
        with run._lock:
            run.interrupted = True
            run.error = "KeyboardInterrupt"
    except BaseException as e:  # noqa: BLE001 — a worker must never die silently
        with run._lock:
            run.error = f"{type(e).__name__}: {e}"
        _dbg(f"_run_worker: {type(e).__name__}: {e}")
    finally:
        with run._lock:
            run.done = True


def exec_start(dialog: str, code: str) -> str:
    """Start an async run of `code` in `dialog`. Returns its run_id. Only one active
    run per dialog: if one is still running, its run_id is returned unchanged (the
    per-dialog lock would serialize a second worker anyway, so we don't spawn one)."""
    with _RUNS_GUARD:
        existing = RUNS.get(dialog)
        if existing is not None and not existing.done:
            return existing.run_id            # busy — reuse the active run
        run = RunState(dialog, code)
        RUNS[dialog] = run
    t = threading.Thread(target=_run_worker, args=(run,), daemon=True)
    run.thread = t
    t.start()
    return run.run_id


def exec_poll(dialog: str, run_id: str) -> dict:
    """Non-blocking snapshot of a run's buffer: {output, done, rich, error,
    interrupted}. rich is only populated once done. Unknown run → done+error."""
    run = RUNS.get(dialog)
    if run is None or run.run_id != run_id:
        return {"output": "", "done": True, "rich": [],
                "error": "unknown run", "interrupted": False}
    return run.snapshot()


def _async_raise(thread_id: int, exctype) -> bool:
    """Raise `exctype` asynchronously in the thread with id `thread_id` via CPython's
    C API.

    KNOWN LIMITATION: PyThreadState_SetAsyncExc only delivers the exception at a
    Python *bytecode boundary*. A thread blocked inside a C extension call (a
    numpy/torch matmul, time.sleep, a blocking socket read) will NOT be interrupted
    until control returns to the Python interpreter loop. Pure-Python loops (the
    common `while True`/training-loop case) interrupt promptly."""
    res = ctypes.pythonapi.PyThreadState_SetAsyncExc(
        ctypes.c_long(thread_id), ctypes.py_object(exctype))
    if res > 1:
        # We hit more than one thread (shouldn't happen for a real tid) — undo it so
        # we don't leave a pending exception in an unrelated thread.
        ctypes.pythonapi.PyThreadState_SetAsyncExc(ctypes.c_long(thread_id), None)
    return res == 1


def exec_stop(dialog: str, run_id: str) -> dict:
    """Interrupt an active run by raising KeyboardInterrupt in its worker thread.
    Returns {ok, done}. A no-op (ok=True) if the run is unknown or already done."""
    run = RUNS.get(dialog)
    if run is None or run.run_id != run_id:
        return {"ok": False, "error": "unknown run"}
    if run.done:
        return {"ok": True, "done": True}
    tid = run.thread_id
    delivered = _async_raise(tid, KeyboardInterrupt) if tid is not None else False
    return {"ok": True, "done": False, "delivered": delivered}


# Variable/expression injection: $`expr` in a prompt is evaluated against the
# dialog's live namespace and replaced with the value, fresh each send — SolveIt's
# way of putting real kernel values in front of the AI ($`df.shape`, $`len(rows)`).
_INJECT_RE = re.compile(r"\$`([^`]+)`")
_INJECT_LIMIT = 4000            # cap a single injected value so one big object
                               # (a whole DataFrame) can't blow the prompt open.


def _inject_str(val) -> str:
    """Render an injected value as text. str() gives the f-string-like form the
    user expects ($`name` reads like {name}); oversized values are truncated."""
    s = str(val)
    if len(s) <= _INJECT_LIMIT:
        return s
    return s[:_INJECT_LIMIT] + f"… [+{len(s) - _INJECT_LIMIT} chars truncated]"


def inject_vars(dialog: str, content: str) -> tuple[str, list]:
    """Replace every $`expr` in `content` with its value from the dialog's kernel
    namespace. Returns (resolved, warnings). Each expression is eval'd fresh (so
    it reflects current state); a failing one is left as a visible marker and its
    error collected, mirroring SolveIt (it never aborts the prompt)."""
    if "$`" not in content:
        return content, []
    warnings_: list = []

    def repl(m, ns):
        expr = m.group(1).strip()
        try:
            val = eval(compile(expr, "<inject>", "eval"), ns)   # noqa: S307 — user's own prompt
            return _inject_str(val)     # str(val) can itself raise (broken __str__) — guard it too
        except Exception as e:  # noqa: BLE001 — surface, don't crash the prompt
            warnings_.append(f"{expr}: {type(e).__name__}: {e}")
            _dbg(f"inject_vars: `{expr}` failed: {type(e).__name__}: {e}")
            return f"[unresolved `{expr}`: {type(e).__name__}]"

    with _dialog_locks(dialog):     # eval against a stable namespace, not one mid-exec
        ns = _ns(dialog)
        return _INJECT_RE.sub(lambda m: repl(m, ns), content), warnings_


# Default API model per provider; override with env (e.g. OPENAI_MODEL=gpt-4.1).
MODEL_NAMES = {
    "claude": os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6"),
    "codex": os.environ.get("OPENAI_MODEL", "gpt-4o-mini"),
    "glm": os.environ.get("ZHIPU_MODEL", "glm-4"),
}


# Subscription-backed Claude (the `claude` CLI), installed Codex CLI, and the
# shared notebook-context preamble live in pappus.*_cli modules, so the web
# app's streaming path and this server's blocking path share the same prompt
# contract.
CLI_MODELS = claude_cli.CLI_MODELS
CODEX_CLI_MODELS = codex_cli.CLI_MODELS
CLI_SESSIONS = claude_cli.CLI_SESSIONS     # same dict the app's SSE path advances
CODEX_SESSIONS = codex_cli.CODEX_SESSIONS
_system = claude_cli.system                # preamble + context, used by the API callers
_wants_diagram = claude_cli._wants_diagram # just-in-time diagram conventions — the CLI
_DIAGRAM_GUIDANCE = claude_cli._DIAGRAM_GUIDANCE  # path attaches these in _build_cmd


def _is_claude_cli_model(model: str | None) -> bool:
    return model in CLI_MODELS or (isinstance(model, str) and model.startswith("claude-"))


def _is_codex_cli_model(model: str | None) -> bool:
    return model in CODEX_CLI_MODELS or (isinstance(model, str) and model.startswith("codex-"))


def _call_claude(key: str, content: str, context: str = "", mode: str | None = None) -> str:
    import anthropic
    client = anthropic.Anthropic(api_key=key)
    kw = {"model": MODEL_NAMES["claude"], "max_tokens": 1500,
          "messages": [{"role": "user", "content": content}]}
    sysmsg = _system(context, mode)
    if sysmsg:
        kw["system"] = sysmsg
    return client.messages.create(**kw).content[0].text


def _call_openai(key: str, content: str, context: str = "", mode: str | None = None) -> str:
    from openai import OpenAI
    client = OpenAI(api_key=key)
    msgs = [{"role": "user", "content": content}]
    sysmsg = _system(context, mode)
    if sysmsg:
        msgs.insert(0, {"role": "system", "content": sysmsg})
    # max_completion_tokens: the reasoning-tier models (o*/gpt-5*) reject the
    # legacy max_tokens param; the newer name works across all current models.
    r = client.chat.completions.create(
        model=MODEL_NAMES["codex"], max_completion_tokens=1500, messages=msgs,
    )
    return r.choices[0].message.content


def _call_zhipu(key: str, content: str, context: str = "", mode: str | None = None) -> str:
    from zhipuai import ZhipuAI
    client = ZhipuAI(api_key=key)
    msgs = [{"role": "user", "content": content}]
    sysmsg = _system(context, mode)
    if sysmsg:
        msgs.insert(0, {"role": "system", "content": sysmsg})
    r = client.chat.completions.create(model=MODEL_NAMES["glm"], messages=msgs)
    return r.choices[0].message.content


CALLERS = {"claude": _call_claude, "codex": _call_openai, "glm": _call_zhipu}
SDK_MODULE = {"claude": "anthropic", "codex": "openai", "glm": "zhipuai"}


def run_prompt(dialog: str, content: str, model: str, context: str = "",
               mode: str | None = None) -> str:
    """Route a prompt to its provider using the key from the shared secrets store
    (Settings page or env). `context` is the serialized notebook (cells above the
    prompt); it's passed to the model as a system preamble. `mode` selects the AI
    persona (learning/concise/standard). All three providers make real calls when
    keyed."""
    import importlib

    if _is_claude_cli_model(model):       # subscription-backed Claude (no API key)
        return claude_cli.call(dialog, content, context, model=model, mode=mode)
    if _is_codex_cli_model(model):        # installed Codex CLI (no Pappus API key)
        return codex_cli.call(dialog, content, context, model=model, mode=mode)

    if _wants_diagram(content):           # mirror the CLI path: attach the diagram
        content += _DIAGRAM_GUIDANCE      # conventions to the turn that asks for one

    try:
        from pappus.secrets_store import key_for_model, PROVIDERS
        api_key = key_for_model(model)
        label = PROVIDERS.get(model, (model, ""))[0]
    except Exception:  # noqa: BLE001 — never let key lookup break execution
        api_key, label = None, model

    caller = CALLERS.get(model)
    if api_key and caller:
        sdk = SDK_MODULE.get(model, "")
        try:                                  # is the SDK actually installed?
            importlib.import_module(sdk)
        except ImportError:
            return (f"[{label}] {sdk} SDK not installed — start the server with "
                    f"`uv run --extra llm ...` to enable live {label} calls.")
        try:                                  # real provider call
            return caller(api_key, content, context, mode)
        except Exception as e:  # noqa: BLE001 — surface provider/runtime errors in the UI
            return f"[{label} error: {e}]"

    with _dialog_locks(dialog):     # snapshot keys without racing a concurrent exec
        known = [k for k in _ns(dialog) if not k.startswith("__")]
    bits = [f"notebook context: {len(context)} chars"] if context else []
    if known:
        bits.append(f"kernel vars: {', '.join(known)}")
    detail = f" ({'; '.join(bits)})" if bits else ""
    return f"[{label}] no API key — add one in Settings to enable live replies.{detail}"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):  # quiet
        pass

    def _send(self, code: int, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else (
            json.dumps(body).encode() if ctype == "application/json" else body.encode()
        )
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _authorized(self) -> bool:
        return _check_auth(_AUTH_TOKEN, self.headers.get("Cookie", ""))

    def _host_ok(self):
        raw = (self.headers.get("Host", "") or "").strip()
        if raw.startswith("["):                       # [::1] or [::1]:5001
            host = raw[1:raw.index("]")] if "]" in raw else raw
        elif raw.count(":") == 1:                     # host:port
            host = raw.rsplit(":", 1)[0]
        else:
            host = raw                                 # bare host, or bare IPv6
        return host in {"127.0.0.1", "localhost", "::1"}

    def do_GET(self):
        if not self._host_ok():
            return self._send(403, {"error": "forbidden host"})
        if not self._authorized():
            return self._send(403, {"error": "unauthorized — bad or missing _solveit token"})
        if self.path.rstrip("/") == "/test_route":
            return self._send(200, "here", ctype="text/plain")
        if self.path.rstrip("/") == "/health":
            return self._send(200, {"ok": True, "dialogs": list(KERNELS)})
        if self.path.split("?", 1)[0].rstrip("/") == "/exec_poll":
            from urllib.parse import urlparse, parse_qs
            q = parse_qs(urlparse(self.path).query)
            dialog = q.get("dialog", ["default"])[0]
            run_id = q.get("run_id", [""])[0]
            return self._send(200, exec_poll(dialog, run_id))
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self._host_ok():
            return self._send(403, {"error": "forbidden host"})
        if not self._authorized():
            return self._send(403, {"error": "unauthorized — bad or missing _solveit token"})
        n = int(self.headers.get("Content-Length", 0))
        try:
            payload = json.loads(self.rfile.read(n) or b"{}")
        except json.JSONDecodeError:
            return self._send(400, {"error": "bad json"})
        path = self.path.rstrip("/")
        if path == "/exec":
            out, rich = run_code(payload.get("dialog", "default"), payload.get("code", ""))
            return self._send(200, {"output": out, "rich": rich})
        if path == "/exec_start":
            run_id = exec_start(payload.get("dialog", "default"), payload.get("code", ""))
            return self._send(200, {"run_id": run_id})
        if path == "/exec_stop":
            r = exec_stop(payload.get("dialog", "default"), payload.get("run_id", ""))
            return self._send(200, r)
        if path == "/complete":
            comps = complete_code(payload.get("dialog", "default"), payload.get("code", ""),
                                  int(payload.get("line", 1)), int(payload.get("col", 0)))
            return self._send(200, {"completions": comps})
        if path == "/prompt":
            dlg = payload.get("dialog", "default")
            content, _warns = inject_vars(dlg, payload.get("content", ""))
            out = run_prompt(dlg, content, payload.get("model", "claude"),
                             payload.get("context", ""), payload.get("mode"))
            return self._send(200, {"output": out, "model": payload.get("model", "claude")})
        if path == "/eval":
            content, warns = inject_vars(payload.get("dialog", "default"),
                                         payload.get("content", ""))
            return self._send(200, {"content": content, "warnings": warns})
        if path == "/syspath":
            # Make a built library importable in the kernel: put its dir on
            # sys.path (process-global, so it's importable from every dialog).
            import sys
            p = (payload.get("path") or "").strip()
            added = bool(p) and p not in sys.path
            if added:
                sys.path.insert(0, p)
            return self._send(200, {"ok": bool(p), "added": added, "path": p})
        if path == "/reset":
            import sys
            d = payload.get("dialog", "")
            with _dialog_locks(d):            # don't drop a namespace mid-exec
                KERNELS.pop(d, None)
                CLI_SESSIONS.pop(d, None)     # drop the CLI session too
                CODEX_SESSIONS.pop(d, None)
                # The backing module still references the whole namespace via
                # sys.modules — drop it too, or /reset frees nothing (slow leak).
                sys.modules.pop(_modname(d), None)
            # And the per-dialog lock (recreated lazily on next use). Done after
            # releasing it; a fresh lock for a just-reset dialog is harmless.
            with _LOCKS_GUARD:
                _LOCKS.pop(d, None)
            return self._send(200, {"ok": True})
        if path == "/rename":
            old, new = payload.get("old", ""), payload.get("new", "")
            with _dialog_locks(old, new):     # hold both ends across the move
                if old in KERNELS and new:
                    KERNELS[new] = KERNELS.pop(old)
                if old in CLI_SESSIONS and new:  # carry the session to the new name
                    CLI_SESSIONS[new] = CLI_SESSIONS.pop(old)
                if old in CODEX_SESSIONS and new:
                    CODEX_SESSIONS[new] = CODEX_SESSIONS.pop(old)
            return self._send(200, {"ok": True})
        return self._send(404, {"error": "not found"})


def main():
    # Headless-safe plotting: a server has no display, so default matplotlib to
    # the Agg backend. User code can still save figures with plt.savefig(...).
    os.environ.setdefault("MPLBACKEND", "Agg")
    global _AUTH_TOKEN
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=5001)
    ap.add_argument("--token", default=os.environ.get("PAPPUS_KERNEL_TOKEN"),
                    help="require this _solveit cookie on every request "
                         "(mandatory when binding off-loopback)")
    args = ap.parse_args()

    if args.token:
        _AUTH_TOKEN = args.token
    elif not _is_loopback(args.host):
        # /exec runs arbitrary code; refuse to expose it unauthenticated.
        ap.error(f"refusing to bind {args.host} without --token — that would expose "
                 f"unauthenticated code execution. Pass --token (or set "
                 f"PAPPUS_KERNEL_TOKEN), or bind 127.0.0.1.")

    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    auth = "token required" if _AUTH_TOKEN else "open (loopback only)"
    print(f"SolveIt kernel server on http://{args.host}:{args.port}  [{auth}]  (Ctrl-C to stop)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()
