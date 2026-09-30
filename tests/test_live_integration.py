"""Live integration tests — run only against a REAL SolveIt server.

These are gated on env vars so they skip cleanly in CI and on machines with no
server. To run them, point at a reachable instance:

    export SOLVEIT_LIVE_URL=http://localhost:5001      # your laptop or tunneled H100
    export SOLVEIT_LIVE_TOKEN=dummy                    # real _solveit cookie for remote
    python -m pytest tests/test_live_integration.py -v

They exercise the real round-trip: create a dialog, run code, read it back,
clean up — so a green run here means the actual SolveIt path works, not the mock.
"""
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

LIVE_URL = os.environ.get("SOLVEIT_LIVE_URL")
LIVE_TOKEN = os.environ.get("SOLVEIT_LIVE_TOKEN", "dummy")

# Skip the whole module unless a live target is configured.
pytestmark = pytest.mark.skipif(
    not LIVE_URL,
    reason="set SOLVEIT_LIVE_URL (and SOLVEIT_LIVE_TOKEN) to run live tests",
)


@pytest.fixture(scope="module")
def target():
    from pappus.targets import Target
    return Target(name="live", url=LIVE_URL, token=LIVE_TOKEN, ssh=None)


def test_solveit_client_importable():
    pytest.importorskip("solveit_client", reason="pip install solveit_client")


def test_doctor_reaches_live_server(target):
    from pappus.doctor import run_checks
    checks = run_checks(target)
    assert any(ok and label == "AUTH" for ok, label, _ in checks), (
        "live /test_route did not accept the token — check SOLVEIT_LIVE_TOKEN"
    )


def test_live_backend_code_roundtrip(target):
    pytest.importorskip("solveit_client")
    from pappus.client import connect

    backend, warning = connect(target)
    assert backend.live, f"expected a live backend, got mock: {warning}"

    dialog = "pappus-ci/roundtrip"
    msg = backend.add(dialog, "6 * 7", "code")
    out = backend.exec(dialog, msg.id)
    assert "42" in (out.output or ""), f"unexpected code output: {out.output!r}"


@pytest.mark.skipif(
    not os.environ.get("SOLVEIT_LIVE_PROMPT"),
    reason="set SOLVEIT_LIVE_PROMPT=1 to exercise a real (billable) AI call",
)
def test_live_prompt_with_model(target):
    """Real AI round-trip — opt-in because it spends tokens. Honors the model switch."""
    pytest.importorskip("solveit_client")
    from pappus.client import connect

    backend, _ = connect(target)
    model = os.environ.get("SOLVEIT_LIVE_MODEL", "claude")
    msg = backend.add("pappus-ci/prompt", "Reply with the single word: pong", "prompt", model=model)
    out = backend.exec("pappus-ci/prompt", msg.id)
    assert out.output and out.output.strip(), "empty AI response"
