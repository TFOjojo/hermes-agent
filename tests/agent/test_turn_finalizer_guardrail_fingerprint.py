"""Phase 2 tests: structured guardrail-halt metadata and failure fingerprint.

Extends the Phase 1 guardrail halt tests to verify:
* failure_fingerprint on both the event payload AND the closed run metadata.
* Determinism: same tool+reason_code => same fingerprint.
* exit_class on both surfaces.
* Budget-exhausted metadata remains stable (no fingerprint, no safety mixing).
"""

from __future__ import annotations

import hashlib
import logging

from agent.tool_guardrails import ToolGuardrailDecision
from agent.turn_finalizer import _failure_fingerprint
from hermes_cli import kanban_db as kb
from tests.agent.test_turn_finalizer_final_response_persistence import FakeAgent

import pytest


# ---------------------------------------------------------------------------
# Fingerprint determinism
# ---------------------------------------------------------------------------


def test_failure_fingerprint_is_deterministic():
    """Same tool+reason => same fingerprint, always."""
    fp1 = _failure_fingerprint("terminal", "same_tool_failure_halt")
    fp2 = _failure_fingerprint("terminal", "same_tool_failure_halt")
    assert fp1 == fp2
    assert len(fp1) == 16  # 64 hex chars truncated to 16
    # Verify it's a valid hex string
    int(fp1, 16)


def test_failure_fingerprint_differs_by_tool():
    fp_terminal = _failure_fingerprint("terminal", "same_tool_failure_halt")
    fp_browser = _failure_fingerprint("browser_navigate", "same_tool_failure_halt")
    assert fp_terminal != fp_browser


def test_failure_fingerprint_differs_by_reason():
    fp_a = _failure_fingerprint("terminal", "same_tool_failure_halt")
    fp_b = _failure_fingerprint("terminal", "different_reason")
    assert fp_a != fp_b


def test_failure_fingerprint_matches_manual_sha256():
    expected = hashlib.sha256(b"terminal:same_tool_failure_halt").hexdigest()[:16]
    assert _failure_fingerprint("terminal", "same_tool_failure_halt") == expected


# ---------------------------------------------------------------------------
# Run metadata shape — fingerprint appears on the closed run
# ---------------------------------------------------------------------------


def test_guardrail_halt_run_metadata_contains_fingerprint(
    tmp_path, monkeypatch,
):
    """The closed run's metadata must include exit_class and failure_fingerprint."""
    db_path = tmp_path / "kanban.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *_a, **_kw: [])

    conn = kb.connect(db_path)
    try:
        task_id = kb.create_task(
            conn, title="fingerprint test", assignee="workhorse"
        )
        assert kb.claim_task(conn, task_id, claimer="fp-test") is not None
        monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)

        agent = FakeAgent()
        agent._tool_guardrail_halt_decision = ToolGuardrailDecision(
            action="halt",
            code="same_tool_failure_halt",
            message="same tool kept failing",
            tool_name="terminal",
            count=5,
        )
        from agent.turn_finalizer import finalize_turn
        finalize_turn(
            agent,
            final_response="halted",
            api_call_count=3,
            interrupted=False,
            failed=False,
            messages=[
                {"role": "user", "content": "go"},
                {"role": "assistant", "content": "halted"},
            ],
            conversation_history=[],
            effective_task_id=task_id,
            turn_id="t1",
            user_message="go",
            original_user_message="go",
            _should_review_memory=False,
            _turn_exit_reason="guardrail_halt",
        )

        run = kb.latest_run(conn, task_id)
        assert run is not None
        assert run.metadata is not None
        assert run.metadata["exit_class"] == "tool_loop_halt"
        assert run.metadata["failure_fingerprint"] == _failure_fingerprint(
            "terminal", "same_tool_failure_halt",
        )
        assert run.metadata["tool_name"] == "terminal"
        assert run.metadata["reason_code"] == "same_tool_failure_halt"
        assert run.metadata["count"] == 5
    finally:
        conn.close()


def test_guardrail_halt_event_payload_contains_fingerprint(
    tmp_path, monkeypatch,
):
    """The gave_up event payload must include exit_class and failure_fingerprint."""
    db_path = tmp_path / "kanban.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *_a, **_kw: [])

    conn = kb.connect(db_path)
    try:
        task_id = kb.create_task(
            conn, title="event fp test", assignee="workhorse"
        )
        assert kb.claim_task(conn, task_id, claimer="fp-test") is not None
        monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)

        agent = FakeAgent()
        agent._tool_guardrail_halt_decision = ToolGuardrailDecision(
            action="halt",
            code="max_calls_reached",
            message="too many calls",
            tool_name="browser_click",
            count=12,
        )
        from agent.turn_finalizer import finalize_turn
        finalize_turn(
            agent,
            final_response="stopped",
            api_call_count=4,
            interrupted=False,
            failed=False,
            messages=[
                {"role": "user", "content": "go"},
                {"role": "assistant", "content": "stopped"},
            ],
            conversation_history=[],
            effective_task_id=task_id,
            turn_id="t2",
            user_message="go",
            original_user_message="go",
            _should_review_memory=False,
            _turn_exit_reason="guardrail_halt",
        )

        events = kb.list_events(conn, task_id)
        gave_up = [e for e in events if e.kind == "gave_up"]
        assert len(gave_up) == 1
        p = gave_up[0].payload
        assert p["exit_class"] == "tool_loop_halt"
        assert p["failure_class"] == "tool_loop_halt"
        assert p["failure_fingerprint"] == _failure_fingerprint(
            "browser_click", "max_calls_reached",
        )
        assert p["tool_name"] == "browser_click"
        assert p["reason_code"] == "max_calls_reached"
        assert p["count"] == 12
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Budget-exhausted metadata remains stable (no fingerprint, no safety mixing)
# ---------------------------------------------------------------------------


def test_budget_exhausted_metadata_no_fingerprint(
    tmp_path, monkeypatch,
):
    """Budget-exhausted runs must NOT carry failure_fingerprint or exit_class
    — those are safety/self-review semantics, not transient provider failure."""
    db_path = tmp_path / "kanban.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))

    conn = kb.connect(db_path)
    try:
        from agent.turn_finalizer import _record_kanban_budget_exhausted
        task_id = kb.create_task(
            conn, title="budget test", assignee="worker"
        )
        assert kb.claim_task(conn, task_id, claimer="budget-test") is not None

        _record_kanban_budget_exhausted(
            task_id,
            api_call_count=60,
            max_iterations=60,
            logger=logging.getLogger(__name__),
        )

        run = kb.latest_run(conn, task_id)
        assert run is not None
        assert run.metadata is not None
        # Budget-exhausted metadata should NOT have safety semantics
        assert "failure_fingerprint" not in run.metadata
        assert "exit_class" not in run.metadata
        assert "reason_code" not in run.metadata
        # Budget-exhausted metadata should have stable structure
        assert run.metadata["trigger_outcome"] == "timed_out"
    finally:
        conn.close()
