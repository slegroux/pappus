"""Thin wrapper over solveit_client with a mock fallback.

Why a wrapper:
  - One object that knows its Target, so the UI can show which box it's on.
  - Graceful degradation: if solveit_client isn't installed or no server is
    reachable, we fall back to an in-memory MockBackend so the interface is
    still demoable (and so the UI can be developed without a live H100).
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import uuid
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path

from .targets import Target, effective_default_model


def _dbg(msg):
    """Log to stderr only when SIDEKICK_DEBUG is set, so the graceful-degradation
    paths (mock fallback on connect) become diagnosable without changing default
    behavior. Mirrors app._dbg."""
    if os.environ.get("SIDEKICK_DEBUG"):
        import sys
        print(f"[sidekick] {msg}", file=sys.stderr)


def _fallback_model() -> str:
    """The model to assume for a prompt cell that has none recorded. Follows the
    user's configured default (Codex CLI, out of the box) so a model-less cell
    never silently routes to a paid provider API key — but resolves through
    effective_default_model, so a machine without the codex binary falls back to
    Claude Opus high instead of erroring."""
    try:
        return effective_default_model()
    except Exception:  # noqa: BLE001 — no/unreadable config: keep the CLI default
        return "codex-gpt-5.5-high"


@dataclass
class Msg:
    id: str
    msg_type: str          # 'code' | 'prompt' | 'note'
    content: str
    output: str = ""
    model: str | None = None   # which AI model a prompt was routed to
    ai_mode: str | None = None # persona for a prompt: learning/concise/standard
    muted: bool = False        # if True, excluded from the AI's notebook context
    pinned: bool = False       # if True, always kept in context (survives budget trimming)
    rich: list = field(default_factory=list)   # rich outputs: [{"type": mime, "data": ...}]


# ---- on-disk persistence for the in-memory backends -------------------------
def _data_dir() -> Path:
    from .datadir import data_root
    return data_root()


# Dialogs are saved per store key (the target name) as one JSON file, so your
# notebook survives a restart — and survives the backend being rebuilt when you
# save Settings or switch targets. Override the directory with SIDEKICK_DATA.
#
# Private dialogs can be kept out of the main store by listing their dialog names
# in dialogs-<key>.private. Those cells are saved in dialogs-<key>.local.json;
# both files are meant to be gitignored for machine/private notes.
def _store_path(key: str) -> Path:
    return _data_dir() / f"dialogs-{key.replace('/', '_')}.json"


def _local_store_path(key: str) -> Path:
    p = _store_path(key)
    return p.with_name(f"{p.stem}.local.json")


def _private_dialogs_path(key: str) -> Path:
    p = _store_path(key)
    return p.with_name(f"{p.stem}.private")


# ---- store safety -------------------------------------------------------------
# One lock for every write to the dialog store: the UI's request threads, the
# streaming worker and the MCP cell-edit route can all save at once.
_SAVE_LOCK = threading.RLock()

# Timestamped copies of the store in <data root>/backups/, taken at most every
# SNAPSHOT_MINUTES while you work, newest SNAPSHOT_KEEP kept. The rolling .bak
# (previous save) alone can't help once a bad file has been saved over it.
SNAPSHOT_MINUTES = float(os.environ.get("SIDEKICK_SNAPSHOT_MINUTES", "15"))
SNAPSHOT_KEEP = int(os.environ.get("SIDEKICK_SNAPSHOT_KEEP", "20"))

# Human-readable notes about recoveries, shown as a banner by the app.
STORE_NOTICES: list[str] = []


class StoreUnreadable(RuntimeError):
    """The store exists but can't be read or set aside, so saving would destroy it."""


def _snapshot_dir(p: Path) -> Path:
    return p.parent / "backups"


def _snapshots(p: Path) -> list[Path]:
    """This store's snapshots, newest first. Matched exactly, so target `kernel`
    never picks up `kernel-h100`'s snapshots (target names are user-defined)."""
    import re
    d = _snapshot_dir(p)
    if not d.is_dir():
        return []
    pat = re.compile(re.escape(p.stem) + r"-\d{8}-\d{6}-\d{6}\.json")
    return sorted((f for f in d.iterdir() if pat.fullmatch(f.name)), reverse=True)


def _parse_store(p: Path) -> dict | None:
    """The store's dict, or None if the file isn't a readable JSON object."""
    try:
        v = json.loads(p.read_text())
    except (OSError, ValueError):
        return None
    return v if isinstance(v, dict) else None


