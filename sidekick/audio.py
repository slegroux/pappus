"""Inline audio playback for the SolveIt kernel.

``play(x, sr)`` wraps a waveform in a small carrier object that the kernel's
rich-repr routine recognizes via ``_repr_audio_wav_`` and renders as an inline
``<audio>`` player (MIME ``audio/wav``). The input convention: ``x`` is a numpy
array (or anything ``np.asarray`` accepts, or a torch tensor by duck-typing).
Mono is a 1-D array of samples; stereo is 2-D shaped ``(channels, samples)`` —
i.e. channels-first, the torchaudio convention — though a ``(samples, channels)``
array is auto-detected and transposed when the second axis is the small one.
Float samples are assumed to lie in ``[-1, 1]`` and are scaled to int16 PCM;
integer arrays are passed through (cast to int16). Only the stdlib ``wave`` +
``base64`` + numpy are used, so ``play()`` has zero heavy dependencies and is
always importable/testable.

Spectrogram / model-dissection toolkit
--------------------------------------
``spec()``, ``wavespec()`` and ``ab()`` extend playback with visual inspection:

- ``spec(x, sr)``    — a log-mel spectrogram rendered as a matplotlib Figure.
- ``wavespec(x, sr)`` — waveform stacked over its log-mel spectrogram.
- ``ab(original, recon, sr)`` — A/B compare two signals: two inline ``<audio>``
  players plus a side-by-side log-mel view of both.

Input convention is the same as ``play()`` (numpy/torch, mono 1-D or stereo
2-D channels-first; stereo is downmixed to mono for the spectrogram). The
spectrogram paths (``spec``/``wavespec`` and ``ab``'s difference view) require
``torchaudio``, which ships in the OPTIONAL ``kernel`` extra and is NOT a base
dependency — they lazy-import it and raise a clear ``ImportError`` with an
install hint ("pip install 'solveit-sidekick[kernel]'" / "uv sync --extra
kernel") when it is missing. ``play()`` and ``ab()``'s audio-player half stay
dependency-light (stdlib ``wave`` + numpy only) and work without ``torchaudio``.
"""
from __future__ import annotations

import base64
import io
import wave

import numpy as np


def _to_numpy(x):
    """Convert x to a numpy array, duck-typing torch tensors without importing torch."""
    if hasattr(x, "detach") and hasattr(x, "cpu") and hasattr(x, "numpy"):
        try:
            return x.detach().cpu().numpy()
        except Exception:  # noqa: BLE001 — fall back to np.asarray for anything odd
            pass
    return np.asarray(x)


def _to_int16(arr: np.ndarray) -> np.ndarray:
    """Map samples to int16 PCM. Floats are assumed in [-1, 1]; ints pass through."""
    if np.issubdtype(arr.dtype, np.floating):
        clipped = np.clip(arr, -1.0, 1.0)
        return (clipped * 32767.0).round().astype("<i2")
    if arr.dtype == np.int16:
        return arr.astype("<i2")
    # Other integer dtypes: clamp into int16 range and cast.
    return np.clip(arr, -32768, 32767).astype("<i2")


class _Audio:
    """Carrier for a waveform. Renders as inline WAV via _repr_audio_wav_."""

    def __init__(self, wav_bytes: bytes, sr: int, nframes: int, channels: int):
        self._wav = wav_bytes
        self.sr = sr
        self.nframes = nframes
        self.channels = channels

    def _repr_audio_wav_(self) -> str:
        """Return the complete WAV file as a base64 string (the audio contract)."""
        return base64.b64encode(self._wav).decode("ascii")

    def __repr__(self) -> str:
        return f"<Audio sr={self.sr} channels={self.channels} frames={self.nframes}>"


