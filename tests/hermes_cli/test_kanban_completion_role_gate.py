from __future__ import annotations

import json

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def conn(tmp_path):
    db = kbc.connect(tmp_path / "kanban.db")
    try:
        yield db
    finally:
        db.close()


@pytest.mark.parametrize("actor", ["workhorse", "dsh-worker", "qianhua"])
def test_governed_worker_profiles_cannot_complete_their_own_task(conn, actor):
    task_id = kb.create_task(conn, title="implementation", assignee=actor)
    claimed = kb.claim_task(conn, task_id, claimer=f"{actor}:test")
    assert claimed is not None

    with pytest.raises(kb.CompletionRoleError):
        kb.complete_task(
            conn,
            task_id,
            summary="self-approved",
            actor_profile=actor,
            expected_run_id=claimed.current_run_id,
        )

    assert kb.get_task(conn, task_id).status == "running"
    events = kb.list_events(conn, task_id)
    blocked = [event for event in events if event.kind == "completion_blocked_role"]
    assert blocked[-1].payload["actor_profile"] == actor
    assert blocked[-1].payload["required_action"] == "request-review"


def test_default_can_only_complete_explicit_final_acceptance(conn):
    implementation_id = kb.create_task(
        conn, title="implementation owned by default", assignee="default"
    )
    with pytest.raises(kb.CompletionRoleError):
        kb.complete_task(conn, implementation_id, actor_profile="default")
    assert kb.get_task(conn, implementation_id).status == "ready"

    gate_id = kb.create_task(conn, title="总验收：feature", assignee="default")
    assert kb.complete_task(
        conn,
        gate_id,
        actor_profile="default",
        metadata={"acceptance_status": "passed"},
    )
    assert kb.get_task(conn, gate_id).status == "done"


def _root_with_completed_final_acceptance(conn, *, acceptance_status: str | None):
    root_id = kb.create_task(
        conn, title="Orchestration root", assignee="default"
    )
    gate_id = kb.create_task(
        conn, title="总验收：orchestration root", assignee="default"
    )
    kb.link_tasks(conn, gate_id, root_id)
    assert kb.complete_task(
        conn,
        gate_id,
        actor_profile="default",
        metadata={"acceptance_status": "passed"},
    )
    if acceptance_status != "passed":
        run = kb.latest_run(conn, gate_id)
        assert run is not None
        metadata = (
            {"acceptance_status": acceptance_status}
            if acceptance_status is not None
            else {}
        )
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE task_runs SET metadata = ? WHERE id = ?",
                (json.dumps(metadata), run.id),
            )
    assert kb.get_task(conn, root_id).status == "ready"
    return root_id


def test_default_can_complete_root_after_passed_direct_final_acceptance(conn):
    root_id = _root_with_completed_final_acceptance(
        conn, acceptance_status="passed"
    )

    assert kb.complete_task(conn, root_id, actor_profile="default")
    assert kb.get_task(conn, root_id).status == "done"


@pytest.mark.parametrize("acceptance_status", [None, "failed"])
def test_default_cannot_complete_root_without_passed_direct_final_acceptance(
    conn, acceptance_status
):
    root_id = _root_with_completed_final_acceptance(
        conn, acceptance_status=acceptance_status
    )

    with pytest.raises(
        kb.CompletionRoleError, match="direct final-acceptance prerequisite"
    ):
        kb.complete_task(conn, root_id, actor_profile="default")
    assert kb.get_task(conn, root_id).status == "ready"


def test_internal_lifecycle_completion_remains_compatible(conn):
    task_id = kb.create_task(conn, title="internal lifecycle", assignee="default")
    assert kb.complete_task(conn, task_id, result="internal lifecycle evidence")
    assert kb.get_task(conn, task_id).status == "done"


def _handoff(conn, *, implementer: str, reviewer: str | None = None) -> str:
    task_id = kb.create_task(conn, title=f"{implementer} implementation", assignee=implementer)
    claimed = kb.claim_task(conn, task_id, claimer=f"{implementer}:test")
    assert claimed is not None
    metadata = {
        "changed_files": ["src/feature.py"],
        "tests_run": [{"command": "pytest", "exit_code": 0}],
    }
    assert kb.request_review(
        conn,
        task_id,
        summary="implementation verified",
        metadata=metadata,
        reviewer=reviewer,
        expected_run_id=claimed.current_run_id,
    )
    return task_id


def test_workhorse_review_defaults_to_qianhua_and_qianhua_can_approve(conn):
    task_id = _handoff(conn, implementer="workhorse")
    review_task = kb.get_task(conn, task_id)
    assert review_task.status == "review"
    assert review_task.assignee == "qianhua"
    claimed_review = kb.claim_review_task(conn, task_id, claimer="qianhua:review")
    assert claimed_review is not None
    assert claimed_review.task_role == "review"

    assert kb.complete_task(
        conn,
        task_id,
        actor_profile="qianhua",
        summary="independent review passed",
        expected_run_id=claimed_review.current_run_id,
    )
    assert kb.get_task(conn, task_id).status == "done"


