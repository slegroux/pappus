"""Faded-scaffolding fade logic (F6) — pure, deterministic, no I/O, no app imports.

The "fade" turns a fully-worked code cell into a graded exercise by progressively
blanking its *load-bearing* lines while keeping the surrounding scaffolding intact,
so the learner always has something to lean on. This is the learning-science
mechanic ("faded worked examples") from
``.omc/plans/faded-scaffolding-learning-prototype.md``.

Three stages, produced from the SAME worked code:

* **level 0** — the fully-worked example, returned unchanged.
* **level 1** — *fill-in-the-code*: the load-bearing lines are blanked to
  ``___  # TODO: <hint>`` while imports, prints, comments, blank lines and
  ``def``/``class`` signatures survive as scaffolding.
* **level 2** — *from scratch*: collapse to a goal comment plus a couple of
  signature/``assert`` stubs; the bodies are gone.

"Load-bearing" heuristic (kept deliberately simple and documented):

* A line is load-bearing if it is a plain top-level assignment ``lhs = rhs``
  whose right-hand side is **not** a trivial literal (number, string, bool,
  ``None`` or an empty collection), or a ``return <expr>`` with a non-trivial
  expression. These are the lines that carry the computation.
* Everything else — imports, ``print`` calls, comments, blanks, control-flow
  keywords, decorators, ``def``/``class`` headers, and augmented assignments
  (``+=`` etc.) — is treated as scaffolding and left intact at level 1.

All functions are pure and deterministic so they can be unit-tested without the
app; the app layer is responsible for I/O (inserting/updating cells) and for the
AI-check step (which reuses the existing "Ask AI" prompt path with
:func:`check_prompt`).
"""

from __future__ import annotations

import re

BLANK = "___"

# Statement keywords that are structural scaffolding — kept verbatim at level 1.
_KEPT_KEYWORDS = frozenset({
    "import", "from", "print", "def", "class", "pass", "assert", "raise",
    "with", "for", "while", "if", "elif", "else", "try", "except", "finally",
    "global", "nonlocal", "del", "yield", "break", "continue", "lambda", "async",
})

# A right-hand side that carries no real computation (safe to reveal at level 1).
_TRIVIAL_RHS_RE = re.compile(
    r"""^(
        [+-]?\d+(\.\d+)?          # int / float literal
      | ["'].*["']                # a single string literal
      | True | False | None
      | \[\s*\] | \{\s*\} | \(\s*\)  # empty list / dict / tuple
    )$""",
    re.VERBOSE,
)


def _placeholder(hint: str) -> str:
    return f"{BLANK}  # TODO: {hint}"


def _split_comment(line: str) -> tuple[str, str]:
    """Split a trailing ``# ...`` comment off ``line``, ignoring ``#`` inside
    strings. Returns ``(code, comment)`` where ``comment`` includes the ``#`` (or
    is empty). Prototype-grade: does not track escapes inside strings."""
    instr = None
    for i, c in enumerate(line):
        if instr:
            if c == instr:
                instr = None
            continue
        if c in "\"'":
            instr = c
        elif c == "#":
            return line[:i].rstrip(), line[i:]
    return line, ""


def _assign_pos(code: str) -> int:
    """Index of a top-level plain-assignment ``=`` in ``code`` (outside brackets
    and strings, not part of ``==``/``<=``/``+=``/``:=`` etc.), or ``-1``."""
    depth = 0
    instr = None
    for i, c in enumerate(code):
        if instr:
            if c == instr:
                instr = None
            continue
        if c in "\"'":
            instr = c
        elif c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        elif c == "=" and depth == 0:
            prev = code[i - 1] if i > 0 else ""
            nxt = code[i + 1] if i + 1 < len(code) else ""
            if nxt != "=" and prev not in "=<>!+-*/%&|^~:":
                return i
    return -1


def _trivial_rhs(rhs: str) -> bool:
    return bool(_TRIVIAL_RHS_RE.match(rhs.strip()))


def _indent_of(s: str) -> str:
    return s[: len(s) - len(s.lstrip())]


def _first_word(stripped: str) -> str:
    return stripped.split(None, 1)[0].rstrip("(:") if stripped else ""


def _fade_line_l1(line: str) -> str:
    """Level-1 transform for a single line: blank a load-bearing assignment /
    return, otherwise leave the line untouched."""
    code, _comment = _split_comment(line)
    stripped = code.strip()
    if not stripped or stripped.startswith("#"):
        return line
    indent = _indent_of(code)
    first = _first_word(stripped)

    if first == "return":
        expr = stripped[len("return"):].strip()
        if expr and not _trivial_rhs(expr):
            return f"{indent}return {_placeholder('return the result')}"
        return line

    if first in _KEPT_KEYWORDS or stripped.startswith("@"):
        return line

    pos = _assign_pos(code)
    if pos != -1:
        lhs = code[:pos].rstrip()
        rhs = code[pos + 1:].strip()
        if rhs and not _trivial_rhs(rhs):
            target = lhs.strip()
            return f"{lhs} = {_placeholder(f'fill in {target}')}"
    return line


def _fade_l2(code: str) -> str:
    """Level-2 transform: a goal comment plus the surviving imports, ``def``/
    ``class`` signature stubs and ``assert`` lines — bodies removed."""
    lines = code.splitlines()

    goal = None
    for ln in lines:
        s = ln.strip()
        if s.startswith("#"):
            goal = s.lstrip("#").strip()
            break
    out = [f"# Goal (write it from scratch): {goal}" if goal
           else "# Goal: reproduce the worked example from scratch"]

    kept_any = False
    for ln in lines:
        code_part, _ = _split_comment(ln)
        s = code_part.strip()
        if not s:
            continue
        first = _first_word(s)
        indent = _indent_of(code_part)
        if first in ("import", "from") or s.startswith("@"):
            out.append(code_part.rstrip())
            kept_any = True
        elif first in ("def", "class"):
            out.append(code_part.rstrip())
            out.append(f"{indent}    {_placeholder('write the body from scratch')}")
            kept_any = True
        elif first == "assert":
            out.append(code_part.rstrip())
            kept_any = True
    if not kept_any:
        out.append(_placeholder("write the full solution from scratch"))
    return "\n".join(out)


def fade_code(code: str, level: int) -> str:
    """Fade ``code`` to ``level`` (0 = worked example, 1 = fill-in, 2 = from
    scratch). Deterministic. ``level <= 0`` returns ``code`` unchanged."""
    if level <= 0:
        return code
    if level == 1:
        return "\n".join(_fade_line_l1(ln) for ln in code.splitlines())
    return _fade_l2(code)


def fade_stages(code: str) -> list[str]:
    """Convenience: ``[level0, level1, level2]`` for the same worked ``code``."""
    return [fade_code(code, 0), fade_code(code, 1), fade_code(code, 2)]


def check_prompt(original: str, learner_attempt: str) -> str:
    """Build the prompt STRING for the AI-check step: it asks the AI to compare a
    learner's attempt to the worked solution and give ONE hint without revealing
    the full answer. Pure string builder — the app feeds this to the existing
    "Ask AI" prompt path; no new AI wiring."""
    return (
        "I'm learning by completing a faded worked example. Here is the worked "
        "solution:\n\n"
        f"```python\n{original}\n```\n\n"
        "Here is my attempt:\n\n"
        f"```python\n{learner_attempt}\n```\n\n"
        "Compare my attempt to the worked solution. Tell me whether it is correct. "
        "If it is not, give me ONE concrete hint about what to fix next — do NOT "
        "reveal the full solution or write the corrected code for me."
    )
