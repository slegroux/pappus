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
import contextlib
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


def _ns(dialog: str) -> dict:
    return KERNELS.setdefault(dialog, {"__name__": "__solveit__"})


def _b64(data: bytes) -> str:
    import base64
    return base64.b64encode(data).decode()


def _capture_figs() -> list:
    """Grab any open matplotlib figures as PNGs, then clear them (inline-plot
    behavior). Returns [] when matplotlib isn't even imported, so there's no cost
    for non-plotting code."""
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
    ns = _ns(dialog)
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

    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
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
    rich = _capture_figs() + rich          # plots created during the cell, newest cell-state
    return buf.getvalue().rstrip("\n"), rich


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


def _call_claude(key: str, content: str, context: str = "") -> str:
    import anthropic
    client = anthropic.Anthropic(api_key=key)
    kw = {"model": MODEL_NAMES["claude"], "max_tokens": 1500,
          "messages": [{"role": "user", "content": content}]}
    sysmsg = _system(context)
    if sysmsg:
        kw["system"] = sysmsg
    return client.messages.create(**kw).content[0].text


def _call_openai(key: str, content: str, context: str = "") -> str:
    from openai import OpenAI
    client = OpenAI(api_key=key)
    msgs = [{"role": "user", "content": content}]
    sysmsg = _system(context)
    if sysmsg:
        msgs.insert(0, {"role": "system", "content": sysmsg})
    r = client.chat.completions.create(
        model=MODEL_NAMES["codex"], max_tokens=1500, messages=msgs,
    )
    return r.choices[0].message.content


def _call_zhipu(key: str, content: str, context: str = "") -> str:
    from zhipuai import ZhipuAI
    client = ZhipuAI(api_key=key)
    msgs = [{"role": "user", "content": content}]
    sysmsg = _system(context)
    if sysmsg:
        msgs.insert(0, {"role": "system", "content": sysmsg})
    r = client.chat.completions.create(model=MODEL_NAMES["glm"], messages=msgs)
    return r.choices[0].message.content


CALLERS = {"claude": _call_claude, "codex": _call_openai, "glm": _call_zhipu}
SDK_MODULE = {"claude": "anthropic", "codex": "openai", "glm": "zhipuai"}


def run_prompt(dialog: str, content: str, model: str, context: str = "") -> str:
    """Route a prompt to its provider using the key from the shared secrets store
    (Settings page or env). `context` is the serialized notebook (cells above the
    prompt); it's passed to the model as a system preamble. All three providers
    make real calls when keyed."""
    import importlib

    if model in CLI_MODELS:               # subscription-backed Claude (no API key)
        return claude_cli.call(dialog, content, context, model=model)

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
            return caller(api_key, content, context)
        except Exception as e:  # noqa: BLE001 — surface provider/runtime errors in the UI
            return f"[{label} error: {e}]"

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
            out = run_prompt(payload.get("dialog", "default"),
                             payload.get("content", ""), payload.get("model", "claude"),
                             payload.get("context", ""))
            return self._send(200, {"output": out, "model": payload.get("model", "claude")})
        if path == "/reset":
            d = payload.get("dialog", "")
            KERNELS.pop(d, None)
            CLI_SESSIONS.pop(d, None)         # drop the CLI session too
            return self._send(200, {"ok": True})
        if path == "/rename":
            old, new = payload.get("old", ""), payload.get("new", "")
            if old in KERNELS and new:
                KERNELS[new] = KERNELS.pop(old)
            if old in CLI_SESSIONS and new:    # carry the session to the new name
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
