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
# one command — start the kernel server AND the UI together (Ctrl+C stops both):
just dev        # → kernel on :5055, UI on http://localhost:8000

# …or the two steps by hand:
# terminal 1 — start the kernel server (--extra kernel adds numpy/torch/etc.)
uv run --extra kernel python -m server.kernel_server --port 5055
# terminal 2 — verify and launch
uv run python -m sidekick.cli doctor kernel      # -> all checks pass
SIDEKICK_TARGET=kernel uv run python -m sidekick.cli serve
```

> `just dev` needs [`just`](https://github.com/casey/just) (`brew install just`).
> `just dev` runs in the **foreground** (Ctrl+C stops). To run it like a service,
> use the background pair **`just start`** / **`just stop`**. Run `just` to see all
> recipes (`start`, `stop`, `dev`, `app`, `kernel`, `ui`, `test`, `doctor`).

### Run it like a Mac app

`just app` builds a double-click **`SolveIt Sidekick.app`** launcher: it starts the
kernel server and the UI (if not already running), then opens the browser to the
app — quitting it stops the servers it launched. Drag it to `/Applications` and
your Dock. It's a thin launcher around the same servers (not a native wrapper),
so it stays light; logs go to `~/Library/Logs/SolveItSidekick/`. Re-run `just app`
if you move the project (the path is baked into the bundle). To stop everything
from the terminal: `just stop`.

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
| `paper` | marker-pdf | app, for high-quality PDF → markdown |
| `web` | trafilatura | app, for clean web-page/blog → markdown |
| `solveit` | solveit_client | app, to talk to a real SolveIt server |
| `all` | kernel + paper + web + solveit | a full local workstation |

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

#### Collapsible sections

A **note that starts with a markdown heading** (`#`, `##`, …) becomes a section
header with a **▾ caret**: click it to fold every cell beneath it, down to the
next heading of the **same or higher level** (so collapsing a `#` folds its `##`
subsections too). The header shows an **"N hidden"** pill, and the collapsed state
is **remembered per dialog**. Folding is **view-only** — hidden cells still run
and still count toward the AI context (use **Mute** to drop a cell from context).

#### Keyboard shortcuts (Jupyter-style)

The notebook has a **command mode** and an **edit mode**, like Jupyter:

| Key | Action |
|-----|--------|
| `Enter` | enter edit mode on the selected cell |
| `Esc` | leave edit mode back to command mode (no edits lost) |
| `↑` / `↓` or `k` / `j` | select the previous / next cell |
| `a` / `b` | insert a **note** cell above / below (use the ＋ menu for code / Ask AI) |
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

#### Code completion

In a code cell, completion suggestions appear **as you type** (SolveIt-style) —
or press **`Ctrl+Space`** to summon them. **`Tab`** (or `Enter`) accepts the
highlighted one; `Esc` dismisses. It's **namespace-aware**: completions come from
the kernel's live state via [jedi](https://github.com/davidhalter/jedi), so after
you run `import numpy as np`, typing `np.ar` offers `arange`; after `df =
pd.read_csv(...)`, `df.` offers its columns and methods. Like Jupyter, it
completes against what you've actually **run** — variables you've typed but not
executed are inferred from the source where possible. Completion needs the
**kernel** backend (it introspects a real namespace); the mock/Claude-only
targets don't offer it, and it's part of the `kernel` extra.

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

#### Inject live values into a prompt

