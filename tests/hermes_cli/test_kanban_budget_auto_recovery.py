from __future__ import annotations

import json

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


def test_budget_exhaustion_with_handoff_is_auto_recovered_once(tmp_path):
    conn = kbc.connect(tmp_path / "kanban.db")
    try:
        task_id = kb.create_task(conn, title="budgeted work", assignee="dsh-worker")
        kb._append_event(
            conn,
            task_id,
            "gave_up",
            {
                "trigger_outcome": "timed_out",
                "handoff_required": True,
                "recommended_action": "split-or-request-review",
                "budget_used": 100,
                "budget_max": 100,
            },
        )
        kb.add_comment(conn, task_id, "dsh-worker", "已完成部分实现，留下 handoff 和测试结果。")
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status='blocked', consecutive_failures=2, "
                "last_failure_error='Iteration budget exhausted' WHERE id=?",
                (task_id,),
            )

        recovered = kb.recover_budget_exhausted_tasks(conn)

        assert recovered == [task_id]
        task = kb.get_task(conn, task_id)
        assert task.status == "triage"
        assert task.consecutive_failures == 0
        assert task.block_kind is None
        assert any(e.kind == "budget_auto_recovered" for e in kb.list_events(conn, task_id))
    finally:
        conn.close()


def test_budget_exhaustion_without_handoff_stays_blocked(tmp_path):
    conn = kbc.connect(tmp_path / "kanban.db")
    try:
        task_id = kb.create_task(conn, title="no handoff", assignee="dsh-worker")
        kb._append_event(conn, task_id, "gave_up", {"trigger_outcome": "timed_out"})
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='blocked' WHERE id=?", (task_id,))

        assert kb.recover_budget_exhausted_tasks(conn) == []
        assert kb.get_task(conn, task_id).status == "blocked"
    finally:
        conn.close()


def test_budget_auto_recovery_is_idempotent(tmp_path):
    conn = kbc.connect(tmp_path / "kanban.db")
    try:
        task_id = kb.create_task(conn, title="one retry", assignee="dsh-worker")
        kb._append_event(
            conn,
            task_id,
            "gave_up",
            {"trigger_outcome": "timed_out", "handoff_required": True},
        )
        kb.add_comment(conn, task_id, "dsh-worker", "handoff")
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status='blocked', consecutive_failures=1 "
                "WHERE id=?",
                (task_id,),
            )

        assert kb.recover_budget_exhausted_tasks(conn) == [task_id]
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='blocked' WHERE id=?", (task_id,))
        assert kb.recover_budget_exhausted_tasks(conn) == []
        assert kb.get_task(conn, task_id).status == "blocked"
    finally:
        conn.close()
