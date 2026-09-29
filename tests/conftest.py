import sys
from pathlib import Path

import pytest

# Make `import sidekick` / `import server` work for any test file run alone
# (uv runs the project unpackaged, `package = false`).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture(autouse=True)
def _isolated_user_data(tmp_path, monkeypatch):
    """Real notebooks, papers, keys and tool config live in
    ~/.config/solveit-sidekick. Point every test at its own tmp_path so none can
    read or clobber them. Tests may still override any of these themselves."""
    monkeypatch.setenv("SIDEKICK_DATA", str(tmp_path))
    monkeypatch.setenv("SIDEKICK_PAPERS", str(tmp_path / "papers"))
    monkeypatch.setenv("SIDEKICK_SECRETS", str(tmp_path / "secrets.json"))
    monkeypatch.setenv("SIDEKICK_TOOLS", str(tmp_path / "tools.json"))
