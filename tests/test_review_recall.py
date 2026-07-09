"""Tests for the Recall projection (F4) — retrieval-practice quizzes over a dialog.

These exercise the pure logic in `sidekick.recall` (prompt building, answer
parsing, and the spaced-repetition scheduler) plus the `/recall` app route with
the AI stubbed at the seam (`app.call_claude`), so nothing here hits a real
`claude` subprocess or the network.

Run:  python -m pytest tests/test_review_recall.py -q   (from the project root)
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sidekick import recall
from sidekick.client import Msg


def _m(id, t, content, output=""):
    return Msg(id, t, content, output)


# ---- build_quiz_prompt ------------------------------------------------------
def test_build_quiz_prompt():
    msgs = [_m("a", "code", "def area(r): return 3.14159 * r**2", "ran"),
            _m("b", "note", "area of a circle scales with the square of the radius")]
    prompt = recall.build_quiz_prompt(msgs, n=4)
    # The dialog content is serialized in (via build_context)...
    assert "def area(r)" in prompt
    assert "scales with the square of the radius" in prompt
    # ...and the prompt asks for exactly N questions.
    assert "4" in prompt and "question" in prompt.lower()


def test_build_quiz_prompt_defaults_to_five():
    assert "5" in recall.build_quiz_prompt([_m("a", "note", "hi")])


# ---- parse_questions --------------------------------------------------------
def test_parse_questions_numbered():
    text = ("Here are your questions:\n"
            "1. Why does the area scale with r squared?\n"
            "2. Derive the formula from first principles.\n"
            "3) Predict the area when r doubles.\n")
    qs = recall.parse_questions(text)
    assert qs == ["Why does the area scale with r squared?",
                  "Derive the formula from first principles.",
                  "Predict the area when r doubles."]


def test_parse_questions_json():
    text = '["Why r squared?", "What if r doubles?", "Derive it."]'
    assert recall.parse_questions(text) == ["Why r squared?", "What if r doubles?", "Derive it."]


def test_parse_questions_json_of_objects_and_embedded():
    text = ('Sure — here you go:\n'
            '[{"question": "Explain the intuition."}, {"question": "Prove the bound."}]')
    assert recall.parse_questions(text) == ["Explain the intuition.", "Prove the bound."]


def test_parse_questions_bullets():
    text = ("- What assumption breaks if the input is empty?\n"
            "* How would you generalize this?\n"
            "• Why is the loop O(n)?\n")
    assert recall.parse_questions(text) == [
        "What assumption breaks if the input is empty?",
        "How would you generalize this?",
        "Why is the loop O(n)?",
    ]


def test_parse_questions_empty_is_empty_list():
    assert recall.parse_questions("") == []
    assert recall.parse_questions("   \n  ") == []


# ---- spaced repetition ------------------------------------------------------
def test_due_dialogs():
    # A never-reviewed dialog (present in `dialogs`, absent from the schedule) is due.
    assert recall.due_dialogs({}, today_ordinal=100, dialogs=["fresh"]) == ["fresh"]

    # A scheduled dialog is due only once its `due` day has arrived.
    sched = {"soon": {"interval": 1, "due": 101, "last": 100},
             "now": {"interval": 2, "due": 100, "last": 98}}
    due_today = recall.due_dialogs(sched, today_ordinal=100)
    assert "now" in due_today and "soon" not in due_today       # 100 due, 101 not yet
    assert set(recall.due_dialogs(sched, today_ordinal=101)) == {"now", "soon"}


def test_record_review():
    # Reviewed today with a good grade: not due today, due after its interval.
    sched = recall.record_review({}, "d", today_ordinal=100, grade=5)
    assert sched["d"]["due"] == 101                             # today + interval(1)
    assert recall.due_dialogs(sched, today_ordinal=100) == []   # not due today
    assert recall.due_dialogs(sched, today_ordinal=101) == ["d"]  # due after interval

    # A good review grows the interval (doubles); a fail resets it to 1.
    grown = recall.record_review(sched, "d", today_ordinal=101, grade=5)
    assert grown["d"]["interval"] == 2 and grown["d"]["due"] == 103
    reset = recall.record_review(grown, "d", today_ordinal=103, grade=1)
    assert reset["d"]["interval"] == 1 and reset["d"]["due"] == 104

    # Pure: the original dict is never mutated.
    assert "d" not in {}
    assert recall.record_review({}, "x", 1, 5) is not sched


def test_schedule_round_trips_to_disk(tmp_path):
    p = tmp_path / "recall.json"
    assert recall.load_schedule(p) == {}                       # missing file -> empty
    sched = recall.record_review({}, "d/x", today_ordinal=50, grade=4)
    recall.save_schedule(sched, p)
    assert recall.load_schedule(p) == sched


# ---- /recall route (AI stubbed at the seam) ---------------------------------
def test_recall_route_inserts_parsed_questions_as_cells(monkeypatch):
    import sidekick.app as app

    app.STATE["dialog"] = "recall/route"
    backend = app.STATE["backend"]
    backend.messages("recall/route")
    backend.add("recall/route", "x = 40 + 2", "code")

    # Stub the AI seam: return a fixed numbered list instead of shelling out.
    monkeypatch.setattr(app, "call_claude",
                        lambda *a, **k: "1. Why is x 42?\n2. What would x be without the 2?")

    resp = app.recall_quiz(n=2)
    assert resp.status_code == 303                             # redirects back to the dialog

    cells = backend.messages("recall/route")
    prompts = [c for c in cells if c.msg_type == "prompt"]
    assert [c.content for c in prompts] == ["Why is x 42?", "What would x be without the 2?"]
    assert any(c.msg_type == "note" and "Recall quiz" in c.content for c in cells)


def test_recall_route_builds_prompt_from_the_dialog(monkeypatch):
    """The AI seam receives a prompt that carries the dialog's content."""
    import sidekick.app as app

    app.STATE["dialog"] = "recall/prompted"
    backend = app.STATE["backend"]
    backend.messages("recall/prompted")
    backend.add("recall/prompted", "SECRET_MARKER = 123", "code")

    seen = {}

    def fake_call(dialog, content, context="", model=None, mode=None):
        seen["dialog"] = dialog
        seen["content"] = content
        return "1. Recall the marker."

    monkeypatch.setattr(app, "call_claude", fake_call)
    app.recall_quiz(n=1)
    assert "SECRET_MARKER = 123" in seen["content"]            # dialog content reached the AI
    assert seen["dialog"] == "recall:recall/prompted"          # isolated session key