def _read_raw_dialogs(p: Path) -> dict:
    """Load a store file. A missing file is an empty store. A corrupt one is
    never treated as empty (the next save would overwrite it): it is moved
    aside to <name>.corrupt-<time>, and the newest good copy (.bak, then the
    snapshots) is restored in its place, with a notice for the UI."""
    # Loads are rare (startup, target switch), so the whole read takes the lock:
    # no loader can see the file mid-recovery (moved aside) and read it as empty.
    with _SAVE_LOCK:
        if not p.exists():
            return {}
        return _recover_store(p)


def _recover_store(p: Path) -> dict:
    v = _parse_store(p) if p.exists() else {}
    if v is not None:
        return v                       # another thread already recovered it
    from datetime import datetime
    aside = p.with_name(f"{p.name}.corrupt-{datetime.now().strftime('%Y%m%d-%H%M%S-%f')}")
    try:
        p.replace(aside)
    except OSError as e:
        raise StoreUnreadable(f"{p} is unreadable and could not be moved aside: {e}") from e
    for cand in [p.with_suffix(f"{p.suffix}.bak"), *_snapshots(p)]:
        good = _parse_store(cand) if cand.exists() else None
        if good is not None:
            import shutil
            tmp = _tmp_path(p)
            shutil.copy2(cand, tmp)
            tmp.replace(p)             # atomic, like every other store write
            msg = (f"{p.name} was unreadable and has been restored from {cand.name} "
                   f"({time.strftime('%Y-%m-%d %H:%M', time.localtime(cand.stat().st_mtime))}). "
                   f"Edits after that time may be missing; the damaged file is kept as {aside.name}.")
            break
    else:
        good = {}
        msg = (f"{p.name} was unreadable and no good backup was found; starting empty. "
               f"The damaged file is kept as {aside.name}.")
    STORE_NOTICES.append(msg)
    _dbg(msg)
    import sys
    print(f"[sidekick] WARNING: {msg}", file=sys.stderr)
    return good


def _read_private_dialogs(key: str) -> set[str]:
    p = _private_dialogs_path(key)
    try:
        return {
            line.strip() for line in p.read_text().splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        }
    except OSError:
        return set()


# Header preserved atop the machine-managed .private file so a human who opens it
# understands what the (otherwise bare) list of names does.
_PRIVATE_HEADER = (
    "# Dialogs listed here are kept out of the committed store (dialogs-<key>.json)\n"
    "# and saved to dialogs-<key>.local.json instead — both gitignored, so these\n"
    "# stay on this machine and never sync. Toggle via the dialog's ⋯ menu.\n")


def _write_private_dialogs(key: str, names: set[str]) -> None:
    """Persist the set of local-only dialog names. Removing the last name drops the
    file entirely so an empty list leaves no trace."""
    p = _private_dialogs_path(key)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        if not names:
            p.unlink(missing_ok=True)
            return
        body = _PRIVATE_HEADER + "".join(f"{n}\n" for n in sorted(names))
        with _SAVE_LOCK:
            tmp = _tmp_path(p)
            tmp.write_text(body)
            tmp.replace(p)             # atomic
    except OSError:
        pass                           # never let a disk hiccup break the toggle


def _load_dialogs(key: str) -> dict[str, list[Msg]]:
    raw = _read_raw_dialogs(_store_path(key))
    raw.update(_read_raw_dialogs(_local_store_path(key)))  # local/private wins
    if not raw:
        return {}
    fields = Msg.__dataclass_fields__
    return {name: [Msg(**{k: v for k, v in m.items() if k in fields}) for m in msgs]
            for name, msgs in raw.items()}


def _tmp_path(p: Path) -> Path:
    # unique per process and thread, so two writers never share a temp file
    return p.with_name(f"{p.name}.{os.getpid()}-{threading.get_ident()}.tmp")


def _maybe_snapshot(p: Path) -> None:
    """Copy the store about to be replaced into backups/, throttled and pruned.
    Only a readable store is kept, so snapshots are always good restore points."""
    if SNAPSHOT_KEEP <= 0 or not p.exists():
        return
    snaps = _snapshots(p)
    if snaps and time.time() - snaps[0].stat().st_mtime < SNAPSHOT_MINUTES * 60:
        return
    if _parse_store(p) is None:
        return
    import shutil
    d = _snapshot_dir(p)
    d.mkdir(parents=True, exist_ok=True)
    from datetime import datetime
    dst = d / f"{p.stem}-{datetime.now().strftime('%Y%m%d-%H%M%S-%f')}.json"
    shutil.copy2(p, dst)
    os.utime(dst)                      # throttle on when it was taken
    for old in _snapshots(p)[SNAPSHOT_KEEP:]:
        old.unlink(missing_ok=True)


