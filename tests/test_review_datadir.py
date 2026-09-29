"""User data is local, never in the repo, and portable via backup/restore."""
import json
import os
import tarfile

import pytest

from sidekick import cli, datadir
from sidekick import paper as paperlib


def _seed(root):
    (root / "papers" / "uploads").mkdir(parents=True)
    (root / "dialogs-kernel.json").write_text('{"d": []}')
    (root / "dialogs-kernel.json.bak").write_text("old")
    (root / "recall.json").write_text("{}")
    (root / "blog" / "posts").mkdir(parents=True)
    (root / "blog" / "posts" / "p.qmd").write_text("post")
    pdf = root / "papers" / "uploads" / "abc.pdf"
    pdf.write_bytes(b"%PDF")
    (root / "papers" / f"{paperlib._md_key(pdf)}.md").write_text("# conv")
    (root / "papers" / "assets" / "k1").mkdir(parents=True)
    (root / "papers" / "assets" / "k1" / "f.jpeg").write_bytes(b"img")


def _env(monkeypatch, root):
    monkeypatch.setenv("SIDEKICK_DATA", str(root))
    monkeypatch.setenv("SIDEKICK_PAPERS", str(root / "papers"))
    monkeypatch.setenv("SIDEKICK_SECRETS", str(root / "secrets.json"))
    monkeypatch.setenv("SIDEKICK_TOOLS", str(root / "tools.json"))