Put `` $`expr` `` anywhere in an *Ask AI* prompt and it's replaced with the value
of that Python expression — evaluated against the **live kernel namespace, fresh
each time you send** (SolveIt's variable-injection syntax). So after you run
`df = pd.read_csv(...)`, asking

> Why might `` $`df.shape` `` rows drop to `` $`len(df.dropna())` `` after dropna?

sends the AI the *actual* numbers, not the literal text. It's the same idea as an
f-string, in the prompt: a bare name (`` $`n` ``) injects its current value, and a
full expression (`` $`len(items)` ``, `` $`df.columns.tolist()` ``) is computed on
the spot. Only the text the AI receives is filled in — the cell keeps the raw
`` $`…` `` so it re-evaluates on every re-ask. A lone `$` (e.g. `$5`) is left
alone, and an expression that errors (a typo'd name) becomes a visible
`` [unresolved `…`] `` marker instead of aborting the prompt, so the AI still sees
the rest of your question.

This needs the **kernel** backend (the expression runs in its namespace); the
mock target leaves `` $`…` `` literal, and a real **solveit** target does its own
injection server-side. Oversized values are truncated so one big object can't blow
the prompt open.

#### The AI can edit cells (Max plan)

Ask AI doesn't only *answer* — it can **edit the notebook for you**. Ask it to
"fix the bug in **cell 3**," "vectorise this loop," or "add a test below," and it
rewrites the target cell (or inserts a new one) in place; the notebook refreshes
to show the change. This is the same idea as SolveIt's `dialoghelper`, which
gives its AI tools to manipulate the dialog — here it runs on your **Claude Max
subscription** (no API credits) via the `claude` CLI plus a tiny, local
[MCP server](server/mcp_cells.py) exposing `list_cells` / `update_cell` /
`str_replace` / `insert_cell`.

**Pointing it at a cell.** Each cell shows a small **number** in its gutter, and
the AI sees that same number — every cell reaches it tagged `n="3" id="…"`. So
you can say "fix cell 3," or just describe it ("the `add` function"), and it
targets the right one without you ever typing an id. Numbers are positional, so
they renumber when you insert, delete, or reorder cells.

It only touches cells when you **explicitly ask**; an ordinary question is still
answered in text. The tools are loopback-only and token-guarded, and active only
on the `claude-cli` (subscription) model. Set `SIDEKICK_CELL_TOOLS=0` to turn
them off (e.g. for the leanest time-to-first-token).

#### …but the right hands, not a free-roaming agent

Notice what those cell tools have in common: they edit the **shared notebook you
can see**, and they never *run* code — you still press run yourself. That's the
SolveIt posture (the human is the agent; the AI is a thinking partner working in
small steps), and it's deliberate.

The catch is that the Max-plan path shells out to `claude -p`, which is the full
Claude Code **agent** — so out of the box it *also* has Write/Edit/Bash. Left
alone it does what agents do: writes the whole solution to a scratchpad file and
executes it off-screen, taking the executor's seat you're supposed to hold and
steamrolling the small-steps [persona](sidekick/claude_cli.py) we append. So the
sidekick launches it with `--disallowed-tools Write Edit Bash`
([sidekick/claude_cli.py](sidekick/claude_cli.py)). The line it draws: the AI may
make **visible, in-notebook** edits when asked, but it can't run code or work
off-screen. This strips only the *spawned assistant's* hands — your own Claude
Code tools are untouched.

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

## Reading papers (and web pages)

Open a source with the **📄** button — **choose a PDF** or **paste any URL** — and
it appears in a left reading column. The URL field **auto-detects** what you give
it:

- An **arXiv** link (`arxiv.org/abs/…`), a **`.pdf`** link, or anything served as
  `application/pdf` is **downloaded and run through marker** (the PDF pipeline —
  equations as LaTeX, tables, structure). So pasting `arxiv.org/abs/1706.03762`
  imports the *paper*, not the abstract page.
- Any other page (a **blog/article**) is fetched and reduced to just the main
  content (no nav/ads/footer): **trafilatura** with the **`web`** extra
  (`uv pip install "solveit-sidekick[web]"`), or a built-in **bs4 + markdownify**
  fallback without it.

Either way the source becomes markdown in the same reader, so the stepper,
highlight-import, and TOC all work the same. **Highlight any passage** and a small
toolbar pops up:

- **→ Notebook** — import the highlighted passage as a **note**, followed by an
  **empty code cell that opens focused**, so you can *reimplement that idea
  yourself*. This is the cherry-pick flow: pull in only the parts that matter,
  skip the rest of the paper.
- **Ask AI ↗** — drop the quoted passage into the composer (Ask AI mode), so your
  next question carries it as context.

You can hide the paper text with the **▾** toggle (the header — name, controls —
stays put) to give the notebook more room.

### Or step through it, the way Jeremy Howard does

Prefer to go front-to-back? The **Next section ▸** button works the paper the
*dialogue-engineering* way — small steps, deep understanding — instead of dumping
the whole thing. Each click brings the **next section** into the notebook as a
**note** + a focused **code cell** to reimplement, and a counter (`2/3`) tracks
your progress (the AI sees everything above, so "is my version equivalent?" just
works). Prefer it all at once? The **¶** / **§** buttons bulk-import every
paragraph or section as notes.

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
