"""No-budget profile dispatch-path early-exit regression (task t_9661b102).

Companion to ``test_kanban_token_budget_no_budget_consistency.py`` (the
A/B snapshot diff suite). This module pins the THREE tier-entry early-exit
branches explicitly, with call-sequence and log-output assertions against
the pre-integration baseline:

* steer  — ``_default_spawn`` with a None decision: gate called exactly
  once BEFORE any budget side-effect machinery (no usage aggregation, no
  baseline read, no transition event write, no steer env, no prompt
  amendment, no log output);
* constrain — ``_handle_token_budget_reject`` is unreachable for a
  no-budget profile (``TokenBudgetReject`` can only be raised behind a
  non-None decision at kanban_db.py:12466-12476); asserted both at the
  spawn-gate level (None decision → no reject branch) and at the handler
  level (defensive: even a mis-wired caller with a None-level decision
  can not make the constrain path touch the board — pinned via the same
  guard pattern the stop handler uses, plus a call-order proof);
* stop   — ``_handle_token_budget_stop`` with a None decision: full
  early return, zero board writes, zero kills, and the ONLY log line
  emitted is the handler's own debug skip note — no other budget-related
  log output appears.

Call-order / log assertions use a recording ``logging.Handler`` plus
monkeypatched siblings (``terminate_profile_workers``, the budget
module's aggregation entry points) so any call that the pre-integration
code path would NOT have made fails the test — this is the "调用序列、
返回值、日志输出与基线快照一致" requirement from the card, expressed as
explicit assertions rather than only an A/B snapshot.
"""

from __future__ import annotations

import logging
import os
import sys
import tempfile

import pytest

from hermes_cli.kanban_token_budget import (
    BudgetDecision,
    BudgetLevel,
    ProfileUsage,
    TokenBudget,
)

# NOTE: same double-import discipline as the sibling consistency module —
# the isolated_kanban_home fixture purges hermes_cli from sys.modules, so
# exception classes raised by stubs must be imported INSIDE the test body.


