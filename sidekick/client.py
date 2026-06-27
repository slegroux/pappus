"""Thin wrapper over solveit_client with a mock fallback.

Why a wrapper:
  - One object that knows its Target, so the UI can show which box it's on.
  - Graceful degradation: if solveit_client isn't installed or no server is
    reachable, we fall back to an in-memory MockBackend so the interface is
    still demoable (and so the UI can be developed without a live H100).
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from .targets import Target


@dataclass
class Msg:
    id: str
    msg_type: str          # 'code' | 'prompt' | 'note'
    content: str
    output: str = ""
    model: str | None = None   # which AI model a prompt was routed to


def _rename_in(dialogs: dict, old: str, new: str) -> None:
    """Re-key a dialog in an in-memory dict, preserving message order."""
    new = (new or "").strip()
    if not new or new == old:
        return
    if new in dialogs:
        raise ValueError(f"A dialog named '{new}' already exists.")
    dialogs[new] = dialogs.pop(old, [])


class _InMemoryBackend:
    """Shared dialog/message bookkeeping for backends that keep state in-process.

    MockBackend and HttpKernelBackend both hold dialogs as ``{name: [Msg, ...]}``
    and only differ in how ``exec`` produces output, so the CRUD lives here:
    list/open dialogs, add/edit/delete messages, and rename. Subclasses provide
    ``exec`` (and may seed initial dialogs in ``__init__``).
    """

    def __init__(self):
        self._dialogs: dict[str, list[Msg]] = {}

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
        return m

    def update(self, dialog: str, msg_id: str, content: str) -> Msg | None:
        """Edit a cell's source in place. Stale output is cleared so the UI never
        shows an answer that no longer matches the (now-edited) input."""
        m = self._find(dialog, msg_id)
        if m is not None:
            m.content = content
            m.output = ""
        return m

    def delete(self, dialog: str, msg_id: str) -> None:
        lst = self._dialogs.get(dialog)
        if lst is not None:
            self._dialogs[dialog] = [m for m in lst if m.id != msg_id]

    def rename(self, old: str, new: str) -> None:
        _rename_in(self._dialogs, old, new)


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
        super().__init__()
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
            m.output = self._post("/exec", {"dialog": dialog, "code": m.content})["output"]
        elif m.msg_type == "prompt":
            r = self._post("/prompt", {"dialog": dialog, "content": m.content,
                                       "model": m.model or "claude"})
            m.output = r["output"]
        return m

    def rename(self, old: str, new: str) -> None:
        # Move the message list; also re-key the server-side kernel namespace
        # so executed variables survive the rename.
        super().rename(old, new)
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
