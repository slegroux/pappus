"""Tests for the review-batch kernel additions: audio carrier (play), the
kernel rich-repr audio branch, and the Host-header defense. Kept dependency-light
(no torchaudio, no real HTTP bind) — the pure helpers are exercised directly.
"""
from __future__ import annotations

import base64
import io
import sys
import wave
from pathlib import Path

import pytest

np = pytest.importorskip("numpy")   # lives only in the `kernel` extra — skip, don't
                                    # error at collection, on a base install

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sidekick.audio import play
from server import kernel_server


def _decode_wav(carrier):
    b64 = carrier._repr_audio_wav_()
    raw = base64.b64decode(b64)
    return b64, raw


def test_play_roundtrip():
    carrier = play(np.zeros(1000, dtype=np.float32), 16000)
    _b64, raw = _decode_wav(carrier)
    assert raw.startswith(b"RIFF")
    assert b"WAVE" in raw
    with wave.open(io.BytesIO(raw), "rb") as w:
        assert w.getframerate() == 16000
        assert w.getnframes() == 1000
        assert w.getnchannels() == 1
        assert w.getsampwidth() == 2


def test_play_accepts_int16():
    data = np.array([-32768, 0, 32767, 1000], dtype=np.int16)
    carrier = play(data, 8000)
    _b64, raw = _decode_wav(carrier)
    with wave.open(io.BytesIO(raw), "rb") as w:
        assert w.getframerate() == 8000
        assert w.getnframes() == 4
        assert w.getnchannels() == 1
        frames = np.frombuffer(w.readframes(4), dtype="<i2")
    assert frames.tolist() == data.tolist()


def test_play_stereo():
    # channels-first (2, N): left ramps up, right is silent.
    left = np.linspace(-1.0, 1.0, 500, dtype=np.float32)
    right = np.zeros(500, dtype=np.float32)
    stereo = np.stack([left, right])  # shape (2, 500)
    carrier = play(stereo, 44100)
    _b64, raw = _decode_wav(carrier)
    with wave.open(io.BytesIO(raw), "rb") as w:
        assert w.getnchannels() == 2
        assert w.getnframes() == 500
        assert w.getframerate() == 44100
        frames = np.frombuffer(w.readframes(500), dtype="<i2").reshape(-1, 2)
    # Right channel is silent throughout; frames are interleaved L,R.
    assert np.all(frames[:, 1] == 0)


def test_play_stereo_samples_first_autodetected():
    # (N, 2) samples-first is auto-transposed to 2 channels.
    stereo = np.zeros((500, 2), dtype=np.float32)
    carrier = play(stereo, 22050)
    _b64, raw = _decode_wav(carrier)
    with wave.open(io.BytesIO(raw), "rb") as w:
        assert w.getnchannels() == 2
        assert w.getnframes() == 500


class _FakeHeaders:
    def __init__(self, host):
        self._host = host

    def get(self, key, default=""):
        if key == "Host":
            return self._host
        return default


def _host_ok(host):
    # Exercise the exact Handler._host_ok logic against a fake headers object,
    # without binding a socket.
    handler = kernel_server.Handler.__new__(kernel_server.Handler)
    handler.headers = _FakeHeaders(host)
    return kernel_server.Handler._host_ok(handler)


def test_host_ok():
    assert _host_ok("127.0.0.1") is True
    assert _host_ok("localhost") is True
    # port stripping (rsplit on the last colon)
    assert _host_ok("127.0.0.1:5001") is True
    assert _host_ok("localhost:5001") is True
    # IPv6 loopback arrives bracketed with a port in real Host headers;
    # rsplit strips the port, strip("[]") removes the brackets -> "::1".
    assert _host_ok("[::1]:5001") is True
    # rejected
    assert _host_ok("evil.com") is False
    assert _host_ok("evil.com:5001") is False
    assert _host_ok("") is False


def test_rich_audio_branch():
    class Dummy:
        def _repr_audio_wav_(self):
            return "AAA="

    out = kernel_server._rich_repr(Dummy())
    assert out == {"type": "audio/wav", "data": "AAA="}


