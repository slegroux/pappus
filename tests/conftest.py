import sys
from pathlib import Path

import pytest

# Make `import pappus` / `import server` work for any test file run alone
# (uv runs the project unpackaged, `package = false`).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture(autouse=True)
def _isolated_user_data(tmp_path, monkeypatch):
    """Real notebooks, papers, keys and tool config live in
    ~/.config/pappus. Point every test at its own tmp_path so none can
    read or clobber them. Tests may still override any of these themselves."""
    # A throwaway HOME too: startup code that deliberately ignores PAPPUS_DATA
    # (e.g. the one-time ~/.config migration) must never reach the real home.
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("PAPPUS_DATA", str(tmp_path))
    monkeypatch.setenv("PAPPUS_PAPERS", str(tmp_path / "papers"))
    monkeypatch.setenv("PAPPUS_SECRETS", str(tmp_path / "secrets.json"))
    monkeypatch.setenv("PAPPUS_TOOLS", str(tmp_path / "tools.json"))
