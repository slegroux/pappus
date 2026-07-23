"""Load and resolve SolveIt target profiles from targets.yaml."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass
class SSHConfig:
    host: str
    remote_port: int = 5001
    local_port: int = 5101
    start_cmd: str | None = None


@dataclass
class Target:
    name: str
    url: str
    token: str
    ssh: SSHConfig | None = None
    backend: str = "solveit"      # 'solveit' (real client) | 'kernel' (bundled server)

    @property
    def is_remote(self) -> bool:
        return self.ssh is not None


def _config_path(explicit: str | os.PathLike | None = None) -> Path:
    """Find targets.yaml: explicit arg > $SIDEKICK_CONFIG > package-adjacent default."""
    if explicit:
        return Path(explicit)
    env = os.environ.get("SIDEKICK_CONFIG")
    if env:
        return Path(env)
    return Path(__file__).resolve().parent.parent / "targets.yaml"


def _resolve_token(raw: dict) -> str:
    """A target may give `token` directly or name an env var via `token_env`.

    We never raise here: a missing token is a normal runtime state (you haven't
    exported it yet). We return "" so the UI can still load and the doctor/
    connection layer can report it clearly instead of crashing on switch.
    """
    if raw.get("token"):
        return str(raw["token"])
    env_name = raw.get("token_env")
    if env_name:
        return os.environ.get(env_name, "")
    # Fall back to solveit_client's own convention.
    return os.environ.get("SOLVEIT_TOKEN", "dummy")


def load_config(path: str | os.PathLike | None = None) -> dict:
    cfg_path = _config_path(path)
    if not cfg_path.exists():
        raise FileNotFoundError(f"No target config at {cfg_path}")
    with open(cfg_path) as f:
        return yaml.safe_load(f)


def list_targets(path: str | os.PathLike | None = None) -> list[str]:
    return list(load_config(path).get("targets", {}).keys())


def list_models(path: str | os.PathLike | None = None) -> list[dict]:
    """Return [{'id','label'}, ...] of selectable AI models for prompts."""
    cfg = load_config(path)
    models = cfg.get("models") or [{"id": "claude", "label": "Claude"}]
    return models


def default_model(path: str | os.PathLike | None = None) -> str:
    cfg = load_config(path)
    return cfg.get("default_model") or list_models(path)[0]["id"]


# Where a Codex default falls back to when the `codex` binary is missing.
_CODEX_MISSING_FALLBACK = "claude-opus-high"


def effective_default_model(path: str | os.PathLike | None = None) -> str:
    """The configured `default_model`, made resilient to a missing Codex CLI.

    Codex stays the committed default (it works on machines that have it), but if
    the default routes to the Codex CLI and the `codex` binary isn't installed on
    THIS machine, fall back to Claude Opus high — so a fresh session lands on a
    model that actually runs here instead of erroring on every prompt. The probe
    is best-effort and only substitutes when the fallback is itself a known model."""
    mid = default_model(path)
    if isinstance(mid, str) and mid.startswith("codex-"):
        try:
            from .codex_cli import codex_bin
            if codex_bin() is None and any(
                    m["id"] == _CODEX_MISSING_FALLBACK for m in list_models(path)):
                return _CODEX_MISSING_FALLBACK
        except Exception:  # noqa: BLE001 — availability probe never blocks resolution
            pass
    return mid


def get_target(name: str | None = None, path: str | os.PathLike | None = None) -> Target:
    cfg = load_config(path)
    targets = cfg.get("targets", {})
    name = name or os.environ.get("SIDEKICK_TARGET") or cfg.get("default")
    if name not in targets:
        raise KeyError(f"Unknown target '{name}'. Known: {', '.join(targets)}")
    raw = targets[name]
    ssh_raw = raw.get("ssh")
    ssh = (
        SSHConfig(
            host=ssh_raw["host"],
            remote_port=int(ssh_raw.get("remote_port", 5001)),
            local_port=int(ssh_raw.get("local_port", 5101)),
            start_cmd=ssh_raw.get("start_cmd"),
        )
        if ssh_raw
        else None
    )
    return Target(name=name, url=raw["url"], token=_resolve_token(raw), ssh=ssh,
                  backend=raw.get("backend", "solveit"))