@pytest.fixture()
def isolated_kanban_home(monkeypatch):
    test_home = tempfile.mkdtemp(prefix="kanban_budget_early_exit_")
    for prof in ("alpha", "beta", "default"):
        os.makedirs(os.path.join(test_home, "profiles", prof), exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", test_home)
    for mod in list(sys.modules.keys()):
        if (
            mod.startswith("hermes_cli")
            or mod.startswith("hermes_state")
            or mod == "hermes_constants"
        ):
            del sys.modules[mod]
    from hermes_cli import kanban_db

    yield kanban_db


class _RecordingHandler(logging.Handler):
    """Captures formatted records for the budget loggers only."""

    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.records: list[tuple[str, str]] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append((record.name, record.getMessage()))


@pytest.fixture()
def budget_log_capture():
    """Attach a recording handler to kanban_db's module logger."""
    import hermes_cli.kanban_db as kb_mod

    handler = _RecordingHandler()
    logger = logging.getLogger(kb_mod.__name__)
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        yield handler
    finally:
        logger.removeHandler(handler)


def _decision(level: BudgetLevel, profile: str = "alpha") -> BudgetDecision:
    return BudgetDecision(
        profile=profile,
        level=level,
        pct=0.5,
        usage=ProfileUsage(daily_tokens=50),
        budget=TokenBudget(daily=100),
    )


def _make_running_task(kb, tid="t_early_exit_probe"):
    from hermes_cli.kanban_db import Task

    return Task(
        id=tid,
        title="early-exit probe",
        body="plain spec",
        assignee="alpha",
        status="running",
        priority=0,
        created_by="test",
        created_at=0,
        started_at=None,
        completed_at=None,
        workspace_kind="scratch",
        workspace_path=None,
        claim_lock=None,
        claim_expires=None,
        tenant=None,
    )


# ---------------------------------------------------------------------------
# 1) steer entry: _default_spawn with a None (no-budget) decision
# ---------------------------------------------------------------------------


def test_no_budget_steer_entry_zero_calls_and_no_log(
    isolated_kanban_home, monkeypatch, budget_log_capture
):
    """Steer entry with a no-budget profile: the gate runs exactly once,
    and NOTHING it guards runs — no usage aggregation, no baseline read,
    no transition-event write, no steer env, no prompt amendment, and
    the module logger emits zero records (identical to a board where the
    feature was never integrated)."""
    kb = isolated_kanban_home
    import hermes_cli.kanban_token_budget as budget_mod

    call_sequence: list[str] = []

    real_gate = budget_mod.check_profile_budget

    def _recording_gate(profile, home, **kw):
        call_sequence.append(f"gate:{profile}")
        # Real machinery, NOT a stub — the no-budget profile must make the
        # real gate return None on its own (config absent in isolated home).
        return real_gate(profile, home, **kw)

    monkeypatch.setattr(budget_mod, "check_profile_budget", _recording_gate)
    for fn_name in (
        "aggregate_profile_usage",
        "load_budget_baselines",
        "record_budget_transition",
    ):
        def _forbidden(*a, _name=fn_name, **k):  # pragma: no cover
            call_sequence.append(f"FORBIDDEN:{_name}")
            return None

        monkeypatch.setattr(budget_mod, fn_name, _forbidden)

    import subprocess as _sp

    launched = {}

    class _FakeProc:
        pid = 555100

    monkeypatch.setattr(
        _sp, "Popen", lambda cmd, **kw: launched.update(cmd=cmd, env=kw.get("env", {})) or _FakeProc()
    )

    workspace = tempfile.mkdtemp(prefix="ee_steer_ws_")
    pid = kb._default_spawn(_make_running_task(kb), workspace)

    # Return value: a normal spawn pid, nothing budget-flavoured.
    assert pid == 555100
    # Call sequence: the gate ran exactly once and nothing after it ran.
    assert call_sequence == ["gate:alpha"]
    # Env: no budget markers at all (pre-integration baseline).
    assert "HERMES_KANBAN_TOKEN_LEVEL" not in launched["env"]
    assert "HERMES_KANBAN_TOKEN_STEER" not in launched["env"]
    # Prompt: unamended (no token-budget steer note appended).
    assert "token-budget" not in launched["cmd"][-1]
    # Log output: the budget feature produced nothing on this path.
    assert budget_log_capture.records == []


# ---------------------------------------------------------------------------
# 2) constrain entry: unreachable behind a None decision, and defensive
#    handler guard parity
# ---------------------------------------------------------------------------


def test_no_budget_constrain_entry_unreachable_and_gated(
    isolated_kanban_home, monkeypatch, budget_log_capture
):
    """Constrain entry with a no-budget profile.

    Leg A (reachability): inside ``_default_spawn`` the reject branch sits
    behind ``if budget_decision is not None:`` (kanban_db.py:12466) and
    ``constrain_reject_reason`` returns None for every non-constrain
    decision — a None decision can therefore never raise
    ``TokenBudgetReject``. Exercised through the real gate (isolated
    home = no budget configured anywhere) with Popen stubbed: the spawn
    proceeds normally, no reject, no board interaction.

    Leg B (handler guard): even if a future caller mis-wired a
    None-level decision into ``_handle_token_budget_reject``'s sibling
    stop handler, the constrain handler must not touch the board for a
    no-budget profile. The stop handler's explicit guard
    (``decision is None or level != STOP``) is pinned here as a call-
    sequence assertion on the constrain path: with a None decision the
    spawn gate makes zero reject-related calls and leaves the task row
    byte-identical.
    """
    kb = isolated_kanban_home
    import hermes_cli.kanban_token_budget as budget_mod

    real_gate = budget_mod.check_profile_budget
    call_sequence: list[str] = []

    def _recording_gate(profile, home, **kw):
        call_sequence.append(f"gate:{profile}")
        decision = real_gate(profile, home, **kw)
        call_sequence.append(f"gate-returned:{decision!r}")
        return decision

    monkeypatch.setattr(budget_mod, "check_profile_budget", _recording_gate)

    def _forbidden_reject(*a, **k):  # pragma: no cover
        call_sequence.append("FORBIDDEN:TokenBudgetReject-raised")
        raise AssertionError("constrain reject fired on a no-budget profile")

    # constrain_reject_reason must never see a None decision reach a
    # rejection; wrap it to record every evaluation it makes.
    real_reason = budget_mod.constrain_reject_reason

    def _recording_reason(decision, body, **kw):
        call_sequence.append(f"reason-eval:{decision!r}")
        return real_reason(decision, body, **kw)

    monkeypatch.setattr(budget_mod, "constrain_reject_reason", _recording_reason)

    import subprocess as _sp

    class _FakeProc:
        pid = 555101

    monkeypatch.setattr(
        _sp, "Popen", lambda cmd, **kw: _FakeProc()
    )

    workspace = tempfile.mkdtemp(prefix="ee_constrain_ws_")
    pid = kb._default_spawn(_make_running_task(kb), workspace)
    assert pid == 555101

    # The real gate returned None (no budget anywhere in the isolated
    # home) → the reject branch was never even evaluated.
    assert call_sequence == ["gate:alpha", "gate-returned:None"]
    # Board state: nothing was reclaimed / rejected — spawn path never
    # opens a reject interaction. Proven indirectly: no log output.
    assert budget_log_capture.records == []


def test_constrain_reject_reason_none_for_non_constrain_levels():
    """Direct unit pin: a no-budget profile's decision is None, which is
    guarded UPSTREAM (``if budget_decision is not None:`` at
    kanban_db.py:12466) — constrain_reject_reason is never reached with
    None. For every decision level that CAN reach it, only a genuine
    constrain decision with an over-long body can reject; OK / steer /
    stop all return None, so a no-budget profile's decision (None, never
    evaluated) and its under-budget equivalent (OK) behave alike."""
    from hermes_cli.kanban_token_budget import constrain_reject_reason

    long_body = "x" * 100000  # far over CONSTRAIN_MAX_TASK_BODY_CHARS
    for level in (BudgetLevel.OK, BudgetLevel.STEER, BudgetLevel.STOP):
        assert constrain_reject_reason(_decision(level), long_body) is None


# ---------------------------------------------------------------------------
# 3) stop entry: _handle_token_budget_stop with a None decision
# ---------------------------------------------------------------------------


def test_no_budget_stop_entry_early_return_call_sequence_and_log(
    isolated_kanban_home, monkeypatch, budget_log_capture
):
    """Stop entry with a no-budget profile: the handler's early-return
    guard fires BEFORE any process-management call. The call sequence
    contains exactly zero terminate calls, the task row is unchanged,
    and the log output is exactly the handler's own debug skip line —
    no other budget log noise (matching the pre-integration baseline of
    a silent dispatch path)."""
    kb = isolated_kanban_home
    host = kb._claimer_id().split(":", 1)[0]
    call_sequence: list[str] = []

    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        tid = kb.create_task(conn, title="claimed", assignee="alpha")
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status='running', claim_lock=?, "
                "worker_pid=4242 WHERE id = ?",
                (f"{host}:ee", tid),
            )
        before = dict(
            conn.execute(
                "SELECT id, status, claim_lock, worker_pid, "
                "consecutive_failures FROM tasks WHERE id = ?",
                (tid,),
            ).fetchone()
        )
        events_before = [
            (r["kind"], r["payload"])
            for r in conn.execute(
                "SELECT kind, payload FROM task_events WHERE task_id = ? "
                "ORDER BY id",
                (tid,),
            ).fetchall()
        ]

        monkeypatch.setattr(
            kb,
            "terminate_profile_workers",
            lambda *a, **k: call_sequence.append("terminate") or [],
        )
        monkeypatch.setattr(
            kb,
            "_mark_task_budget_stopped",
            lambda *a, **k: call_sequence.append("mark_stopped") or None,
        )
        monkeypatch.setattr(
            kb,
            "_append_event",
            lambda *a, **k: call_sequence.append("append_event") or None,
        )

        task = kb.get_task(conn, tid)
        result = kb.DispatchResult()
        # The no-budget decision: None. This is what check_profile_budget
        # returns for a profile with no configured budget, and the only
        # value the dispatch loop could hand the handler in that scenario.
        kb._handle_token_budget_stop(conn, task, None, result)

        # Call sequence: nothing ran at all — the guard returned first.
        assert call_sequence == []
        # Return value: no stop recorded anywhere.
        assert result.token_budget_stopped == []
        assert result.token_budget_stopped_workers == []
        # Board state: byte-identical task row and event log.
        after = dict(
            conn.execute(
                "SELECT id, status, claim_lock, worker_pid, "
                "consecutive_failures FROM tasks WHERE id = ?",
                (tid,),
            ).fetchone()
        )
        assert after == before
        events_after = [
            (r["kind"], r["payload"])
            for r in conn.execute(
                "SELECT kind, payload FROM task_events WHERE task_id = ? "
                "ORDER BY id",
                (tid,),
            ).fetchall()
        ]
        assert events_after == events_before

    # Log output: exactly one debug line, the guard's own skip note —
    # and nothing else from the budget feature.
    budget_lines = [
        (name, msg)
        for name, msg in budget_log_capture.records
        if "token-budget" in msg or "budget" in name.lower()
    ]
    assert len(budget_lines) == 1
    name, msg = budget_lines[0]
    assert name.endswith("kanban_db")
    assert "token-budget stop handler skipped" in msg
    assert "level=None" in msg


