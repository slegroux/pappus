# SolveIt Sidekick

A clean, Claude-desktop-style interface for [SolveIt](https://solve.it.com) that
runs against **your laptop** or a **remote H100** — switchable from a dropdown in
the top-right, with no change to how you work.

## The core idea

SolveIt's own client (`solveit_client`) already connects to *any* server URL:
it defaults to `http://localhost:5001` and takes `SOLVEIT_URL` for hosted
instances. So "local vs H100" is just **which target the client points at**.

Sidekick turns that into a first-class switch:

```
┌──────────────┐         local  → http://localhost:5001        (laptop)
│  Sidekick UI │ ──────► h100   → http://localhost:5101  ⇢ SSH ⇢ H100:5001
└──────────────┘         (the H100's SolveIt port is tunneled to localhost,
                          so it never touches the public internet)
```

Same UI, same client code, both targets. The only difference is the active
profile in `targets.yaml`.

## What's here

| File | Purpose |
|------|---------|
| `targets.yaml`        | Define `local` and `h100` targets (URL, token, SSH) |
| `sidekick/targets.py` | Load/resolve profiles (token from env, graceful if unset) |
| `sidekick/tunnel.py`  | Open/close the SSH tunnel to the H100, wait until live |
| `sidekick/doctor.py`  | Diagnose connection/setup (DNS, port, token, `/test_route`) |
| `sidekick/client.py`  | Wrap `solveit_client`; fall back to an in-memory mock |
| `sidekick/app.py`     | The web UI (FastHTML), styled like the Claude desktop app |
| `sidekick/cli.py`     | `sidekick targets / doctor / up / serve` |

The mock fallback means the UI runs **with no SolveIt server at all** — handy for
trying the interface or developing it away from the H100. When a real target is
reachable it talks to it directly.

## Setup

This project uses [uv](https://docs.astral.sh/uv/). Install it once
(`curl -LsSf https://astral.sh/uv/install.sh | sh`), then you never run `pip`
or manage a venv by hand — `uv run` reads `pyproject.toml`, creates the venv, and
installs deps on first use.

```bash
# H100 token (your _solveit cookie value; see solveit_client README to grab it)
export SOLVEIT_H100_TOKEN='...'
```

Edit `targets.yaml` and set the `h100.ssh.host` to your H100 (an entry in
`~/.ssh/config`, or `user@1.2.3.4`).

> Prefer pip? It still works: `pip install python-fasthtml pyyaml uvicorn`
> (add `solveit_client` for the real SolveIt server), then drop the `uv run`
> prefix from the commands below.

## Connect a real server right now (no proprietary SolveIt needed)

Answer.AI's hosted SolveIt server image isn't self-hostable, so this repo bundles
a small **SolveIt-compatible kernel server** that genuinely executes code. It's
the fastest way to see the interface go *live* (green LED) end-to-end:

```bash
# terminal 1 — start the kernel server (--extra kernel adds numpy/torch/etc.)
uv run --extra kernel python -m server.kernel_server --port 5055

# terminal 2 — verify and launch
uv run python -m sidekick.cli doctor kernel      # -> all checks pass
SIDEKICK_TARGET=kernel uv run python -m sidekick.cli serve
```

The LLM SDKs (anthropic/openai/zhipuai) are in the base install, so Ask AI works
as soon as you add a key in Settings — no extra flag needed.

The bundled `kernel` target (`backend: kernel` in `targets.yaml`) runs real Python
in a persistent per-dialog namespace — variables carry across cells, just like a
notebook. On the **H100**, run the same server there and `sidekick up h100`
tunnels it back.

> **Security.** `/exec` runs arbitrary code, so the kernel server is secure by
> default: on loopback it's open (local dev), but it **refuses to bind a
> non-loopback host without `--token`** (or `SIDEKICK_KERNEL_TOKEN`), after which
> every request must carry a matching `_solveit` cookie. The H100 flow keeps it on
> `127.0.0.1` and reaches it through the SSH tunnel, so no token is needed; only
> pass `--token` if you expose the port directly. Likewise, `sidekick serve` binds
> `127.0.0.1` by default — set `SIDEKICK_HOST=0.0.0.0` to expose the UI on your LAN.

When you have access to the real Answer.AI SolveIt server, just point the `local`
or `h100` targets at it (they use `backend: solveit` via `solveit_client`) — same UI.

## Two dependency groups: the server vs your coding stack

Sidekick keeps two kinds of dependency apart, because they serve different jobs:

- **The server** (base install) — what *runs* sidekick: FastHTML, uvicorn, the
  markdown/code renderers, and the AI SDKs. Light by design.
- **The coding environment** (`kernel` extra) — what your *notebook code* imports:
  numpy, pandas, matplotlib, scikit-learn, scipy, torch. Kept separate because
  torch is large, and because the kernel server is a standalone HTTP process —
  this stack belongs wherever the kernel actually runs.

| Extra | Installs | Where it belongs |
|-------|----------|------------------|
| (base) | FastHTML, uvicorn, mistune, pygments, AI SDKs | wherever the **app** runs |
| `kernel` (alias `ml`) | numpy, pandas, matplotlib, scikit-learn, scipy, torch | wherever the **kernel** runs |
| `solveit` | solveit_client | app, to talk to a real SolveIt server |
| `all` | kernel + solveit | a full local workstation |

```bash
# one machine, one venv: give the kernel the coding stack
uv run --extra kernel python -m server.kernel_server --port 5055
```

Because the kernel talks to the app over HTTP, you can also **separate the
environments entirely**: run the kernel server in its own venv (or on the H100)
with `[kernel]` installed, and keep the app's venv lean — the app never imports
torch, only the kernel does. (This is exactly the laptop-app ↔ H100-kernel split.)

matplotlib defaults to the headless `Agg` backend on the server, so
`plt.savefig(...)` works without a display.

## Models & API keys

Open the **⚙ Settings** page (gear, top-right) to paste API keys for each
provider — Anthropic (Claude), OpenAI (Codex), Zhipu (GLM). Keys are stored in
`~/.config/solveit-sidekick/secrets.json` (chmod 600, gitignored); an environment
variable of the same name always overrides the file. Claude is the default model,
so once your Anthropic key is set, "Ask AI" talks to your Claude account.

## Use it

```bash
# 1. (remote only) open the tunnel to the H100 in one terminal
uv run python -m sidekick.cli up h100

# 2. check a target is healthy
uv run python -m sidekick.cli doctor h100

# 3. launch the UI
uv run python -m sidekick.cli serve        # http://localhost:8000
```

In the UI, the top-right dropdown flips between `local` and `h100`. A green LED =
live server; amber = running on the mock (with a banner telling you why).

### Editable cells (SolveIt-style)

Every message is a live cell, like a notebook:

- **Rendered by default, click to edit** — notes show as **markdown**, code as
  **syntax-highlighted** Python, prompts show their question + the AI's markdown
  answer. Click any cell to drop into a raw editor; **Save/Run/Ask** commits,
  **Cancel** discards. All rendering and highlighting is server-side (mistune +
  Pygments) so it works offline, no CDN.
- **Run / re-run** — hover a cell for its **Run / In context / Pin / Delete**
  controls, or press **Cmd/Ctrl+Enter** while editing. Code re-executes in the
  persistent kernel namespace; an *Ask AI* cell re-asks with the edited prompt.

Edits and runs swap only the conversation (via htmx), so re-running a cell never
reloads the whole page.

#### Keyboard shortcuts (Jupyter-style)

The notebook has a **command mode** and an **edit mode**, like Jupyter:

| Key | Action |
|-----|--------|
| `Enter` | enter edit mode on the selected cell |
| `Esc` | leave edit mode back to command mode (no edits lost) |
| `↑` / `↓` or `k` / `j` | select the previous / next cell |
| `a` / `b` | insert a cell above / below |
| `y` / `m` / `i` | convert the cell to **code** / **note** (markdown) / **Ask AI** |
| `dd` | delete the selected cell |
| `z` | undo the last delete |
| `Cmd`/`Ctrl`/`Shift`+`Enter` | run the cell |

The **selected** cell shows a green bar on its left. Command-mode keys only fire
when no editor or input is focused, so they never interrupt typing. Each cell's
hover toolbar also has a **⇆ menu** to change its type and a **＋ menu** to
insert **Code / Note / Ask AI** above or below — so the shortcuts are
discoverable. Converting a cell keeps its source text (a prompt's question
becomes the new source) and clears any stale output.

#### The AI sees the notebook

Like a real SolveIt dialog, an *Ask AI* cell isn't answered in isolation — the
**cells above it** (notes, code, code output, prior Q&A) are sent as context, so
you can ask "what did that return?" or "why is this slow?" and it knows.

To keep within the model's token window, context is managed two ways:

- **Truncation (automatic)** — long outputs are middle-out truncated, and if the
  whole notebook exceeds a budget the **oldest** cells drop first (newest are most
  relevant). Tune with `SIDEKICK_CTX_OUT_TRUNC` and `SIDEKICK_CTX_MAX_CHARS`.
- **Mute (manual)** — each cell has an **In context / Muted** toggle. Muting drops
  it from what the AI sees (it dims, but still runs) — the lever for steering
  context and staying under the limit.

Each cell shows an estimated **token count**, and a live meter at the foot of the
dialog reads out the total context (`AI context ≈ N tokens · M/K cells in`) — so
you can see yourself approaching the limit. **Pin** a cell (toggle next to Mute)
to keep it in context even when older cells are trimmed.

This applies to the bundled **kernel** backend. On a real **solveit** target,
SolveIt's server assembles the dialog context itself.

#### Rich output

Code cells render **plots and rich values inline**, not just text: matplotlib
figures are captured as images, and anything implementing the notebook display
protocol (`_repr_html_` for DataFrames, `_repr_png_` for images) renders too. A
trailing `;` suppresses the last expression's value, Jupyter-style.

## Your work is saved

Dialogs on the bundled **kernel** backend are persisted to disk — one JSON file
per target at `~/.config/solveit-sidekick/dialogs-<target>.json` (override the
directory with `SIDEKICK_DATA`). So your notebook survives a restart, and it
survives the backend being rebuilt when you save Settings or switch targets.
Cells, outputs, plots, and pin/mute flags are all restored. (The mock fallback
used when no server is reachable stays ephemeral; a real **solveit** target keeps
its dialogs on the SolveIt server.)

## Reading papers

Open a PDF with the **📄** button (it opens a file picker) and it appears in a
left reading column. Select any passage → an **"Ask AI about this"** button
drops the quoted paragraph into the composer (Ask AI mode), so your question
carries the paragraph as context — then run code, plot, and take notes beside it.

### Step through it, the way Jeremy Howard does

The reading panel has a **Next section ▸** button that works the paper the
*dialogue-engineering* way — small steps, deep understanding — instead of dumping
the whole thing. Each click brings the **next section** into the notebook as a
**note**, followed by an **empty code cell that opens focused** so you can
*reimplement the idea yourself* (writing the code is what builds understanding;
the AI sees everything above, so "is my version equivalent?" just works). A
counter (`2/3`) tracks your progress through the paper. Prefer it all at once?
The **¶** / **§** buttons still bulk-import every paragraph or section as notes.

PDFs are converted to markdown and rendered with the same math/code pipeline as
the rest of the app. The recommended reader is **marker** — install the **`paper`**
extra (`uv pip install "solveit-sidekick[paper]"`, also in `[all]`) and it becomes
the default engine: it preserves structure, tables, and **equations as LaTeX**
(rendered via KaTeX), which is much nicer for ML papers. marker is heavier (torch
+ model downloads, GPL-3.0) and slower, so conversions run in the background and
are cached to disk. Without it, the panel falls back to lightweight **pypdf**
text extraction with no extra setup.

## Pain points this targets

- **Switching local ↔ H100** — one dropdown; the tunnel keeps the URL constant.
- **Setup/environment** — `doctor` tells you *exactly* where a connection breaks
  (DNS? port? token?) instead of a generic failure.
- **Clunky UI/UX** — a calm, single-pane chat-style interface over the dialog
  model (code / ask-AI / note), instead of juggling raw client calls.

## Assumptions (correct me if wrong)

This assumes you run a **SolveIt server** on each box (the laptop and the H100) —
that's what the localhost:5001 default + dummy-token-for-localhost behavior in
`solveit_client` implies is possible. If instead you only use hosted
`solve.it.com` and want the H100 purely as remote *compute attached to* a hosted
instance, the switcher still applies — we'd just point `h100.url` at the hosted
instance URL and drop the SSH tunnel.
```
