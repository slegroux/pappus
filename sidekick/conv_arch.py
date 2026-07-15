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
    channels : int        -> slab width + depth (log-scaled): the # of feature maps
    size     : int|tuple  -> slab height (log-scaled): spatial extent. Pass (H, W)
                             — shown as "112×112" — or a scalar area like 12544.
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


# Human-readable names for the color groups, shown in the legend.
_GROUP_NAMES = {"enc": "encoder", "lat": "latent", "dec": "decoder"}


def _fmt(n):
    """Thousands-separated size, so 65536 reads as 65,536."""
    return f"{n:,}" if isinstance(n, int) else str(n)


def _spatial(size):
    """Map a layer's `size` to (area_for_scaling, readable_label).

    A tuple/list is the explicit spatial shape: (112, 112) -> area 12544, shown
    as "112×112" (far clearer than the flattened product). A scalar is taken as
    the area directly and shown comma-separated.
    """
    if isinstance(size, (tuple, list)):
        dims = [int(d) for d in size]
        area = 1
        for d in dims:
            area *= max(d, 1)
        return max(area, 1), "×".join(_fmt(d) for d in dims)
    if isinstance(size, (int, float)):
        return max(int(size), 1), _fmt(int(size))
    return 1, str(size)


def _draw(specs, title, depth_scale, height_scale, width_scale,
          base_width, base_depth, gap, arrows, legend):
    """Build and return the matplotlib Figure. Channels drive BOTH the slab width
    and the 3-D extrusion depth (so channel growth is unmistakable); the spatial
    `size` drives height. An encoder then reads as an hourglass — tall-thin slabs
    narrowing to a short-but-chunky bottleneck.

    Labels make the encoding explicit: the number ABOVE each block is its spatial
    size, the line BELOW is the layer name + channel count, a legend keys the
    colors, and a caption spells out what height/thickness mean."""
    import matplotlib.pyplot as plt
    from matplotlib.patches import Rectangle, Polygon, FancyArrowPatch, Patch

    def H(size):  return base_width + height_scale * math.log2(max(size, 1))
    def W(ch):    return base_width + width_scale * math.log2(max(ch, 1))
    def D(ch):    return base_depth + depth_scale * math.log2(max(ch, 1))

    spatial = [_spatial(s.get("size", 1)) for s in specs]      # (area, label) each
    widths = [W(s.get("channels", 1)) for s in specs]
    heights = [H(area) for area, _ in spatial]
    depths = [D(s.get("channels", 1)) for s in specs]
    max_h = max(heights) + max(depths)
    total_w = sum(w + d for w, d in zip(widths, depths)) + gap * (len(specs) - 1)

    fig, ax = plt.subplots(figsize=(min(15, max(6.5, total_w * 0.85)),
                                    max(3.0, max_h * 0.5 + 1.4)))
    ax.set_aspect("equal")
    ax.axis("off")

    cy = 0.0
    x = 0.0
    centers = []
    for i, (s, w, h, d) in enumerate(zip(specs, widths, heights, depths)):
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
        # below: layer name + channel count; above: spatial size (with units the
        # first time, so the reader learns which number is which).
        cap, ch = str(s.get("label", "")), s.get("channels", "")
        chtxt = f"{ch} ch" if i == 0 else f"{ch}"
        below = f"{cap}\n{chtxt}" if cap else chtxt
        ax.text(cx, y0 - 0.55, below, ha="center", va="top", fontsize=8,
                color="#2C2C2A", linespacing=1.3)
        # spatial label above: "112×112" when dims are known, else the area
        ax.text(cx, y1 + d + 0.28, spatial[i][1], ha="center", va="bottom",
                fontsize=7, color="#888780")
        centers.append((x, x + w, d))
        x += w + d + gap

    if arrows:
        for (_l0, r0, d0), (l1, _r1, _d1) in zip(centers, centers[1:]):
            ax.add_patch(FancyArrowPatch(
                (r0 + d0 + 0.06, 0), (l1 - 0.06, 0),
                arrowstyle="-|>", mutation_scale=9, lw=1.0,
                color="#B4B2A9", shrinkA=0, shrinkB=0))

    ax.set_xlim(-0.5, x - gap + 0.5)
    ax.set_ylim(-max_h / 2 - 1.7, max_h / 2 + 1.2)

    if legend:
        # Color key — only the groups actually present, in first-seen order.
        seen, handles = set(), []
        for s in specs:
            g = s.get("group")
            if g in _GROUP_NAMES and g not in seen:
                seen.add(g)
                handles.append(Patch(facecolor=_PALETTE[g][0], edgecolor="none",
                                     label=_GROUP_NAMES[g]))
        if handles:
            ax.legend(handles=handles, loc="upper center",
                      bbox_to_anchor=(0.5, -0.02), ncol=len(handles),
                      frameon=False, fontsize=8, handlelength=1.1,
                      columnspacing=1.6, handletextpad=0.5)
        # Encoding key, anchored just under the legend (axes-relative, so it hugs
        # the blocks instead of floating at the figure's bottom edge). Name the
        # spatial label as height×width when any layer carries explicit 2-D dims.
        two_d = any(isinstance(s.get("size"), (tuple, list)) and len(s["size"]) >= 2
                    for s in specs)
        spatial_word = "spatial size, height×width" if two_d else "spatial size"
        ax.text(0.5, -0.14,
                f"block height = {spatial_word} (above)   ·   "
                "width + depth = channels (below)",
                transform=ax.transAxes, ha="center", va="top",
                fontsize=7.5, color="#888780")

    if title:
        ax.set_title(title, fontsize=12, color="#2C2C2A", pad=10)
    return fig                                 # savefig(bbox_inches="tight") crops margins