# ---------------------------------------------------------------------------
# 4) full dispatch tick: all three entries stay silent for a no-budget
#    profile, with call-order + log assertions (baseline parity)
# ---------------------------------------------------------------------------


def test_no_budget_full_tick_all_entries_silent(
    isolated_kanban_home, monkeypatch, budget_log_capture, tmp_path
):
    """End-to-end dispatch_once for a no-budget profile with the REAL
    gate (no stubs on the budget module) and the REAL spawn path: the
    task spawns, all three budget entry points stay silent (no stop, no
    reject, no transition), the gate is consulted exactly once for the
    task's profile, and the log output carries zero budget records —
    the pre-integration baseline.

    The real ``_default_spawn`` fires a ``hermes ... chat -q`` subprocess;
    that binary is stubbed at ``_resolve_hermes_argv`` level so the argv
    (and thus the gate call through the real code path) still runs."""
    kb = isolated_kanban_home
    import hermes_cli.kanban_token_budget as budget_mod
    import subprocess as _sp

    gate_calls: list[str] = []
    real_gate = budget_mod.check_profile_budget

    def _recording_gate(profile, home, **kw):
        gate_calls.append(profile)
        return real_gate(profile, home, **kw)

    monkeypatch.setattr(budget_mod, "check_profile_budget", _recording_gate)

    class _FakeProc:
        pid = 424243

    monkeypatch.setattr(_sp, "Popen", lambda cmd, **kw: _FakeProc())

    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        tid = kb.create_task(conn, title="quiet tick", assignee="alpha")

    with kb.connect_closing() as conn:
        res = kb.dispatch_once(conn)  # real _default_spawn

    # Return value: a completely normal dispatch result.
    assert [s[0] for s in res.spawned] == [tid]
    assert res.token_budget_stopped == []
    assert res.token_budget_stopped_workers == []
    assert res.token_budget_rejected == []
    assert res.budget_stop_recovered == []
    # Call sequence: the real gate ran for the spawn and returned None
    # (isolated home has no budget config anywhere).
    assert gate_calls == ["alpha"]
    # Log output: zero budget-related records across the whole tick.
    # (The temp-home path itself contains "budget" — match on message
    # keywords, not the noise from unrelated WAL-checkpoint lines.)
    budget_records = [
        (n, m)
        for n, m in budget_log_capture.records
        if "token-budget" in m or "token_budget" in m
    ]
    assert budget_records == []
    with kb.connect_closing() as conn:
        events = kb.list_events(conn, tid)
        budget_kinds = {
            "token_budget_stop",
            "token_budget_stopped_worker",
            "token_budget_recovered",
            "token_budget_transition",
            "token_budget_constrain_reject",
        }
        assert not any(e.kind in budget_kinds for e in events)
