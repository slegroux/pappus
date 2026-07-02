# Design note: graph vs. tree — how dialogs relate

Status: **accepted** · First of the `docs/design/` notes.

## The question

As Sidekick grows past single, independent dialogs, two ways of "relating"
dialogs pull in different directions:

- **Graph** — dialogs cross-reference each other (the `#_msgid` message links),
  and a dialog's meaning is *how it connects* to others. This is the
  learning / knowledge-base view.
- **Tree** — a Python library is a hierarchy (`package/module.py`), and building
  one means laying selected cells into that hierarchy. This is the
  export / artifact view.

The instinct is to treat these as competing models and pick one — or to make one
structure (e.g. the sidebar folder hierarchy) serve both. Both instincts are
wrong.

## Decision

**Graph and tree are not competing organizations of the same thing. They are two
different questions asked over the same cells, served by two different
mechanisms, and they are kept orthogonal.**

| Question | Mechanism | Model | Serves |
|---|---|---|---|
| How does my knowledge connect? What does the AI see as context? | dialog names + message links + `build_context` | **graph** | learning, exploration |
| How is the shipped library laid out? | `#\| export <module>` directives → `export.tangle` | **tree** | building, packaging |

`sidekick/export.py` already embodies this: its docstring calls it *"the inverse
of `build_context`."* One serializes cells **into** AI context (graph); the other
emits cells **as** a package (tree). Same cells, opposite direction.

The tree is a **projection built on demand** from a *selected subset* of cells —
not a hierarchy your authoring has to live inside. You author in the graph; you
tangle a chosen subset into a tree when you want a library.

## The load-bearing principle

**Dialog organization and library structure are orthogonal. Never overload
folders to mean package structure.**

- Where a dialog sits in the sidebar (its `/`-separated name) and what it links
  to is for *you* and the AI — the graph. Folders are cosmetic grouping.
- Which module a cell ships to is declared *on the cell*, independent of where
  its dialog lives:

  ```
  #| default_exp core     # this dialog's exported cells → src/<pkg>/core.py
  #| export               # emit this cell into the default module
  #| export utils         # emit this cell into src/<pkg>/utils.py
  ```

This is the nbdev model (fast.ai's own, which SolveIt is built on): a notebook
anywhere can export to any module; notebook location ≠ module location.

## Why this rejects folder-inheritance CRAFT

SolveIt's `CRAFT.ipynb` shares AI context down a **folder tree** (a dialog
inherits context from its parent folders). Ported naively, that uses a *tree*
mechanism (folder inheritance) to solve a *graph* concern (shared context) — the
wrong tool, and it would force folders to mean something semantic, breaking the
orthogonality above.

The graph-native way to get CRAFT's value (reusable shared context) is
**include-by-reference**: a dialog explicitly pulls another dialog's cells into
its context via a link, composing with the message links we already have. No
folder semantics, no reserved `CRAFT` filename, editable as an ordinary dialog.

Note also: Sidekick has no real folders on disk. Dialogs are stored as one JSON
per target, keyed by `/`-separated *name*; the sidebar tree is derived from those
names. So "a file in a folder" isn't even a thing here — another reason the
file-based CRAFT model doesn't map, and a naming/reference convention does.

## Implications / roadmap

- **Learning (graph side):** message links (done). Next candidate:
  *include-by-reference* — a dialog references another and those cells join its
  `build_context`. This is the graph-native replacement for CRAFT.
- **Library (tree side):** `export.py` today tangles a **single** dialog into one
  package. The gap vs. "select a few dialogs/cells and export them" is
  **cross-dialog build**: let one tangle consume `#| export`-marked cells from
  several dialogs into one `src/<pkg>/` tree. Cell-level selection already exists
  (the per-cell Export toggle); module targeting already exists (`#| export
  <module>`). A "library" becomes a named set of dialogs whose exported cells
  build together.

## What to check any future feature against

Before adding a feature that "relates dialogs," ask which question it answers:

- Context / navigation / meaning → it's **graph**. Use names, links, references.
  Do not make it depend on folder position.
- Output / packaging / build layout → it's **tree**. Use `#| export` directives.
  Do not derive it from where dialogs are authored.

If a feature seems to need both, it's probably two features.
