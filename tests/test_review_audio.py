"""Tests for the audio spectrogram toolkit (sidekick.audio) and the conv_arch
per-layer summary. torchaudio-backed paths skip cleanly when the optional
'kernel' extra is absent; the dependency-light paths always run."""
import base64
import sys
from pathlib import Path

import pytest

np = pytest.importorskip("numpy")   # lives only in the `kernel` extra — skip, don't
                                    # error at collection, on a base install

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sidekick.audio import ab, play


def test_spec_requires_torchaudio_or_skips():
    """spec() returns a displayable matplotlib Figure (skips without torchaudio)."""
    import pytest
    pytest.importorskip("torchaudio")
    from sidekick.audio import spec

    fig = spec(np.random.randn(16000).astype("float32"), 16000)
    # A matplotlib Figure has axes and a savefig method — it is displayable.
    assert hasattr(fig, "savefig")
    assert fig.axes, "spec() figure should have at least one axis"


def test_ab_players_without_torchaudio():
    """ab()'s audio-player half works with NO torchaudio: two inline WAV players."""
    orig = np.random.randn(8000).astype("float32")
    recon = np.random.randn(8000).astype("float32")

    view = ab(orig, recon, 16000)
    html = view._repr_html_()
    assert html.count("data:audio/wav;base64,") == 2, (
        "ab() must embed exactly two inline audio/wav players")


def test_summary_table():
    """summary() returns an object whose _repr_html_ is a table with param counts."""
    import pytest
    torch = pytest.importorskip("torch")
    import torch.nn as nn

    from sidekick.conv_arch import summary

    model = nn.Sequential(
        nn.Conv2d(3, 8, 3, padding=1),
        nn.ReLU(),
        nn.Conv2d(8, 4, 3, padding=1),
    )
    table = summary(model, torch.randn(1, 3, 8, 8))
    html = table._repr_html_()
    assert "<table" in html
    # Conv2d(3,8,3): 3*8*9 + 8 = 224 params — the count must show in the table.
    assert "224" in html
    assert "total" in html


def test_play_still_works():
    """The existing play() contract is unchanged: base64 of a real WAV (RIFF)."""
    audio = play(np.zeros(1000, dtype="float32"), 22050)
    b64 = audio._repr_audio_wav_()
    raw = base64.b64decode(b64)
    assert raw[:4] == b"RIFF"
    assert raw[8:12] == b"WAVE"
