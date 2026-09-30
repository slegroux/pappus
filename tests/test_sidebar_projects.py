"""Sidebar editing: groups (name prefixes), their arrangement (groups.json),
rename / move / new group, and the all-or-nothing group rename."""
import json

import pytest
from fasthtml.common import to_xml

import pappus.app as app
from pappus import groups
from pappus.client import MockBackend


@pytest.fixture(autouse=True)
def _restore_app_state(monkeypatch):
    """Tests here swap the backend and the open dialog; put them back after."""
    monkeypatch.setitem(app.STATE, "backend", app.STATE.get("backend"))
    monkeypatch.setitem(app.STATE, "dialog", app.STATE.get("dialog"))


@pytest.fixture
def backend():
    b = MockBackend()
    for name in ["speech/tts/fastpitch", "speech/stt/whisper", "speech/intro",
                 "audio/mfcc", "audio/codec/encodec", "scratch"]:
        b.add(name, f"# {name}", "note")
    app.STATE["backend"] = b
    app.STATE["dialog"] = "speech/intro"
    return b


def _body(resp):
    return json.loads(resp.body)


# ---- groups.json -------------------------------------------------------------
def test_layout_roundtrip_and_damaged_file():
    groups.set_order("", ["f:speech", "d:scratch", "x:bad"])
    groups.set_collapsed("audio", True)
    lay = groups.load()
    assert lay["order"][""] == ["f:speech", "d:scratch"]      # unknown kinds dropped
    assert lay["collapsed"] == ["audio"]
    groups.set_collapsed("audio", False)
    assert groups.load()["collapsed"] == []
    groups._path().write_text("{not json")
    assert groups.load() == {"order": {}, "collapsed": []}    # damaged file = default layout


def test_sort_children_placed_first_then_default():
    lay = {"order": {"": ["d:scratch", "f:speech"]}, "collapsed": []}
    items = [("f", "audio"), ("f", "speech"), ("d", "scratch"), ("d", "alpha")]
    assert groups.sort_children("", items, lay) == [
        ("d", "scratch"), ("f", "speech"),                     # user's order
        ("f", "audio"), ("d", "alpha")]                        # then groups, then dialogs


def test_rename_group_carries_order_and_collapse():
    groups.set_order("", ["f:speech", "f:audio"])
    groups.set_order("speech", ["f:tts", "d:intro"])
    groups.set_order("speech/tts", ["d:fastpitch"])
    groups.set_collapsed("speech/tts", True)
    groups.rename_group("speech", "voice")
    lay = groups.load()
    assert lay["order"][""] == ["f:voice", "f:audio"]
    assert lay["order"]["voice"] == ["f:tts", "d:intro"]
    assert lay["order"]["voice/tts"] == ["d:fastpitch"]
    assert lay["collapsed"] == ["voice/tts"]


# ---- rendering ---------------------------------------------------------------
def test_sidebar_follows_saved_order_and_collapse(backend):
    groups.set_order("", ["d:scratch", "f:speech", "f:audio"])
    groups.set_collapsed("audio", True)
    h = to_xml(app.Sidebar())
    tree = h[h.index('class="tree-root"'):]
    assert tree.index('data-dialog="scratch"') < tree.index('data-path="speech"') < tree.index('data-path="audio"')
    audio = tree[tree.index('data-path="audio"') - 200:tree.index('data-path="audio"')]
    assert " open" not in audio                                 # collapsed group renders closed
    assert 'class="grp-new"' in h and 'class="grp-dots"' in h   # new-group + group menu
    assert 'id="groupList"' in h and 'value="speech/tts"' in h  # Move-to suggestions
    assert "conv-rename" in h and "conv-move" in h


# ---- rename / move / new -----------------------------------------------------
def test_rename_dialog_from_sidebar_keeps_open_dialog(backend):
    r = _body(app.sidebar_rename_dialog("audio/mfcc", "audio/mfcc-by-hand"))
    assert r == {"ok": True, "name": "audio/mfcc-by-hand", "open": "speech/intro"}
    assert "audio/mfcc-by-hand" in backend.list_dialogs()
    assert app.STATE["dialog"] == "speech/intro"                 # not switched