def _write_dialog_store(p: Path, raw: dict) -> None:
    # History guard: the previous save goes to a rolling <name>.bak and, now
    # and then, to a timestamped snapshot. Best-effort — a failed backup never
    # blocks the actual save.
    if p.exists():
        try:
            import shutil
            _maybe_snapshot(p)
            shutil.copy2(p, p.with_suffix(f"{p.suffix}.bak"))
        except OSError:
            pass
    tmp = _tmp_path(p)
    try:
        tmp.write_text(json.dumps(raw, indent=2))
        tmp.replace(p)                 # atomic write
    finally:
        tmp.unlink(missing_ok=True)


def _save_dialogs(key: str, dialogs: dict[str, list[Msg]]) -> None:
    p = _store_path(key)
    local = _local_store_path(key)
    with _SAVE_LOCK:
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            private_names = _read_private_dialogs(key)
            public = {}
            private = {}
            # list() copies are atomic in CPython, so another thread adding a
            # dialog or cell mid-save can't break the iteration
            for name, msgs in list(dialogs.items()):
                raw_msgs = [asdict(m) for m in list(msgs)]
                if name in private_names:
                    private[name] = raw_msgs
                else:
                    public[name] = raw_msgs
            _write_dialog_store(p, public)
            if private:
                _write_dialog_store(local, private)
            elif local.exists():
                local.unlink()
        except OSError as e:           # never let a disk hiccup break the app,
            import sys                 # but don't hide it either
            print(f"[sidekick] WARNING: could not save {p}: {e}", file=sys.stderr)


def _rename_in(dialogs: dict, old: str, new: str) -> None:
    """Re-key a dialog in an in-memory dict, preserving message order."""
    new = (new or "").strip()
    if not new or new == old:
        return
    if new in dialogs:
        raise ValueError(f"A dialog named '{new}' already exists.")
    dialogs[new] = dialogs.pop(old, [])


# ---- notebook context for the AI (mirrors SolveIt's to_xml semantics) -------
# Tunables, env-overridable like the kernel server's other settings:
#   SIDEKICK_CTX_OUT_TRUNC  middle-out truncate each code/answer output to N chars
#   SIDEKICK_CTX_MAX_CHARS  total context budget; oldest cells drop first when over
def _ctx_limits(out_trunc: int | None, max_chars: int | None) -> tuple[int, int]:
    import os
    if out_trunc is None:
        out_trunc = int(os.environ.get("SIDEKICK_CTX_OUT_TRUNC", 2000))
    if max_chars is None:
        max_chars = int(os.environ.get("SIDEKICK_CTX_MAX_CHARS", 100_000))
    return out_trunc, max_chars


