"""Pappus — a unified, Claude-desktop-style interface for SolveIt
that runs against your laptop or a remote H100, switchable at runtime."""
__version__ = "0.1.0"

# Former name: settings exported as SIDEKICK_* (old launchers, other machines,
# shell profiles) keep working as their PAPPUS_* equivalents. An explicit
# PAPPUS_* value always wins.
import os as _os

for _k, _v in list(_os.environ.items()):
    if _k.startswith("SIDEKICK_"):
        _os.environ.setdefault("PAPPUS_" + _k[len("SIDEKICK_"):], _v)
