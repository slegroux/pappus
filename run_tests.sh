#!/usr/bin/env bash
# One-command test runner for Pappus (uv-based).
# uv creates the venv and installs deps from pyproject.toml on first run.
# No SolveIt server required — live tests skip unless SOLVEIT_LIVE_URL is set.
set -e
cd "$(dirname "$0")"
if command -v uv >/dev/null 2>&1; then
  uv run --extra dev pytest -v
else
  echo "uv not found — falling back to pip/pytest"
  python3 -m pip install -q python-fasthtml pyyaml pytest 2>/dev/null || true
  python3 -m pytest -v
fi
