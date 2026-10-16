from __future__ import annotations

from pathlib import Path

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


def _running(conn, tmp_path, declarations: list[str], *, kind="dir"):
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    body = "Touched files:\n" + "\n".join(f"- {item}" for item in declarations)
    task_id = kb.create_task(
        conn,
        title="scoped implementation",
        body=body,
        assignee="builder",
        workspace_kind=kind,
        workspace_path=str(workspace),
    )
    claimed = kb.claim_task(conn, task_id, claimer="builder:test")
    assert claimed is not None
    return task_id, claimed, workspace


def _request(conn, task_id, run_id, changed_files):
    metadata = {} if changed_files is None else {"changed_files": changed_files}
    return kb.request_review(
        conn,
        task_id,
        summary="ready",
        metadata=metadata,
        reviewer="reviewer",
        expected_run_id=run_id,
        with_reason=True,
    )


def test_declared_file_allows_same_normalized_path(conn, tmp_path):
    task_id, run, workspace = _running(conn, tmp_path, ["src/app.py"])
    ok, reason = _request(conn, task_id, run.current_run_id, ["src\\APP.py"])
    assert ok is True
    assert reason is None
    assert kb.get_task(conn, task_id).status == "review"


def test_declared_directory_allows_nested_file_in_worktree(conn, tmp_path):
    task_id, run, workspace = _running(conn, tmp_path, ["src/"], kind="worktree")
    ok, reason = _request(conn, task_id, run.current_run_id, ["src/pkg/feature.py"])
    assert ok is True
    assert reason is None


def test_absolute_declared_directory_works_without_git(conn, tmp_path):
    allowed = tmp_path / "plain" / "tools"
    allowed.mkdir(parents=True)
    changed = allowed / "nested" / "mod.py"
    task_id, run, _workspace = _running(conn, tmp_path, [str(allowed)])
    ok, reason = _request(conn, task_id, run.current_run_id, [str(changed)])
    assert ok is True
    assert reason is None


def test_out_of_scope_changed_file_is_rejected_and_audited(conn, tmp_path):
    task_id, run, _workspace = _running(conn, tmp_path, ["src/"])
    ok, reason = _request(conn, task_id, run.current_run_id, ["tests/test_app.py"])
    assert ok is False
    assert "outside Touched files" in reason
    assert kb.get_task(conn, task_id).status == "running"
    rejected = [e for e in kb.list_events(conn, task_id) if e.kind == "review_scope_rejected"]
    assert rejected[-1].payload["outside"] == ["tests/test_app.py"]


def test_declared_scope_requires_changed_files_attestation(conn, tmp_path):
    task_id, run, _workspace = _running(conn, tmp_path, ["src/"])
    ok, reason = _request(conn, task_id, run.current_run_id, None)
    assert ok is False
    assert reason == "Touched files declaration requires metadata.changed_files"
    assert kb.get_task(conn, task_id).status == "running"


@pytest.mark.parametrize(
    "metadata, expected_fragment",
    [
        ({"changed_files": [], "no_code_changes": True}, "no_code_changes_reason"),
        (
            {
                "changed_files": [],
                "no_code_changes": True,
                "no_code_changes_reason": "inspection found no implementation delta",
            },
            "verification evidence",
        ),
    ],
)
def test_noop_attestation_requires_reason_and_evidence(
    conn, tmp_path, metadata, expected_fragment
):
    task_id, run, _workspace = _running(conn, tmp_path, ["src/"])
    ok, reason = kb.request_review(
        conn, task_id, summary="no change required", metadata=metadata,
        reviewer="reviewer", expected_run_id=run.current_run_id, with_reason=True,
    )
    assert ok is False
    assert expected_fragment in reason
    assert kb.get_task(conn, task_id).status == "running"


def test_explicit_verified_noop_attestation_is_allowed(conn, tmp_path):
    task_id, run, _workspace = _running(conn, tmp_path, ["src/"])
    metadata = {
        "changed_files": [],
        "no_code_changes": True,
        "no_code_changes_reason": "implementation already satisfies the contract",
        "tests_run": [{"command": "pytest -q tests/test_contract.py", "exit_code": 0}],
    }
    ok, reason = kb.request_review(
        conn, task_id, summary="verified no-op", metadata=metadata,
        reviewer="reviewer", expected_run_id=run.current_run_id, with_reason=True,
    )
    assert ok is True
    assert reason is None
    assert kb.get_task(conn, task_id).status == "review"


def test_task_without_touched_files_remains_backward_compatible(conn, tmp_path):
    task_id = kb.create_task(conn, title="analysis only", assignee="builder")
    run = kb.claim_task(conn, task_id, claimer="builder:test")
    assert run is not None
    ok, reason = kb.request_review(
        conn,
        task_id,
        summary="ready",
        reviewer="reviewer",
        expected_run_id=run.current_run_id,
        with_reason=True,
    )
    assert ok is True
    assert reason is None