def play(x, sr: int = 44100) -> _Audio:
    """Wrap waveform ``x`` (mono 1-D or stereo 2-D) as an inline WAV player.

    See the module docstring for the input convention. Returns a carrier object
    whose ``_repr_audio_wav_()`` yields base64 of a complete WAV file at ``sr`` Hz.
    """
    arr = _to_numpy(x)
    arr = np.asarray(arr)

    if arr.ndim == 1:
        channels = 1
        interleaved = arr
    elif arr.ndim == 2:
        rows, cols = arr.shape
        # Channels-first convention (channels, samples). If it instead looks like
        # (samples, channels) — the second axis is the small one — transpose it.
        if rows > cols:
            arr = arr.T
            rows, cols = arr.shape
        channels = rows
        interleaved = arr.T.reshape(-1)   # -> (samples, channels) flattened, interleaved
    else:
        raise ValueError(f"play() expects 1-D or 2-D audio, got shape {arr.shape}")

    pcm = _to_int16(interleaved)
    nframes = pcm.size // channels

    bio = io.BytesIO()
    with wave.open(bio, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)               # int16 -> 2 bytes
        w.setframerate(int(sr))
        w.writeframes(pcm.tobytes())

    return _Audio(bio.getvalue(), int(sr), nframes, channels)


_KERNEL_HINT = (
    "This needs torchaudio (the optional 'kernel' extra). Install it with: "
    "pip install 'solveit-sidekick[kernel]'  (or: uv sync --extra kernel)."
)


def _mono(x) -> np.ndarray:
    """Return a 1-D float32 mono view of ``x`` (channels-first stereo is averaged)."""
    arr = np.asarray(_to_numpy(x), dtype="float32")
    if arr.ndim > 1:
        arr = arr.mean(axis=0)          # channels-first -> mono
    return arr


def _log_mel(x, sr: int, n_mels: int, n_fft: int, hop: int) -> np.ndarray:
    """Compute a log-mel spectrogram (mel bins x frames) via torchaudio.

    Lazy-imports torch/torchaudio and raises a clear ImportError (with an install
    hint) when the optional ``kernel`` extra is not installed.
    """
    try:
        import torch
        import torchaudio
    except ImportError as e:                     # torchaudio is in the 'kernel' extra
        raise ImportError(_KERNEL_HINT) from e

    wav = torch.as_tensor(_mono(x), dtype=torch.float32)
    mel = torchaudio.transforms.MelSpectrogram(
        sample_rate=int(sr), n_fft=n_fft, hop_length=hop, n_mels=n_mels)(wav)
    log_mel = torchaudio.transforms.AmplitudeToDB(stype="power")(mel)
    return log_mel.detach().cpu().numpy()


def _draw_spec(ax, log_mel: np.ndarray, sr: int, hop: int, *, cmap="magma"):
    """Paint a log-mel spectrogram onto ``ax`` and return the image handle."""
    duration = log_mel.shape[-1] * hop / sr
    im = ax.imshow(log_mel, origin="lower", aspect="auto", cmap=cmap,
                   extent=[0.0, duration, 0, log_mel.shape[0]])
    ax.set_xlabel("time (s)")
    ax.set_ylabel("mel bin")
    return im


def spec(x, sr: int = 44100, n_mels: int = 128, n_fft: int = 2048, hop: int = 512):
    """Log-mel spectrogram of ``x`` as a labelled matplotlib Figure (with colorbar).

    Returns the Figure — the kernel captures matplotlib figures automatically, so
    just call it in a cell. Requires the ``kernel`` extra (torchaudio); see the
    module docstring. ``x`` follows the ``play()`` input convention (stereo is
    downmixed to mono).
    """
    import matplotlib.pyplot as plt

    log_mel = _log_mel(x, sr, n_mels, n_fft, hop)
    fig, ax = plt.subplots(figsize=(8, 3))
    im = _draw_spec(ax, log_mel, sr, hop)
    fig.colorbar(im, ax=ax, label="dB")
    fig.tight_layout()
    return fig


def wavespec(x, sr: int = 44100, **kw):
    """Waveform (top) stacked over its log-mel spectrogram (bottom), shared time axis.

    Reuses ``spec()``'s log-mel computation. Accepts the same ``n_mels`` / ``n_fft``
    / ``hop`` keywords. Returns a matplotlib Figure; requires the ``kernel`` extra.
    """
    import matplotlib.pyplot as plt

    n_mels = kw.get("n_mels", 128)
    n_fft = kw.get("n_fft", 2048)
    hop = kw.get("hop", 512)

    mono = _mono(x)
    log_mel = _log_mel(x, sr, n_mels, n_fft, hop)
    duration = len(mono) / sr

    fig, (ax_w, ax_s) = plt.subplots(
        2, 1, figsize=(8, 5), sharex=True,
        gridspec_kw={"height_ratios": [1, 2]})
    ax_w.plot(np.linspace(0.0, duration, num=len(mono)), mono,
              lw=0.5, color="#378ADD")
    ax_w.set_ylabel("amplitude")
    ax_w.margins(x=0)
    im = _draw_spec(ax_s, log_mel, sr, hop)
    fig.colorbar(im, ax=(ax_w, ax_s), label="dB")
    return fig


