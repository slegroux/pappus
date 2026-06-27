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


class MockBackend:
    """In-memory stand-in so the UI runs with no SolveIt server."""

    live = False

    def __init__(self):
        self._dialogs: dict[str, list[Msg]] = {"demo/welcome": []}

    def list_dialogs(self) -> list[str]:
        return list(self._dialogs)

    def messages(self, dialog: str) -> list[Msg]:
        return self._dialogs.setdefault(dialog, [])

    def add(self, dialog: str, content: str, msg_type: str, model: str | None = None) -> Msg:
        m = Msg(id="_" + uuid.uuid4().hex[:8], msg_type=msg_type, content=content, model=model)
        self._dialogs.setdefault(dialog, []).append(m)
        return m

    def exec(self, dialog: str, msg_id: str) -> Msg:
        for m in self._dialogs.get(dialog, []):
            if m.id == msg_id:
                if m.msg_type == "code":
                    m.output = "(mock) ran code — connect a real SolveIt server for real output"
                elif m.msg_type == "prompt":
                    m.output = f"(mock) {m.model or 'the AI'} would answer here once a target is live."
                return m
        raise KeyError(msg_id)

    def rename(self, old: str, new: str) -> None:
        _rename_in(self._dialogs, old, new)


class LiveBackend:
    """Adapter onto solveit_client.SolveItClient."""

    live = True

    def __init__(self, target: Target):
        from solveit_client.core import SolveItClient  # imported lazily

        self.target = target
        self.sic = SolveItClient(target.url, token=target.token)

    def list_dialogs(self) -> list[str]:
        # solveit_client doesn't expose a stable list endpoint across versions;
        # the UI lets you open/create by name, so we return what we know.
        return []

    def _dlg(self, dialog: str):
        return self.sic.create_dialog(dialog)  # create_dialog opens if it exists

    def messages(self, dialog: str) -> list[Msg]:
        dlg = self._dlg(dialog)
        out = []
        for m in dlg.messages:
            d = m if isinstance(m, dict) else m.__dict__
            out.append(Msg(d.get("id", ""), d.get("msg_type", "code"),
                           d.get("content", ""), d.get("output", "")))
        return out

    def add(self, dialog: str, content: str, msg_type: str, model: str | None = None) -> Msg:
        # Pass model only for prompts, and only if this solveit_client build
        # accepts it — older versions don't, so we degrade gracefully.
        kw = {"msg_type": msg_type}
        if model and msg_type == "prompt":
            kw["model"] = model
        try:
            d = self._dlg(dialog).add_msg(content, **kw)
        except TypeError:
            kw.pop("model", None)
            d = self._dlg(dialog).add_msg(content, **kw)
        d = d if isinstance(d, dict) else d.__dict__
        return Msg(d.get("id", ""), msg_type, content, d.get("output", ""), model)

    def exec(self, dialog: str, msg_id: str) -> Msg:
        dlg = self._dlg(dialog)
        m = dlg.read_msg(id=msg_id)
        m.exec()
        d = m if isinstance(m, dict) else m.__dict__
        return Msg(d.get("id", msg_id), d.get("msg_type", "code"),
                   d.get("content", ""), d.get("output", ""))


class HttpKernelBackend:
    """Talks to the bundled kernel server (server/kernel_server.py) over HTTP.

    A real backend: code is actually executed server-side and output returned.
    Used when a target sets `backend: kernel`.
    """

    live = True

    def __init__(self, target: Target):
        import urllib.request  # stdlib
        self._req = urllib.request
        self.target = target
        self.base = target.url.rstrip("/")
        self._dialogs: dict[str, list[Msg]] = {}

    def _post(self, path: str, body: dict) -> dict:
        import json
        data = json.dumps(body).encode()
        req = self._req.Request(self.base + path, data=data,
                                headers={"Content-Type": "application/json",
                                         "Cookie": f"_solveit={self.target.token}"})
        with self._req.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode())

    def list_dialogs(self) -> list[str]:
        return list(self._dialogs)

    def messages(self, dialog: str) -> list[Msg]:
        return self._dialogs.setdefault(dialog, [])

    def add(self, dialog: str, content: str, msg_type: str, model: str | None = None) -> Msg:
        m = Msg(id="_" + uuid.uuid4().hex[:8], msg_type=msg_type, content=content, model=model)
        self._dialogs.setdefault(dialog, []).append(m)
        return m

    def exec(self, dialog: str, msg_id: str) -> Msg:
        for m in self._dialogs.get(dialog, []):
            if m.id != msg_id:
                continue
            if m.msg_type == "code":
                m.output = self._post("/exec", {"dialog": dialog, "code": m.content})["output"]
            elif m.msg_type == "prompt":
                r = self._post("/prompt", {"dialog": dialog, "content": m.content,
                                           "model": m.model or "claude"})
                m.output = r["output"]
            return m
        raise KeyError(msg_id)

    def rename(self, old: str, new: str) -> None:
        # Move the message list; also re-key the server-side kernel namespace
        # so executed variables survive the rename.
        _rename_in(self._dialogs, old, new)
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
