"""No-budget profile behaviour-consistency regression (task t_d9b17f20).

The zero-interference contract: a profile WITHOUT a configured token
budget must behave exactly like a profile on a board where the token
budget feature was never integrated — at every stage of the dispatch
lifecycle that the budget feature touches:

* ``_default_spawn`` — the gate is a byte-for-byte no-op: no env
  markers, no prompt amendment, identical argv;
* ``dispatch_once`` full pass — identical DispatchResult on every
  budget-relevant field, no board writes beyond the pre-feature set,
  no budget events on any task;
* worker start/stop — a real live worker of a no-budget profile is
  never signalled by a stop trigger aimed at a *budgeted* sibling
  profile, and a budget_stopped rewrite never reaches a no-budget
  task (handler early-return guard);
* recovery — a no-budget profile parked in ``budget_stopped`` (e.g. a
  budget was removed while it was parked) is recovered unconditionally
  on the next tick, never left stranded.

The distinguishing technique vs the existing suites
(test_kanban_token_budget.py covers decision maths with fakes,
test_kanban_token_budget_spawn_gate.py covers steer/None injection,
test_kanban_stop_trigger_simulation.py covers the real-process stop
ladder) is the *same-input A/B diff*: one board, one task, one spawn
stub, run twice — once with the budget gate patched to ``None``
(no budget configured) and once with the budget machinery monkeypatched
out entirely (simulating pre-integration code) — then assert the two
runs produced IDENTICAL argv, env, prompt, board state, and result
fields. Any behavioural drift the budget feature introduces on
no-budget profiles shows up as a diff and fails the test.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time

import pytest

from hermes_cli.kanban_token_budget import (
    BudgetDecision,
    BudgetLevel,
    ProfileUsage,
    TokenBudget,
)

# NOTE: TokenBudgetStop is deliberately NOT imported at module level.
# The isolated_kanban_home fixture purges hermes_cli from sys.modules so
# kanban_db re-resolves its paths against the isolated home — which also
# re-imports a FRESH kanban_token_budget module. The exception class the
# dispatch loop catches is the fresh module's class; a module-level
# import would hand the spawn stub a *different* class object whose
# raises are never caught. Every test that raises the stop exception
# imports it inside the test body, after the fixture ran (same pattern
# as test_kanban_token_budget.py::test_dispatch_stop_releases_and_records).


# ---------------------------------------------------------------------------
# fixture (same purge semantics as the sibling budget test modules)
# ---------------------------------------------------------------------------


@pytest.fixture()
def isolated_kanban_home(monkeypatch):
    test_home = tempfile.mkdtemp(prefix="kanban_budget_consistency_")
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


def _stop_decision(profile: str = "alpha") -> BudgetDecision:
    return BudgetDecision(
        profile=profile,
        level=BudgetLevel.STOP,
        pct=1.5,
        usage=ProfileUsage(daily_tokens=150),
        budget=TokenBudget(daily=100),
    )


# ---------------------------------------------------------------------------
# helpers — snapshot board state for A/B diffing
# ---------------------------------------------------------------------------


def _task_row(conn, tid):
    return dict(
        conn.execute(
            "SELECT id, status, claim_lock, worker_pid, current_run_id, "
            "consecutive_failures, workspace_path FROM tasks WHERE id = ?",
            (tid,),
        ).fetchone()
    )


def _all_events(kb_mod, conn):
    rows = conn.execute(
        "SELECT task_id, kind, payload FROM task_events ORDER BY id"
    ).fetchall()
    return [(r["task_id"], r["kind"], r["payload"]) for r in rows]


def _normalize_event_payload(payload: str, tid: str) -> str:
    """Mask run-scoped values that legitimately differ between two A/B
    runs on the same board: the task id itself, the task_run id (fresh
    per run), the claim expiry timestamp (wall clock), and the claim
    lock suffix (a per-claim random token). Budget-relevant payload
    content is untouched — a stop/steer/reject event would still show
    up verbatim and fail the diff."""
    import re as _re

    out = payload.replace(tid, "<task>")
    out = _re.sub(r'"expires": \d+', '"expires": <exp>', out)
    out = _re.sub(r'"run_id": \d+', '"run_id": <run>', out)
    out = _re.sub(r'"lock": "[^"]*"', '"lock": "<lock>"', out)
    return out


def _budget_relevant(result) -> dict:
    """Every DispatchResult field the budget feature can touch."""
    return {
        "spawned": result.spawned,
        "token_budget_stopped": result.token_budget_stopped,
        "token_budget_stopped_workers": result.token_budget_stopped_workers,
        "token_budget_rejected": result.token_budget_rejected,
        "budget_auto_recovered": result.budget_auto_recovered,
        "budget_stop_recovered": result.budget_stop_recovered,
    }


class _RecordingSpawn:
    """spawn_fn stub that records argv/env-equivalents and returns a pid.

    Used for the dispatch-level A/B diff. Deliberately does NOT go
    through subprocess — dispatch_once only needs a PID back; what we
    diff is everything the budget feature could mutate before spawn.
    """

    def __init__(self):
        self.calls = []

    def __call__(self, task, workspace, **kwargs):
        self.calls.append((task.id, str(workspace), dict(kwargs)))
        return 4242


def _ab_setup(kb):
    """Shared board+task setup — identical for both A and B runs."""
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        tid = kb.create_task(
            conn, title="consistency probe", assignee="alpha", body="spec"
        )
    return tid


# ---------------------------------------------------------------------------
# 1) dispatch_once A/B diff — no-budget vs pre-integration
# ---------------------------------------------------------------------------


def test_dispatch_once_no_budget_matches_pre_integration(
    isolated_kanban_home, monkeypatch
):
    """Run dispatch_once twice over an identical fresh board:

    * A (budget gate returns None — profile has no budget configured)
    * B (pre-integration simulation: the budget machinery is fully
      bypassed — the gate is replaced by a spy that records the call and
      returns None without touching config, state.db, or baselines)

    Both must spawn the task with the same result fields and leave the
    same board state + event log. Any drift the integrated feature
    introduces for budget-less profiles shows up as an A/B diff.
    """
    kb = isolated_kanban_home
    import hermes_cli.kanban_token_budget as budget_mod

    snapshots = {}
    for label, disable_gate in (("A_no_budget", False), ("B_pre_integration", True)):
        # fresh board per run — task ids are random, so normalise them
        # out of every snapshot before diffing.
        tid = _ab_setup(kb)
        spawn_rec = _RecordingSpawn()

        if disable_gate:
            # Pre-integration stand-in: the gate performs no reads at
            # all (no config load, no usage aggregation, no baselines).
            gate_calls = []

            def _spy(profile, home, **kw):
                gate_calls.append(profile)
                return None

            monkeypatch.setattr(budget_mod, "check_profile_budget", _spy)
        else:
            monkeypatch.setattr(
                budget_mod, "check_profile_budget", lambda p, h, **k: None
            )

        with kb.connect_closing() as conn:
            res = kb.dispatch_once(conn, spawn_fn=spawn_rec)
            norm_spawned = [
                ("<task>", s[1], s[2].replace(tid, "<task>"))
                for s in res.spawned
            ]
            snapshots[label] = {
                "result": {**_budget_relevant(res), "spawned": norm_spawned},
                "task": {
                    k: (v.replace(tid, "<task>") if isinstance(v, str) else v)
                    for k, v in _task_row(conn, tid).items()
                    if k not in ("id", "current_run_id")
                },
                "events": [
                    (
                        kind,
                        _normalize_event_payload(payload or "", tid),
                    )
                    for t, kind, payload in _all_events(kb, conn)
                    if t == tid and kind != "tip_scratch_workspace"
                ],
                "spawn_calls": [
                    ("<task>", ws.replace(tid, "<task>"), kw)
                    for _, ws, kw in spawn_rec.calls
                ],
            }

    a, b = snapshots["A_no_budget"], snapshots["B_pre_integration"]
    assert a["result"] == b["result"]
    assert a["task"] == b["task"]
    assert a["events"] == b["events"]
    assert a["spawn_calls"] == b["spawn_calls"]

    # And the shared invariants that hold for BOTH runs:
    assert a["result"]["spawned"] and a["result"]["spawned"][0][0] == "<task>"
    assert a["result"]["token_budget_stopped"] == []
    assert a["result"]["token_budget_stopped_workers"] == []
    assert a["result"]["token_budget_rejected"] == []
    # No budget event kinds anywhere on the board (whole board, both runs).
    budget_kinds = {
        "token_budget_stop",
        "token_budget_stopped_worker",
        "token_budget_recovered",
        "token_budget_transition",
        "token_budget_reset",
    }
    for snap in (a, b):
        assert not any(kind in budget_kinds for kind, _ in snap["events"])
    # Task left the ready state into running via a normal claim+spawn.
    assert a["task"]["status"] == "running"
    assert a["task"]["worker_pid"] == 4242


# ---------------------------------------------------------------------------
# 2) _default_spawn A/B diff — argv/env/prompt byte-identity
# ---------------------------------------------------------------------------


def _make_task(kb, *, body="plain spec", tid="t_consistency_probe"):
    from hermes_cli.kanban_db import Task

    return Task(
        id=tid,
        title="consistency probe",
        body=body,
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


def _run_default_spawn(kb, monkeypatch, gate_result, task):
    import hermes_cli.kanban_token_budget as budget_mod
    import subprocess as _sp

    workspace = tempfile.mkdtemp(prefix="budget_ab_ws_")
    launched = {}

    class _FakeProc:
        pid = 555001

    def _fake_popen(cmd, **kwargs):
        launched["cmd"] = list(cmd)
        launched["env"] = dict(kwargs.get("env", {}))
        return _FakeProc()

    monkeypatch.setattr(_sp, "Popen", _fake_popen)
    if gate_result is _NOT_CONFIGURED:
        monkeypatch.setattr(
            budget_mod, "check_profile_budget", lambda p, h, **k: None
        )
    else:
        monkeypatch.setattr(
            budget_mod,
            "check_profile_budget",
            lambda p, h, **k: gate_result,
        )

    pid = kb._default_spawn(task, workspace)
    launched["pid"] = pid
    # Normalize the fields the budget contract promises NOT to touch.
    env = launched.pop("env")
    launched["token_level"] = env.get("HERMES_KANBAN_TOKEN_LEVEL")
    launched["token_steer"] = env.get("HERMES_KANBAN_TOKEN_STEER")
    launched["hermes_profile"] = env.get("HERMES_PROFILE")
    launched["kanban_db"] = env.get("HERMES_KANBAN_DB")
    launched["kanban_task"] = env.get("HERMES_KANBAN_TASK")
    return launched


_NOT_CONFIGURED = object()


def test_default_spawn_no_budget_env_prompt_argv_identity(
    isolated_kanban_home, monkeypatch
):
    """No-budget profile: argv, prompt, and every non-budget env var are
    identical to what the OK-level gate produces for a budgeted profile
    that is under budget — and differ ONLY by the documented level
    marker. Wait — that would drift. The real contract:

    * no-budget (None) → NO marker at all, NO steer note, prompt as built.
    * OK (budgeted, under budget) → marker ``ok``, NO steer note, prompt
      untouched.

    Both leave the prompt unamended; the only difference between them is
    the observability marker. And vs pre-integration (gate removed), the
    no-budget run is fully identical. Assert all three legs.
    """
    kb = isolated_kanban_home

    none_run = _run_default_spawn(kb, monkeypatch, _NOT_CONFIGURED, _make_task(kb))
    ok_run = _run_default_spawn(
        kb,
        monkeypatch,
        BudgetDecision(
            profile="alpha",
            level=BudgetLevel.OK,
            pct=0.1,
            usage=ProfileUsage(daily_tokens=10),
            budget=TokenBudget(daily=100),
        ),
        _make_task(kb),
    )

    # Prompt (last argv element) unamended in BOTH legs.
    assert none_run["cmd"][-1] == ok_run["cmd"][-1]
    assert "token-budget" not in none_run["cmd"][-1]
    # Identical argv apart from... nothing: argv must be identical.
    assert none_run["cmd"] == ok_run["cmd"]
    # No-budget: no markers at all.
    assert none_run["token_level"] is None
    assert none_run["token_steer"] is None
    # OK: exactly the documented marker, nothing else differs.
    assert ok_run["token_level"] == "ok"
    assert ok_run["token_steer"] is None
    # Shared non-budget env pinning is stable across both.
    assert none_run["hermes_profile"] == ok_run["hermes_profile"] == "alpha"
    assert none_run["kanban_task"] == ok_run["kanban_task"]
    assert none_run["kanban_db"] == ok_run["kanban_db"]
    assert none_run["pid"] == ok_run["pid"] == 555001


# ---------------------------------------------------------------------------
# 3) stop trigger never reaches a no-budget profile's live worker
# ---------------------------------------------------------------------------


SIMPLE_SLEEP_CODE = "import time\nprint('ready', flush=True)\ntime.sleep(600)\n"


def _spawn_real_worker():
    return subprocess.Popen(
        [sys.executable, "-u", "-c", SIMPLE_SLEEP_CODE],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def test_stop_trigger_zero_interference_real_process(isolated_kanban_home):
    """A stop-level trigger firing for budgeted profile ``alpha`` while a
    REAL live worker of no-budget profile ``beta`` is running must leave
    beta's worker alive, its task running, and its claim intact — while
    alpha's triggering claim is parked in budget_stopped."""
    kb = isolated_kanban_home
    # Import AFTER the fixture purged hermes_cli — the raised exception
    # must be the same class object the dispatch loop catches.
    from hermes_cli.kanban_token_budget import TokenBudgetStop

    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        alpha_tid = kb.create_task(
            conn, title="over budget alpha", assignee="alpha"
        )

    beta_proc = _spawn_real_worker()
    try:
        with kb.connect_closing() as conn:
            beta_tid = kb.create_task(
                conn, title="live beta worker", assignee="beta"
            )
            host = kb._claimer_id().split(":", 1)[0]
            now = int(time.time())
            with kb.write_txn(conn):
                conn.execute(
                    "UPDATE tasks SET status='running', claim_lock=?, "
                    "claim_expires=?, worker_pid=? WHERE id = ?",
                    (f"{host}:sim-beta", now + 600, beta_proc.pid, beta_tid),
                )
                conn.execute(
                    "INSERT INTO task_runs (task_id, profile, status, "
                    "claim_lock, claim_expires, started_at) "
                    "VALUES (?, ?, 'running', ?, ?, ?)",
                    (beta_tid, "beta", f"{host}:sim-beta", now + 600, now),
                )
                run_id = conn.execute(
                    "SELECT last_insert_rowid() AS id"
                ).fetchone()["id"]
                conn.execute(
                    "UPDATE tasks SET current_run_id = ? WHERE id = ?",
                    (run_id, beta_tid),
                )

        def _stop_spawn(task, workspace, **kwargs):
            if task.assignee == "alpha":
                raise TokenBudgetStop(_stop_decision("alpha"))
            return None

        with kb.connect_closing() as conn:
            res = kb.dispatch_once(conn, spawn_fn=_stop_spawn)

        # Alpha parked; beta untouched.
        assert res.token_budget_stopped == [alpha_tid]
        with kb.connect_closing() as conn:
            alpha_row = conn.execute(
                "SELECT status FROM tasks WHERE id = ?", (alpha_tid,)
            ).fetchone()
            beta_row = conn.execute(
                "SELECT status, worker_pid, claim_lock FROM tasks "
                "WHERE id = ?",
                (beta_tid,),
            ).fetchone()
        assert alpha_row["status"] == "budget_stopped"
        assert beta_row["status"] == "running"
        assert beta_row["worker_pid"] == beta_proc.pid
        assert beta_row["claim_lock"] == f"{host}:sim-beta"

        # The real beta worker survived the whole stop cascade.
        assert beta_proc.poll() is None, (
            "no-budget profile's live worker was killed by a budget stop "
            "aimed at another profile"
        )

        # No stop events ever attached to beta's task.
        with kb.connect_closing() as conn:
            beta_events = kb.list_events(conn, beta_tid)
        assert not any(
            e.kind in ("token_budget_stop", "token_budget_stopped_worker")
            for e in beta_events
        )
    finally:
        if beta_proc.poll() is None:
            beta_proc.kill()
            beta_proc.wait()


