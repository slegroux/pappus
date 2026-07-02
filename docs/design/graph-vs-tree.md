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
- **Library (tree side):** `export.py` today tangles a **single** dialog into one
  package. The gap vs. "select a few dialogs/cells and export them" is
  **cross-dialog build**. Cell-level selection already exists (the per-cell Export
  toggle) and module targeting already exists (`#| export <module>`), but the
  *merge* does not: tangling N dialogs raises collisions the current code resolves
  silently and lossily — two `#| default_exp core`s concatenate, and `__all__` is
  "first definition wins" so one dialog's `train()` shadows another's with no
  diagnostic. The identity of the library is the crux — see next.

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

**Cross-dialog export**
- *Collision policy:* two dialogs targeting the same module, or defining the same
  public name — namespace by dialog, error on duplicate, or last-wins? Today it's
  silent concat + first-wins `__all__` shadowing.
- *Provenance:* `_header` stamps a single `dialog_name`; a multi-source module
  needs per-cell origin.
- *Identity storage:* where the manifest lives (file vs. dialog) and its schema.

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
