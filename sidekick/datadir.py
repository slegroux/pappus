"""Where your notebooks and everything else you create live — and how to move them.

All user data is local to the machine and never part of the git repo:

    ~/.config/solveit-sidekick/          (override with SIDEKICK_DATA)
        dialogs-<target>.json            notebooks: cells, outputs, sidebar list
        dialogs-<target>.local.json      dialogs marked "keep local only"
        dialogs-<target>.private         names of those dialogs
        recall.json                      spaced-repetition schedule
        libraries.json                   registered nbdev libraries
        blog/                            the Quarto blog project
        papers/                          reading library (SIDEKICK_PAPERS)
        tools.json, secrets.json         Ask-AI tool policy, API keys

`backup()` packs that into one .tar.gz and `restore()` unpacks it on a new
install (`sidekick backup` / `sidekick restore`). API keys are left out of a
backup unless asked for, so an archive can be copied around safely.

`migrate_repo_data()` moves an old in-repo store (`<repo>/data`, which the
justfile used to point SIDEKICK_DATA at and `just sync` pushed to GitHub) into
the local home once, never overwriting anything already there.
"""
from __future__ import annotations

import os
import shutil
import tarfile
import time
from pathlib import Path, PurePosixPath

REPO_DATA = Path(__file__).resolve().parent.parent / "data"

# rolling save-backups and half-written temp files are not worth carrying over
_SKIP_SUFFIXES = (".bak", ".tmp", ".restore-tmp", ".tar.gz")   # .tar.gz: older backups
_SKIP_NAMES = {".DS_Store", ".serve.pid"}
_SKIP_DIRS = {".git", "__pycache__", ".ipynb_checkpoints", ".quarto"}


def data_root() -> Path:
    base = os.environ.get("SIDEKICK_DATA")
    return Path(base).expanduser() if base else Path.home() / ".config" / "solveit-sidekick"


def _config_files() -> dict[str, Path]:
    """Config that lives at its own (env-overridable) path, keyed by archive name."""
    from .secrets_store import secrets_path
    from .tools_config import tools_path
    return {"config/tools.json": tools_path(), "config/secrets.json": secrets_path()}


def _same(a: Path, b: Path) -> bool:
    try:
        return a.resolve() == b.resolve()
    except OSError:
        return False


# ---- one-time move out of the repo ------------------------------------------
def migrate_repo_data(old: Path | None = None) -> list[str]:
    """Move an old in-repo data dir into data_root(). Non-destructive: an entry
    that already exists at the destination is left in both places (reported by
    the caller as skipped). Papers go through paper.migrate_legacy_cache so
    figure links survive and conversions are re-keyed. Returns what moved."""
    from . import paper
    old = old or REPO_DATA
    new = data_root()
    if not old.is_dir() or _same(old, new):
        return []
    new.mkdir(parents=True, exist_ok=True)
    moved = [f"papers/{m}" for m in paper.migrate_legacy_cache(old / "papers")]
    for src in sorted(old.iterdir()):
        if src.name == "papers" or src.name in _SKIP_NAMES:
            continue
        dst = new / src.name
        if dst.exists():
            continue
        shutil.move(str(src), dst)
        moved.append(src.name)
    return moved


def leftovers(old: Path | None = None) -> list[str]:
    """Entries still in the old in-repo dir after migration (name clashes)."""
    old = old or REPO_DATA
    if not old.is_dir() or _same(old, data_root()):
        return []
    out = []
    for p in sorted(old.rglob("*")):
        if p.is_file() and p.name not in _SKIP_NAMES and not p.name.endswith(_SKIP_SUFFIXES) \
                and p.name != "sources.json.migrated":
            out.append(str(p.relative_to(old)))
    return out


# ---- is the app running? -------------------------------------------------------
def _pidfile() -> Path:
    return data_root() / ".serve.pid"


def mark_serving() -> None:
    """Record this `sidekick serve` process, so restore can refuse to run under it."""
    import atexit
    f = _pidfile()
    if running_server_pid():
        return                         # don't clobber a live server's record; if
    try:                               # this serve then fails to bind, it's intact
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(str(os.getpid()))
    except OSError:
        return
    me = os.getpid()

    def _clear():
        try:
            if f.read_text().strip() == str(me):
                f.unlink()
        except OSError:
            pass
    atexit.register(_clear)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def running_server_pid() -> int | None:
    """PID of a live `sidekick serve` using this data root, else None."""
    try:
        pid = int(_pidfile().read_text().strip())
    except (OSError, ValueError):
        return None
    return pid if pid != os.getpid() and _pid_alive(pid) else None


class AppRunning(RuntimeError):
    pass


# ---- backup / restore --------------------------------------------------------
_INDEX = "meta/paper-keys.json"   # upload name -> markdown cache key at backup time


def _walk(base: Path, prefix: str, special: list[Path],
          with_secrets: bool = False) -> list[tuple[Path, str]]:
    out = []
    if not base.is_dir():
        return out
    for p in sorted(base.rglob("*")):
        if not p.is_file() or p.is_symlink():
            continue
        rel = p.relative_to(base)
        if _SKIP_DIRS.intersection(rel.parts[:-1]):
            continue
        if prefix == "data/" and rel.parts[0] == "backups":
            continue                             # the store's own snapshots
        if ".corrupt-" in p.name:
            continue                             # damaged stores set aside
        if p.name in _SKIP_NAMES or p.name.endswith(_SKIP_SUFFIXES):
            continue
        if p.name == "secrets.json" and not with_secrets:
            continue                             # stray key files (e.g. old data/) too
        if any(_same(p, s) for s in special):
            continue                             # config is archived under config/
        out.append((p, prefix + rel.as_posix()))
    return out


