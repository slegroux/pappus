# Design note: graph vs. tree — how dialogs relate

Status: **accepted** (frame consensus-reviewed by Planner/Architect/Critic; roadmap carries named open questions) · First of the `docs/design/` notes.

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
| How is the shipped library laid out? | `#\| export <module>` directives → nbdev build (or `export.tangle`) | **tree** | building, packaging |

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
its context via a reference. No folder semantics, no reserved `CRAFT` filename,
editable as an ordinary dialog.

This is a *new* mechanism, not free reuse of what exists. Today a message link is
inert **navigation** — it renders an `<a>` that scrolls you to a cell. An
**include** is **transclusion**: it needs a new resolver that turns a reference
into the referenced cells and feeds them to `build_context`. Link and include
share *syntax*, not *machinery* — don't mistake "we render a link" for "we resolve
a transclusion." And because an include is the same cells viewed from two places,
it carries a **freshness** question (if A includes B and B changes, is A's context
stale?) — the flip side of the living-knowledge-base direction, and something a
static "concat the cells" implementation would get wrong.

Note also: Sidekick has no real folders on disk. Dialogs are stored as one JSON
per target, keyed by `/`-separated *name*; the sidebar tree is derived from those
names. So "a file in a folder" isn't even a thing here — another reason the
file-based CRAFT model doesn't map, and a naming/reference convention does.

## Implications / roadmap

- **Learning (graph side):** message links (done). Next candidate:
  *include-by-reference* (above) — a dialog references another and those cells
  join its `build_context`. Sound direction; the hard parts are named under
  "Open questions" below, not free.
