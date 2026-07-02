"""Which tools the sidekick AI (the `claude -p` path) may use.

One declarative place to control the Ask-AI agent's tools, instead of hardcoded
lists in claude_cli. Stored as JSON:
    $SIDEKICK_TOOLS  (if set)  else  ~/.config/solveit-sidekick/tools.json

    {
      "allow": ["WebSearch", "WebFetch"],   # tools the agent may call
      "deny":  ["Write", "Edit", "Bash"]    # tools it may never call
    }

Defaults (when the file is absent, or a key is missing) keep the SolveIt ethos:
web *research* is allowed (a thinking-partner tool — SolveIt's dialoghelper ships
search/read_url), while the executor's hands (Write/Edit/Bash — run code / work
off-screen) are denied. To fully open it up, set `"deny": []`.

`allow` is additive with the cell-editing MCP tools, which claude_cli wires
separately (they need the MCP server + token). This file governs everything else.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

# The ethos-safe baseline, used when there's no file or a key is omitted.
DEFAULT_ALLOW = ["WebSearch", "WebFetch"]
DEFAULT_DENY = ["Write", "Edit", "Bash"]


def tools_path() -> Path:
    env = os.environ.get("SIDEKICK_TOOLS")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".config" / "solveit-sidekick" / "tools.json"


def _read() -> dict:
    p = tools_path()
    if p.exists():
        try:
            data = json.loads(p.read_text())
            return data if isinstance(data, dict) else {}
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _strs(v, default: list[str]) -> list[str]:
    """A clean list[str] from a config value, falling back to `default`."""
    if not isinstance(v, list):
        return list(default)
    return [str(x) for x in v if isinstance(x, str) and x.strip()]


def load() -> dict:
    """The effective config: ``{"allow": [...], "deny": [...]}``. A missing key
    falls back to its default; a present key (even ``[]``) is honored."""
    data = _read()
    return {
        "allow": _strs(data["allow"], DEFAULT_ALLOW) if "allow" in data else list(DEFAULT_ALLOW),
        "deny": _strs(data["deny"], DEFAULT_DENY) if "deny" in data else list(DEFAULT_DENY),
    }


def allow_list() -> list[str]:
    return load()["allow"]


def deny_list() -> list[str]:
    return load()["deny"]


def save(allow: list[str], deny: list[str]) -> None:
    p = tools_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"allow": allow, "deny": deny}, indent=2))
        tmp.replace(p)
    except OSError:
        pass