def test_default_root_is_home_config(tmp_path, monkeypatch):
    monkeypatch.delenv("SIDEKICK_DATA", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert datadir.data_root() == tmp_path / ".config" / "solveit-sidekick"


def test_every_store_uses_the_data_root(tmp_path, monkeypatch):
    from sidekick import blog, client, libraries, recall
    monkeypatch.setenv("SIDEKICK_DATA", str(tmp_path / "r"))
    r = tmp_path / "r"
    assert client._store_path("kernel").parent == r
    assert recall.default_schedule_path() == r / "recall.json"
    assert libraries._path() == r / "libraries.json"
    assert blog.default_blog_dir() == str(r / "blog")


def test_repo_data_is_gitignored():
    gi = (datadir.REPO_DATA.parent / ".gitignore").read_text().splitlines()
    assert "data/" in gi


def test_migrate_repo_data_moves_without_overwriting(tmp_path, monkeypatch):
    old = tmp_path / "repo-data"
    _seed(old)
    new = tmp_path / "home"
    new.mkdir()
    (new / "recall.json").write_text('{"mine": 1}')             # already here
    _env(monkeypatch, new)
    moved = datadir.migrate_repo_data(old)
    assert (new / "dialogs-kernel.json").read_text() == '{"d": []}'
    assert (new / "blog" / "posts" / "p.qmd").read_text() == "post"
    assert (new / "papers" / "assets" / "k1" / "f.jpeg").is_file()
    assert paperlib.cache_path(new / "papers" / "uploads" / "abc.pdf").read_text() == "# conv"
    assert json.loads((new / "recall.json").read_text()) == {"mine": 1}
    assert "recall.json" in datadir.leftovers(old)               # reported, not lost
    assert "dialogs-kernel.json" in moved
    assert datadir.migrate_repo_data(old) == []                  # idempotent


def test_serve_ignores_stale_repo_data_env(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SIDEKICK_DATA", str(datadir.REPO_DATA))
    monkeypatch.setattr(datadir, "migrate_repo_data", lambda old=None: [])
    monkeypatch.setattr(datadir, "leftovers", lambda old=None: [])
    cli._move_out_of_repo()
    assert "SIDEKICK_DATA" not in os.environ
    assert "ignoring SIDEKICK_DATA" in capsys.readouterr().err


def test_backup_restore_roundtrip_to_new_install(tmp_path, monkeypatch):
    a = tmp_path / "machineA"
    _seed(a)
    (a / "secrets.json").write_text('{"ANTHROPIC_API_KEY": "sk"}')
    (a / "tools.json").write_text('{"allow": []}')
    _env(monkeypatch, a)
    arc, n = datadir.backup(tmp_path / "b.tar.gz")
    assert oct(arc.stat().st_mode & 0o777) == "0o600"
    names = tarfile.open(arc).getnames()
    assert "data/dialogs-kernel.json" in names
    assert "config/tools.json" in names
    assert not any("secrets" in x for x in names)                # keys opt-in only
    assert not any(x.endswith(".bak") for x in names)
    assert not any(x == "data/tools.json" for x in names)        # not archived twice

    b = tmp_path / "machineB"                                    # a different path:
    _env(monkeypatch, b)                                         # md keys must re-key
    restored, skipped = datadir.restore(arc)
    assert not skipped
    assert (b / "dialogs-kernel.json").read_text() == '{"d": []}'
    assert (b / "tools.json").read_text() == '{"allow": []}'
    assert (b / "papers" / "assets" / "k1" / "f.jpeg").is_file()
    assert paperlib.cache_path(b / "papers" / "uploads" / "abc.pdf").read_text() == "# conv"
    assert not (b / "secrets.json").exists()


def test_backup_with_secrets_and_restore_keeps_them_private(tmp_path, monkeypatch):
    a = tmp_path / "a"
    a.mkdir()
    (a / "secrets.json").write_text('{"K": "v"}')
    _env(monkeypatch, a)
    arc, _ = datadir.backup(tmp_path / "s.tar.gz", with_secrets=True)
    assert "config/secrets.json" in tarfile.open(arc).getnames()
    b = tmp_path / "b"
    _env(monkeypatch, b)
    datadir.restore(arc)
    assert oct((b / "secrets.json").stat().st_mode & 0o777) == "0o600"


def test_restore_keeps_existing_unless_forced(tmp_path, monkeypatch):
    a = tmp_path / "a"
    _seed(a)
    _env(monkeypatch, a)
    arc, _ = datadir.backup(tmp_path / "x.tar.gz")
    (a / "dialogs-kernel.json").write_text("newer")
    _, skipped = datadir.restore(arc)
    assert (a / "dialogs-kernel.json").read_text() == "newer"
    assert "data/dialogs-kernel.json (exists)" in skipped
    datadir.restore(arc, force=True)
    assert (a / "dialogs-kernel.json").read_text() == '{"d": []}'


@pytest.mark.parametrize("evil", ["data/../../escape.txt", "/etc/evil", "other/x", "data"])
def test_restore_refuses_unsafe_members(tmp_path, monkeypatch, evil):
    import io
    arc = tmp_path / "evil.tar.gz"
    with tarfile.open(arc, "w:gz") as t:
        info = tarfile.TarInfo(evil)
        info.size = 3
        t.addfile(info, io.BytesIO(b"bad"))
        link = tarfile.TarInfo("data/link")
        link.type, link.linkname = tarfile.SYMTYPE, "/etc/passwd"
        t.addfile(link)
    root = tmp_path / "root"
    _env(monkeypatch, root)
    restored, skipped = datadir.restore(arc)
    assert restored == []
    assert not (tmp_path / "escape.txt").exists()
    assert not (root / "link").exists()


def test_cli_backup_and_restore(tmp_path, monkeypatch, capsys):
    a = tmp_path / "a"
    _seed(a)
    _env(monkeypatch, a)
    assert cli.main(["backup", str(tmp_path)]) == 0
    arc = next(tmp_path.glob("sidekick-backup-*.tar.gz"))
    b = tmp_path / "b"
    _env(monkeypatch, b)
    assert cli.main(["restore", str(arc)]) == 0
    assert (b / "dialogs-kernel.json").is_file()
    assert cli.main(["restore"]) == 2


def test_restore_refuses_bare_papers_member(tmp_path, monkeypatch):
    import io
    arc = tmp_path / "p.tar.gz"
    with tarfile.open(arc, "w:gz") as t:
        info = tarfile.TarInfo("data/papers")
        info.size = 1
        t.addfile(info, io.BytesIO(b"x"))
    root = tmp_path / "r"
    _env(monkeypatch, root)
    (root / "papers").mkdir(parents=True)
    for force in (False, True):
        restored, skipped = datadir.restore(arc, force=force)
        assert restored == [] and (root / "papers").is_dir()
    assert not list(root.glob("*.restore-tmp"))


def test_restored_secrets_stay_private_under_any_name(tmp_path, monkeypatch):
    a = tmp_path / "a"
    a.mkdir()
    (a / "secrets.json").write_text('{"K": "v"}')
    _env(monkeypatch, a)
    arc, _ = datadir.backup(tmp_path / "s.tar.gz", with_secrets=True)
    b = tmp_path / "b"
    _env(monkeypatch, b)
    monkeypatch.setenv("SIDEKICK_SECRETS", str(b / "keys.cfg"))
    datadir.restore(arc)
    assert oct((b / "keys.cfg").stat().st_mode & 0o777) == "0o600"


def test_stray_secrets_and_old_archives_not_backed_up(tmp_path, monkeypatch):
    a = tmp_path / "a"
    _seed(a)
    _env(monkeypatch, a)
    monkeypatch.setenv("SIDEKICK_SECRETS", str(tmp_path / "elsewhere.json"))
    (a / "secrets.json").write_text("stray key")                 # e.g. from old data/
    (a / "old.tar.gz").write_bytes(b"prior backup")
    (a / "blog" / ".git").mkdir()
    (a / "blog" / ".git" / "HEAD").write_text("ref")
    arc, _ = datadir.backup(tmp_path / "n.tar.gz")
    names = tarfile.open(arc).getnames()
    assert not any("secrets" in x for x in names)
    assert not any(x.endswith("old.tar.gz") or "/.git/" in x for x in names)
    assert "data/blog/posts/p.qmd" in names
