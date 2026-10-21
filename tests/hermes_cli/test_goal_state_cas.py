"""W1 goal-state CAS: reliable writes, versioned snapshots, atomic migration.

The five review-focus hazards from the goal-optimization plan (第八节 T01—T05), each pinned
against the REAL persistence layer (SessionDB ``state_meta``) inside an isolated HERMES_HOME —
no fake stores, no production DB:

T01 persistence layer unavailable  -> no "goal created" claim, no automatic round started
T02 corrupt JSON / unknown schema  -> original bytes preserved, explicit error, never "no goal"
T03 migration half-write           -> all-or-nothing; parent never lost, never two active goals
T04 pause/clear vs a late judge    -> the user's newer state wins, the stale verdict is rejected
T05 clear + recreate the same text -> new goal_id; an old reference cannot overwrite

Every case is driven through the entry points production uses (``GoalManager``,
``load_goal`` / ``save_goal``, ``migrate_goal_to_session``, ``dispatch_goal_command``) so a
passing test is a behaviour contract, not an API-shape check.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from hermes_cli import goals


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME so SessionDB.state_meta writes never touch a real profile."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    goals._DB_CACHE.clear()
    yield home
    goals._DB_CACHE.clear()


def _raw(session_id: str):
    """The exact stored bytes for a session's goal row (None when the row is absent)."""
    return goals._get_session_db().get_meta(goals._meta_key(session_id))


def _write_raw(session_id: str, raw: str) -> None:
    goals._get_session_db().set_meta(goals._meta_key(session_id), raw)


def _break_writes_for(db, key: str, monkeypatch) -> None:
    """Make every TRANSACTIONAL write of ``key`` fail after the read, as a dying disk would."""
    real_set_meta = db.set_meta

    def flaky(key_, value, *, cursor=None):
        if cursor is not None and key_ == key:
            raise sqlite3.OperationalError("disk I/O error (injected)")
        return real_set_meta(key_, value, cursor=cursor)

    monkeypatch.setattr(db, "set_meta", flaky)


# ──────────────────────────────────────────────────────────────────────
# T01 — the persistence layer is unavailable
# ──────────────────────────────────────────────────────────────────────


class TestPersistenceUnavailable:
    def test_no_store_means_no_created_goal_and_no_automatic_round(self, hermes_home, monkeypatch):
        """A goal whose snapshot could not be written must not be reported as created, and the
        manager must hold nothing that could start a round."""
        monkeypatch.setattr(goals, "_get_session_db", lambda: None)
        mgr = goals.GoalManager("t01-no-store")

        with pytest.raises(goals.GoalStoreUnavailable):
            mgr.set("ship the release")

        assert mgr.state is None, "a failed persist must not leave a goal the loop can run"
        assert mgr.is_active() is False and mgr.has_goal() is False
        # Reading with no store is "unavailable" too — it must not be reported as "no goal".
        with pytest.raises(goals.GoalStoreUnavailable):
            goals.load_goal("t01-no-store")
        assert mgr.evaluate_after_turn("looks finished")["should_continue"] is False

    def test_a_dying_write_surface_is_reported_not_swallowed(self, hermes_home, monkeypatch):
        """A store that reads fine but fails the write must surface as unavailable, and the row
        must stay exactly as it was."""
        mgr = goals.GoalManager("t01-dying-write")
        mgr.set("ship the release")
        before = _raw("t01-dying-write")
        db = goals._get_session_db()
        _break_writes_for(db, goals._meta_key("t01-dying-write"), monkeypatch)

        with pytest.raises(goals.GoalStoreUnavailable):
            mgr.add_subgoal("and the migration")

        assert _raw("t01-dying-write") == before, "a failed commit must not touch the row"

    def test_the_goal_command_never_claims_success_on_a_dead_store(self, hermes_home, monkeypatch):
        """``/goal set`` must return an error result — never "Goal set" — when nothing landed."""
        from hermes_cli.goal_command import dispatch_goal_command

        monkeypatch.setattr(goals, "_get_session_db", lambda: None)
        mgr = goals.GoalManager("t01-command")

        result = dispatch_goal_command(mgr, "ship the release", authorize_gate=lambda: None)

        assert result.error is True
        assert "Goal set" not in result.output
        assert mgr.state is None


# ──────────────────────────────────────────────────────────────────────
# T02 — corrupt JSON / unknown schema
# ──────────────────────────────────────────────────────────────────────


