"""F5 — streaming + interruptible kernel execution.

Exercises the async execution path added to the kernel server (exec_start /
exec_poll / exec_stop), the in-memory backend's trivial synchronous stand-in, and
that the existing synchronous /exec path is unchanged. Kept fast and deterministic:
the interrupt test uses a pure-Python busy loop (interruptible at a bytecode
boundary) and joins the worker with a timeout so a runaway thread never hangs CI.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server import kernel_server
from sidekick.client import MockBackend


def _poll_until_done(dialog: str, run_id: str, timeout: float = 5.0) -> dict:
    """Poll a run to completion (or until `timeout`), returning the final snapshot."""
    deadline = time.monotonic() + timeout
    snap = kernel_server.exec_poll(dialog, run_id)
    while not snap["done"] and time.monotonic() < deadline:
        time.sleep(0.01)
        snap = kernel_server.exec_poll(dialog, run_id)
    return snap


def test_exec_start_poll_roundtrip():
    dialog = "f5/roundtrip"
    code = "import time\n[print(i) for i in range(3)]"
    run_id = kernel_server.exec_start(dialog, code)
    assert isinstance(run_id, str) and run_id

    snap = _poll_until_done(dialog, run_id)
    assert snap["done"] is True
    assert snap["error"] is None
    assert snap["interrupted"] is False
    for expected in ("0", "1", "2"):
        assert expected in snap["output"]
    # The trailing list-expression's repr is suppressed only by a ';'; here the
    # list comprehension is a statement-expression whose value ([None, None, None])
    # is reported. rich is a list (no plots here) — the poll shape is well-formed.
    assert isinstance(snap["rich"], list)


def test_exec_poll_before_done_streams_partial():
    # A run that prints then sleeps: an early poll should see partial output while
    # the run is still active (proves output streams incrementally, not all-at-end).
    dialog = "f5/partial"
    code = "import time\nprint('first')\ntime.sleep(0.3)\nprint('second')"
    run_id = kernel_server.exec_start(dialog, code)
    # Give the worker a moment to print 'first' but not reach 'second'.
    early = None
    for _ in range(50):
        early = kernel_server.exec_poll(dialog, run_id)
        if "first" in early["output"]:
            break
        time.sleep(0.005)
    assert early is not None and "first" in early["output"]
    final = _poll_until_done(dialog, run_id)
    assert "second" in final["output"]


def test_exec_stop():
    dialog = "f5/stop"
    # A pure-Python busy loop: interruptible at a bytecode boundary (no C-extension
    # blocking), so the async KeyboardInterrupt lands promptly.
    code = "x = 0\nwhile True:\n    x += 1"
    run_id = kernel_server.exec_start(dialog, code)

    # Let the loop actually start spinning, then interrupt it.
    time.sleep(0.1)
    res = kernel_server.exec_stop(dialog, run_id)
    assert res["ok"] is True

    snap = _poll_until_done(dialog, run_id, timeout=3.0)
    assert snap["done"] is True
    assert snap["interrupted"] is True
    assert snap["error"] == "KeyboardInterrupt"

    # The worker thread must actually die — join with a timeout so a failure surfaces
    # as an assertion, never a hung test.
    run = kernel_server.RUNS[dialog]
    if run.thread is not None:
        run.thread.join(timeout=2.0)
        assert run.thread.is_alive() is False


def test_exec_stop_unknown_run_is_noop():
    res = kernel_server.exec_stop("f5/nope", "does-not-exist")
    assert res["ok"] is False


def test_only_one_active_run_per_dialog():
    # While a run is active, exec_start returns the SAME run_id (the per-dialog lock
    # guarantees one worker at a time; we don't spawn a second, blocked one).
    dialog = "f5/single"
    code = "import time\ntime.sleep(0.3)"
    first = kernel_server.exec_start(dialog, code)
    second = kernel_server.exec_start(dialog, "print('other')")
    assert first == second
    _poll_until_done(dialog, first)


def test_mock_backend_exec_start():
    b = MockBackend()
    m = b.add("demo/welcome", "print('hello')", "code")
    run_id = b.exec_start("demo/welcome", m.id)
    assert isinstance(run_id, str) and run_id
    snap = b.exec_poll("demo/welcome", run_id)
    assert snap["done"] is True
    assert snap["error"] is None
    assert "mock" in snap["output"].lower()
    # The cell itself carries the same output the run produced (ran synchronously).
    assert m.output == snap["output"]
    assert b.exec_stop("demo/welcome", run_id)["ok"] is True


def test_sync_exec_still_works():
    # The original blocking path is unchanged: run_code returns (text, rich) with
    # stdout and a suppressed/last-expression repr, holding the dialog lock.
    dialog = "f5/sync"
    out, rich = kernel_server.run_code(dialog, "print('hi')\n1 + 1")
    assert "hi" in out
    assert "2" in out                       # last-expression repr reported
    assert rich == []

    # Trailing ';' suppresses the last-expression repr (Jupyter semantics preserved).
    out2, _ = kernel_server.run_code(dialog, "40 + 2;")
    assert "42" not in out2

    # A syntax error is surfaced as output text, not raised.
    out3, _ = kernel_server.run_code(dialog, "def (")
    assert "SyntaxError" in out3
