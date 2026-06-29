"""Draw PlotNeuralNet-style 3-D convolution-block diagrams with matplotlib.

Why this exists: reading DL papers, you want to *see* tensor shapes flow through
a net — the spatial/time axis shrinking while channels grow. PlotNeuralNet gives
that look but needs a LaTeX toolchain. This renders the same volume-block metaphor
in pure matplotlib, so it shows up inline through the kernel's existing rich-output
path with no extra system dependencies.

Output formats (both flow through server.kernel_server):
  - fmt="svg" (default): crisp, scalable vector — returns an object with
    _repr_svg_, rendered inline as image/svg+xml (needs the kernel's _repr_svg_
    branch + the app's cell-svg case, both wired up alongside this module).
  - fmt="png": returns the matplotlib Figure, captured by _capture_figs. Handy if
    you want to keep tweaking the fig, but the cell also prints "<Figure …>"; end
    the cell with ';' to hide that.

Quick start:

    from sidekick.conv_arch import conv_arch, PRESETS, from_torch
    conv_arch(PRESETS["oobleck"], title="Oobleck VAE")        # a bundled example
    conv_arch([(2, 65536, "in"), (128, 8192, "down"),         # terse tuples
               (2048, 32, "z"), (128, 32768, "up")])
    from_torch(my_model, (1, 3, 224, 224))                    # introspect a net

Each layer is a dict (or a tuple — see _norm) with:
    channels : int  -> slab width + depth (log-scaled): the # of feature maps
    size     : int  -> slab height (log-scaled): spatial/time extent (H*W or T)
    label    : str  -> caption under the slab (optional)
    group    : str  -> "enc" | "lat" | "dec" for default coloring (optional),
                       or pass `color` directly to override.
"""
from __future__ import annotations

import io
import math

# Default palette keyed by group. Each entry is (front, top, side) — the three
# visible cube faces, light-to-dark, so the slab reads as a solid volume.
_PALETTE = {
    "enc": ("#1D9E75", "#5DCAA5", "#0F6E56"),   # teal  — encoder
    "lat": ("#7F77DD", "#AFA9EC", "#534AB7"),   # purple — latent bottleneck
    "dec": ("#D85A30", "#F0997B", "#993C1D"),   # coral — decoder
    None:  ("#378ADD", "#85B7EB", "#185FA5"),   # blue  — default / ungrouped
}


class _Svg:
    """A tiny carrier so an SVG string renders inline via the IPython display
    protocol (server.kernel_server._rich_repr -> image/svg+xml)."""

    def __init__(self, svg: str):
        self.svg = svg

    def _repr_svg_(self) -> str:
        return self.svg

    def __repr__(self) -> str:  # keep the text echo terse if it ever shows
        return f"<conv_arch diagram: {len(self.svg)} bytes svg>"


def _norm(layer) -> dict:
    """Accept dicts or terse tuples so cells stay readable.

    (channels, size)                -> minimal
    (channels, size, label)         -> + caption
    (channels, size, label, group)  -> + color group
    """
    if isinstance(layer, dict):
        return layer
    keys = ("channels", "size", "label", "group")
    return dict(zip(keys, layer))


def _faces(color):
    """Resolve a layer's (front, top, side) face colors. A `color` group key
    picks from the palette; an explicit hex string is shaded into three tones."""
    if color in _PALETTE:
        return _PALETTE[color]
    import matplotlib.colors as mc
    r, g, b = mc.to_rgb(color)
    light = tuple(min(1.0, c + (1 - c) * 0.45) for c in (r, g, b))
    dark = tuple(c * 0.65 for c in (r, g, b))
    return color, light, dark