def conv_arch(layers, title=None, *, fmt="svg", depth_scale=0.16,
              height_scale=0.62, width_scale=0.20, base_width=0.5,
              base_depth=0.3, gap=0.55, arrows=True, legend=True):
    """Render a list of layer specs as a row of 3-D conv slabs.

    fmt="svg" (default) returns a vector diagram (inline, scalable); fmt="png"
    returns the matplotlib Figure (captured as a raster image). Returns whatever
    the kernel can display — just call it in a cell. `legend=True` adds a color
    key and an encoding caption that spell out what the geometry means.
    """
    specs = [_norm(layer) for layer in layers]
    fig = _draw(specs, title, depth_scale, height_scale, width_scale,
                base_width, base_depth, gap, arrows, legend)
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


def _shape_to_ch_size(shape):
    """Map a captured output shape to (channels, size) for the slab geometry."""
    if len(shape) >= 3:                         # (N, C, *spatial) — keep H,W for the label
        return shape[1], tuple(shape[2:])
    if len(shape) == 2:                         # (N, features)
        return shape[1], 1
    return shape[-1], 1


def _human_count(n: int) -> str:
    """Compact parameter count: 1234 -> '1.2k', 1_500_000 -> '1.5M'."""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(int(n))


def _hook_shapes(model, example, *, kinds=None, all_leaves=False):
    """Run one forward pass and capture each leaf module's output shape via hooks.

    Shared shape-introspection used by both `from_torch` (filtered by `kinds`) and
    `summary` (`all_leaves=True`). `example` is either a size tuple (a zero tensor
    is fed) or an actual input tensor. Returns a list of records preserving forward
    order, each ``{"module", "name", "shape"}``.
    """
    import torch

    records: list[dict] = []

    def make_hook(module):
        def hook(_m, _inp, out):
            if isinstance(out, (tuple, list)):
                out = out[0]
            shape = getattr(out, "shape", None)
            if shape is None:
                return
            records.append({"module": module,
                            "name": type(module).__name__,
                            "shape": tuple(int(d) for d in shape)})
        return hook

    handles = []
    for module in model.modules():
        if list(module.children()):             # not a leaf
            continue
        if all_leaves or (kinds and any(k in type(module).__name__.lower() for k in kinds)):
            handles.append(module.register_forward_hook(make_hook(module)))

    inp = example if isinstance(example, torch.Tensor) else torch.zeros(*example)
    was_training = False                          # set before try: if reading
    try:                                          # model.training raises, `finally`
        was_training = model.training             # must not throw NameError over it
        model.eval()
        with torch.no_grad():
            model(inp)
    finally:
        for h in handles:
            h.remove()
        if was_training:
            model.train()
    return records


