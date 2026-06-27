"""A lightweight, self-hostable SolveIt-compatible kernel server.

This is NOT Answer.AI's proprietary SolveIt server — it's a minimal stand-in you
can actually run on your laptop or H100 so the Sidekick interface has a *real*
backend: it executes Python code in a persistent per-dialog namespace and returns
output, just like a notebook kernel. Prompts get a routed (stubbed, or real if an
API key is present) AI reply.

Endpoints (simple JSON, no auth beyond the _solveit cookie check):
    GET  /test_route            -> "here"          (what doctor/solveit_client probe)
    GET  /health                -> {ok, dialogs}
    POST /exec    {dialog,code}                    -> {output}
    POST /prompt  {dialog,content,model,context}   -> {output, model}
    POST /reset   {dialog}                 -> {ok}

Run:  python -m server.kernel_server --port 5001        # laptop ('local' target)
      python -m server.kernel_server --port 5001        # on the H100, then tunnel
Stdlib only — no dependencies.
"""
from __future__ import annotations

import argparse
import ast
import io
import json
import os
import contextlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Per-dialog execution namespaces — this is the "kernel" state.
KERNELS: dict[str, dict] = {}


def _ns(dialog: str) -> dict:
    return KERNELS.setdefault(dialog, {"__name__": "__solveit__"})


def run_code(dialog: str, code: str) -> str:
    """Execute code in the dialog's namespace; return stdout + last-expr repr."""
    ns = _ns(dialog)
    buf = io.StringIO()
    try:
        tree = ast.parse(code, mode="exec")
    except SyntaxError as e:
        return f"SyntaxError: {e}"

    last_expr = None
    if tree.body and isinstance(tree.body[-1], ast.Expr):
        last_expr = ast.Expression(tree.body.pop().value)

    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            exec(compile(tree, "<dialog>", "exec"), ns)
            if last_expr is not None:
                val = eval(compile(last_expr, "<dialog>", "eval"), ns)
                if val is not None:
                    print(repr(val), file=buf)
    except Exception as e:  # noqa: BLE001 — surface kernel errors as output
        print(f"{type(e).__name__}: {e}", file=buf)
    return buf.getvalue().rstrip("\n")


# Default API model per provider; override with env (e.g. OPENAI_MODEL=gpt-4.1).
MODEL_NAMES = {
    "claude": os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6"),
    "codex": os.environ.get("OPENAI_MODEL", "gpt-4o-mini"),
    "glm": os.environ.get("ZHIPU_MODEL", "glm-4"),
}


# Prepended to the notebook context so the model knows what it's reading.
_SYSTEM_PREAMBLE = (
    "You are an AI assistant embedded in a computational notebook — a SolveIt-style "
    "dialog of code, output, notes, and prior Q&A. The notebook so far is below, in "
    "order. Use it as context to answer the user's question; refer to variables, "
    "results, and notes already present.\n\n"
)


def _system(context: str) -> str | None:
    return (_SYSTEM_PREAMBLE + context) if context else None


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

    def do_GET(self):
        if self.path.rstrip("/") == "/test_route":
            return self._send(200, "here", ctype="text/plain")
        if self.path.rstrip("/") == "/health":
            return self._send(200, {"ok": True, "dialogs": list(KERNELS)})
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        try:
            payload = json.loads(self.rfile.read(n) or b"{}")
        except json.JSONDecodeError:
            return self._send(400, {"error": "bad json"})
        path = self.path.rstrip("/")
        if path == "/exec":
            out = run_code(payload.get("dialog", "default"), payload.get("code", ""))
            return self._send(200, {"output": out})
        if path == "/prompt":
            out = run_prompt(payload.get("dialog", "default"),
                             payload.get("content", ""), payload.get("model", "claude"),
                             payload.get("context", ""))
            return self._send(200, {"output": out, "model": payload.get("model", "claude")})
        if path == "/reset":
            KERNELS.pop(payload.get("dialog", ""), None)
            return self._send(200, {"ok": True})
        if path == "/rename":
            old, new = payload.get("old", ""), payload.get("new", "")
            if old in KERNELS and new:
                KERNELS[new] = KERNELS.pop(old)
            return self._send(200, {"ok": True})
        return self._send(404, {"error": "not found"})


def main():
    # Headless-safe plotting: a server has no display, so default matplotlib to
    # the Agg backend. User code can still save figures with plt.savefig(...).
    os.environ.setdefault("MPLBACKEND", "Agg")
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=5001)
    args = ap.parse_args()
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"SolveIt kernel server on http://{args.host}:{args.port}  (Ctrl-C to stop)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()
