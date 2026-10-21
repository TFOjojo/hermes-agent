"""``SessionDB.exchange_meta`` — the guarded metadata transaction W1's goal CAS is built on.

``state_meta`` upserts were blind last-write-wins. ``exchange_meta`` reads every guarded key,
compares it with the value the caller read, and only then writes — all inside ONE
``BEGIN IMMEDIATE`` — so a row that moved between the caller's read and the write loses the
guard instead of being clobbered, and a failure mid-transaction leaves nothing behind.
"""

from __future__ import annotations

import sqlite3

import pytest

from hermes_state import SessionDB


@pytest.fixture()
def db(tmp_path):
    db_path = tmp_path / "test_state.db"
    session_db = SessionDB(db_path=db_path)
    yield session_db
    session_db.close()


def test_commits_when_every_guard_still_matches(db):
    db.set_meta("goal:a", "one")
    db.set_meta("goal:b", "two")

    assert db.exchange_meta({"goal:a": "one", "goal:b": "two"}, {"goal:a": "ONE", "goal:b": "TWO"}) == "committed"

    assert db.get_meta("goal:a") == "ONE"
    assert db.get_meta("goal:b") == "TWO"


def test_writes_nothing_when_one_guard_drifted(db):
    """The other writer landed first: the whole exchange is refused, not partially applied."""
    db.set_meta("goal:a", "one")
    db.set_meta("goal:b", "two")

    assert db.exchange_meta({"goal:a": "one", "goal:b": "STALE"}, {"goal:a": "ONE"}) == "conflict"

    assert db.get_meta("goal:a") == "one"
    assert db.get_meta("goal:b") == "two"


def test_a_none_guard_requires_the_row_to_be_absent(db):
    db.set_meta("goal:a", "one")

    assert db.exchange_meta({"goal:missing": None}, {"goal:missing": "fresh"}) == "committed"
    assert db.get_meta("goal:missing") == "fresh"

    db.set_meta("goal:taken", "already here")
    assert db.exchange_meta({"goal:taken": None}, {"goal:taken": "clobber"}) == "conflict"
    assert db.get_meta("goal:taken") == "already here"


def test_a_failing_write_rolls_the_whole_exchange_back(db, monkeypatch):
    """Atomicity under fault injection: the second write dies inside the transaction, so the
    first write must not survive either (the old two-call shape left exactly this half-state)."""
    db.set_meta("goal:parent", "live goal")
    real_set_meta = db.set_meta

    def flaky(key, value, *, cursor=None):
        if cursor is not None and key == "goal:child":
            raise sqlite3.OperationalError("disk I/O error (injected)")
        return real_set_meta(key, value, cursor=cursor)

    monkeypatch.setattr(db, "set_meta", flaky)

    with pytest.raises(sqlite3.OperationalError):
        db.exchange_meta(
            {"goal:parent": "live goal", "goal:child": None},
            {"goal:child": "migrated goal", "goal:parent": "archived tombstone"},
        )

    assert db.get_meta("goal:child") is None, "no half-written child row"
    assert db.get_meta("goal:parent") == "live goal", "the parent row was never archived"