def from_torch(model, input_size, *, title=None, fmt="svg",
               kinds=_TORCH_KINDS, collapse=True, **kw):
    """Introspect a PyTorch nn.Module and draw it from its real per-layer shapes.

    Runs one forward pass on a zero tensor of `input_size` (e.g. (1, 3, 224, 224)),
    capturing each interesting leaf module's output shape via forward hooks. So you
    point it at a model instead of hand-typing dims. Each block is annotated with
    its parameter count (below the layer name), so heavy layers stand out.

      from_torch(torchvision.models.resnet18(), (1, 3, 224, 224))

    `kinds` filters which leaf modules to draw (substring match on the class name,
    lower-cased). `collapse=True` drops consecutive layers with the same (channels,
    size) so repeated blocks don't pile up. Transposed convs are colored as decoder.
    """
    captured: list[dict] = []
    for rec in _hook_shapes(model, input_size, kinds=kinds):
        ch, size = _shape_to_ch_size(rec["shape"])
        name = rec["name"]
        grp = "dec" if "convtranspose" in name.lower() else "enc"
        params = sum(p.numel() for p in rec["module"].parameters())
        short = name.replace("Conv", "C").replace("Transpose", "T")
        label = f"{short}\n{_human_count(params)}p" if params else short
        captured.append({"label": label, "channels": ch, "size": size, "group": grp})

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


class _Table:
    """Carrier so an HTML table renders inline via the IPython display protocol."""

    def __init__(self, html: str):
        self._html = html

    def _repr_html_(self) -> str:
        return self._html

    def __repr__(self) -> str:
        return "<conv_arch summary table>"


