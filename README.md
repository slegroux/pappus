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
# terminal 1 — start the kernel server (add --extra ml for numpy/torch/etc.)
uv run --extra ml python -m server.kernel_server --port 5055

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

When you have access to the real Answer.AI SolveIt server, just point the `local`
or `h100` targets at it (they use `backend: solveit` via `solveit_client`) — same UI.

## ML libraries

The LLM SDKs (anthropic, openai, zhipuai) ship in the base install. The heavier
data-science stack is opt-in so the base stays light:

| Extra | Installs |
|-------|----------|
| `ml`  | numpy, pandas, matplotlib, scikit-learn, scipy, torch |
| `all` | ml + solveit |

Start the kernel server with the ML stack via
`uv run --extra ml python -m server.kernel_server --port 5055`.
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

- **Edit anything** — click into any cell and change its source in place. Cells
  auto-grow to fit their content.
- **Run / re-run** — hover a cell for its **Run** and **Delete** controls, or
  press **Cmd/Ctrl+Enter** inside it. Code re-executes in the persistent kernel
  namespace; an *Ask AI* cell re-asks with the edited prompt.
- **Notes render as markdown** — written server-side (works offline, no CDN), and
  AI answers render as markdown too.

Edits and runs swap only the conversation (via htmx), so re-running a cell never
reloads the whole page.

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

This applies to the bundled **kernel** backend. On a real **solveit** target,
SolveIt's server assembles the dialog context itself.

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