def _members(with_secrets: bool) -> list[tuple[Path, str]]:
    from .paper import _cache_dir
    root, papers = data_root(), _cache_dir()
    special = list(_config_files().values())
    try:
        papers_inside = papers.resolve().is_relative_to(root.resolve())
    except OSError:
        papers_inside = True
    out = [(p, a) for p, a in _walk(root, "data/", special, with_secrets)
           if not (papers_inside is False and a.startswith("data/papers/"))]
    if not papers_inside:                        # SIDEKICK_PAPERS elsewhere
        out += _walk(papers, "data/papers/", special, with_secrets)
    cfg = _config_files()
    if not with_secrets:
        cfg.pop("config/secrets.json")
    out += [(p, arc) for arc, p in cfg.items() if p.is_file()]
    return out


def _paper_keys() -> dict[str, str]:
    """Markdown cache keys embed each upload's absolute path + mtime, which a new
    install won't share. Record them so restore can re-key (no re-conversion)."""
    from .paper import _cache_dir, _md_key
    up = _cache_dir() / "uploads"
    keys = {}
    for pdf in sorted(up.glob("*.pdf")) if up.is_dir() else []:
        k = _md_key(pdf)
        if (_cache_dir() / f"{k}.md").is_file():
            keys[pdf.name] = k
    return keys


def backup(dest: str | Path | None = None, with_secrets: bool = False) -> tuple[Path, int]:
    """Write every local data file to one .tar.gz. Returns (path, file count).
    The archive is created 0600, since notebooks can hold private content."""
    import io
    import json
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = Path(dest).expanduser() if dest else Path.cwd() / f"sidekick-backup-{stamp}.tar.gz"
    if dest.is_dir():
        dest = dest / f"sidekick-backup-{stamp}.tar.gz"
    members = _members(with_secrets)
    index = json.dumps(_paper_keys(), indent=1).encode()
    fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh, tarfile.open(fileobj=fh, mode="w:gz") as tar:
        for p, arc in members:
            tar.add(p, arcname=arc, recursive=False)
        info = tarfile.TarInfo(_INDEX)
        info.size, info.mtime = len(index), int(time.time())
        tar.addfile(info, io.BytesIO(index))
    return dest, len(members)


def _target_for(name: str) -> Path | None:
    """Map an archive member name to where it restores, or None if unsafe."""
    from .paper import _cache_dir
    pp = PurePosixPath(name)
    if pp.is_absolute() or ".." in pp.parts or len(pp.parts) < 2:
        return None
    cfg = _config_files()
    if name in cfg:
        return cfg[name]
    if pp.parts[0] != "data":
        return None
    if pp.parts[1] == "papers":
        if len(pp.parts) < 3:
            return None                          # would replace the papers dir
        return _cache_dir().joinpath(*pp.parts[2:])
    return data_root().joinpath(*pp.parts[1:])


def restore(archive: str | Path, force: bool = False) -> tuple[list[str], list[str]]:
    """Unpack a backup into this install. Existing files are kept unless
    `force`. Returns (restored, skipped). Run it with the app stopped: the app
    loads notebooks at startup and would overwrite them on its next save."""
    import json
    from .paper import _cache_dir, _md_key
    from .secrets_store import secrets_path
    pid = running_server_pid()
    if pid:
        # the app holds notebooks in memory and rewrites the store on every
        # edit, so restoring under it would be silently undone
        raise AppRunning(f"the app is running (pid {pid}); stop it first (just stop). "
                         f"If it isn't, delete {_pidfile()}")
    secret = secrets_path()
    restored, skipped, index = [], [], {}
    with tarfile.open(Path(archive).expanduser(), "r:gz") as tar:
        for m in tar.getmembers():
            if m.isdir():
                continue
            if not m.isfile():
                skipped.append(f"{m.name} (not a regular file)")
                continue                         # no links or devices
            if m.name == _INDEX:
                try:
                    index = json.loads(tar.extractfile(m).read())
                except ValueError:
                    index = {}
                continue
            dst = _target_for(m.name)
            if dst is None:
                skipped.append(f"{m.name} (unsafe path)")
                continue
            if dst.is_dir() or (dst.exists() and not force):
                skipped.append(f"{m.name} ({'is a directory' if dst.is_dir() else 'exists'})")
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            tmp = dst.with_name(dst.name + ".restore-tmp")
            try:
                fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "wb") as out:
                    shutil.copyfileobj(tar.extractfile(m), out)
                if not _same(dst, secret) and m.name != "config/secrets.json":
                    os.chmod(tmp, 0o644)         # keys stay 0600 whatever they're called
                os.utime(tmp, (m.mtime, m.mtime))
                tmp.replace(dst)
            except OSError as e:
                tmp.unlink(missing_ok=True)
                skipped.append(f"{m.name} ({e.strerror or e})")
                continue
            restored.append(m.name)
    # re-key converted markdown to this install's upload paths
    cache = _cache_dir()
    for name, old_key in (index.items() if isinstance(index, dict) else []):
        pdf, md = cache / "uploads" / str(name), cache / f"{old_key}.md"
        if not (isinstance(old_key, str) and old_key.isalnum() and "/" not in str(name)):
            continue                             # a crafted index can't point outside
        if not (pdf.is_file() and md.is_file()):
            continue
        new_md = cache / f"{_md_key(pdf)}.md"
        if not new_md.exists():
            shutil.copy2(md, new_md)
    return restored, skipped
