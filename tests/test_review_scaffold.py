"""Tests for the faded-scaffolding learning mechanic (F6).

Covers the pure fade logic in ``pappus.scaffold`` and the ``/cell/fade`` route
wiring in ``pappus.app`` (mirrors the split-to-code pattern).
"""

from pappus import scaffold


# A worked example that exercises every fade rule: an import (scaffolding), a
# comment (kept as the goal), load-bearing assignments, a non-trivial return,
# and a print (scaffolding).
WORKED = """\
import numpy as np


def standardize(X):
    # subtract the mean and divide by the std
    mean = X.mean(axis=0)
    std = X.std(axis=0)
    result = (X - mean) / std
    return result


print(standardize(data))"""


def _code_mass(src: str) -> int:
    """Amount of *revealed* code: characters of each line before any comment,
    stripped. Blanking a RHS or dropping a body strictly reduces this."""
    total = 0
    for ln in src.splitlines():
        code, _ = scaffold._split_comment(ln)
        total += len(code.strip())
    return total


def test_fade_level0_identity():
    assert scaffold.fade_code(WORKED, 0) == WORKED


def test_fade_level1_blanks_loadbearing():
    faded = scaffold.fade_code(WORKED, 1)
    # the placeholder appears where the load-bearing RHS used to be
    assert "___" in faded
    # a key right-hand side is gone (the computation is blanked)
    assert "X.mean(axis=0)" not in faded
    assert "(X - mean) / std" not in faded
    # scaffolding survives: import, comment, def signature, and print
    assert "import numpy as np" in faded
    assert "def standardize(X):" in faded
    assert "# subtract the mean and divide by the std" in faded
    assert "print(standardize(data))" in faded


def test_fade_level1_keeps_trivial_assignment():
    src = "n = 3\ntotal = a * b + c"
    faded = scaffold.fade_code(src, 1)
    assert "n = 3" in faded                 # trivial literal RHS is revealed
    assert "a * b + c" not in faded         # non-trivial RHS is blanked
    assert "___" in faded


def test_fade_level2_mostly_blank():
    l2 = scaffold.fade_code(WORKED, 2)
    # keeps a goal + signature stub, drops the worked bodies
    assert "def standardize(X):" in l2
    assert "___" in l2
    assert "X.mean(axis=0)" not in l2
    assert "(X - mean) / std" not in l2
    # substantially less code than the worked example
    assert _code_mass(l2) < _code_mass(WORKED)
    assert len(l2.splitlines()) < len(WORKED.splitlines())


def test_fade_stages():
    stages = scaffold.fade_stages(WORKED)
    assert len(stages) == 3
    assert stages[0] == WORKED
    m0, m1, m2 = (_code_mass(s) for s in stages)
    # monotonically less revealed code as the fade deepens
    assert m0 >= m1 >= m2
    assert m0 > m2


def test_check_prompt():
    prompt = scaffold.check_prompt("x = expensive()", "x = cheap()")
    assert "x = expensive()" in prompt        # the worked solution
    assert "x = cheap()" in prompt            # the learner's attempt
    assert "hint" in prompt.lower()           # asks for a hint
    assert "do not" in prompt.lower() or "not" in prompt.lower()  # don't reveal answer


# ── route wiring ─────────────────────────────────────────────────────────────
def test_fade_route_inserts_exercise():
    import pappus.app as app
    app.STATE["dialog"] = "fade/route"
    bk = app.STATE["backend"]
    bk.messages("fade/route")
    src = bk.add(
        "fade/route",
        "import numpy as np\nmean = X.mean(axis=0)\nresult = mean * 2\nprint(result)",
        "code",
    )
    before = len(bk.messages("fade/route"))

    app.cell_fade(id=src.id, level=1)
    msgs = bk.messages("fade/route")
    assert len(msgs) == before + 1           # one derived cell inserted below
    ex = msgs[-1]
    assert ex.msg_type == "code"
    assert app._is_exercise(ex.content)      # it's a faded exercise cell
    assert "___" in ex.content               # load-bearing lines are blanked
    assert "import numpy as np" in ex.content  # scaffolding survives


def test_fade_route_refades_in_place():
    import pappus.app as app
    app.STATE["dialog"] = "fade/inplace"
    bk = app.STATE["backend"]
    bk.messages("fade/inplace")
    src = bk.add("fade/inplace", "mean = X.mean(axis=0)\nprint(mean)", "code")

    app.cell_fade(id=src.id, level=1)
    ex = bk.messages("fade/inplace")[-1]
    count = len(bk.messages("fade/inplace"))

    # "Show worked answer" (level 0) re-fades the SAME exercise cell in place.
    app.cell_fade(id=ex.id, level=0)
    msgs = bk.messages("fade/inplace")
    assert len(msgs) == count                # no new cell
    ex2 = next(m for m in msgs if m.id == ex.id)
    assert "X.mean(axis=0)" in ex2.content   # full worked solution revealed
    assert app._is_exercise(ex2.content)     # still detectable as an exercise


def test_check_route_inserts_prompt():
    import pappus.app as app
    app.STATE["dialog"] = "fade/check"
    bk = app.STATE["backend"]
    bk.messages("fade/check")
    src = bk.add("fade/check", "result = expensive_call()", "code")
    app.cell_fade(id=src.id, level=1)
    ex = bk.messages("fade/check")[-1]
    before = len(bk.messages("fade/check"))

    app.cell_check(id=ex.id)
    msgs = bk.messages("fade/check")
    assert len(msgs) == before + 1
    prompt = msgs[-1]
    assert prompt.msg_type == "prompt"
    assert "expensive_call()" in prompt.content   # reuses the worked source
