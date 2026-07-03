"""A lightweight, self-hostable SolveIt-compatible kernel server.

This is NOT Answer.AI's proprietary SolveIt server — it's a minimal stand-in you
can actually run on your laptop or H100 so the Sidekick interface has a *real*
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

from sidekick import claude_cli   # CLI (subscription) Claude + shared notebook preamble

# Request auth. main() sets _AUTH_TOKEN: when it's not None, every request must
# carry a matching `_solveit` cookie (this is what makes binding off-loopback
# safe — /exec runs arbitrary code). On loopback with no token it stays open.
_AUTH_TOKEN: str | None = None


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
        slug = "".join(c if c.isalnum() else "_" for c in dialog).strip("_") or "default"
        modname = f"__solveit_{slug}__"
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
        except Exception:  # noqa: BLE001 — a bad figure shouldn't break the cell
            pass
    plt.close("all")
    return out


def _rich_repr(val) -> dict | None:
    """Map a value to a rich output via the IPython display protocol.

    Covers DataFrames (_repr_html_), PIL/other images (_repr_png_/_repr_jpeg_).
    matplotlib Figures are handled by _capture_figs, so skip them here.
    """
    if type(val).__module__.startswith("matplotlib"):
        return None
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


def run_code(dialog: str, code: str) -> tuple[str, list]:
    """Execute code in the dialog's namespace.

    Returns (text, rich) where text is stdout/stderr plus the last expression's
    repr, and rich is a list of MIME-typed outputs (plots, images, dataframes).
    """
    buf = io.StringIO()
    rich: list = []
    try:
        tree = ast.parse(code, mode="exec")
    except SyntaxError as e:
        return f"SyntaxError: {e}", rich

    # A trailing ';' suppresses the last expression's value, Jupyter-style. The
    # statement still runs (side effects, plots) — only its repr is hidden.
    suppress = code.rstrip().endswith(";")
    last_expr = None
    if not suppress and tree.body and isinstance(tree.body[-1], ast.Expr):
        last_expr = ast.Expression(tree.body.pop().value)

    with _dialog_locks(dialog):        # serialize exec/complete against this namespace
        ns = _ns(dialog)
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
                            rich.append(r)
                        else:
                            print(repr(val), file=buf)
        except Exception as e:  # noqa: BLE001 — surface kernel errors as output
            print(f"{type(e).__name__}: {e}", file=buf)
        rich = _capture_figs() + rich      # plots created during the cell, newest cell-state
    return buf.getvalue().rstrip("\n"), rich


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


# Subscription-backed Claude (the `claude` CLI) and the shared notebook-context
# preamble both live in sidekick.claude_cli, so the web app's streaming path and
# this server's blocking path share one session store + system prompt.
CLI_MODELS = claude_cli.CLI_MODELS
CLI_SESSIONS = claude_cli.CLI_SESSIONS     # same dict the app's SSE path advances
_system = claude_cli.system                # preamble + context, used by the API callers


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
    r = client.chat.completions.create(
        model=MODEL_NAMES["codex"], max_tokens=1500, messages=msgs,
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

    if model in CLI_MODELS:               # subscription-backed Claude (no API key)
        return claude_cli.call(dialog, content, context, model=model, mode=mode)

    try:
        from sidekick.secrets_store import key_for_model, PROVIDERS
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

    def do_GET(self):
        if not self._authorized():
            return self._send(403, {"error": "unauthorized — bad or missing _solveit token"})
        if self.path.rstrip("/") == "/test_route":
            return self._send(200, "here", ctype="text/plain")
        if self.path.rstrip("/") == "/health":
            return self._send(200, {"ok": True, "dialogs": list(KERNELS)})
        return self._send(404, {"error": "not found"})

    def do_POST(self):
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
            d = payload.get("dialog", "")
            with _dialog_locks(d):            # don't drop a namespace mid-exec
                KERNELS.pop(d, None)
                CLI_SESSIONS.pop(d, None)     # drop the CLI session too
            return self._send(200, {"ok": True})
        if path == "/rename":
            old, new = payload.get("old", ""), payload.get("new", "")
            with _dialog_locks(old, new):     # hold both ends across the move
                if old in KERNELS and new:
                    KERNELS[new] = KERNELS.pop(old)
                if old in CLI_SESSIONS and new:  # carry the session to the new name
                    CLI_SESSIONS[new] = CLI_SESSIONS.pop(old)
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
    ap.add_argument("--token", default=os.environ.get("SIDEKICK_KERNEL_TOKEN"),
                    help="require this _solveit cookie on every request "
                         "(mandatory when binding off-loopback)")
    args = ap.parse_args()

    if args.token:
        _AUTH_TOKEN = args.token
    elif not _is_loopback(args.host):
        # /exec runs arbitrary code; refuse to expose it unauthenticated.
        ap.error(f"refusing to bind {args.host} without --token — that would expose "
                 f"unauthenticated code execution. Pass --token (or set "
                 f"SIDEKICK_KERNEL_TOKEN), or bind 127.0.0.1.")

    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    auth = "token required" if _AUTH_TOKEN else "open (loopback only)"
    print(f"SolveIt kernel server on http://{args.host}:{args.port}  [{auth}]  (Ctrl-C to stop)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()