def test_rename_open_dialog_follows_it(backend):
    app.sidebar_rename_dialog("speech/intro", "speech/overview")
    assert app.STATE["dialog"] == "speech/overview"


def test_rename_dialog_clash_is_reported(backend):
    resp = app.sidebar_rename_dialog("audio/mfcc", "scratch")
    assert resp.status_code == 400 and "already exists" in _body(resp)["error"]
    assert "audio/mfcc" in backend.list_dialogs()


def test_move_dialog_between_groups_and_to_top(backend):
    assert _body(app.sidebar_move_dialog("audio/mfcc", "speech"))["name"] == "speech/mfcc"
    assert _body(app.sidebar_move_dialog("speech/mfcc", ""))["name"] == "mfcc"
    names = backend.list_dialogs()
    assert "mfcc" in names and "audio/mfcc" not in names
    assert backend.messages("mfcc")[0].content == "# audio/mfcc"  # cells travel with it


def test_new_group_creates_first_dialog_and_opens_it(backend):
    r = _body(app.sidebar_new_group(" robotics / sim "))
    assert r["name"] == "robotics/sim/untitled"
    assert r["name"] in backend.list_dialogs() and app.STATE["dialog"] == r["name"]
    assert _body(app.sidebar_new_group("robotics/sim"))["name"] == "robotics/sim/untitled-2"


@pytest.mark.parametrize("bad", ["", "  ", "/", "a//b", "a/../b", "."])
def test_invalid_names_rejected(backend, bad):
    assert app.sidebar_new_group(bad).status_code == 400
    assert app.sidebar_rename_dialog("scratch", bad).status_code == 400


# ---- group rename: all or nothing --------------------------------------------
def test_rename_group_moves_every_member(backend):
    assert _body(app.sidebar_rename_group("speech", "voice"))["ok"]
    names = set(backend.list_dialogs())
    assert {"voice/tts/fastpitch", "voice/stt/whisper", "voice/intro"} <= names
    assert not any(n.startswith("speech/") for n in names)
    assert app.STATE["dialog"] == "voice/intro"                  # open dialog followed


def test_rename_nested_group(backend):
    assert _body(app.sidebar_rename_group("audio/codec", "audio/codecs"))["ok"]
    assert "audio/codecs/encodec" in backend.list_dialogs()


def test_rename_group_rolls_back_on_failure(backend, monkeypatch):
    before = sorted(backend.list_dialogs())
    real, calls = backend.rename, []

    def flaky(old, new):
        calls.append(old)
        if len(calls) == 2 and new.startswith("voice/"):
            raise RuntimeError("disk full")
        real(old, new)

    monkeypatch.setattr(backend, "rename", flaky)
    with pytest.raises(RuntimeError):
        app._rename_group("speech", "voice")
    assert sorted(backend.list_dialogs()) == before              # nothing half-moved


def test_rename_group_refuses_clash_and_self_nesting(backend):
    backend.add("voice/intro", "x", "note")
    assert "already exists" in _body(app.sidebar_rename_group("speech", "voice"))["error"]
    assert "inside itself" in _body(app.sidebar_rename_group("speech", "speech/old"))["error"]
    assert "No group" in _body(app.sidebar_rename_group("nope", "x"))["error"]
    assert "speech/intro" in backend.list_dialogs()


# ---- order + collapse routes -------------------------------------------------
def test_order_and_collapse_routes(backend):
    assert _body(app.sidebar_order("speech", json.dumps(["d:intro", "f:tts"])))["ok"]
    assert groups.load()["order"]["speech"] == ["d:intro", "f:tts"]
    assert app.sidebar_order("", "not json").status_code == 400
    assert app.sidebar_order("", json.dumps([1, 2])).status_code == 400
    app.sidebar_collapse("audio", 1)
    assert groups.load()["collapsed"] == ["audio"]
    app.sidebar_collapse("audio", 0)
    assert groups.load()["collapsed"] == []


