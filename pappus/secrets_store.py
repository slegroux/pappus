"""API-key storage for the LLM providers behind each model.

Keys are saved to a JSON file outside the repo (so they're never committed):
    $PAPPUS_SECRETS  (if set)  else  ~/.config/pappus/secrets.json
The file is written with 0600 perms. An environment variable always wins over
the stored file, so CI / shell exports keep working.

Both the web app and the kernel server import this module, so a key you save in
Settings is immediately usable by the server that answers prompts.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

# model id -> (provider label, env var the SDKs read)
PROVIDERS: dict[str, tuple[str, str]] = {
    "claude": ("Anthropic", "ANTHROPIC_API_KEY"),
    "glm": ("Zhipu", "ZHIPU_API_KEY"),
    "codex": ("OpenAI", "OPENAI_API_KEY"),
}

# Providers shown in Settings. The OpenAI SDK route remains internally supported
# as `codex` for explicit calls/tests, but the UI keeps only Codex CLI to avoid
# presenting two "Codex" choices.
SETTINGS_PROVIDERS = {k: PROVIDERS[k] for k in ("claude", "glm")}


def secrets_path() -> Path:
    env = os.environ.get("PAPPUS_SECRETS")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".config" / "pappus" / "secrets.json"


def load() -> dict:
    p = secrets_path()
    if p.exists():
        try:
            return json.loads(p.read_text())
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def save(provider_env: str, key: str) -> None:
    """Persist one key by its env-var name (e.g. ANTHROPIC_API_KEY)."""
    p = secrets_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    data = load()
    key = (key or "").strip()
    if key:
        data[provider_env] = key
    else:
        data.pop(provider_env, None)        # empty value clears it
    # Write via a 0600 temp file + atomic rename so the key material is never
    # readable in a world-readable window (mkstemp creates the file 0600 from the
    # start, unlike write_text()+chmod which is briefly 0644).
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".secrets-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(data, indent=2))
        os.replace(tmp, p)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def get_key(provider_env: str) -> str | None:
    """Env var wins, then the stored file."""
    return os.environ.get(provider_env) or load().get(provider_env)


def key_for_model(model_id: str) -> str | None:
    label_env = PROVIDERS.get(model_id)
    return get_key(label_env[1]) if label_env else None


def status() -> list[dict]:
    """For the Settings UI: one row per provider with whether a key is set."""
    out = []
    for model_id, (label, env) in SETTINGS_PROVIDERS.items():
        key = get_key(env)
        out.append({
            "model": model_id,
            "label": label,
            "env": env,
            "set": bool(key),
            "masked": (key[:3] + "…" + key[-4:]) if key and len(key) > 8 else ("set" if key else ""),
            "from_env": bool(os.environ.get(env)),
        })
    return out
