"""Papers are per-machine: never under the git-synced PAPPUS_DATA dir, and an
old in-repo cache migrates out without re-conversion or broken figure links."""
import json
import os

from pappus import paper as paperlib


def test_cache_dir_is_local_default(tmp_path, monkeypatch):
    monkeypatch.delenv("PAPPUS_DATA", raising=False)
    monkeypatch.delenv("PAPPUS_PAPERS", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert paperlib._cache_dir() == tmp_path / "home" / ".config" / "pappus" / "papers"


def test_cache_dir_follows_data_root(tmp_path, monkeypatch):
    monkeypatch.setenv("PAPPUS_DATA", str(tmp_path / "mydata"))
    monkeypatch.delenv("PAPPUS_PAPERS", raising=False)
    assert paperlib._cache_dir() == tmp_path / "mydata" / "papers"


def test_cache_dir_override(tmp_path, monkeypatch):
    monkeypatch.setenv("PAPPUS_PAPERS", str(tmp_path / "mine"))
    assert paperlib._cache_dir() == tmp_path / "mine"


def _legacy(tmp_path):
    old = tmp_path / "data" / "papers"
    (old / "uploads").mkdir(parents=True)
    pdf = old / "uploads" / "abc.pdf"
    pdf.write_bytes(b"%PDF-1.4 x")
    os.utime(pdf, ns=(1_700_000_000_000_000_000, 1_700_000_000_000_000_000))
    (old / f"{paperlib._md_key(pdf)}.md").write_text("# converted")
    (old / "assets" / "k1").mkdir(parents=True)
    (old / "assets" / "k1" / "fig.jpeg").write_bytes(b"img")
    (old / "url-deadbeef.md").write_text("# web page")
    (old / "sources.json").write_text(json.dumps({
        "paper/a": {"source": str(pdf), "name": "a.pdf"},
        "paper/b": {"source": "/elsewhere/b.pdf", "name": "b.pdf"},
    }))
    return old


def test_migrate_moves_everything_and_rekeys(tmp_path, monkeypatch):
    old = _legacy(tmp_path)
    monkeypatch.setenv("PAPPUS_DATA", str(tmp_path / "data"))
    new = tmp_path / "local"
    monkeypatch.setenv("PAPPUS_PAPERS", str(new))

    moved = paperlib.migrate_legacy_cache()
    assert moved

    pdf = new / "uploads" / "abc.pdf"
    assert pdf.read_bytes() == b"%PDF-1.4 x"
    assert pdf.stat().st_mtime_ns == 1_700_000_000_000_000_000   # mtime kept
    # markdown reachable under the NEW path-keyed name → no re-conversion
    assert paperlib.cache_path(pdf).read_text() == "# converted"
    # figures keep their key so /paper/asset/k1/... links in dialogs still resolve
    assert (new / "assets" / "k1" / "fig.jpeg").read_bytes() == b"img"
    assert (new / "url-deadbeef.md").read_text() == "# web page"
    srcs = json.loads((new / "sources.json").read_text())
    assert srcs["paper/a"]["source"] == str(pdf)
    assert srcs["paper/b"]["source"] == "/elsewhere/b.pdf"
    assert not (old / "uploads" / "abc.pdf").exists()


def test_migrate_never_overwrites_and_is_idempotent(tmp_path, monkeypatch):
    _legacy(tmp_path)
    monkeypatch.setenv("PAPPUS_DATA", str(tmp_path / "data"))
    new = tmp_path / "local"
    (new / "assets" / "k1").mkdir(parents=True)
    (new / "assets" / "k1" / "fig.jpeg").write_bytes(b"newer")
    (new / "sources.json").write_text(json.dumps({"paper/a": {"source": "/keep.pdf"}}))
    monkeypatch.setenv("PAPPUS_PAPERS", str(new))

    paperlib.migrate_legacy_cache()
    assert (new / "assets" / "k1" / "fig.jpeg").read_bytes() == b"newer"
    assert json.loads((new / "sources.json").read_text())["paper/a"]["source"] == "/keep.pdf"
    before = sorted(p.name for p in new.rglob("*"))
    assert paperlib.migrate_legacy_cache() == []                 # nothing left to report
    assert sorted(p.name for p in new.rglob("*")) == before


def test_migrate_does_not_resurrect_deleted_links(tmp_path, monkeypatch):
    old = _legacy(tmp_path)
    monkeypatch.setenv("PAPPUS_DATA", str(tmp_path / "data"))
    new = tmp_path / "local"
    monkeypatch.setenv("PAPPUS_PAPERS", str(new))
    paperlib.migrate_legacy_cache()
    assert (old / "sources.json.migrated").is_file() and not (old / "sources.json").exists()
    (new / "sources.json").write_text("{}")                      # user dropped the links
    paperlib.migrate_legacy_cache()
    assert json.loads((new / "sources.json").read_text()) == {}


def test_migrate_survives_malformed_sources(tmp_path, monkeypatch):
    old = _legacy(tmp_path)
    (old / "sources.json").write_text("[1]")                     # not an object
    monkeypatch.setenv("PAPPUS_DATA", str(tmp_path / "data"))
    new = tmp_path / "local"
    new.mkdir()
    (new / "sources.json").write_text("[]")                      # nor this
    monkeypatch.setenv("PAPPUS_PAPERS", str(new))
    paperlib.migrate_legacy_cache()                              # must not raise
    assert (new / "sources.json").read_text() == "[]"            # unreadable dst untouched
    assert (new / "uploads" / "abc.pdf").is_file()               # the rest still moved


def test_migrate_rewrites_sources_recorded_via_symlink(tmp_path, monkeypatch):
    _legacy(tmp_path)
    link = tmp_path / "datalink"
    link.symlink_to(tmp_path / "data")
    srcf = tmp_path / "data" / "papers" / "sources.json"
    srcf.write_text(json.dumps({"paper/a": {"source": str(link / "papers" / "uploads" / "abc.pdf")}}))
    monkeypatch.setenv("PAPPUS_DATA", str(link))
    new = tmp_path / "local"
    monkeypatch.setenv("PAPPUS_PAPERS", str(new))
    paperlib.migrate_legacy_cache()
    assert json.loads((new / "sources.json").read_text())["paper/a"]["source"] == \
        str(new / "uploads" / "abc.pdf")


def test_migrate_noop_without_legacy_or_when_same_dir(tmp_path, monkeypatch):
    monkeypatch.delenv("PAPPUS_DATA", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))   # never look at the real home
    assert paperlib.migrate_legacy_cache() == []
    monkeypatch.setenv("PAPPUS_DATA", str(tmp_path))          # conftest points papers
    (tmp_path / "papers").mkdir()                                # at tmp_path/papers too
    assert paperlib.migrate_legacy_cache() == []