class TestCorruptAndUnknownSchema:
    def test_a_corrupt_row_is_not_no_goal_and_keeps_its_bytes(self, hermes_home):
        sid = "t02-corrupt"
        _write_raw(sid, '{"goal": "half written", "status": ')
        original = _raw(sid)

        with pytest.raises(goals.GoalStateCorrupt):
            goals.load_goal(sid)
        assert _raw(sid) == original, "an unreadable row must keep its original bytes"

        # Mutations refuse instead of overwriting evidence they cannot read.
        with pytest.raises(goals.GoalStateCorrupt):
            goals.clear_goal(sid)
        assert _raw(sid) == original

        mgr = goals.GoalManager(sid)
        assert mgr.state is None
        assert mgr.is_active() is False, "an unreadable goal must not auto-run"
        assert mgr.load_error is not None and mgr.load_error.kind == "corrupt"

    def test_an_unknown_schema_version_is_refused_and_preserved(self, hermes_home):
        """A row written by a NEWER Hermes must not be reinterpreted by this one."""
        sid = "t02-future-schema"
        payload = json.dumps({"schema_version": 99, "goal": "from the future", "status": "active"})
        _write_raw(sid, payload)

        with pytest.raises(goals.GoalStateCorrupt):
            goals.load_goal(sid)
        assert _raw(sid) == payload

        mgr = goals.GoalManager(sid)
        assert mgr.state is None and mgr.is_active() is False
        assert "schema" in str(mgr.load_error).lower()

        # Compression is an automatic flow: it must not run over an unknown row either.
        assert goals.migrate_goal_to_session(sid, sid + "-child", reason="compression") is False
        assert _raw(sid) == payload
        assert _raw(sid + "-child") is None

    def test_legacy_rows_without_version_fields_still_load(self, hermes_home):
        """Pre-versioning rows are compatible reads; the fields arrive with the first change."""
        sid = "t02-legacy"
        legacy = json.dumps({"goal": "do a thing", "status": "active", "turns_used": 2})
        _write_raw(sid, legacy)

        state = goals.load_goal(sid)
        assert state is not None and state.goal == "do a thing"
        assert state.schema_version == goals.GOAL_STATE_SCHEMA_VERSION
        assert state.goal_id == "" and state.revision == 0

    def test_an_explicit_set_is_the_recovery_path_for_an_unreadable_row(self, hermes_home):
        """Fail-closed everywhere else, but the user's own ``/goal set`` may replace the row."""
        sid = "t02-recover"
        _write_raw(sid, "not json at all")

        mgr = goals.GoalManager(sid)
        mgr.set("a fresh goal")

        state = goals.load_goal(sid)
        assert state.goal == "a fresh goal" and state.goal_id
        assert state.revision == 1


# ──────────────────────────────────────────────────────────────────────
# T03 — the compression migration is one transaction
# ──────────────────────────────────────────────────────────────────────


class TestAtomicMigration:
    def test_a_failed_second_write_leaves_the_parent_goal_intact(self, hermes_home, monkeypatch):
        """The old code wrote the child row and archived the parent in two independent calls:
        a failure in between cleared a live goal that had not moved anywhere."""
        parent, child = "t03-parent", "t03-child"
        goals.GoalManager(parent).set("finish the migration")
        before = _raw(parent)
        db = goals._get_session_db()
        _break_writes_for(db, goals._meta_key(child), monkeypatch)

        assert goals.migrate_goal_to_session(parent, child, reason="compression") is False

        assert _raw(parent) == before, "the parent row must not be archived when the move failed"
        assert goals.load_goal(parent).status == "active"
        assert _raw(child) is None, "no half-written child row may survive"

    def test_a_live_child_goal_blocks_the_move_without_touching_either_row(self, hermes_home):
        parent, child = "t03-parent-live", "t03-child-live"
        goals.GoalManager(parent).set("parent objective")
        goals.GoalManager(child).set("child already has its own goal")
        parent_before, child_before = _raw(parent), _raw(child)

        assert goals.migrate_goal_to_session(parent, child, reason="compression") is False

        assert _raw(parent) == parent_before
        assert _raw(child) == child_before
        assert goals.load_goal(parent).status == "active", "exactly one active goal per lineage"
        assert goals.load_goal(child).goal == "child already has its own goal"

    def test_the_transfer_moves_the_goal_and_archives_the_parent(self, hermes_home):
        parent, child = "t03-ok-parent", "t03-ok-child"
        goals.GoalManager(parent).set("finish the migration")
        original = goals.load_goal(parent)

        assert goals.migrate_goal_to_session(parent, child, reason="compression") is True

        migrated = goals.load_goal(child)
        assert migrated.goal == "finish the migration" and migrated.status == "active"
        assert migrated.goal_id == original.goal_id, "the same logical goal continues"
        assert migrated.revision == original.revision, "a move is not a change of the goal itself"
        archived = goals.load_goal(parent)
        assert archived.status == "cleared"
        assert archived.revision == original.revision + 1, "the tombstone moves the revision on"


