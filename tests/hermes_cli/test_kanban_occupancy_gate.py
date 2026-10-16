from __future__ import annotations

from pathlib import Path

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / "src").mkdir()
    return repo


def _task(conn, title: str, path: Path, **kwargs) -> str:
    return kb.create_task(
        conn,
        title=title,
        assignee="workhorse",
        body=f"Implementation\n\nTouched files:\n- {path}\n",
        **kwargs,
    )


def test_same_repository_is_single_writer(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    repo = _repo(tmp_path)
    with kbc.connect() as conn:
        first = _task(conn, "first", repo / "src" / "a.py")
        second = _task(conn, "second", repo / "src" / "b.py")
        assert kb.claim_task(conn, first, claimer="writer-1") is not None
        assert kb.claim_task(conn, second, claimer="writer-2") is None
        events = kb.list_events(conn, second)
        blocked = [event for event in events if event.kind == "claim_rejected"]
        assert blocked
        assert blocked[-1].payload["reason"] == "occupancy_overlap"
        assert blocked[-1].payload["blocker_task_id"] == first


def test_dir_workspace_at_git_root_collides_with_repository_writer(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    repo = _repo(tmp_path)
    with kbc.connect() as conn:
        writer = _task(conn, "writer", repo / "src" / "a.py")
        dir_task = kb.create_task(
            conn,
            title="dir workspace task",
            assignee="workhorse",
            body="Implementation without a parseable touched-files section",
            workspace_kind="dir",
            workspace_path=str(repo),
        )
        assert kb.claim_task(conn, writer, claimer="writer") is not None
        assert kb.claim_task(conn, dir_task, claimer="dir-worker") is None
        rejected = [
            event for event in kb.list_events(conn, dir_task)
            if event.kind == "claim_rejected"
        ]
        assert rejected[-1].payload["overlap_keys"] == [
            f"repo:{kb._normalised_occupancy_path(repo)}"
        ]


def test_review_still_occupies_repository(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    repo = _repo(tmp_path)
    with kbc.connect() as conn:
        first = _task(conn, "reviewing", repo / "src" / "a.py")
        second = _task(conn, "waiting", repo / "src" / "b.py")
        claimed = kb.claim_task(conn, first, claimer="writer")
        assert claimed is not None
        assert kb.request_review(
            conn,
            first,
            summary="ready for review",
            metadata={
                "changed_files": [str(repo / "src" / "a.py")],
                "tests_run": [{"command": "pytest", "exit_code": 0}],
            },
            expected_run_id=claimed.current_run_id,
        )
        assert kb.claim_task(conn, second, claimer="writer-2") is None


def test_review_claim_waits_for_running_repository_writer(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    repo = _repo(tmp_path)
    with kbc.connect() as conn:
        writer = _task(conn, "writer", repo / "src" / "a.py")
        review = _task(conn, "review", repo / "src" / "b.py")
        assert kb.claim_task(conn, writer, claimer="writer") is not None
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status = 'review' WHERE id = ?", (review,))
        assert kb.claim_review_task(conn, review, claimer="reviewer") is None
        assert kb.get_task(conn, review).status == "review"
        rejected = [e for e in kb.list_events(conn, review) if e.kind == "claim_rejected"]
        assert rejected[-1].payload["reason"] == "occupancy_overlap"
        assert rejected[-1].payload["blocker_task_id"] == writer


def test_two_readonly_review_runs_do_not_deadlock_each_other(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    repo = _repo(tmp_path)
    with kbc.connect() as conn:
        first = _task(conn, "review one", repo / "src" / "a.py")
        second = _task(conn, "review two", repo / "src" / "b.py")
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status = 'review', assignee = 'qianhua' "
                "WHERE id IN (?, ?)",
                (first, second),
            )
        assert kb.claim_review_task(conn, first, claimer="reviewer-1") is not None
        assert kb.claim_review_task(conn, second, claimer="reviewer-2") is not None


def test_unknown_paths_do_not_deadlock(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    with kbc.connect() as conn:
        first = kb.create_task(conn, title="analysis one", assignee="workhorse")
        second = kb.create_task(conn, title="analysis two", assignee="workhorse")
        assert kb.claim_task(conn, first, claimer="one") is not None
        assert kb.claim_task(conn, second, claimer="two") is not None


def test_completion_releases_repository_occupancy(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    repo = _repo(tmp_path)
    with kbc.connect() as conn:
        first = _task(conn, "first", repo / "src" / "a.py")
        second = _task(conn, "second", repo / "src" / "b.py")
        assert kb.claim_task(conn, first, claimer="one") is not None
        assert kb.claim_task(conn, second, claimer="two") is None
        assert kb.complete_task(conn, first, summary="internal completion")
        assert kb.claim_task(conn, second, claimer="two") is not None


def test_distinct_worktrees_may_run_in_parallel(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    repo = _repo(tmp_path)
    wt1 = tmp_path / "wt-one"
    wt2 = tmp_path / "wt-two"
    wt1.mkdir()
    wt2.mkdir()
    with kbc.connect() as conn:
        # B1: a worktree task is exempt only for paths INSIDE its own checkout,
        # so both declarations point into their worktree to remain parallel
        # (declaring shared repo paths would now collide, by design).
        first = _task(
            conn,
            "wt one",
            wt1 / "src" / "a.py",
            workspace_kind="worktree",
            workspace_path=str(wt1),
        )
        second = _task(
            conn,
            "wt two",
            wt2 / "src" / "b.py",
            workspace_kind="worktree",
            workspace_path=str(wt2),
        )
        assert kb.claim_task(conn, first, claimer="one") is not None
        assert kb.claim_task(conn, second, claimer="two") is not None