def summary(model, input):
    """Per-layer summary table (name / output shape / param count) as rich HTML.

    Reuses the same forward-hook shape-introspection as `from_torch` (`all_leaves`),
    running the model on the given `input` tensor and reporting every leaf module in
    forward order. Returns an object whose `_repr_html_` is a table — just call it
    in a cell.

      summary(model, torch.randn(1, 3, 224, 224))

    MACs are intentionally omitted: reliable per-layer MAC counts need op-specific
    formulas beyond the shape capture the forward hooks provide, so output shapes and
    parameter counts are reported instead (a note in the table's footer says so).
    """
    rows, total = [], 0
    for rec in _hook_shapes(model, input, all_leaves=True):
        params = sum(p.numel() for p in rec["module"].parameters())
        total += params
        shape_str = "×".join(str(d) for d in rec["shape"])
        rows.append((rec["name"], shape_str, params))

    body = "".join(
        f'<tr><td style="padding:2px 12px 2px 0;">{name}</td>'
        f'<td style="padding:2px 12px 2px 0;font-family:monospace;">{shape}</td>'
        f'<td style="padding:2px 0;text-align:right;">{params:,}</td></tr>'
        for name, shape, params in rows)
    html = (
        '<table style="border-collapse:collapse;font:13px sans-serif;">'
        '<thead><tr style="border-bottom:1px solid #ccc;text-align:left;">'
        '<th style="padding:2px 12px 2px 0;">layer</th>'
        '<th style="padding:2px 12px 2px 0;">output shape</th>'
        '<th style="padding:2px 0;text-align:right;">params</th></tr></thead>'
        f'<tbody>{body}</tbody>'
        '<tfoot><tr style="border-top:1px solid #ccc;font-weight:600;">'
        '<td colspan="2" style="padding:2px 12px 2px 0;">total</td>'
        f'<td style="padding:2px 0;text-align:right;">{total:,}</td></tr></tfoot>'
        '</table>'
        '<div style="font:italic 11px sans-serif;color:#888;margin-top:4px;">'
        'MACs omitted — needs op-specific formulas beyond forward-hook shapes.</div>')
    return _Table(html)


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
        {"label": "in",    "channels": 3,    "size": (256, 256), "group": "enc"},
        {"label": "enc1",  "channels": 64,   "size": (256, 256), "group": "enc"},
        {"label": "enc2",  "channels": 128,  "size": (128, 128), "group": "enc"},
        {"label": "enc3",  "channels": 256,  "size": (64, 64),   "group": "enc"},
        {"label": "enc4",  "channels": 512,  "size": (32, 32),   "group": "enc"},
        {"label": "bneck", "channels": 1024, "size": (16, 16),   "group": "lat"},
        {"label": "dec4",  "channels": 512,  "size": (32, 32),   "group": "dec"},
        {"label": "dec3",  "channels": 256,  "size": (64, 64),   "group": "dec"},
        {"label": "dec2",  "channels": 128,  "size": (128, 128), "group": "dec"},
        {"label": "dec1",  "channels": 64,   "size": (256, 256), "group": "dec"},
        {"label": "out",   "channels": 1,    "size": (256, 256), "group": "dec"},
    ],
    # ResNet-50 classifier stem->stages->head, 224x224 input.
    "resnet": [
        {"label": "in",     "channels": 3,    "size": (224, 224), "group": "enc"},
        {"label": "conv1",  "channels": 64,   "size": (112, 112), "group": "enc"},
        {"label": "layer1", "channels": 256,  "size": (56, 56),   "group": "enc"},
        {"label": "layer2", "channels": 512,  "size": (28, 28),   "group": "enc"},
        {"label": "layer3", "channels": 1024, "size": (14, 14),   "group": "enc"},
        {"label": "layer4", "channels": 2048, "size": (7, 7),     "group": "enc"},
        {"label": "pool",   "channels": 2048, "size": (1, 1),     "group": "lat"},
        {"label": "fc",     "channels": 1000, "size": 1,          "group": "dec"},
    ],
    # --- Audio models (schematic; 1-D time axis, sizes are #samples/frames) ---
    # EnCodec-style neural codec: conv encoder downsamples to an RVQ latent, then
    # a mirror decoder reconstructs. ~24kHz, 1s clip, strides ~(2,4,5,8).
    "encodec": [
        {"label": "in",      "channels": 1,    "size": 24000, "group": "enc"},
        {"label": "conv",    "channels": 32,   "size": 24000, "group": "enc"},
        {"label": "down",    "channels": 64,   "size": 12000, "group": "enc"},
        {"label": "down",    "channels": 128,  "size": 3000,  "group": "enc"},
        {"label": "down",    "channels": 256,  "size": 600,   "group": "enc"},
        {"label": "down",    "channels": 512,  "size": 75,    "group": "enc"},
        {"label": "z (RVQ)", "channels": 128,  "size": 75,    "group": "lat"},
        {"label": "up",      "channels": 512,  "size": 75,    "group": "dec"},
        {"label": "up",      "channels": 256,  "size": 600,   "group": "dec"},
        {"label": "up",      "channels": 128,  "size": 3000,  "group": "dec"},
        {"label": "up",      "channels": 64,   "size": 12000, "group": "dec"},
        {"label": "out",     "channels": 1,    "size": 24000, "group": "dec"},
    ],
    # HiFi-GAN-style vocoder (decoder only): a mel spectrogram is transposed-conv
    # upsampled back to a raw waveform.
    "hifigan": [
        {"label": "mel",  "channels": 80,  "size": 32,   "group": "lat"},
        {"label": "conv", "channels": 512, "size": 32,   "group": "dec"},
        {"label": "up×8", "channels": 256, "size": 256,  "group": "dec"},
        {"label": "up×8", "channels": 128, "size": 2048, "group": "dec"},
        {"label": "up×2", "channels": 64,  "size": 4096, "group": "dec"},
        {"label": "up×2", "channels": 32,  "size": 8192, "group": "dec"},
        {"label": "wav",  "channels": 1,   "size": 8192, "group": "dec"},
    ],
    # Whisper-encoder-style (encoder only): 2 conv layers (one strided) over a
    # log-mel input, then a stack of constant-width transformer blocks.
    "whisper": [
        {"label": "mel",    "channels": 80,  "size": 3000, "group": "enc"},
        {"label": "conv1",  "channels": 512, "size": 3000, "group": "enc"},
        {"label": "conv2",  "channels": 512, "size": 1500, "group": "enc"},
        {"label": "block×6", "channels": 512, "size": 1500, "group": "enc"},
        {"label": "enc out", "channels": 512, "size": 1500, "group": "lat"},
    ],
}
