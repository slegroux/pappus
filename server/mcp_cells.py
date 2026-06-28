"""A tiny, zero-dependency MCP server that lets Claude edit the live notebook.

Sidekick's Max-plan path talks to Claude through the `claude` CLI (subscription
auth). The CLI is itself a tool-calling agent, so the way to let it *edit cells*
— the way SolveIt's dialoghelper does on the API — is to hand it MCP tools. This
module is that toolset, spoken over stdio as newline-delimited JSON-RPC 2.0.

It holds no state of its own: each tool call reaches back to the running Sidekick
web app over loopback HTTP (the app owns the live, in-memory dialog). The app
URL, the target dialog, and a shared one-shot token arrive via env, set by
`sidekick.claude_cli` when it spawns `claude -p`:

    SIDEKICK_APP_URL     e.g. http://127.0.0.1:8000  (loopback only)
    SIDEKICK_DIALOG      the dialog whose cells these tools edit
    SIDEKICK_MCP_TOKEN   shared secret; the app rejects calls without it

Tools: list_cells, update_cell, str_replace, insert_cell — deliberately the same
verbs dialoghelper/Claude Code use, so the model already knows how to drive them.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.parse
import urllib.request

APP_URL = os.environ.get("SIDEKICK_APP_URL", "http://127.0.0.1:8000").rstrip("/")
DIALOG = os.environ.get("SIDEKICK_DIALOG", "")
TOKEN = os.environ.get("SIDEKICK_MCP_TOKEN", "")

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "sidekick-cells", "version": "0.1.0"}

TOOLS = [
    {
        "name": "list_cells",
        "description": (
            "List the notebook's cells in order with their id, type (code/note/"
            "prompt), and current source. Call this first to get the exact cell "
            "id and text before editing."
        ),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "update_cell",
        "description": "Replace a cell's entire source with new content.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "cell_id": {"type": "string", "description": "id from list_cells"},
                "content": {"type": "string", "description": "the new full source"},
            },
            "required": ["cell_id", "content"],
            "additionalProperties": False,
        },
    },
    {
        "name": "str_replace",
        "description": (
            "Replace an exact substring in a cell's source. old_str must match "
            "exactly once (include enough surrounding text to be unique)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "cell_id": {"type": "string"},
                "old_str": {"type": "string"},
                "new_str": {"type": "string"},
            },
            "required": ["cell_id", "old_str", "new_str"],
            "additionalProperties": False,
        },
    },
    {
        "name": "insert_cell",
        "description": (
            "Insert a new cell. By default appends at the end; pass after_id to "
            "place it just below an existing cell."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "content": {"type": "string"},
                "cell_type": {"enum": ["code", "note", "prompt"], "default": "code"},
                "after_id": {"type": "string", "description": "insert below this cell id"},
            },
            "required": ["content"],
            "additionalProperties": False,
        },
    },
]


# ---- HTTP back to the app ----------------------------------------------------
def _request(method: str, path: str, params: dict) -> dict:
    """Call the app's /internal API. GET puts params in the query; POST form-encodes."""
    params = {**params, "dialog": DIALOG, "tok": TOKEN}
    url = APP_URL + path
    data = None
    if method == "GET":
        url += "?" + urllib.parse.urlencode(params)
    else:
        data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(url, data=data, method=method)
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode())


def _fmt_cells(cells: list) -> str:
    if not cells:
        return "(the notebook is empty)"
    out = []
    for i, c in enumerate(cells, 1):           # 1-based: matches the UI cell number
        src = (c.get("content") or "").rstrip()
        if len(src) > 1200:
            src = src[:1200] + "\n…(truncated)"
        head = f"cell {i}  id={c.get('id')}  type={c.get('type')}"
        out.append(f"{head}\n{src}" if src else f"{head}\n(empty)")
    return "\n\n".join(out)


def _call_tool(name: str, args: dict) -> str:
    if name == "list_cells":
        r = _request("GET", "/internal/cells", {})
        return _fmt_cells(r.get("cells", []))
    if name == "update_cell":
        r = _request("POST", "/internal/cell/update",
                     {"id": args["cell_id"], "content": args.get("content", "")})
    elif name == "str_replace":
        r = _request("POST", "/internal/cell/str_replace",
                     {"id": args["cell_id"], "old": args["old_str"], "new": args["new_str"]})
    elif name == "insert_cell":
        r = _request("POST", "/internal/cell/insert",
                     {"content": args.get("content", ""),
                      "cell_type": args.get("cell_type", "code"),
                      "after_id": args.get("after_id", "")})
    else:
        raise ValueError(f"unknown tool: {name}")
    if not r.get("ok"):
        raise RuntimeError(r.get("error", "edit failed"))
    return r.get("message", "done")


# ---- JSON-RPC over stdio -----------------------------------------------------
def _send(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def _reply(rid, result=None, error=None) -> None:
    msg = {"jsonrpc": "2.0", "id": rid}
    if error is not None:
        msg["error"] = error
    else:
        msg["result"] = result
    _send(msg)


def _handle(msg: dict) -> None:
    method = msg.get("method")
    rid = msg.get("id")
    if method == "initialize":
        client_ver = (msg.get("params") or {}).get("protocolVersion")
        _reply(rid, {"protocolVersion": client_ver or PROTOCOL_VERSION,
                     "capabilities": {"tools": {}}, "serverInfo": SERVER_INFO})
    elif method == "tools/list":
        _reply(rid, {"tools": TOOLS})
    elif method == "tools/call":
        params = msg.get("params") or {}
        name = params.get("name", "")
        args = params.get("arguments") or {}
        try:
            text = _call_tool(name, args)
            _reply(rid, {"content": [{"type": "text", "text": text}], "isError": False})
        except Exception as e:  # noqa: BLE001 — surface any failure to the model, don't crash
            _reply(rid, {"content": [{"type": "text", "text": f"Error: {e}"}], "isError": True})
    elif method == "ping":
        _reply(rid, {})
    elif rid is not None:                      # an unknown *request* needs an error reply
        _reply(rid, error={"code": -32601, "message": f"method not found: {method}"})
    # notifications (no id), e.g. notifications/initialized → nothing to send


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except (ValueError, TypeError):
            continue
        try:
            _handle(msg)
        except Exception:  # noqa: BLE001 — never let one bad message kill the server
            pass


if __name__ == "__main__":
    main()