def _draw(specs, title, depth_scale, height_scale, width_scale,
          base_width, base_depth, gap, arrows):
    """Build and return the matplotlib Figure. Channels drive BOTH the slab width
    and the 3-D extrusion depth (so channel growth is unmistakable); the spatial
    `size` drives height. An encoder then reads as an hourglass — tall-thin slabs
    narrowing to a short-but-chunky bottleneck."""
    import matplotlib.pyplot as plt
    from matplotlib.patches import Rectangle, Polygon, FancyArrowPatch

    def H(size):  return base_width + height_scale * math.log2(max(size, 1))
    def W(ch):    return base_width + width_scale * math.log2(max(ch, 1))
    def D(ch):    return base_depth + depth_scale * math.log2(max(ch, 1))

    widths = [W(s.get("channels", 1)) for s in specs]
    heights = [H(s.get("size", 1)) for s in specs]
    depths = [D(s.get("channels", 1)) for s in specs]
    max_h = max(heights) + max(depths)
    total_w = sum(w + d for w, d in zip(widths, depths)) + gap * (len(specs) - 1)

    fig, ax = plt.subplots(figsize=(min(15, max(6, total_w * 0.85)),
                                    max(2.8, max_h * 0.7 + 1.3)))
    ax.set_aspect("equal")
    ax.axis("off")

    cy = 0.0
    x = 0.0
    centers = []
    for s, w, h, d in zip(specs, widths, heights, depths):
        front, top, side = _faces(s.get("color", s.get("group")))
        y0, y1 = cy - h / 2, cy + h / 2
        # right (dark) + top (light) faces, then front (mid) painted over them
        ax.add_patch(Polygon([(x + w, y0), (x + w + d, y0 + d),
                              (x + w + d, y1 + d), (x + w, y1)],
                             closed=True, facecolor=side, edgecolor="none"))
        ax.add_patch(Polygon([(x, y1), (x + d, y1 + d),
                              (x + w + d, y1 + d), (x + w, y1)],
                             closed=True, facecolor=top, edgecolor="none"))
        ax.add_patch(Rectangle((x, y0), w, h, facecolor=front,
                               edgecolor="#ffffff", linewidth=0.6))
        cx = x + w / 2
        cap, ch = str(s.get("label", "")), s.get("channels", "")
        below = f"{cap}\n{ch}" if cap else f"{ch}"
        ax.text(cx, y0 - 0.5, below, ha="center", va="top", fontsize=8,
                color="#2C2C2A", linespacing=1.3)
        ax.text(cx, y1 + d + 0.28, str(s.get("size", "")), ha="center",
                va="bottom", fontsize=7, color="#888780")
        centers.append((x, x + w, d))
        x += w + d + gap

    if arrows:
        for (l0, r0, d0), (l1, r1, d1) in zip(centers, centers[1:]):
            ax.add_patch(FancyArrowPatch(
                (r0 + d0 + 0.06, 0), (l1 - 0.06, 0),
                arrowstyle="-|>", mutation_scale=9, lw=1.0,
                color="#B4B2A9", shrinkA=0, shrinkB=0))

    ax.set_xlim(-0.5, x - gap + 0.5)
    ax.set_ylim(-max_h / 2 - 1.5, max_h / 2 + 1.2)
    if title:
        ax.set_title(title, fontsize=12, color="#2C2C2A", pad=10)
    fig.tight_layout()
    return fig


def conv_arch(layers, title=None, *, fmt="svg", depth_scale=0.16,
              height_scale=0.62, width_scale=0.20, base_width=0.5,
              base_depth=0.3, gap=0.55, arrows=True):
    """Render a list of layer specs as a row of 3-D conv slabs.

    fmt="svg" (default) returns a vector diagram (inline, scalable); fmt="png"
    returns the matplotlib Figure (captured as a raster image). Returns whatever
    the kernel can display — just call it in a cell.
    """
    specs = [_norm(layer) for layer in layers]
    fig = _draw(specs, title, depth_scale, height_scale, width_scale,
                base_width, base_depth, gap, arrows)
    if fmt == "svg":
        import matplotlib.pyplot as plt
        buf = io.StringIO()
        fig.savefig(buf, format="svg", bbox_inches="tight")
        plt.close(fig)                       # so _capture_figs doesn't also emit a PNG
        svg = buf.getvalue()
        svg = svg[svg.index("<svg"):]        # strip xml decl/doctype for clean inline
        return _Svg(svg)
    return fig                                # png: captured by _capture_figs


# Default module types worth drawing — shape-changing layers, not activations/norms.
_TORCH_KINDS = ("conv", "linear", "pool", "convtranspose")