def _fig_to_png_b64(fig) -> str:
    """Render a matplotlib Figure to a base64 PNG string (for inline data URIs)."""
    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight", dpi=100)
    return base64.b64encode(buf.getvalue()).decode("ascii")


class _ABView:
    """Container that renders two inline audio players plus a log-mel diff view.

    A single cell result renders one rich output, so ``ab()`` packs everything into
    one HTML block: both ``<audio>`` players (always available, torchaudio-free) and
    — when torchaudio is present — a side-by-side log-mel spectrogram comparison.
    """

    def __init__(self, html: str):
        self._html = html

    def _repr_html_(self) -> str:
        return self._html

    def __repr__(self) -> str:
        return "<ab: original vs reconstruction>"


def ab(original, recon, sr: int = 44100, **kw):
    """A/B compare two signals for audio-model dissection.

    Returns an ``_ABView`` whose ``_repr_html_`` embeds TWO inline ``<audio>``
    players (original vs reconstruction, via ``play()``) and a difference view:
    the two log-mel spectrograms side by side.

    Design choice: the audio players are built from ``play()._repr_audio_wav_()``
    (stdlib only) so the A/B listening half ALWAYS works, even without torchaudio.
    The spectrogram-diff half needs the ``kernel`` extra; when torchaudio is absent
    it is gracefully omitted and replaced with an install note rather than raising.
    Accepts the same ``n_mels`` / ``n_fft`` / ``hop`` keywords as ``spec()``.
    """
    orig_b64 = play(original, sr)._repr_audio_wav_()
    recon_b64 = play(recon, sr)._repr_audio_wav_()

    players = (
        '<div style="display:flex;gap:24px;flex-wrap:wrap;align-items:center;">'
        f'<div><div style="font:600 13px sans-serif;margin-bottom:4px;">original</div>'
        f'<audio controls src="data:audio/wav;base64,{orig_b64}"></audio></div>'
        f'<div><div style="font:600 13px sans-serif;margin-bottom:4px;">reconstruction</div>'
        f'<audio controls src="data:audio/wav;base64,{recon_b64}"></audio></div>'
        '</div>'
    )

    # Spectrogram-diff half — optional, needs torchaudio (the 'kernel' extra).
    try:
        import matplotlib.pyplot as plt

        n_mels = kw.get("n_mels", 128)
        n_fft = kw.get("n_fft", 2048)
        hop = kw.get("hop", 512)
        lm_o = _log_mel(original, sr, n_mels, n_fft, hop)
        lm_r = _log_mel(recon, sr, n_mels, n_fft, hop)
        vmin = min(lm_o.min(), lm_r.min())
        vmax = max(lm_o.max(), lm_r.max())
        fig, (a0, a1) = plt.subplots(1, 2, figsize=(10, 3), sharey=True)
        for ax, lm, name in ((a0, lm_o, "original"), (a1, lm_r, "reconstruction")):
            im = _draw_spec(ax, lm, sr, hop)
            im.set_clim(vmin, vmax)
            ax.set_title(name, fontsize=10)
        a1.set_ylabel("")
        fig.colorbar(im, ax=(a0, a1), label="dB")
        png = _fig_to_png_b64(fig)
        plt.close(fig)
        diff = (f'<div style="margin-top:12px;">'
                f'<img style="max-width:100%;" '
                f'src="data:image/png;base64,{png}"/></div>')
    except ImportError:
        diff = ('<p style="font:italic 12px sans-serif;color:#888;margin-top:12px;">'
                "log-mel comparison unavailable: install the kernel extra "
                "(pip install 'solveit-sidekick[kernel]') to see the spectrogram diff."
                "</p>")

    return _ABView(f'<div>{players}{diff}</div>')