def _trunc_middle(s: str, n: int) -> str:
    """Middle-out truncation: keep the head and tail, elide the middle."""
    s = s or ""
    if n <= 0 or len(s) <= n:
        return s
    half = max(1, (n - 3) // 2)
    return f"{s[:half]}\n…\n{s[-half:]}"


def _cell_xml(m: "Msg", out_trunc: int, num: int) -> str:
    """Render one cell as an XML-ish block for the AI to read.

    Each block carries `n` (the cell's 1-based number, matching the UI badge) and
    `id` (the stable handle the cell-editing tools target), so the AI can act on
    "fix cell 3" / "the add function" without a separate lookup."""
    attrs = f' n="{num}" id="{m.id}"'
    if m.msg_type == "note":
        return f"<note{attrs}>\n{m.content}\n</note>"
    if m.msg_type == "code":
        out = _trunc_middle(m.output or "", out_trunc)
        body = f"{m.content}\n<output>\n{out}\n</output>" if out else m.content
        return f"<code{attrs}>\n{body}\n</code>"
    # prompt: the question plus the answer it produced
    ans = _trunc_middle(m.output or "", out_trunc)
    body = f"{m.content}\n<answer>\n{ans}\n</answer>" if ans else m.content
    return f"<prompt{attrs}>\n{body}\n</prompt>"


def est_tokens(text: str) -> int:
    """Rough token estimate (~4 chars/token) — provider-agnostic, no tokenizer dep."""
    return (len(text or "") + 3) // 4


def build_context(msgs: list, upto_id: str | None = None,
                  out_trunc: int | None = None, max_chars: int | None = None) -> str:
    """Serialize the cells *before* `upto_id` into the AI's notebook context.

    Empty and muted cells are skipped. Long outputs are middle-out truncated.
    Pinned cells are always kept; if the rest exceeds the char budget, the oldest
    non-pinned cells drop first (newest are most relevant), with a marker noting
    how many were omitted.
    """
    out_trunc, max_chars = _ctx_limits(out_trunc, max_chars)
    cells = []                          # [(pinned, xml), ...] in notebook order
    for i, m in enumerate(msgs):        # i+1 = the cell's 1-based number (matches the UI)
        if upto_id is not None and m.id == upto_id:
            break
        if getattr(m, "muted", False):
            continue
        if not (m.content or "").strip() and not (m.output or "").strip():
            continue
        cells.append((getattr(m, "pinned", False), _cell_xml(m, out_trunc, i + 1)))

    # `text[i]` is what we actually ship for cell i (may be truncated); None = dropped.
    text: list[str | None] = [None] * len(cells)
    budget = max_chars
    for i, (pinned, c) in enumerate(cells):       # pinned cells survive first
        if pinned:
            if budget <= 0:                       # no room left even for a pin
                continue
            # If the pinned content alone would blow the budget, truncate it so the
            # total kept never exceeds max_chars (a huge pin must not push us over,
            # nor starve every unpinned cell into being dropped).
            kept_c = c if len(c) <= budget else _trunc_middle(c, budget)
            text[i] = kept_c
            budget -= len(kept_c)
    for i in range(len(cells) - 1, -1, -1):       # fill remaining budget, newest first
        pinned, c = cells[i]
        if not pinned and len(c) <= budget:
            text[i] = c
            budget -= len(c)
    # Guarantee the newest non-empty cell survives even if it alone exceeds the
    # budget: truncate it to fit rather than shipping a near-empty context.
    if cells and all(t is None for t in text):
        i = len(cells) - 1
        text[i] = _trunc_middle(cells[i][1], max_chars)

    kept = [t for t in text if t is not None]
    dropped = len(cells) - len(kept)
    body = "\n".join(kept)
    if dropped:
        body = (f'<omitted note="{dropped} earlier cell(s) dropped to fit the '
                f'context budget"/>\n' + body)
    return body


class _InMemoryBackend:
    """Shared dialog/message bookkeeping for backends that keep state in-process.

    MockBackend and HttpKernelBackend both hold dialogs as ``{name: [Msg, ...]}``
    and only differ in how ``exec`` produces output, so the CRUD lives here:
    list/open dialogs, add/edit/delete messages, and rename. Subclasses provide
    ``exec`` (and may seed initial dialogs in ``__init__``).
    """

    def __init__(self, store_key: str | None = None):
        # store_key None -> ephemeral (e.g. the demo MockBackend); a key -> the
        # dialogs are loaded from / saved to disk under that key.
        self._store_key = store_key
        self._dialogs: dict[str, list[Msg]] = _load_dialogs(store_key) if store_key else {}
        # Deleted cells, newest last: (dialog, prev_cell_id_or_None, msg) — the
        # anchor is the id of the cell above, so undo() ('z') re-inserts correctly.
        self._undo: list[tuple[str, str | None, Msg]] = []

    def _save(self) -> None:
        if getattr(self, "_store_key", None):
            _save_dialogs(self._store_key, self._dialogs)

    def list_dialogs(self) -> list[str]:
        return list(self._dialogs)

    def messages(self, dialog: str) -> list[Msg]:
        return self._dialogs.setdefault(dialog, [])

    def _find(self, dialog: str, msg_id: str) -> Msg | None:
        for m in self._dialogs.get(dialog, []):
            if m.id == msg_id:
                return m
        return None

    def add(self, dialog: str, content: str, msg_type: str, model: str | None = None,
            ai_mode: str | None = None) -> Msg:
        m = Msg(id="_" + uuid.uuid4().hex[:8], msg_type=msg_type, content=content,
                model=model, ai_mode=ai_mode)
        self._dialogs.setdefault(dialog, []).append(m)
        self._save()
        return m

    def insert(self, dialog: str, content: str, msg_type: str, anchor_id: str,
               above: bool = False, model: str | None = None) -> Msg:
        """Insert a new cell above or below `anchor_id` (at the end if not found)."""
        lst = self._dialogs.setdefault(dialog, [])
        m = Msg(id="_" + uuid.uuid4().hex[:8], msg_type=msg_type, content=content, model=model)
        idx = next((i for i, x in enumerate(lst) if x.id == anchor_id), len(lst) - 1)
        lst.insert(idx if above else idx + 1, m)
        self._save()
        return m

    def update(self, dialog: str, msg_id: str, content: str) -> Msg | None:
        """Edit a cell's source in place. Stale output is cleared so the UI never
        shows an answer (or plot) that no longer matches the (now-edited) input."""
        m = self._find(dialog, msg_id)
        if m is not None:
            m.content = content
            m.output = ""
            m.rich = []
            self._save()
        return m

    def set_model(self, dialog: str, msg_id: str, model: str) -> Msg | None:
        """Stamp a prompt cell's AI model. Used when a model-less cell adopts the
        currently selected model at run time, so routing, the answer's byline, and
        future re-runs all agree instead of silently falling back to the default."""
        m = self._find(dialog, msg_id)
        if m is not None and m.model != model:
            m.model = model
            self._save()
        return m

    def update_output(self, dialog: str, msg_id: str, output: str) -> Msg | None:
        """Edit a cell's AI answer in place, leaving its source (the question)
        untouched — SolveIt's editable AI response (the `n` shortcut). Used for
        prompt cells; unlike `update`, it does NOT clear or re-run anything."""
        m = self._find(dialog, msg_id)
        if m is not None:
            m.output = output
            self._save()
        return m

    def set_type(self, dialog: str, msg_id: str, msg_type: str) -> Msg | None:
        """Convert a cell to another type (code/note/prompt). The source text is
        kept — a prompt's question becomes the new source — and stale output is
        cleared, since a code result or AI answer no longer applies."""
        if msg_type not in ("code", "note", "prompt"):
            return None
        m = self._find(dialog, msg_id)
        if m is not None and m.msg_type != msg_type:
            m.msg_type = msg_type
            m.output = ""
            m.rich = []
            self._save()
        return m

    def delete(self, dialog: str, msg_id: str) -> None:
        lst = self._dialogs.get(dialog)
        if lst is not None:
            idx = next((i for i, m in enumerate(lst) if m.id == msg_id), None)
            if idx is not None:
                # Anchor on the *id* of the cell above (None if it was first), not an
                # absolute index — so undo lands correctly even after intervening
                # reorders/inserts/deletes.
                prev_id = lst[idx - 1].id if idx > 0 else None
                self._undo.append((dialog, prev_id, lst[idx]))
                del self._undo[:-50]                          # cap the undo history
            self._dialogs[dialog] = [m for m in lst if m.id != msg_id]
            self._save()

    def undo(self, dialog: str) -> Msg | None:
        """Restore the most recently deleted cell (Jupyter's 'z'), right after the
        cell it used to follow — or at the front if it was first, or at the end if
        that anchor has since been deleted too."""
        for i in range(len(self._undo) - 1, -1, -1):
            d, prev_id, m = self._undo[i]
            if d == dialog:
                del self._undo[i]
                lst = self._dialogs.setdefault(dialog, [])
                if prev_id is None:
                    pos = 0
                else:
                    j = next((k for k, x in enumerate(lst) if x.id == prev_id), None)
                    pos = j + 1 if j is not None else len(lst)
                lst.insert(pos, m)
                self._save()
                return m
        return None

    def set_muted(self, dialog: str, msg_id: str, muted: bool | None = None) -> Msg | None:
        """Toggle (or set) whether a cell is included in the AI's context."""
        m = self._find(dialog, msg_id)
        if m is not None:
            m.muted = (not m.muted) if muted is None else bool(muted)
            self._save()
        return m

    def set_pinned(self, dialog: str, msg_id: str, pinned: bool | None = None) -> Msg | None:
        """Toggle (or set) whether a cell is pinned into context (survives trimming)."""
        m = self._find(dialog, msg_id)
        if m is not None:
            m.pinned = (not m.pinned) if pinned is None else bool(pinned)
            self._save()
        return m

    def rename(self, old: str, new: str) -> None:
        _rename_in(self._dialogs, old, new)
        # Carry the local-only flag across a rename — otherwise a renamed private
        # dialog silently becomes public and lands in the committed store.
        key = getattr(self, "_store_key", None)
        new = (new or "").strip()
        if key and new and new != old:
            names = _read_private_dialogs(key)
            if old in names:
                names.discard(old)
                names.add(new)
                _write_private_dialogs(key, names)
        self._save()

    def delete_dialog(self, dialog: str) -> bool:
        """Remove a whole dialog (and its cells). Returns whether it existed."""
        if dialog in self._dialogs:
            del self._dialogs[dialog]
            key = getattr(self, "_store_key", None)      # drop any stale private entry
            if key:
                names = _read_private_dialogs(key)
                if dialog in names:
                    names.discard(dialog)
                    _write_private_dialogs(key, names)
            self._save()
            return True
        return False

    def is_private(self, dialog: str) -> bool:
        """Whether `dialog` is kept local-only (out of the committed/synced store)."""
        key = getattr(self, "_store_key", None)
        return bool(key) and dialog in _read_private_dialogs(key)

    def set_private(self, dialog: str, private: bool | None = None) -> bool:
        """Toggle (or set) whether `dialog` stays on this machine. Updates the
        .private list, then re-saves so the dialog moves between the committed store
        and the local overlay. Returns the resulting local-only state.

        No-ops (returns False) on ephemeral backends with no on-disk store."""
        key = getattr(self, "_store_key", None)
        if not key:
            return False
        names = _read_private_dialogs(key)
        new = (dialog not in names) if private is None else bool(private)
        if new:
            names.add(dialog)
        else:
            names.discard(dialog)
        _write_private_dialogs(key, names)
        self._save()                  # re-split public/local per the updated list
        return new

    def reorder(self, dialog: str, ordered_ids: list[str]) -> None:
        """Reorder a dialog's cells to match `ordered_ids` (from a drag). Any id
        not listed is appended in its original order, so we never drop a cell."""
        lst = self._dialogs.get(dialog)
        if not lst:
            return
        by_id = {m.id: m for m in lst}
        listed = set(ordered_ids)
        self._dialogs[dialog] = ([by_id[i] for i in ordered_ids if i in by_id]
                                 + [m for m in lst if m.id not in listed])
        self._save()

    # ---- trivial synchronous stand-in for the async exec protocol -----------
    # The real kernel (HttpKernelBackend) streams a run over exec_start/poll/stop.
    # An in-process backend has no long-running worker to stream, so it runs the
    # cell synchronously in exec_start and reports it already done on the first
    # poll — the app's streaming path then works unchanged (one poll, then done)
    # without a live kernel. HttpKernelBackend overrides all three with real HTTP.
    def exec_start(self, dialog: str, msg_id: str) -> str:
        m = self.exec(dialog, msg_id)     # run now (mock output or subclass exec)
        run_id = "_run_" + uuid.uuid4().hex[:8]
        runs = self.__dict__.setdefault("_runs", {})
        runs[run_id] = {"output": m.output, "rich": list(getattr(m, "rich", []) or []),
                        "done": True, "error": None, "interrupted": False}
        return run_id

    def exec_poll(self, dialog: str, run_id: str) -> dict:
        runs = self.__dict__.get("_runs", {})
        return runs.get(run_id, {"output": "", "rich": [], "done": True,
                                 "error": None, "interrupted": False})

    def exec_stop(self, dialog: str, run_id: str) -> dict:
        return {"ok": True, "done": True}   # already completed synchronously

    def copy_cell(self, src_dialog: str, msg_id: str, dst_dialog: str) -> Msg | None:
        """Copy a single cell into another dialog, appended at the end. The clone
        gets a fresh id (and its own `rich` list) so the two dialogs stay fully
        independent. Returns the new cell, or None if the source doesn't exist."""
        src = self._find(src_dialog, msg_id)
        if src is None:
            return None
        m = replace(src, id="_" + uuid.uuid4().hex[:8], rich=list(src.rich))
        self._dialogs.setdefault(dst_dialog, []).append(m)
        self._save()
        return m


class MockBackend(_InMemoryBackend):
    """In-memory stand-in so the UI runs with no SolveIt server."""

    live = False

    def __init__(self):
        super().__init__()
        self._dialogs["demo/welcome"] = []

    def exec(self, dialog: str, msg_id: str) -> Msg:
        m = self._find(dialog, msg_id)
        if m is None:
            raise KeyError(msg_id)
        if m.msg_type == "code":
            m.output = "(mock) ran code — connect a real SolveIt server for real output"
        elif m.msg_type == "prompt":
            m.output = f"(mock) {m.model or 'the AI'} would answer here once a target is live."
        return m


def _live_msg(m, model: str | None = None) -> Msg:
    """Build our Msg from a solveit_client Message.

    solveit_client stores message fields in `m.data` (and exposes them via
    attribute access); the field names match SolveIt's own CLI:
    `['id', 'msg_type', 'content', 'output']`.
    """
    d = getattr(m, "data", None) or {}
    return Msg(d.get("id", ""), d.get("msg_type", "code") or "code",
               d.get("content", "") or "", d.get("output", "") or "", model)


class LiveBackend:
    """Adapter onto solveit_client — Answer.AI's real SolveIt server.

    Maps our backend protocol onto `solveit_client.core` (SolveItClient / Dialog
    / Message). Used when a target sets `backend: solveit`.
    """

    live = True

    def __init__(self, target: Target):
        from solveit_client.core import SolveItClient  # imported lazily

        self.target = target
        self.sic = SolveItClient(target.url, token=target.token)

    def list_dialogs(self) -> list[str]:
        # SolveIt's client has no list-dialogs endpoint; the UI opens/creates by
        # name, so there's nothing reliable to enumerate.
        return []

    def _dlg(self, dialog: str):
        # create_dialog is SolveIt's open-or-create: it returns the existing
        # dialog if `dialog` already exists, else makes it. Our UI addresses
        # dialogs purely by name, so this is the single entry point we need.
        return self.sic.create_dialog(dialog)

    def messages(self, dialog: str) -> list[Msg]:
        return [_live_msg(m) for m in self._dlg(dialog).messages]

    def add(self, dialog: str, content: str, msg_type: str, model: str | None = None,
            ai_mode: str | None = None) -> Msg:
        # add_msg has no `model`/`mode` parameter — SolveIt selects the AI and mode
        # per dialog on its own side — so they stay on our own Msg for display only.
        m = self._dlg(dialog).add_msg(content, msg_type=msg_type)
        return _live_msg(m, model if msg_type == "prompt" else None)

    def exec(self, dialog: str, msg_id: str) -> Msg:
        m = self._dlg(dialog).read_msg(id=msg_id)
        m.exec()                 # queues + polls to completion, refreshing m.data
        return _live_msg(m)

    def update(self, dialog: str, msg_id: str, content: str) -> Msg:
        m = self._dlg(dialog).read_msg(id=msg_id)
        m.update(content=content)   # refreshes m.data with the edited content
        return _live_msg(m)

    def delete(self, dialog: str, msg_id: str) -> None:
        self._dlg(dialog).read_msg(id=msg_id).delete()


class HttpKernelBackend(_InMemoryBackend):
    """Talks to the bundled kernel server (server/kernel_server.py) over HTTP.

    A real backend: code is actually executed server-side and output returned.
    Used when a target sets `backend: kernel`. Cell CRUD (add/edit/delete) is
    inherited from _InMemoryBackend; only `exec` and `rename` reach the server.
    """

    live = True

    def __init__(self, target: Target):
        super().__init__(store_key=target.name)   # persist this target's dialogs to disk
        import urllib.request  # stdlib
        self._req = urllib.request
        self.target = target
        self.base = target.url.rstrip("/")

    # Per-endpoint client timeouts. A blocking /prompt is a full LLM round-trip
    # (the kernel allows the `claude` CLI up to 180s) and /exec runs arbitrary
    # code (training loops etc.), so a flat 30s would abandon legitimately-long
    # work mid-flight — the request keeps running server-side but the UI throws
    # the result away with a TimeoutError. Completions/rename stay snappy.
    _TIMEOUTS = {"/prompt": 185, "/exec": 600}

    def _post(self, path: str, body: dict, timeout: float | None = None) -> dict:
        import json
        data = json.dumps(body).encode()
        req = self._req.Request(self.base + path, data=data,
                                headers={"Content-Type": "application/json",
                                         "Cookie": f"_solveit={self.target.token}"})
        t = timeout if timeout is not None else self._TIMEOUTS.get(path, 30)
        with self._req.urlopen(req, timeout=t) as r:
            return json.loads(r.read().decode())

    def _get(self, path: str, timeout: float | None = None) -> dict:
        import json
        req = self._req.Request(self.base + path,
                                headers={"Cookie": f"_solveit={self.target.token}"})
        t = timeout if timeout is not None else 30
        with self._req.urlopen(req, timeout=t) as r:
            return json.loads(r.read().decode())

    # ---- streaming / interruptible code execution ---------------------------
    # exec_start kicks off an async run on the kernel and returns immediately with a
    # run_id; the app then polls exec_poll a few times a second (over SSE) to stream
    # partial stdout into the cell, and can exec_stop to interrupt a runaway loop.
    # All three degrade gracefully (like exec/complete) so a dead kernel never
    # blows up the caller — the UI just falls back to a readable error.
    def exec_start(self, dialog: str, msg_id: str) -> str | None:
        m = self._find(dialog, msg_id)
        code = m.content if m is not None else ""
        try:
            r = self._post("/exec_start", {"dialog": dialog, "code": code})
            return r.get("run_id")
        except (urllib.error.URLError, TimeoutError, OSError):
            return None

    def exec_poll(self, dialog: str, run_id: str) -> dict:
        from urllib.parse import quote
        try:
            return self._get(f"/exec_poll?dialog={quote(dialog)}&run_id={quote(run_id)}")
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            # A poll failure ends the stream with a readable error rather than
            # hanging the SSE generator forever.
            return {"output": f"[kernel error: {e}]", "done": True, "rich": [],
                    "error": str(e), "interrupted": False}

    def exec_stop(self, dialog: str, run_id: str) -> dict:
        try:
            return self._post("/exec_stop", {"dialog": dialog, "run_id": run_id})
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            return {"ok": False, "error": str(e)}

    def exec(self, dialog: str, msg_id: str) -> Msg:
        m = self._find(dialog, msg_id)
        if m is None:
            raise KeyError(msg_id)
        if m.msg_type == "code":
            try:
                r = self._post("/exec", {"dialog": dialog, "code": m.content})
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                # Kernel unreachable/timed out: degrade to a readable error in the
                # cell rather than blowing up the caller (mirrors the best-effort
                # try/except that add_syspath/eval_exprs/complete/rename already use).
                m.output = f"[kernel error: {e}]"
                m.rich = []
                self._save()
                return m
            m.output = r.get("output", "")
            m.rich = r.get("rich", [])      # plots/images/dataframes from the kernel
        elif m.msg_type == "prompt":
            # Give the AI the notebook so far (cells above this one) as context,
            # the way a real SolveIt dialog does.
            context = build_context(self._dialogs.get(dialog, []), upto_id=msg_id)
            r = self._post("/prompt", {"dialog": dialog, "content": m.content,
                                       "model": m.model or _fallback_model(),
                                       "context": context, "mode": m.ai_mode})
            m.output = r["output"]
        self._save()                         # persist the new output/plots
        return m

    def reset(self, dialog: str) -> bool:
        """Restart the kernel for `dialog`: drop its server-side namespace via the
        kernel's /reset endpoint so the next run starts clean. Best-effort — a dead
        kernel just yields False rather than blowing up the caller."""
        try:
            r = self._post("/reset", {"dialog": dialog})
            return bool(r.get("ok"))
        except Exception:  # noqa: BLE001 — never break the UI over a restart
            return False

    def add_syspath(self, path: str) -> bool:
        """Put `path` on the kernel's sys.path so a built library there becomes
        importable from any dialog. Best-effort; returns whether it took."""
        try:
            r = self._post("/syspath", {"path": path})
            return bool(r.get("ok"))
        except Exception:  # noqa: BLE001 — never break the UI over this
            return False

    def eval_exprs(self, dialog: str, content: str) -> tuple[str, list]:
        """Resolve $`expr` injections in a prompt against the kernel's live
        namespace (the app process can't reach it directly — it lives in the
        kernel server). Best-effort: on any failure the prompt is sent as-is."""
        try:
            r = self._post("/eval", {"dialog": dialog, "content": content})
            return r.get("content", content), r.get("warnings", [])
        except Exception:  # noqa: BLE001 — injection must never break a prompt
            return content, []

    def complete(self, dialog: str, code: str, line: int, col: int) -> list:
        """Code completions at (line, col) from the kernel's live namespace.
        Best-effort: any failure yields no completions rather than an error."""
        try:
            r = self._post("/complete", {"dialog": dialog, "code": code,
                                         "line": line, "col": col})
            return r.get("completions", [])
        except Exception:  # noqa: BLE001 — completion must never break typing
            return []

    def rename(self, old: str, new: str) -> None:
        # Move the message list; also re-key the server-side kernel namespace
        # so executed variables survive the rename.
        super().rename(old, new)             # also persists (base rename calls _save)
        try:
            self._post("/rename", {"old": old, "new": new})
        except Exception:  # noqa: BLE001 — namespace move is best-effort
            pass


def connect(target: Target) -> tuple[object, str | None]:
    """Return (backend, error). Falls back to MockBackend, reporting why."""
    try:
        from .doctor import check_test_route
        ok, _, detail = check_test_route(target)
        if not ok:
            return MockBackend(), detail
        if target.backend == "kernel":
            return HttpKernelBackend(target), None
        return LiveBackend(target), None
    except ImportError:
        return MockBackend(), "solveit_client not installed (pip install solveit_client)"
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        # Expected "server down / unreachable" shape → degrade to the mock quietly.
        return MockBackend(), str(e)
    except Exception as e:  # noqa: BLE001 — an *unexpected* error (e.g. a bug in a
        # backend __init__) shouldn't masquerade as a plain connection issue. Still
        # degrade so the UI stays usable, but name the type so it's diagnosable
        # (and dump it under SIDEKICK_DEBUG) instead of looking like "server down".
        _dbg(f"connect() unexpected {type(e).__name__}: {e}")
        return MockBackend(), f"{type(e).__name__}: {e}"
