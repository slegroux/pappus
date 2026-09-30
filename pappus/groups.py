"""The sidebar arrangement the user makes: an order for each group's children
and which groups are expanded (groups are folded by default), saved as
groups.json in the data root.

Membership is never stored here. A dialog's group is still the prefix of its
name (`speech/tts/fastpitch` lives in `speech` → `tts`), so this file only
records arrangement; losing it loses nothing but the order.

Children of a group are keyed `f:<segment>` for a sub-group and `d:<segment>`
for a dialog, so a group and a dialog that share a name never collide.
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path

from .datadir import data_root


# One lock for read-modify-write: the sidebar can send several updates at once.
_LOCK = threading.RLock()


def _path() -> Path:
    return data_root() / "groups.json"


def load() -> dict:
    """{"order": {parent_path: [key, ...]}, "expanded": [group_path, ...]}.
    A missing or damaged file reads as empty: alphabetical, everything folded.
    (An older "collapsed" list is ignored: folded is now the default.)"""
    try:
        raw = json.loads(_path().read_text())
    except (OSError, ValueError):
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    order = raw.get("order") if isinstance(raw.get("order"), dict) else {}
    expanded = raw.get("expanded") if isinstance(raw.get("expanded"), list) else []
    return {
        "order": {str(k): [str(x) for x in v] for k, v in order.items() if isinstance(v, list)},
        "expanded": sorted({str(x) for x in expanded}),
    }


def save(layout: dict) -> None:
    with _LOCK:
        p = _path()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(f"{p.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        tmp.write_text(json.dumps(layout, indent=2))
        tmp.replace(p)                               # atomic, like the dialog store


def set_order(parent: str, keys: list[str]) -> None:
    with _LOCK:
        layout = load()
        layout["order"][parent] = [k for k in dict.fromkeys(keys) if k[:2] in ("f:", "d:")]
        save(layout)


def set_collapsed(path: str, collapsed: bool) -> None:
    """Remember a fold/unfold. Only expanded groups are stored."""
    with _LOCK:
        layout = load()
        names = set(layout["expanded"])
        (names.discard if collapsed else names.add)(path)
        layout["expanded"] = sorted(names)
        save(layout)


def _moved(path: str, old: str, new: str) -> str:
    """`path` with the group prefix `old` replaced by `new` (unchanged if outside)."""
    if path == old:
        return new
    if path.startswith(old + "/"):
        return new + path[len(old):]
    return path


def _parent_and_seg(path: str) -> tuple[str, str]:
    parent, _, seg = path.rpartition("/")
    return parent, seg


def rename_group(old: str, new: str) -> None:
    """Carry the arrangement across a group rename: its own order lists, its
    expanded state, and its entry in its parent's order."""
    with _LOCK:
        layout = load()
        order: dict[str, list[str]] = {}
        for k, v in layout["order"].items():         # merging into an existing group
            dest = order.setdefault(_moved(k, old, new), [])   # combines both orders
            dest.extend(x for x in v if x not in dest)
        old_parent, old_seg = _parent_and_seg(old)
        new_parent, new_seg = _parent_and_seg(new)
        if old_parent in order:
            siblings = order[old_parent]
            if old_parent == new_parent:
                renamed = [f"f:{new_seg}" if k == f"f:{old_seg}" else k for k in siblings]
                order[old_parent] = list(dict.fromkeys(renamed))
            else:
                order[old_parent] = [k for k in siblings if k != f"f:{old_seg}"]
        layout["order"] = order
        layout["expanded"] = sorted({_moved(c, old, new) for c in layout["expanded"]})
        save(layout)


def rename_dialog(old: str, new: str) -> None:
    """Carry a dialog's place across a rename: same group → same slot under the
    new name; different group → leave its old group's order (the drop that
    moved it records its new place)."""
    with _LOCK:
        layout = load()
        old_parent, old_seg = _parent_and_seg(old)
        new_parent, new_seg = _parent_and_seg(new)
        siblings = layout["order"].get(old_parent)
        if not siblings or f"d:{old_seg}" not in siblings:
            return
        if old_parent == new_parent:
            layout["order"][old_parent] = [f"d:{new_seg}" if k == f"d:{old_seg}" else k
                                           for k in siblings]
        else:
            layout["order"][old_parent] = [k for k in siblings if k != f"d:{old_seg}"]
        save(layout)


def sort_children(parent: str, items: list[tuple[str, str]], layout: dict) -> list[tuple[str, str]]:
    """Order a group's children. `items` are (kind, segment), kind 'f' or 'd'.
    Children the user placed come first, in their order; the rest follow in the
    default order (sub-groups, then dialogs, each alphabetical)."""
    placed = {k: i for i, k in enumerate(layout["order"].get(parent, []))}

    def key(item):
        kind, seg = item
        k = f"{kind}:{seg}"
        if k in placed:
            return (0, placed[k], 0, "", "")
        return (1, 0, 0 if kind == "f" else 1, seg.lower(), seg)

    return sorted(items, key=key)