def from_torch(model, input_size, *, title=None, fmt="svg",
               kinds=_TORCH_KINDS, collapse=True, **kw):
    """Introspect a PyTorch nn.Module and draw it from its real per-layer shapes.

    Runs one forward pass on a zero tensor of `input_size` (e.g. (1, 3, 224, 224)),
    capturing each interesting leaf module's output shape via forward hooks. So you
    point it at a model instead of hand-typing dims.

      from_torch(torchvision.models.resnet18(), (1, 3, 224, 224))

    `kinds` filters which leaf modules to draw (substring match on the class name,
    lower-cased). `collapse=True` drops consecutive layers with the same (channels,
    size) so repeated blocks don't pile up. Transposed convs are colored as decoder.
    """
    import torch

    captured: list[dict] = []

    def hook(module, _inp, out):
        if isinstance(out, (tuple, list)):
            out = out[0]
        shape = getattr(out, "shape", None)
        if shape is None:
            return
        shape = tuple(int(d) for d in shape)
        if len(shape) >= 3:                     # (N, C, *spatial)
            ch, size = shape[1], math.prod(shape[2:])
        elif len(shape) == 2:                   # (N, features)
            ch, size = shape[1], 1
        else:
            ch, size = shape[-1], 1
        name = type(module).__name__
        grp = "dec" if "convtranspose" in name.lower() else "enc"
        captured.append({"label": name.replace("Conv", "C").replace("Transpose", "T"),
                         "channels": ch, "size": size, "group": grp})

    handles = []
    for module in model.modules():
        if list(module.children()):             # not a leaf
            continue
        if any(k in type(module).__name__.lower() for k in kinds):
            handles.append(module.register_forward_hook(hook))
    try:
        was_training = model.training
        model.eval()
        with torch.no_grad():
            model(torch.zeros(*input_size))
    finally:
        for h in handles:
            h.remove()
        if was_training:
            model.train()

    layers = captured
    if collapse:                                # fold runs of identical shape
        folded = []
        for layer in captured:
            key = (layer["channels"], layer["size"])
            if folded and (folded[-1]["channels"], folded[-1]["size"]) == key:
                continue
            folded.append(layer)
        layers = folded
    return conv_arch(layers, title=title, fmt=fmt, **kw)


# A few ready-made specs so the helper is discoverable: conv_arch(PRESETS["unet"]).
PRESETS = {
    # Stable Audio's Oobleck VAE (AutoencoderOobleck defaults), example T=65536.
    "oobleck": [
        {"label": "in",   "channels": 2,    "size": 65536, "group": "enc"},
        {"label": "conv", "channels": 128,  "size": 65536, "group": "enc"},
        {"label": "down", "channels": 128,  "size": 32768, "group": "enc"},
        {"label": "down", "channels": 256,  "size": 8192,  "group": "enc"},
        {"label": "down", "channels": 512,  "size": 2048,  "group": "enc"},
        {"label": "down", "channels": 1024, "size": 256,   "group": "enc"},
        {"label": "down", "channels": 2048, "size": 32,    "group": "enc"},
        {"label": "z",    "channels": 64,   "size": 32,    "group": "lat"},
        {"label": "up",   "channels": 1024, "size": 256,   "group": "dec"},
        {"label": "up",   "channels": 512,  "size": 2048,  "group": "dec"},
        {"label": "up",   "channels": 256,  "size": 8192,  "group": "dec"},
        {"label": "up",   "channels": 128,  "size": 32768, "group": "dec"},
        {"label": "out",  "channels": 2,    "size": 65536, "group": "dec"},
    ],
    # A small U-Net (encoder/decoder with a bottleneck), 256x256 input.
    "unet": [
        {"label": "in",    "channels": 3,   "size": 65536, "group": "enc"},
        {"label": "enc1",  "channels": 64,  "size": 65536, "group": "enc"},
        {"label": "enc2",  "channels": 128, "size": 16384, "group": "enc"},
        {"label": "enc3",  "channels": 256, "size": 4096,  "group": "enc"},
        {"label": "enc4",  "channels": 512, "size": 1024,  "group": "enc"},
        {"label": "bneck", "channels": 1024, "size": 256,  "group": "lat"},
        {"label": "dec4",  "channels": 512, "size": 1024,  "group": "dec"},
        {"label": "dec3",  "channels": 256, "size": 4096,  "group": "dec"},
        {"label": "dec2",  "channels": 128, "size": 16384, "group": "dec"},
        {"label": "dec1",  "channels": 64,  "size": 65536, "group": "dec"},
        {"label": "out",   "channels": 1,   "size": 65536, "group": "dec"},
    ],
    # ResNet-style classifier stem->stages->head, 224x224 input.
    "resnet": [
        {"label": "in",    "channels": 3,    "size": 50176, "group": "enc"},
        {"label": "conv1", "channels": 64,   "size": 12544, "group": "enc"},
        {"label": "layer1", "channels": 256, "size": 3136,  "group": "enc"},
        {"label": "layer2", "channels": 512, "size": 784,   "group": "enc"},
        {"label": "layer3", "channels": 1024, "size": 196,  "group": "enc"},
        {"label": "layer4", "channels": 2048, "size": 49,   "group": "enc"},
        {"label": "pool",  "channels": 2048, "size": 1,     "group": "lat"},
        {"label": "fc",    "channels": 1000, "size": 1,     "group": "dec"},
    ],
}
