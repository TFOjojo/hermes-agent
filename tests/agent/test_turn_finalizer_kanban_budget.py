from __future__ import annotations

import logging

from agent.turn_finalizer import _record_kanban_budget_exhausted
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


def test_iteration_budget_exhaustion_blocks_instead_of_requeue(tmp_path, monkeypatch):
    db_path = tmp_path / "kanban.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    conn = kbc.connect(db_path)
    try:
        task_id = kb.create_task(conn, title="long implementation", assignee="workhorse")
        claimed = kb.claim_task(conn, task_id, claimer="test-budget")
        assert claimed is not None

        _record_kanban_budget_exhausted(
            task_id,
            api_call_count=60,
            max_iterations=60,
            logger=logging.getLogger(__name__),
        )

        task = kb.get_task(conn, task_id)
        assert task is not None
        assert task.status == "blocked"
        assert task.claim_lock is None
        assert task.worker_pid is None
        assert task.consecutive_failures == 1

        run = kb.latest_run(conn, task_id)
        assert run is not None
        assert run.outcome == "gave_up"
        assert run.status == "gave_up"
        assert "Iteration budget exhausted" in (run.error or "")

        events = kb.list_events(conn, task_id)
        gave_up = [event for event in events if event.kind == "gave_up"]
        assert gave_up
        assert gave_up[-1].payload["trigger_outcome"] == "timed_out"
        assert gave_up[-1].payload["budget_used"] == 60
        assert gave_up[-1].payload["budget_max"] == 60
        assert gave_up[-1].payload["handoff_required"] is True
    finally:
        conn.close()
