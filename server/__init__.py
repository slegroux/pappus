"""Self-hostable SolveIt-compatible kernel server (a runnable stand-in)."""

# Former name: settings exported as SIDEKICK_* (old launchers, other machines,
# shell profiles) keep working as their PAPPUS_* equivalents. An explicit
# PAPPUS_* value always wins.
import os as _os

for _k, _v in list(_os.environ.items()):
    if _k.startswith("SIDEKICK_"):
        _os.environ.setdefault("PAPPUS_" + _k[len("SIDEKICK_"):], _v)
