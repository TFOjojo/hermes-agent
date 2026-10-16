"""B1: a worktree task is exempt only for paths that resolve INSIDE its worktree.

``_task_occupancy_keys`` used to return ``{worktree:<path>}`` early for every
``workspace_kind='worktree'`` row, so a declaration pointing outside the
worktree — a shared path in the host repository, a ``..`` escape, or a
junction/symlink into another directory — produced no ``repo:``/``path-root:``
key and therefore never collided with another writer.

These tests pin the boundary:
- declarations resolving inside the worktree stay exempt (worktrees keep their
  parallel-isolation promise);
- declarations resolving outside the worktree add exactly the keys an ordinary
  task would get, so the pre-claim occupancy gate blocks or waits.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        [
            "git", "-C", str(cwd),
            "-c", "user.name=Test User",
            "-c", "user.email=test@example.com",
            "-c", "commit.gpgsign=false",
            *args,
        ],
        check=True, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    return proc.stdout


def _make_repo(tmp_path: Path) -> Path:
    """A real Git repo with an ``src/`` and a ``shared/`` directory."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(
        ["git", "init", "-b", "main", str(repo)], check=True, capture_output=True,
    )
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    (repo / "src").mkdir()
    (repo / "src" / "main.py").write_text("print('base')\n", encoding="utf-8")
    (repo / "shared").mkdir()
    (repo / "shared" / "common.txt").write_text("shared\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "init")
    return repo


def _add_worktree(repo: Path, target: Path, branch: str) -> Path:
    _git(repo, "worktree", "add", str(target), "-b", branch, "HEAD")
    return target


def _task(
    conn,
    title: str,
    declared: list[str],
    *,
    workspace_kind: str | None = None,
    workspace_path: Path | None = None,
) -> str:
    body = "Implementation\n\nTouched files:\n" + "".join(f"- {p}\n" for p in declared)
    kwargs: dict = {}
    if workspace_kind is not None:
        kwargs["workspace_kind"] = workspace_kind
    if workspace_path is not None:
        kwargs["workspace_path"] = str(workspace_path)
    return kb.create_task(conn, title=title, assignee="worker", body=body, **kwargs)


def _keys(conn, task_id: str) -> set[str]:
    """Occupancy keys exactly as ``_claim_overlap_blocker`` computes them."""
    row = conn.execute(
        "SELECT id, body, workspace_kind, workspace_path FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    return kb._task_occupancy_keys(row)


def _make_dir_link(link: Path, target: Path) -> bool:
    """Directory junction on Windows, falling back to a symlink; False if neither."""
    if os.name == "nt":
        proc = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
        )
        if proc.returncode == 0 and link.exists():
            return True
    try:
        link.symlink_to(target, target_is_directory=True)
        return True
    except (OSError, NotImplementedError):
        return False


# ---------------------------------------------------------------------------
# occupancy keys
# ---------------------------------------------------------------------------

def test_external_declaration_adds_repo_key(tmp_path, monkeypatch):
    """(a) absolute + '..'-escaped declarations outside the worktree -> repo key."""
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    repo = _make_repo(tmp_path)
    wt = _add_worktree(repo, tmp_path / "wt-external", "wt/external")
    with kbc.connect() as conn:
        tid = _task(
            conn,
            "worktree task touching shared files",
            [
                repo / "shared" / "common.txt",          # absolute, host checkout
                "../repo/shared/common.txt",             # relative escape
            ],
            workspace_kind="worktree",
            workspace_path=wt,
        )
        keys = _keys(conn, tid)
    assert f"worktree:{kb._normalised_occupancy_path(wt)}" in keys
    assert f"repo:{kb._normalised_occupancy_path(repo)}" in keys


def test_inside_only_declaration_keeps_worktree_exemption(tmp_path, monkeypatch):
    """(b) declarations inside the worktree add nothing beyond the worktree key."""
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    repo = _make_repo(tmp_path)
    wt = _add_worktree(repo, tmp_path / "wt-inside", "wt/inside")
    with kbc.connect() as conn:
        tid = _task(
            conn,
            "worktree task writing only its own checkout",
            [
                wt / "src" / "feature.py",      # absolute inside
                "src/relative_inside.py",        # relative inside
            ],
            workspace_kind="worktree",
            workspace_path=wt,
        )
        keys = _keys(conn, tid)
    assert keys == {f"worktree:{kb._normalised_occupancy_path(wt)}"}
    assert not any(key.startswith("repo:") for key in keys)


# ---------------------------------------------------------------------------
# claim gate
# ---------------------------------------------------------------------------

def test_claim_overlap_blocker_flags_same_repo_writer(tmp_path, monkeypatch):
    """(c) worktree + external declaration collides with a same-repo writer."""
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    repo = _make_repo(tmp_path)
    wt = _add_worktree(repo, tmp_path / "wt-blocked", "wt/blocked")
    with kbc.connect() as conn:
        repo_writer = _task(conn, "ordinary repo writer", [repo / "src" / "main.py"])
        wt_external = _task(
            conn,
            "worktree writer touching a shared file",
            [repo / "shared" / "common.txt"],
            workspace_kind="worktree",
            workspace_path=wt,
        )
        assert kb.claim_task(conn, repo_writer, claimer="repo-writer") is not None

        blocker = kb._claim_overlap_blocker(conn, wt_external)
        assert blocker is not None
        blocker_id, overlap_keys = blocker
        assert blocker_id == repo_writer
        assert f"repo:{kb._normalised_occupancy_path(repo)}" in overlap_keys

        # End to end: the claim itself is rejected and the event recorded.
        assert kb.claim_task(conn, wt_external, claimer="wt-writer") is None
        rejected = [
            event for event in kb.list_events(conn, wt_external)
            if event.kind == "claim_rejected"
        ]
        assert rejected
        assert rejected[-1].payload["reason"] == "occupancy_overlap"
        assert rejected[-1].payload["blocker_task_id"] == repo_writer


def test_two_worktrees_each_writing_own_checkout_stay_parallel(tmp_path, monkeypatch):
    """(d) two worktree tasks confined to their own checkouts do not collide."""
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    repo = _make_repo(tmp_path)
    wt_one = _add_worktree(repo, tmp_path / "wt-one", "wt/one")
    wt_two = _add_worktree(repo, tmp_path / "wt-two", "wt/two")
    with kbc.connect() as conn:
        first = _task(
            conn, "worktree one", [wt_one / "src" / "one.py"],
            workspace_kind="worktree", workspace_path=wt_one,
        )
        second = _task(
            conn, "worktree two", ["src/two.py"],
            workspace_kind="worktree", workspace_path=wt_two,
        )
        assert kb._claim_overlap_blocker(conn, first) is None
        assert kb._claim_overlap_blocker(conn, second) is None
        assert kb.claim_task(conn, first, claimer="one") is not None
        assert kb.claim_task(conn, second, claimer="two") is not None


def test_junction_into_shared_directory_is_not_exempt(tmp_path, monkeypatch):
    """(e) a junction/symlink inside the worktree resolving elsewhere is external."""
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    repo = _make_repo(tmp_path)
    wt = _add_worktree(repo, tmp_path / "wt-junction", "wt/junction")
    link = wt / "linked-shared"
    if not _make_dir_link(link, repo / "shared"):
        pytest.xfail("cannot create a directory junction/symlink on this machine")
    with kbc.connect() as conn:
        tid = _task(
            conn,
            "worktree task reaching a shared directory through a junction",
            ["linked-shared/common.txt"],
            workspace_kind="worktree",
            workspace_path=wt,
        )
        keys = _keys(conn, tid)
    assert f"worktree:{kb._normalised_occupancy_path(wt)}" in keys
    assert f"repo:{kb._normalised_occupancy_path(repo)}" in keys
