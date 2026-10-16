"""Occupancy gate must occupy the real destination of out-of-worktree declarations.

Regression guard for a real functional gap on this branch: ``_task_occupancy_keys``
returns early with only ``{worktree:<path>}`` for every ``workspace_kind='worktree'``
row, so a declaration resolving OUTSIDE the worktree (shared path in the host
checkout, ``..`` escape, junction/symlink to another directory) produces no
``repo:``/``path-root:`` key and therefore never collides with another writer.
A worktree could silently write shared paths in parallel with another task.

The fix exists upstream on other branches as ``_occupancy_keys_for_path`` /
``_worktree_declared_location`` but never landed here.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(cwd), "-c", "user.name=T", "-c", "user.email=t@e.com",
         "-c", "commit.gpgsign=false", *args],
        check=True, capture_output=True,
    )


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
    (repo / "src").mkdir()
    (repo / "src" / "main.py").write_text("base\n", encoding="utf-8")
    (repo / "shared").mkdir()
    (repo / "shared" / "common.txt").write_text("s\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "init")
    return repo


def _add_worktree(repo: Path, target: Path, branch: str) -> Path:
    _git(repo, "worktree", "add", str(target), "-b", branch, "HEAD")
    return target


def _make_dir_link(link: Path, target: Path) -> bool:
    if os.name == "nt":
        proc = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                              capture_output=True)
        if proc.returncode == 0 and link.exists():
            return True
    try:
        link.symlink_to(target, target_is_directory=True)
        return True
    except (OSError, NotImplementedError):
        return False


def _task(conn, title: str, declared: list[str], **kwargs) -> str:
    body = "Implementation\n\nTouched files:\n" + "".join(f"- {p}\n" for p in declared)
    return kb.create_task(conn, title=title, assignee="worker", body=body, **kwargs)


def _keys(conn, task_id: str) -> set[str]:
    row = conn.execute(
        "SELECT id, body, workspace_kind, workspace_path FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    return kb._task_occupancy_keys(row)


def test_helper_exists():
    assert callable(getattr(kb, "_occupancy_keys_for_path", None)), (
        "_occupancy_keys_for_path is missing — out-of-worktree occupancy resolution "
        "never landed on this branch"
    )
    assert callable(getattr(kb, "_worktree_declared_location", None)), (
        "_worktree_declared_location is missing"
    )


def test_worktree_declaration_inside_stays_exempt(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    repo = _make_repo(tmp_path)
    wt = _add_worktree(repo, tmp_path / "wt-inside", "wt/inside")
    with kbc.connect() as conn:
        tid = _task(conn, "inside", ["src/main.py"],
                    workspace_kind="worktree", workspace_path=str(wt))
        keys = _keys(conn, tid)
    assert f"worktree:{kb._normalised_occupancy_path(wt)}" in keys
    assert not any(k.startswith("repo:") for k in keys), keys


def test_worktree_declaration_outside_collides(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    repo = _make_repo(tmp_path)
    wt = _add_worktree(repo, tmp_path / "wt-out", "wt/out")
    with kbc.connect() as conn:
        tid = _task(conn, "outside", [str(repo / "shared" / "common.txt")],
                    workspace_kind="worktree", workspace_path=str(wt))
        keys = _keys(conn, tid)
    assert f"repo:{kb._normalised_occupancy_path(repo)}" in keys, keys


def test_worktree_junction_resolving_outside_collides(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    repo = _make_repo(tmp_path)
    wt = _add_worktree(repo, tmp_path / "wt-junction", "wt/junction")
    link = wt / "linked-shared"
    if not _make_dir_link(link, repo / "shared"):
        pytest.xfail("cannot create a directory junction/symlink on this machine")
    with kbc.connect() as conn:
        tid = _task(conn, "junction", ["linked-shared/common.txt"],
                    workspace_kind="worktree", workspace_path=str(wt))
        keys = _keys(conn, tid)
    assert f"worktree:{kb._normalised_occupancy_path(wt)}" in keys
    assert f"repo:{kb._normalised_occupancy_path(repo)}" in keys, keys


def test_ordinary_task_keys_unchanged(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    repo = _make_repo(tmp_path)
    with kbc.connect() as conn:
        tid = _task(conn, "ordinary", [str(repo / "src" / "main.py")])
        keys = _keys(conn, tid)
    assert keys == {f"repo:{kb._normalised_occupancy_path(repo)}"}


def test_pre_claim_gate_actually_blocks_outside_writer(tmp_path, monkeypatch):
    """End-to-end: the gate must REJECT the second claim, not just compute keys."""
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    repo = _make_repo(tmp_path)
    wt = _add_worktree(repo, tmp_path / "wt-e2e", "wt/e2e")
    with kbc.connect() as conn:
        first = _task(conn, "writer", [str(repo / "src" / "main.py")])
        outsider = _task(conn, "worktree reaching shared", [str(repo / "shared" / "common.txt")],
                         workspace_kind="worktree", workspace_path=str(wt))
        assert kb.claim_task(conn, first, claimer="writer-1") is not None
        assert kb.claim_task(conn, outsider, claimer="writer-2") is None, (
            "a worktree task declaring a shared host path must be blocked by occupancy"
        )
        events = kb.list_events(conn, outsider)
        blocked = [e for e in events if e.kind == "claim_rejected"]
        assert blocked and blocked[-1].payload["reason"] == "occupancy_overlap"
