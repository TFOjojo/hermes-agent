from __future__ import annotations

from agent.tool_guardrails import ToolGuardrailDecision
from agent.turn_finalizer import finalize_turn
from hermes_cli import kanban_db as kb
from tests.agent.test_turn_finalizer_final_response_persistence import FakeAgent


def _finalize(agent: FakeAgent, *, exit_reason: str, final_response: str):
    return finalize_turn(
        agent,
        final_response=final_response,
        api_call_count=3,
        interrupted=False,
        failed=False,
        messages=[
            {"role": "user", "content": "keep trying"},
            {"role": "assistant", "content": final_response},
        ],
        conversation_history=[],
        effective_task_id="task",
        turn_id="turn",
        user_message="keep trying",
        original_user_message="keep trying",
        _should_review_memory=False,
        _turn_exit_reason=exit_reason,
    )


def test_guardrail_halt_closes_and_blocks_active_kanban_run(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "kanban.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *_a, **_kw: [])

    conn = kb.connect(db_path)
    try:
        task_id = kb.create_task(
            conn, title="recover without looping", assignee="workhorse"
        )
        assert kb.claim_task(conn, task_id, claimer="guardrail-test") is not None
        monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)

        agent = FakeAgent()
        agent._tool_guardrail_halt_decision = ToolGuardrailDecision(
            action="halt",
            code="same_tool_failure_halt",
            message="same tool kept failing",
            tool_name="terminal",
            count=8,
        )
        halt_response = "I stopped retrying this failed tool call."

        result = _finalize(
            agent,
            exit_reason="guardrail_halt",
            final_response=halt_response,
        )

        assert result["final_response"] == halt_response
        assert result["turn_exit_reason"] == "guardrail_halt"

        task = kb.get_task(conn, task_id)
        assert task is not None
        assert task.status == "blocked"
        assert task.current_run_id is None
        assert task.claim_lock is None
        assert task.worker_pid is None
        assert task.consecutive_failures == 1

        run = kb.latest_run(conn, task_id)
        assert run is not None
        assert run.status == "gave_up"
        assert run.outcome == "gave_up"
        assert run.ended_at is not None
        assert run.error == (
            "Tool guardrail halted terminal: same_tool_failure_halt (count=8)"
        )

        gave_up = [
            event for event in kb.list_events(conn, task_id)
            if event.kind == "gave_up"
        ]
        assert len(gave_up) == 1
        assert gave_up[0].run_id == run.id
        assert gave_up[0].payload is not None
        assert gave_up[0].payload["failure_class"] == "tool_loop_halt"
        assert gave_up[0].payload["reason_code"] == "same_tool_failure_halt"
        assert gave_up[0].payload["tool_name"] == "terminal"
        assert gave_up[0].payload["count"] == 8
        assert gave_up[0].payload["handoff_required"] is True
        assert gave_up[0].payload["recommended_action"] == (
            "change-strategy-or-request-review"
        )
    finally:
        conn.close()


def test_guardrail_halt_is_idempotent_after_run_is_closed(tmp_path, monkeypatch):
    db_path = tmp_path / "kanban.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *_a, **_kw: [])

    conn = kb.connect(db_path)
    try:
        task_id = kb.create_task(
            conn, title="close guardrail run once", assignee="workhorse"
        )
        assert kb.claim_task(conn, task_id, claimer="guardrail-test") is not None
        monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)

        agent = FakeAgent()
        agent._tool_guardrail_halt_decision = ToolGuardrailDecision(
            action="halt",
            code="same_tool_failure_halt",
            tool_name="terminal",
            count=8,
        )

        _finalize(
            agent,
            exit_reason="guardrail_halt",
            final_response="I stopped retrying this failed tool call.",
        )
        first_task = kb.get_task(conn, task_id)
        first_events = kb.list_events(conn, task_id)

        _finalize(
            agent,
            exit_reason="guardrail_halt",
            final_response="I stopped retrying this failed tool call.",
        )

        task = kb.get_task(conn, task_id)
        assert task is not None
        assert first_task is not None
        assert task.consecutive_failures == first_task.consecutive_failures == 1
        assert kb.list_events(conn, task_id) == first_events
        assert len(kb.list_runs(conn, task_id)) == 1
    finally:
        conn.close()


def test_guardrail_halt_does_nothing_for_non_kanban_turn(monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *_a, **_kw: [])

    agent = FakeAgent()
    agent._tool_guardrail_halt_decision = ToolGuardrailDecision(
        action="halt",
        code="same_tool_failure_halt",
        tool_name="terminal",
        count=3,
    )

    result = _finalize(
        agent,
        exit_reason="guardrail_halt",
        final_response="halt message",
    )

    assert result["final_response"] == "halt message"
    assert result["turn_exit_reason"] == "guardrail_halt"