# ──────────────────────────────────────────────────────────────────────
# T04 — pause/clear racing a late judge verdict
# ──────────────────────────────────────────────────────────────────────


class TestLateJudgeVerdict:
    def test_a_late_done_cannot_overwrite_the_users_pause(self, hermes_home, monkeypatch):
        """The judge is a slow call: the user may pause while it runs. The stale 'done' must be
        rejected by CAS and the paused state must survive."""
        sid = "t04-late-judge"
        mgr = goals.GoalManager(sid)
        mgr.set("ship the release")

        def pausing_judge(_goal, _response, **_kwargs):
            # The user acts while the judge is in flight (a second manager, as the UI opens).
            goals.GoalManager(sid).pause("user-paused")
            return ("done", "everything is finished", False, None, False)

        monkeypatch.setattr(goals, "judge_goal", pausing_judge)
        decision = mgr.evaluate_after_turn("all done")

        assert decision["should_continue"] is False, "a stale verdict must not start a round"
        stored = goals.load_goal(sid)
        assert stored.status == "paused", "the user's newer state wins"
        assert stored.last_verdict != "done"
        assert mgr.state is not None and mgr.state.status == "paused"

    def test_a_stale_manager_snapshot_is_rejected_by_cas(self, hermes_home):
        """A writer still holding the pre-pause revision loses the compare-and-swap."""
        sid = "t04-stale-writer"
        goals.GoalManager(sid).set("ship the release")
        stale = goals.GoalManager(sid)          # holds revision 1
        assert stale.state.revision == 1
        goals.GoalManager(sid).pause("user-paused")

        with pytest.raises(goals.GoalConflict) as conflict:
            stale.mark_done("late judge verdict")

        assert conflict.value.current is not None and conflict.value.current.status == "paused"
        stored = goals.load_goal(sid)
        assert stored.status == "paused"
        assert stored.revision == 2, "the pause is the only committed change"


# ──────────────────────────────────────────────────────────────────────
# T05 — clear, recreate the same text, old clients edit
# ──────────────────────────────────────────────────────────────────────


class TestClearAndRecreate:
    def test_recreating_the_same_text_mints_a_new_goal_id(self, hermes_home):
        sid = "t05-recreate"
        first = goals.GoalManager(sid)
        first.set("ship the same text")
        original = goals.load_goal(sid)

        first.clear()
        tombstone = goals.load_goal(sid)
        assert tombstone.status == "cleared"
        assert tombstone.goal_id == original.goal_id, "the tombstone keeps the old identity"
        assert tombstone.revision == original.revision + 1

        goals.GoalManager(sid).set("ship the same text")
        recreated = goals.load_goal(sid)
        assert recreated.status == "active"
        assert recreated.goal_id != original.goal_id, "a recreated goal is a new goal"

    def test_an_old_reference_cannot_overwrite_the_recreated_goal(self, hermes_home):
        sid = "t05-old-client"
        goals.GoalManager(sid).set("ship the same text")
        original = goals.load_goal(sid)
        goals.GoalManager(sid).clear()
        goals.GoalManager(sid).set("ship the same text")
        recreated = goals.load_goal(sid)

        # A manager that still holds the pre-clear snapshot (an old client's edit).
        with pytest.raises(goals.GoalConflict):
            goals.save_goal(sid, replace(original, turns_used=99, status="done"))

        assert goals.load_goal(sid).goal_id == recreated.goal_id
        assert goals.load_goal(sid).status == "active"
        assert goals.load_goal(sid).turns_used == 0

    def test_a_manager_that_never_saw_the_goal_cannot_create_over_it(self, hermes_home):
        """Two surfaces racing to create: the one that read an empty store loses."""
        sid = "t05-racing-create"
        unaware = goals.GoalManager(sid)       # read before any goal existed
        goals.GoalManager(sid).set("the goal that landed first")

        with pytest.raises(goals.GoalConflict):
            unaware.set("the goal that landed second")

        assert goals.load_goal(sid).goal == "the goal that landed first"