def test_dsh_worker_review_defaults_to_qianhua_and_qianhua_can_approve(conn):
    task_id = _handoff(conn, implementer="dsh-worker")
    review_task = kb.get_task(conn, task_id)
    assert review_task.status == "review"
    assert review_task.assignee == "qianhua"
    claimed_review = kb.claim_review_task(conn, task_id, claimer="qianhua:dsh-worker-review")
    assert claimed_review is not None
    assert claimed_review.task_role == "review"

    assert kb.complete_task(
        conn,
        task_id,
        actor_profile="qianhua",
        summary="independent dsh-worker review passed",
        expected_run_id=claimed_review.current_run_id,
    )
    assert kb.get_task(conn, task_id).status == "done"


def test_default_review_defaults_to_qianhua_and_qianhua_can_approve(conn):
    task_id = _handoff(conn, implementer="default")
    review_task = kb.get_task(conn, task_id)
    assert review_task.status == "review"
    assert review_task.assignee == "qianhua"

    claimed_review = kb.claim_review_task(
        conn, task_id, claimer="qianhua:default-review"
    )
    assert claimed_review is not None
    assert claimed_review.task_role == "review"
    assert kb.complete_task(
        conn,
        task_id,
        actor_profile="qianhua",
        summary="independent review of default implementation passed",
        expected_run_id=claimed_review.current_run_id,
    )
    assert kb.get_task(conn, task_id).status == "done"


def test_final_acceptance_cannot_be_approved_by_qianhua_via_default_review_route(conn):
    """总验收卡禁止走 default→qianhua 审核路由：qianhua 不得 complete FA 卡。"""
    gate_id = kb.create_task(
        conn, title="总验收：feature gate", assignee="default"
    )
    claimed = kb.claim_task(conn, gate_id, claimer="default:test")
    assert claimed is not None
    metadata = {
        "changed_files": ["src/gate.py"],
        "tests_run": [{"command": "pytest", "exit_code": 0}],
    }
    assert kb.request_review(
        conn,
        gate_id,
        summary="FA gate handoff",
        metadata=metadata,
        expected_run_id=claimed.current_run_id,
    )
    review_task = kb.get_task(conn, gate_id)
    assert review_task.status == "review"
    assert review_task.assignee == "qianhua"

    claimed_review = kb.claim_review_task(
        conn, gate_id, claimer="qianhua:fa-review"
    )
    assert claimed_review is not None
    assert claimed_review.task_role == "review"

    # qianhua should NOT be able to complete a final-acceptance card
    with pytest.raises(kb.CompletionRoleError):
        kb.complete_task(
            conn,
            gate_id,
            actor_profile="qianhua",
            summary="qianhua attempts to close FA",
            expected_run_id=claimed_review.current_run_id,
        )
    # The card stays claimed (running) — not done, and not returned to review.
    assert kb.get_task(conn, gate_id).status == "running"


def test_qianhua_review_defaults_to_default_and_default_can_approve(conn):
    task_id = _handoff(conn, implementer="qianhua")
    review_task = kb.get_task(conn, task_id)
    assert review_task.status == "review"
    assert review_task.assignee == "default"

    assert kb.complete_task(
        conn,
        task_id,
        actor_profile="default",
        summary="controller review passed",
    )
    assert kb.get_task(conn, task_id).status == "done"


def test_governed_reviewer_cannot_approve_review_assigned_to_someone_else(conn):
    task_id = _handoff(conn, implementer="workhorse", reviewer="default")
    with pytest.raises(kb.CompletionRoleError):
        kb.complete_task(
            conn,
            task_id,
            actor_profile="qianhua",
            summary="not my review",
        )
    assert kb.get_task(conn, task_id).status == "review"


def test_governed_profile_cannot_nominate_itself_as_reviewer(conn):
    task_id = kb.create_task(conn, title="self review", assignee="qianhua")
    claimed = kb.claim_task(conn, task_id, claimer="qianhua:test")
    assert claimed is not None
    assert not kb.request_review(
        conn,
        task_id,
        summary="self review",
        reviewer="qianhua",
        expected_run_id=claimed.current_run_id,
    )
    assert kb.get_task(conn, task_id).status == "running"


def test_legacy_review_without_reviewer_is_repaired_before_claim(conn):
    import json

    task_id = _handoff(conn, implementer="workhorse")
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET assignee = 'workhorse' WHERE id = ?", (task_id,))
        row = conn.execute(
            "SELECT id, payload FROM task_events WHERE task_id = ? "
            "AND kind = 'review_requested' ORDER BY id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        payload = json.loads(row["payload"])
        payload["reviewer"] = None
        conn.execute(
            "UPDATE task_events SET payload = ? WHERE id = ?",
            (json.dumps(payload), int(row["id"])),
        )
    claimed = kb.claim_review_task(conn, task_id, claimer="gateway:test")
    assert claimed is not None
    assert claimed.assignee == "qianhua"
    repaired = [
        e for e in kb.list_events(conn, task_id)
        if e.kind == "review_route_repaired"
    ]
    assert repaired[-1].payload["implementer"] == "workhorse"
    assert repaired[-1].payload["reviewer"] == "qianhua"


def test_request_changes_rejects_self_reviewer_even_for_legacy_run(conn):
    task_id = _handoff(conn, implementer="workhorse")
    claimed = kb.claim_review_task(conn, task_id, claimer="gateway:test")
    assert claimed is not None
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET assignee = 'workhorse' WHERE id = ?", (task_id,))
    ok, reason = kb.request_changes(
        conn,
        task_id,
        reason="self review must not land",
        expected_run_id=claimed.current_run_id,
    )
    assert not ok
    assert "self-review" in reason
    assert kb.get_task(conn, task_id).status == "running"