- **Library (tree side):** the build engine is **nbdev** (decided — see "Build
  engine" below). Sidekick projects a library's tagged cells into nbdev-ready
  notebooks and nbdev builds the package + docs + tests; module assembly and
  collision handling are nbdev's job, not ours. Cell-level selection and module
  targeting already exist (the per-cell Export toggle, `#| export <module>`); what
  is new is the cross-dialog *projection* to notebooks and the library *identity*
  (a manifest) — the crux, see next.

### Library identity is an explicit manifest, not folder-derived

A cross-dialog library must answer "what is this package called, and which
dialogs belong to it?" The tempting answer is "the folder" — but that overloads
folders as package structure, the one thing this note forbids, and an implementer
reaching for "the only grouping that exists" would grab the folder prefix. The
answer that keeps orthogonality is a **manifest**: an explicit artifact (a
`library.json`, or a "manifest" dialog) that *declares* member dialog names and a
target package name. That's a *third* kind of thing — **a selection over the
tree**, neither graph nor tree — and it declares membership exactly the way
`#| export` declares a module target on a cell, rather than inferring it from
sidebar position. Membership is stated, never guessed from where a dialog sits.

**Backend constraint:** cross-dialog features must enumerate dialogs, and the live
SolveIt backend can't — `LiveBackend.list_dialogs()` returns `[]`. So cross-dialog
build (and cross-dialog include-by-reference) is a **kernel/mock backend**
capability today; the live backend needs an enumeration primitive first. Existing
dialogs are **opt-in** to any manifest — nothing is auto-included by folder or
name.

## Cross-dialog libraries (the tree side, in detail)

The manifest becomes concrete as a **cell-centric library**: the library is a
first-class object, and *cells* — not dialogs — subscribe to it. This is the
flexible model. A library can draw cells from many learning dialogs, and one
dialog can feed several libraries; a dialog never has to "belong" to anything.

- **Membership is a sticky per-cell tag.** A cell opts in with
  `#| export <lib>:<module>` (no `<lib>:` prefix → today's behaviour: the dialog's
  own package). Tagging once is enough — editing the cell later does not re-tag it.
- **Identity is a manifest dialog.** A library is registered as a special
  *manifest dialog* holding its package name, nbdev project path, and deps; its
  note cells describe the library (its README / nbdev index). No separate settings
  surface, no folder-derived identity — explicit, and it dogfoods the notebook UI.
- **The library is its live cells; the `.py` is a snapshot.** Two levels of
  "current," and only one needs a build:
  - *Logical content* = the set of tagged cells, **always live**. Browsing a
    library, querying it, or asking the AI to review it reads the cells directly,
    so an edit shows up immediately — no build.
  - *Materialized package* = the built library (`.py` + docs), produced by
    **nbdev** from generated notebooks, refreshed only on an explicit **Build**.
    The cells are the source of truth; the generated notebooks and `.py` are never
    hand-edited. Build carries a **"N cells changed since last build"** staleness
    badge so it is never *silently* stale.
- **A library working-view.** Opening a library assembles its tagged cells into a
  virtual notebook you can run, edit (editing the real underlying cells), and
  extend — cells added there are auto-tagged, and the AI can add to the library on
  request. So "keep working on the library" happens on the cells (graph), through a
  library-scoped lens, not by hopping across dialogs.
- **Provenance closes the loop.** Each tangled cell's header carries a message-ID
  link back to its source cell (`# source: conv/conv1d #_a1b2c3d4`), so the built
  package points back into the dialogs where it was worked out. Graph → tree → back
  to graph.

**Cell ownership — decided: the *lens* model.** A tagged cell stays in its
learning dialog; the library is a view over the cells tagged to it. One cell, two
views — editing it in the library view edits the same cell in its learning dialog
(edits propagate; that's the point). Membership is a sticky tag, and Build cadence
is orthogonal to this choice (the `.py` snapshot needs a Build either way). The
rejected alternative (*graduate* — tagging moves the cell into the library's home)
added ceremony and pulled cells out of the learning context they were written in.

This also resolves two open questions below — identity storage (a manifest dialog)
and provenance (per-cell back-links).

### Build engine — decided: delegate to nbdev (the tool)

Sidekick does **not** build the library itself. A library's tagged cells are
projected into **nbdev-ready `.ipynb` notebooks** (one notebook per module,
carrying `#| default_exp <module>` plus each cell's `#| export`), written into an
**nbdev project** whose path the manifest holds; **nbdev** then builds the `.py`
package, the docs site, the tests, and the release. Sidekick owns the
*graph→notebook* projection; nbdev owns *notebook→library*. `export.py`'s own
tangler is demoted to a lightweight "quick zip, no nbdev needed" convenience.

This settles the earlier **write-target** question: the target is an nbdev project
directory. Source-of-truth chain: **dialog cells (you edit here) → generated
`.ipynb` (never hand-edited) → nbdev → `.py` + docs + tests**. Note the mild
inversion of normal nbdev use — the notebooks are a *generated* hand-off artifact,
so you edit in Sidekick, not in the notebooks, and a re-Build regenerates them.
Consistent with the lens/snapshot decisions, just one hop longer.

**New open sub-questions (nbdev delegation):**
- *Notebook granularity:* one notebook per module (recommended — all cells tagged
  `lib:core` → `core.ipynb`, regardless of source dialog) vs. per source-dialog.
- *Project lifecycle:* **decided.** First build to a fresh dir generates a real
  nbdev `pyproject.toml` with `nbdev_create_config` — **offline** (no GitHub, so no
  rate limit; `nbdev-new`'s template download hits the hardcoded-unauthenticated
  GitHub API and can't be fixed with a token). Rebuilds reuse the existing project
  and just refresh notebooks. Falls back to a minimal `pyproject.toml` if nbdev
  isn't installed. Opt out with `SIDEKICK_NBDEV_SCAFFOLD=0`. The `nbdev-new`
  template extras (LICENSE/CI/docs) are out of scope — run it yourself once.
- *Integration depth:* **decided — Sidekick shells out** to `nbdev-export` on Build
  (one click), best-effort; if nbdev is absent the notebooks are still emitted.
- *Where nbdev runs:* the project's own env, separate from Sidekick's app env.
- *Provenance carrier:* how the `#_id` back-link rides an emitted cell so it
  survives into the `.py` (a comment nbdev passes through).

## Open questions before build

Deliberately unresolved — they belong to each feature's own planning. Naming them
here stops a reader from assuming the roadmap is turnkey (it is not).

**Include-by-reference**
- *Granularity:* whole dialog, a cell range, or a single cell? (The link syntax
  points at one `_id`, implying cell-level; "those cells join context" implies
  whole-dialog — different features.)
- *Budget / mute / pin:* included cells compete for the same
  `SIDEKICK_CTX_MAX_CHARS` budget. Are they muting-aware, pinnable, truncated,
  droppable? Default stance: an include resolves to specific cells and counts
  against the budget like any other cell — so it can't silently blow it and drop
  the user's own newest cells (`build_context` drops oldest non-pinned first).
- *Cycles / diamonds:* A→B→A and shared-ancestor includes need a visited-set and a
  deterministic linearization. `build_context` has no recursion guard today (flat
  scan), so include-by-reference is what *introduces* this.

**Cross-dialog libraries** — build engine decided (delegate to nbdev), identity
decided (manifest dialog), cell ownership decided (lens). Module assembly and
name collisions are now **nbdev's** concern, not ours. Residual questions live
under "Build engine — delegate to nbdev" above (notebook granularity, project
lifecycle, integration depth, provenance carrier) and the manifest's cell schema.

**Out of scope for this frame:** SolveIt's `TEMPLATE.ipynb` and `AUTORUN/` have no
analog in Sidekick and are not addressed here.

## What to check any future feature against

Before adding a feature that "relates dialogs," ask which question it answers:

- Context / navigation / meaning → it's **graph**. Use names, links, references.
  Do not make it depend on folder position.
- Output / packaging / build layout → it's **tree**. Use `#| export` directives.
  Do not derive it from where dialogs are authored.

If a feature seems to need both, it's usually two features — with one exception: a
**build** legitimately reads a **selection** from the graph (which dialogs/cells to
package). That selection is a *manifest* — a third artifact over the tree — not a
violation of the split. The test still holds: keep the two *axes* separable; don't
let authoring position dictate output layout, or vice versa.
