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
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .targets import Target, default_model


def _fallback_model() -> str:
    """The model to assume for a prompt cell that has none recorded. Follows the
    user's configured default (Claude Max via the CLI, out of the box) so a
    model-less cell never silently routes to the paid Anthropic API."""
    try:
        return default_model()
    except Exception:  # noqa: BLE001 — no/unreadable config: keep the subscription default
        return "claude-cli"


@dataclass
class Msg:
    id: str
    msg_type: str          # 'code' | 'prompt' | 'note'
    content: str
    output: str = ""
    model: str | None = None   # which AI model a prompt was routed to
    muted: bool = False        # if True, excluded from the AI's notebook context
    pinned: bool = False       # if True, always kept in context (survives budget trimming)
    rich: list = field(default_factory=list)   # rich outputs: [{"type": mime, "data": ...}]


# ---- on-disk persistence for the in-memory backends -------------------------
# Dialogs are saved per store key (the target name) as one JSON file, so your
# notebook survives a restart — and survives the backend being rebuilt when you
# save Settings or switch targets. Override the directory with SIDEKICK_DATA.
def _store_path(key: str) -> Path:
    base = os.environ.get("SIDEKICK_DATA")
    base = Path(base).expanduser() if base else Path.home() / ".config" / "solveit-sidekick"
    return base / f"dialogs-{key.replace('/', '_')}.json"


def _load_dialogs(key: str) -> dict[str, list[Msg]]:
    p = _store_path(key)
    if not p.exists():
        return {}
    try:
        raw = json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    fields = Msg.__dataclass_fields__
    return {name: [Msg(**{k: v for k, v in m.items() if k in fields}) for m in msgs]
            for name, msgs in raw.items()}


def _save_dialogs(key: str, dialogs: dict[str, list[Msg]]) -> None:
    p = _store_path(key)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        raw = {name: [asdict(m) for m in msgs] for name, msgs in dialogs.items()}
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(raw, indent=2))
        tmp.replace(p)                 # atomic write
    except OSError:
        pass                           # never let a disk hiccup break the app


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

    keep = [False] * len(cells)
    budget = max_chars
    for i, (pinned, c) in enumerate(cells):       # pinned cells always survive
        if pinned:
            keep[i] = True
            budget -= len(c)
    for i in range(len(cells) - 1, -1, -1):       # fill remaining budget, newest first
        pinned, c = cells[i]
        if not pinned and len(c) <= budget:
            keep[i] = True
            budget -= len(c)

    kept = [c for i, (_, c) in enumerate(cells) if keep[i]]
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

    def add(self, dialog: str, content: str, msg_type: str, model: str | None = None) -> Msg:
        m = Msg(id="_" + uuid.uuid4().hex[:8], msg_type=msg_type, content=content, model=model)
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
        self._save()

    def delete_dialog(self, dialog: str) -> bool:
        """Remove a whole dialog (and its cells). Returns whether it existed."""
        if dialog in self._dialogs:
            del self._dialogs[dialog]
            self._save()
            return True
        return False

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

    def add(self, dialog: str, content: str, msg_type: str, model: str | None = None) -> Msg:
        # add_msg has no `model` parameter — SolveIt selects the AI per dialog,
        # not per message — so `model` stays on our own Msg for display only.
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

    def _post(self, path: str, body: dict) -> dict:
        import json
        data = json.dumps(body).encode()
        req = self._req.Request(self.base + path, data=data,
                                headers={"Content-Type": "application/json",
                                         "Cookie": f"_solveit={self.target.token}"})
        with self._req.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode())

    def exec(self, dialog: str, msg_id: str) -> Msg:
        m = self._find(dialog, msg_id)
        if m is None:
            raise KeyError(msg_id)
        if m.msg_type == "code":
            r = self._post("/exec", {"dialog": dialog, "code": m.content})
            m.output = r.get("output", "")
            m.rich = r.get("rich", [])      # plots/images/dataframes from the kernel
        elif m.msg_type == "prompt":
            # Give the AI the notebook so far (cells above this one) as context,
            # the way a real SolveIt dialog does.
            context = build_context(self._dialogs.get(dialog, []), upto_id=msg_id)
            r = self._post("/prompt", {"dialog": dialog, "content": m.content,
                                       "model": m.model or _fallback_model(),
                                       "context": context})
            m.output = r["output"]
        self._save()                         # persist the new output/plots
        return m

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
    except Exception as e:  # noqa: BLE001 — surface any connection issue to the UI
        return MockBackend(), str(e)
