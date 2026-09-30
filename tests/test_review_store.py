"""The dialog store survives concurrent saves, never treats a corrupt file as
empty, keeps timestamped snapshots, and can't be restored under a running app."""
import json
import os
import tarfile
import threading

import pytest

from pappus import cli, client, datadir
from pappus.client import Msg, _load_dialogs, _save_dialogs, _store_path


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.setattr(client, "STORE_NOTICES", [])
    monkeypatch.setattr(client, "SNAPSHOT_MINUTES", 15.0)
    monkeypatch.setattr(client, "SNAPSHOT_KEEP", 20)


def _dialogs(n=3, tag="x"):
    return {f"d{i}": [Msg(f"m{i}", "note", f"{tag}{i}")] for i in range(n)}


def test_concurrent_saves_while_mutating_stay_valid(tmp_path, capsys):
    dialogs = _dialogs()
    errors = []

    def saver():
        try:
            for _ in range(40):
                _save_dialogs("kernel", dialogs)
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    def mutator():
        for i in range(400):
            dialogs.setdefault(f"new{i % 7}", []).append(Msg(f"n{i}", "note", "y"))
            if i % 5 == 0:
                dialogs.pop(f"new{(i + 3) % 7}", None)

    ts = [threading.Thread(target=saver) for _ in range(4)] + [threading.Thread(target=mutator)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert errors == []
    assert "could not save" not in capsys.readouterr().err
    assert isinstance(json.loads(_store_path("kernel").read_text()), dict)
    assert not list(tmp_path.glob("*.tmp"))


def test_corrupt_store_restores_from_bak_and_keeps_damaged_copy(tmp_path):
    _save_dialogs("kernel", _dialogs(tag="old"))
    _save_dialogs("kernel", _dialogs(tag="new"))               # .bak now holds "old"
    p = _store_path("kernel")
    p.write_text('{"truncated": [')
    loaded = _load_dialogs("kernel")
    assert loaded["d0"][0].content == "old0"
    assert json.loads(p.read_text())["d0"][0]["content"] == "old0"
    aside = list(tmp_path.glob("dialogs-kernel.json.corrupt-*"))
    assert len(aside) == 1 and aside[0].read_text() == '{"truncated": ['
    assert client.STORE_NOTICES and "restored from dialogs-kernel.json.bak" in client.STORE_NOTICES[0]


def test_corrupt_store_and_bak_fall_back_to_newest_good_snapshot(tmp_path, monkeypatch):
    monkeypatch.setattr(client, "SNAPSHOT_MINUTES", 0)
    _save_dialogs("kernel", _dialogs(tag="a"))
    _save_dialogs("kernel", _dialogs(tag="b"))                 # snapshot of "a"
    _save_dialogs("kernel", _dialogs(tag="c"))                 # snapshot of "b"
    p = _store_path("kernel")
    p.write_text("garbage")
    p.with_suffix(".json.bak").write_text("[]")                # not an object either
    assert _load_dialogs("kernel")["d0"][0].content == "b0"


def test_corrupt_store_with_no_backup_starts_empty_but_preserves_it(tmp_path):
    p = _store_path("kernel")
    p.write_text("[1, 2")
    assert _load_dialogs("kernel") == {}
    assert (next(tmp_path.glob("dialogs-kernel.json.corrupt-*"))).read_text() == "[1, 2"
    assert "no good backup" in client.STORE_NOTICES[0]


def test_unmovable_corrupt_store_raises_instead_of_being_overwritten(tmp_path, monkeypatch):
    p = _store_path("kernel")
    p.write_text("{bad")
    real = type(p).replace

    def deny(self, target):
        if self == p:
            raise PermissionError("read-only")
        return real(self, target)
    monkeypatch.setattr(type(p), "replace", deny)
    with pytest.raises(client.StoreUnreadable):
        _load_dialogs("kernel")
    assert p.read_text() == "{bad"


def test_snapshots_are_throttled_pruned_and_only_of_good_stores(tmp_path, monkeypatch):
    for i in range(3):
        _save_dialogs("kernel", _dialogs(tag=str(i)))
    assert len(client._snapshots(_store_path("kernel"))) == 1   # throttled to one
    monkeypatch.setattr(client, "SNAPSHOT_MINUTES", 0)
    monkeypatch.setattr(client, "SNAPSHOT_KEEP", 3)
    for i in range(6):
        _save_dialogs("kernel", _dialogs(tag=f"k{i}"))
    snaps = client._snapshots(_store_path("kernel"))
    assert len(snaps) == 3
    assert all(isinstance(json.loads(s.read_text()), dict) for s in snaps)
    _store_path("kernel").write_text("not json")
    client._maybe_snapshot(_store_path("kernel"))              # damaged: not kept
    assert all(isinstance(json.loads(s.read_text()), dict) for s in client._snapshots(_store_path("kernel")))


def test_page_shows_store_recovery_banner(monkeypatch):
    import pappus.app as app
    from fasthtml.common import to_xml
    monkeypatch.setattr(client, "STORE_NOTICES", ["dialogs-kernel.json was unreadable"])
    assert "dialogs-kernel.json was unreadable" in to_xml(app.Page())


def test_restore_refused_while_app_running(tmp_path, monkeypatch, capsys):
    (tmp_path / "dialogs-kernel.json").write_text("{}")
    arc, _ = datadir.backup(tmp_path / "b.tar.gz")
    (tmp_path / ".serve.pid").write_text(str(os.getpid() + 100000))
    monkeypatch.setattr(datadir, "_pid_alive", lambda pid: True)
    with pytest.raises(datadir.AppRunning):
        datadir.restore(arc)
    assert cli.main(["restore", str(arc)]) == 1
    assert "stop it first" in capsys.readouterr().err
    monkeypatch.setattr(datadir, "_pid_alive", lambda pid: False)   # stale pidfile
    datadir.restore(arc, force=True)


def test_mark_serving_records_own_pid_which_restore_ignores(tmp_path):
    datadir.mark_serving()
    assert (tmp_path / ".serve.pid").read_text() == str(os.getpid())
    assert datadir.running_server_pid() is None                # it's us, not another app


def test_backup_skips_snapshots_damaged_files_and_pidfile(tmp_path, monkeypatch):
    monkeypatch.setattr(client, "SNAPSHOT_MINUTES", 0)
    _save_dialogs("kernel", _dialogs())
    _save_dialogs("kernel", _dialogs(tag="z"))
    (tmp_path / "dialogs-kernel.json.corrupt-20260101-000000").write_text("x")
    (tmp_path / ".serve.pid").write_text("1")
    arc, _ = datadir.backup(tmp_path / "b.tar.gz")
    names = tarfile.open(arc).getnames()
    assert "data/dialogs-kernel.json" in names
    assert not any(n.startswith("data/backups/") or ".corrupt-" in n or n.endswith(".serve.pid")
                   for n in names)


def test_snapshots_never_cross_targets(tmp_path, monkeypatch):
    monkeypatch.setattr(client, "SNAPSHOT_MINUTES", 0)
    _save_dialogs("kernel-h100", {"h": [Msg("h1", "note", "h100 notebook")]})
    _save_dialogs("kernel-h100", {"h": [Msg("h1", "note", "h100 again")]})
    assert client._snapshots(_store_path("kernel")) == []
    _store_path("kernel").write_text("garbage")                   # no .bak for kernel
    assert _load_dialogs("kernel") == {}                          # not h100's notebook
    assert len(client._snapshots(_store_path("kernel-h100"))) == 1


def test_concurrent_loads_of_a_corrupt_store_recover_once(tmp_path):
    _save_dialogs("kernel", _dialogs(tag="good"))
    _save_dialogs("kernel", _dialogs(tag="good"))
    _store_path("kernel").write_text("{bad")
    results, errors = [], []

    def load():
        try:
            results.append(_load_dialogs("kernel"))
        except Exception as e:  # noqa: BLE001
            errors.append(e)
    ts = [threading.Thread(target=load) for _ in range(8)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert errors == []
    assert all(r["d0"][0].content == "good0" for r in results)
    assert len(list(tmp_path.glob("dialogs-kernel.json.corrupt-*"))) == 1


def test_second_serve_does_not_clobber_live_pidfile(tmp_path, monkeypatch):
    (tmp_path / ".serve.pid").write_text("424242")
    monkeypatch.setattr(datadir, "_pid_alive", lambda pid: True)
    datadir.mark_serving()
    assert (tmp_path / ".serve.pid").read_text() == "424242"