# ---- regressions found in the browser ----------------------------------------
def test_concurrent_layout_writes_lose_nothing():
    """Browsers fired a toggle per open group on load; parallel writes used to
    race on one temp file (500s) and could drop each other's updates."""
    import threading
    paths = [f"g{i}" for i in range(24)]
    ts = [threading.Thread(target=groups.set_collapsed, args=(p, True)) for p in paths]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert groups.load()["collapsed"] == sorted(paths)


def test_new_group_survives_a_restart():
    from pappus.client import _InMemoryBackend
    app.STATE["backend"] = _InMemoryBackend(store_key="kernel")
    name = _body(app.sidebar_new_group("robotics"))["name"]
    assert name in _InMemoryBackend(store_key="kernel").list_dialogs()   # reloaded from disk



# ---- review findings ---------------------------------------------------------
def test_renaming_the_open_dialog_never_resurrects_its_old_name(backend):
    """The page used to reload /open?dialog=<old>, recreating <old> empty."""
    res = _body(app.sidebar_rename_dialog("speech/intro", "speech/overview"))
    assert res["open"] == "speech/overview"                     # client navigates here
    app.open_dialog(res["open"])
    app.Page()
    assert "speech/intro" not in backend.list_dialogs()
    res = _body(app.sidebar_rename_group("speech", "voice"))
    assert res["open"] == "voice/overview"


def test_other_tabs_follow_a_rename(backend):
    app.SESSIONS["tab-b"] = {"dialog": "speech/tts/fastpitch"}
    try:
        app.sidebar_rename_group("speech", "voice")
        assert app.SESSIONS["tab-b"]["dialog"] == "voice/tts/fastpitch"
    finally:
        app.SESSIONS.pop("tab-b", None)


def test_dialogs_differing_only_in_empty_segments_all_render(backend):
    backend.add("lab//note", "x", "note")
    backend.add("lab/note", "y", "note")
    h = to_xml(app.Sidebar())
    assert 'data-dialog="lab//note"' in h and 'data-dialog="lab/note"' in h


def test_rename_or_move_of_a_missing_dialog_is_refused(backend):
    assert app.sidebar_rename_dialog("gone/away", "ghost").status_code == 400
    assert app.sidebar_move_dialog("gone/away", "speech").status_code == 400
    assert "ghost" not in backend.list_dialogs()


def test_renamed_dialog_keeps_its_place(backend):
    groups.set_order("speech", ["d:intro", "f:tts", "f:stt"])
    app.sidebar_rename_dialog("speech/intro", "speech/overview")
    assert groups.load()["order"]["speech"] == ["d:overview", "f:tts", "f:stt"]
    app.sidebar_move_dialog("speech/overview", "audio")
    assert groups.load()["order"]["speech"] == ["f:tts", "f:stt"]


def test_merging_into_an_existing_group_combines_orders(backend):
    backend.add("Audio/clap", "c", "note")
    groups.set_order("", ["f:Audio", "f:audio", "f:speech"])
    groups.set_order("Audio", ["d:clap"])
    groups.set_order("audio", ["d:mfcc", "f:codec"])
    assert _body(app.sidebar_rename_group("Audio", "audio"))["ok"]
    lay = groups.load()
    assert lay["order"][""] == ["f:audio", "f:speech"]            # no duplicate entry
    assert lay["order"]["audio"] == ["d:clap", "d:mfcc", "f:codec"]
    assert {"audio/clap", "audio/mfcc"} <= set(backend.list_dialogs())


@pytest.mark.parametrize("bad", ["a\u200bb", "tab\tname", "nul\x00"])
def test_invisible_or_control_characters_rejected(backend, bad):
    assert app.sidebar_new_group(bad).status_code == 400


def test_names_are_nfc_normalised(backend):
    name = _body(app.sidebar_new_group("cafe\u0301"))["name"]        # e + combining accent
    assert name == "caf\u00e9/untitled"
