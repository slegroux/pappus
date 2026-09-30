"""Project a dialog into a retrieval-practice quiz — the *recall* view.

This is the fourth projection over the same cells, alongside the three in
`docs/design/graph-vs-tree.md` and `blog.py`:

    graph  — how dialogs connect (message links + build_context)   → learning
    tree   — how a library is laid out (#| export → nbdev tangle)   → packaging
    blog   — what a reader sees (blog.py)                           → communicating
    recall — what you can still derive from memory (this file)      → remembering

Where the blog path *shows* a finished dialog, the recall path *hides* it and
asks you to reconstruct it. Retrieval practice (the testing effect) is one of the
best-evidenced ways to make learning stick: being asked to re-derive an idea,
rather than re-reading it, is what moves it into durable memory. So this
projection turns a dialog into interview-style questions that test understanding
and derivation — not trivia — and (optionally) schedules when to ask them again.

Two pieces, both easy to test in isolation:

  1. Prompt + parse — `build_quiz_prompt` serializes the dialog (reusing
     `client.build_context`) into a prompt that asks the AI for N questions, and
     `parse_questions` turns the AI's reply back into a `list[str]`, tolerant of
     numbered lists, bullets, or a JSON array. The AI call itself lives in the
     app (it's a subprocess/network hop); this module stays pure so it needs no
     server and no network to test.

  2. Spaced repetition — `due_dialogs` / `record_review` are a tiny SM-2-style
     scheduler over a plain dict. They are deliberately pure: the "today" is
     passed in as a day number (never `datetime.now()` inside), so a review a
     week out is one function call away in a test. `load_schedule`/`save_schedule`
     persist the dict as `recall.json` beside the dialogs store — self-contained,
     no dependency on the backend's internals.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from .client import build_context


# ---- prompt + parse ---------------------------------------------------------
# The instruction wrapped around the serialized dialog. It steers the model
# toward *understanding/derivation* questions (why/how/derive) over *trivia*
# (what-was-the-variable-named), and pins a machine-parseable shape so
# `parse_questions` is simple and robust.
_QUIZ_INSTRUCTIONS = (
    "You are an examiner helping someone consolidate what they learned in the "
    "notebook dialog below. Write exactly {n} recall questions that test whether "
    "they truly understand it — questions that ask them to explain WHY something "
    "works, to DERIVE a result, to predict an outcome, or to justify a choice. "
    "Avoid trivia (exact variable names, verbatim wording); each question should "
    "make them reconstruct an idea from memory, with the notebook hidden.\n\n"
    "Return ONLY the questions as a numbered list, one per line, like:\n"
    "1. <question>\n2. <question>\n"
    "Do not include answers, preamble, or commentary."
)


def build_quiz_prompt(messages: list, n: int = 5) -> str:
    """Build the prompt asking the AI for `n` recall questions from a dialog.

    Reuses `client.build_context` to serialize the dialog's cells (code, output,
    notes, prior Q&A) the same way the live Ask-AI path does, then wraps them in
    the examiner instruction. Pure — returns the prompt string; the caller makes
    the actual AI call.
    """
    ctx = build_context(messages)
    return (f"{_QUIZ_INSTRUCTIONS.format(n=n)}\n\n"
            f"Here is the dialog so far (code, output, notes, and prior Q&A):\n\n"
            f"<dialog>\n{ctx}\n</dialog>\n")


# Leading list markers we strip off a question line: "1." / "1)" / "- " / "* " /
# "• " / "Q1." — anything a model might prefix a numbered or bulleted item with.
_MARKER_RE = re.compile(r"^\s*(?:Q?\d+\s*[.)\]:]|[-*•●▪])\s+", re.IGNORECASE)


def _clean(item: str) -> str:
    """Strip a leading list marker and surrounding quotes/whitespace from one item."""
    s = _MARKER_RE.sub("", (item or "").strip()).strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        s = s[1:-1].strip()
    return s


def parse_questions(ai_text: str) -> list[str]:
    """Parse the AI's reply into a list of question strings.

    Tolerant of the three shapes a model actually returns:
      - a JSON array (of strings, or of ``{"question": ...}`` objects),
      - a numbered list (``1.`` / ``1)`` / ``Q1.``),
      - a bulleted list (``-`` / ``*`` / ``•``), or plain lines as a last resort.
    """
    text = (ai_text or "").strip()
    if not text:
        return []

    # 1) JSON array — either the whole reply, or a [...] block embedded in prose.
    for candidate in _json_candidates(text):
        try:
            data = json.loads(candidate)
        except (ValueError, TypeError):
            continue
        if isinstance(data, list):
            out = []
            for x in data:
                if isinstance(x, dict):
                    x = x.get("question") or x.get("q") or ""
                s = _clean(str(x))
                if s:
                    out.append(s)
            if out:
                return out

    # 2) Numbered / bulleted list — keep only lines that carry a list marker.
    marked = [_clean(ln) for ln in text.splitlines() if _MARKER_RE.match(ln)]
    marked = [m for m in marked if m]
    if marked:
        return marked

    # 3) Fallback — every non-empty line is a question.
    return [_clean(ln) for ln in text.splitlines() if ln.strip()]


def _json_candidates(text: str) -> list[str]:
    """The whole reply, then the first bracketed ``[...]`` block if present."""
    cands = [text]
    m = re.search(r"\[.*\]", text, re.DOTALL)
    if m:
        cands.append(m.group(0))
    return cands


# ---- spaced repetition (pure) -----------------------------------------------
# A schedule maps a dialog name to its review record:
#   {"interval": <days>, "due": <day-number>, "last": <day-number>}
# The "day number" is any monotonic integer count of days — the caller supplies
# it (e.g. `datetime.date.today().toordinal()`), so these helpers never read the
# clock and stay trivially testable. `grade` is SM-2-style 0..5; >=3 is a pass.
_GOOD_GRADE = 3


def due_dialogs(schedule: dict, today_ordinal: int,
                dialogs: list | None = None) -> list[str]:
    """The dialogs due for review on `today_ordinal`.

    A dialog is due when its recorded ``due`` day has arrived (``due <= today``).
    If `dialogs` (the full set of dialog names) is given, any dialog with no
    record yet is also due — a never-reviewed dialog should be practised. Without
    `dialogs`, only scheduled dialogs are considered.
    """
    names = dialogs if dialogs is not None else list(schedule)
    due = []
    for name in names:
        rec = schedule.get(name)
        if rec is None or int(rec.get("due", 0)) <= today_ordinal:
            due.append(name)
    return due


def record_review(schedule: dict, dialog: str, today_ordinal: int,
                  grade: int) -> dict:
    """Return a new schedule with `dialog`'s review recorded on `today_ordinal`.

    A passing grade (``>= 3``) grows the interval (1 → 2 → 4 → …, doubling each
    good review); a failing grade resets it to 1 day so the dialog comes back
    tomorrow. The next ``due`` day is ``today + interval``. Pure: the input dict
    is not mutated.
    """
    updated = dict(schedule)
    prev_interval = int(updated.get(dialog, {}).get("interval", 0))
    if grade >= _GOOD_GRADE:
        interval = prev_interval * 2 if prev_interval else 1
    else:
        interval = 1
    updated[dialog] = {"interval": interval,
                       "due": today_ordinal + interval,
                       "last": today_ordinal}
    return updated


# ---- persistence (recall.json beside the dialogs store) ---------------------
def default_schedule_path() -> Path:
    """Where the review schedule lives: ``recall.json`` under the same data root
    as the dialogs (``PAPPUS_DATA``, else ``~/.config/pappus``).
    Mirrors `blog.default_blog_dir` — self-contained, no backend internals."""
    from .datadir import data_root
    return data_root() / "recall.json"


def load_schedule(path: str | Path | None = None) -> dict:
    """Load the review schedule, or an empty dict if none/unreadable."""
    p = Path(path) if path else default_schedule_path()
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text())
        return data if isinstance(data, dict) else {}
    except (ValueError, OSError):
        return {}


def save_schedule(schedule: dict, path: str | Path | None = None) -> None:
    """Persist the review schedule as JSON. Best-effort — a disk hiccup never
    raises (same posture as the dialogs store's own save)."""
    p = Path(path) if path else default_schedule_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(schedule, indent=2))
    except OSError:
        pass