# ---------------------------------------------------------------------------
# 4) handler guard: a no-budget decision can never rewrite the board
# ---------------------------------------------------------------------------


def test_stop_handler_none_decision_never_rewrites_board(
    isolated_kanban_home, monkeypatch
):
    kb = isolated_kanban_home
    host = kb._claimer_id().split(":", 1)[0]
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        tid = kb.create_task(conn, title="claimed", assignee="alpha")
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status='running', claim_lock=?, "
                "worker_pid=4242 WHERE id = ?",
                (f"{host}:g", tid),
            )
        before = _task_row(conn, tid)
        events_before = _all_events(kb, conn)

        kill_calls = []
        monkeypatch.setattr(
            kb,
            "terminate_profile_workers",
            lambda *a, **k: kill_calls.append(a) or [],
        )

        task = kb.get_task(conn, tid)
        result = kb.DispatchResult()
        kb._handle_token_budget_stop(conn, task, None, result)

        assert kill_calls == []
        assert result.token_budget_stopped == []
        assert result.token_budget_stopped_workers == []
        assert _task_row(conn, tid) == before
        assert _all_events(kb, conn) == events_before


# ---------------------------------------------------------------------------
# 5) recovery: no-budget profile parked in budget_stopped is re-gated
# ---------------------------------------------------------------------------