def test_rich_audio_branch_precedes_others():
    # An object offering both audio and html should render as audio (audio first).
    class DummyBoth:
        def _repr_audio_wav_(self):
            return "QkJC"

        def _repr_html_(self):
            return "<b>hi</b>"

    out = kernel_server._rich_repr(DummyBoth())
    assert out == {"type": "audio/wav", "data": "QkJC"}


# ---- run_prompt provider routing (patched SDKs, no network) -----------------
import types as _types


def _install_fake_sdk(monkeypatch, modname, ctor, kind, captured):
    """Inject a fake provider SDK into sys.modules. The fake records the client's
    api_key and the create(**kw) call into `captured` so a test can assert shape
    without any network. `kind` picks the response object each SDK returns."""
    mod = _types.ModuleType(modname)

    class _Create:
        def create(self, **kw):
            captured.update(kw)
            if kind == "anthropic":                    # .content[0].text
                return _types.SimpleNamespace(content=[_types.SimpleNamespace(text="ok")])
            return _types.SimpleNamespace(              # .choices[0].message.content
                choices=[_types.SimpleNamespace(message=_types.SimpleNamespace(content="ok"))])

    if kind == "anthropic":
        class _Client:
            def __init__(self, api_key=None): captured["api_key"] = api_key
            messages = _Create()
    else:
        class _Client:
            def __init__(self, api_key=None): captured["api_key"] = api_key
            chat = _types.SimpleNamespace(completions=_Create())

    setattr(mod, ctor, _Client)
    monkeypatch.setitem(sys.modules, modname, mod)


def test_run_prompt_routes_to_anthropic(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-a")
    cap = {}
    _install_fake_sdk(monkeypatch, "anthropic", "Anthropic", "anthropic", cap)
    out = kernel_server.run_prompt("d", "hello", "claude", context="ctx")
    assert out == "ok"
    assert cap["api_key"] == "sk-a"
    assert cap["model"] == kernel_server.MODEL_NAMES["claude"]
    assert cap["messages"] == [{"role": "user", "content": "hello"}]
    assert cap.get("system")                           # non-empty context → preamble


def test_run_prompt_routes_to_openai(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-o")
    cap = {}
    _install_fake_sdk(monkeypatch, "openai", "OpenAI", "openai", cap)
    out = kernel_server.run_prompt("d", "hello", "codex", context="ctx")
    assert out == "ok"
    assert cap["api_key"] == "sk-o"
    assert cap["model"] == kernel_server.MODEL_NAMES["codex"]
    # newer param name (reasoning-tier models reject legacy max_tokens)
    assert cap["max_completion_tokens"] == 1500
    assert cap["messages"][-1] == {"role": "user", "content": "hello"}
    assert cap["messages"][0]["role"] == "system"      # context inserted first


def test_run_prompt_routes_to_zhipu(monkeypatch):
    monkeypatch.setenv("ZHIPU_API_KEY", "sk-z")
    cap = {}
    _install_fake_sdk(monkeypatch, "zhipuai", "ZhipuAI", "zhipu", cap)
    out = kernel_server.run_prompt("d", "hello", "glm", context="ctx")
    assert out == "ok"
    assert cap["api_key"] == "sk-z"
    assert cap["model"] == kernel_server.MODEL_NAMES["glm"]


def test_run_prompt_reports_missing_sdk(monkeypatch):
    import importlib
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-a")     # key present…
    orig = importlib.import_module
    monkeypatch.setattr(importlib, "import_module",     # …but the SDK won't import
                        lambda n, *a, **k: (_ for _ in ()).throw(ImportError())
                        if n == "anthropic" else orig(n, *a, **k))
    out = kernel_server.run_prompt("d", "hello", "claude")
    assert "SDK not installed" in out


def test_run_prompt_without_key_is_offline_stub(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr("sidekick.secrets_store.key_for_model", lambda m: None)
    out = kernel_server.run_prompt("d", "hello", "claude")
    assert "no API key" in out