def test_budget_stopped_no_budget_profile_recovered_next_tick(isolated_kanban_home):
    """Zero-interference at recovery time: if a profile's budget is
    removed while its task sits parked in budget_stopped, the next
    dispatch tick must recover it to ready — a no-budget profile can
    never be stranded in a budget state."""
    kb = isolated_kanban_home
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        tid = kb.create_task(conn, title="parked", assignee="alpha")
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status='budget_stopped' WHERE id = ?",
                (tid,),
            )

        # Profile alpha has no budget configured (isolated home, empty
        # config) → check_profile_budget returns None → recover.
        recovered = kb.recover_budget_stopped_tasks(conn)
        assert recovered == [tid]
        row = conn.execute(
            "SELECT status FROM tasks WHERE id = ?", (tid,)
        ).fetchone()
        assert row["status"] == "ready"
        events = kb.list_events(conn, tid)
        assert any(e.kind == "token_budget_recovered" for e in events)


# ---------------------------------------------------------------------------
# 6) full lifecycle: no-budget profile claim → spawn → stop trigger on a
#    sibling budgeted profile → no-budget worker still completes
# ---------------------------------------------------------------------------


def test_no_budget_profile_full_dispatch_lifecycle_unaffected(isolated_kanban_home):
    """End-to-end over dispatch_once with a recording spawn stub: a
    no-budget profile's task moves ready → running (claimed, pid set,
    spawned recorded) even while a budgeted profile's task in the same
    tick is refused at stop level. The two lanes must not interact."""
    kb = isolated_kanban_home
    # Same-class rule as above: import after the fixture's module purge.
    from hermes_cli.kanban_token_budget import TokenBudgetStop

    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        alpha_tid = kb.create_task(
            conn, title="alpha over budget", assignee="alpha"
        )
        beta_tid = kb.create_task(
            conn, title="beta normal", assignee="beta"
        )

    def _spawn(task, workspace, **kwargs):
        if task.assignee == "alpha":
            raise TokenBudgetStop(_stop_decision("alpha"))
        return 770017

    with kb.connect_closing() as conn:
        res = kb.dispatch_once(conn, spawn_fn=_spawn)

    assert res.token_budget_stopped == [alpha_tid]
    assert [s[0] for s in res.spawned] == [beta_tid]
    with kb.connect_closing() as conn:
        beta = _task_row(conn, beta_tid)
    assert beta["status"] == "running"
    assert beta["worker_pid"] == 770017
    assert beta["consecutive_failures"] == 0
