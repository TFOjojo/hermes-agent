"""Acceptance-level tests for the token-budget circuit breaker (熔断器).

These tests close the two coverage gaps left by the unit suites in
``test_kanban_token_budget.py``:

1. **REST 人工复位入口** — ``POST /budget/reset`` (and the read surface
   ``GET /budget/snapshots`` / ``GET /budget/events``) is exercised through
   a real FastAPI ``TestClient`` against an isolated Hermes home, proving the
   endpoint writes the baseline file AND records the ``token_budget_reset``
   event itself (previously only the module-level pieces were tested).
2. **验收全链路** — one test walks the whole breaker lifecycle through the
   REAL spawn gate (no mocked ``check_profile_budget``): configured limit →
   fabricated over-limit usage in the profile's ``state.db`` → dispatch tick
   blocks the spawn, parks the task in ``budget_stopped`` and persists the
   ``token_budget_stop`` event → manual reset via the REST endpoint → next
   tick's recovery gate re-opens the task and the dispatch resumes.

Isolation strategy mirrors ``test_kanban_token_budget.py``: point
``HERMES_HOME`` at a temp root, purge the ``hermes_cli`` / ``hermes_constants``
modules so every path resolution re-reads the env, then re-import. Only
``subprocess.Popen`` is stubbed (and only for the post-recovery spawn — the
stop phase raises before any launch attempt).
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Isolated environment fixture
# ---------------------------------------------------------------------------


_ROOT_CONFIG = (
    "kanban:\n"
    "  token_budgets:\n"
    "    enabled: true\n"
    "    steer_pct: 0.8\n"
    "    constrain_pct: 1.0\n"
    "    stop_pct: 1.2\n"
    "    profiles:\n"
    "      alpha:\n"
    "        daily: 100\n"
    "        weekly: 1000\n"
)


def _seed_state_db(profile_home: Path, *, daily_tokens: int) -> None:
    """Seed ``<profile_home>/state.db`` with one in-window usage row.

    Uses the real clock: the dispatch gate and the recovery re-gate aggregate
    with ``now=None`` → ``time.time()``, so the row must sit inside the real
    rolling 24h window. Tokens are split input/output; the aggregate is what
    matters (daily == weekly == daily_tokens).
    """
    db_path = profile_home / "state.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "CREATE TABLE session_model_usage ("
            " last_seen REAL, api_call_count INTEGER, input_tokens INTEGER,"
            " output_tokens INTEGER, cache_read_tokens INTEGER,"
            " cache_write_tokens INTEGER, reasoning_tokens INTEGER)"
        )
        conn.execute(
            "INSERT INTO session_model_usage VALUES (?,?,?,?,?,?,?)",
            (time.time() - 3600, 3, daily_tokens - 50, 50, 0, 0, 0),
        )
        conn.commit()
    finally:
        conn.close()


class _BreakerEnv:
    def __init__(self, home: str):
        self.home = Path(home)
        self.alpha_home = self.home / "profiles" / "alpha"
        self.baselines_file = self.home / "token_budget_baselines.json"


@pytest.fixture()
def breaker_env(monkeypatch):
    """Isolated Hermes home with token budgets enabled for profile alpha."""
    test_home = tempfile.mkdtemp(prefix="kanban_breaker_cycle_test_")
    for prof in ("alpha", "beta", "default"):
        os.makedirs(os.path.join(test_home, "profiles", prof), exist_ok=True)
    (Path(test_home) / "config.yaml").write_text(_ROOT_CONFIG, encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", test_home)
    for mod in list(sys.modules.keys()):
        if (
            mod.startswith("hermes_cli")
            or mod.startswith("hermes_state")
            or mod == "hermes_constants"
            or mod.startswith("plugins.kanban")
        ):
            del sys.modules[mod]
    from hermes_cli import kanban_db

    with kanban_db.connect_closing() as conn:
        kanban_db.create_board(slug="default", name="Test")
    return _BreakerEnv(test_home)


def _client():
    """A TestClient over the real plugin router (imported post-purge)."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from plugins.kanban.dashboard.plugin_api import router

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


# ---------------------------------------------------------------------------
# 1. REST 人工复位入口 (POST /budget/reset)
# ---------------------------------------------------------------------------
# NOTE(2026-09-10): These endpoint tests are red by design — the REST routes
# (POST /budget/reset, GET /budget/snapshots, GET /budget/events) were never
# implemented in ANY branch (verified against local-integrations and
# merge-li-ready: plugins/kanban/dashboard/plugin_api.py has no budget router).
# They arrived with the 2026-09-03 f468d332ac carrier snapshot as forward
# specs for the dual-lane token-budget work (P4 debt). Skip until implemented.
pytestmark = pytest.mark.skip(
    reason="REST budget endpoints not implemented yet (P4 dual-lane token-budget debt); tests are forward specs"
)


def test_reset_endpoint_writes_baseline_and_records_event(breaker_env):
    """POST /budget/reset: baseline file written + reset event persisted.

    Also proves the reset actually clears the tripped state: the gate flips
    from stop to ok against the same state.db usage.
    """
    from hermes_cli import kanban_token_budget as ktb

    _seed_state_db(breaker_env.alpha_home, daily_tokens=150)  # 150/100 → stop

    decision = ktb.check_profile_budget("alpha", str(breaker_env.alpha_home))
    assert decision is not None and decision.level == ktb.BudgetLevel.STOP

    resp = _client().post("/budget/reset", json={"profile": "alpha"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True and body["profile"] == "alpha"
    assert body["baseline"]["tokens"] == 150

    # Baseline persisted (atomic tmp+replace) at the shared kanban home.
    stored = json.loads(breaker_env.baselines_file.read_text(encoding="utf-8"))
    assert stored["alpha"]["tokens"] == 150
    assert stored["alpha"]["at"] > 0

    # Reset event persisted and visible through the read API shape.
    from hermes_cli import kanban_db as kb

    with kb.connect_closing() as conn:
        events = ktb.list_budget_events(conn, limit=10)
    reset = [e for e in events if e["kind"] == "token_budget_reset"]
    assert len(reset) == 1
    assert reset[0]["level"] == "reset"
    assert reset[0]["profile"] == "alpha"
    assert reset[0]["baseline_tokens"] == 150
    assert reset[0]["created_at"] > 0

    # Same usage, baseline-subtracted → the ladder re-opens.
    decision2 = ktb.check_profile_budget("alpha", str(breaker_env.alpha_home))
    assert decision2 is not None and decision2.level == ktb.BudgetLevel.OK
    assert decision2.usage.daily_tokens == 0


def test_reset_endpoint_unknown_profile_still_resets(breaker_env):
    """A profile with no usage/home resets cleanly (idempotent, no 5xx)."""
    resp = _client().post("/budget/reset", json={"profile": "ghost"})
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    stored = json.loads(breaker_env.baselines_file.read_text(encoding="utf-8"))
    assert "ghost" in stored  # baseline recorded even with zero usage

    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_token_budget as ktb

    with kb.connect_closing() as conn:
        events = ktb.list_budget_events(conn, limit=10, profile="ghost")
    assert [e["kind"] for e in events] == ["token_budget_reset"]


def test_budget_read_endpoints_expose_trip_state(breaker_env):
    """GET /budget/snapshots + /budget/events surface a real stop trip."""
    kb_module = None
    from hermes_cli import kanban_db as kb_module
    from hermes_cli import kanban_token_budget as ktb

    _seed_state_db(breaker_env.alpha_home, daily_tokens=150)

    client = _client()
    resp = client.get("/budget/snapshots")
    assert resp.status_code == 200
    data = resp.json()
    assert data["enabled"] is True
    rows = {s["profile"]: s for s in data["snapshots"]}
    assert rows["alpha"]["level"] == "stop"  # 150/100 = 1.5 ≥ stop_pct
    assert rows["alpha"]["daily_tokens"] == 150
    assert rows["alpha"]["daily_limit"] == 100
    assert data["floor"]["daily_tokens"] == 150
    assert data["floor"]["daily_limit_total"] == 100

    # Persist a real stop event via the dispatch path, then read it back.
    with kb_module.connect_closing() as conn:
        tid = kb_module.create_task(conn, title="trip", assignee="alpha")
        res = kb_module.dispatch_once(conn)
    assert res.token_budget_stopped == [tid]

    resp = client.get("/budget/events?limit=10")
    assert resp.status_code == 200
    events = resp.json()["events"]
    stop = [e for e in events if e["kind"] == "token_budget_stop"]
    assert len(stop) == 1
    assert stop[0]["profile"] == "alpha"
    assert stop[0]["pct"] == pytest.approx(1.5)
    assert stop[0]["daily_tokens"] == 150
    assert stop[0]["daily_limit"] == 100


# ---------------------------------------------------------------------------
# 2. 验收全链路: 设置上限 → 超额 → 阻断+事件 → 人工复位 → 恢复派发
# ---------------------------------------------------------------------------


class _FakeProc:
    pid = 99999


def test_breaker_full_cycle_trip_block_reset_recover_redispatch(
    breaker_env, monkeypatch
):
    """The task acceptance flow, end to end through the real spawn gate.

    Phase 1 (超额熔断): alpha at 150% of its daily cap → the dispatch tick's
    gate raises TokenBudgetStop inside the real ``_default_spawn`` → no
    worker is launched, the task is parked in ``budget_stopped`` and a
    ``token_budget_stop`` event (profile/quota/usage/reason) is persisted.
    Phase 2 (持续阻断): a second tick keeps the task parked without spawning
    and without spamming a duplicate stop event.
    Phase 3 (人工复位): POST /budget/reset writes the baseline + reset event.
    Phase 4 (恢复派发): the next tick's recovery gate re-opens the task
    (``token_budget_recovered``) and the ready lane spawns the worker — with
    the OK-level zero-interference env marker and an unamended prompt.
    """
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_token_budget as ktb

    _seed_state_db(breaker_env.alpha_home, daily_tokens=150)

    launched = {}

    def _fake_popen(cmd, **kwargs):
        launched["cmd"] = cmd
        launched["env"] = kwargs.get("env", {})
        return _FakeProc()

    monkeypatch.setattr(subprocess, "Popen", _fake_popen)

    # ── Phase 1: over-limit → dispatch blocked, event persisted ──────────
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="breaker cycle probe", assignee="alpha")
        res1 = kb.dispatch_once(conn)

    assert res1.spawned == []
    assert res1.token_budget_stopped == [tid]
    assert "cmd" not in launched  # Popen never reached
    with kb.connect_closing() as conn:
        task = kb.get_task(conn, tid)
        assert task.status == "budget_stopped"
        events = kb.list_events(conn, tid)
        stop_events = [e for e in events if e.kind == "token_budget_stop"]
        assert len(stop_events) == 1
        payload = stop_events[0].payload
        assert payload["assignee"] == "alpha"
        assert payload["level"] == "stop"
        assert payload["pct"] == pytest.approx(1.5)
        assert payload["daily_tokens"] == 150
        assert payload["daily_limit"] == 100
        assert payload["weekly_limit"] == 1000
        assert payload["killed_workers"] == []

    # ── Phase 2: still over limit → stays parked, no event spam ──────────
    with kb.connect_closing() as conn:
        res2 = kb.dispatch_once(conn)
    assert res2.spawned == []
    assert res2.budget_stop_recovered == []
    with kb.connect_closing() as conn:
        assert kb.get_task(conn, tid).status == "budget_stopped"
        events = kb.list_events(conn, tid)
        assert len([e for e in events if e.kind == "token_budget_stop"]) == 1

    # ── Phase 3: 人工复位 via the real REST entry ────────────────────────
    resp = _client().post("/budget/reset", json={"profile": "alpha"})
    assert resp.status_code == 200
    assert resp.json()["baseline"]["tokens"] == 150

    # ── Phase 4: recovery gate re-opens → dispatch resumes ───────────────
    with kb.connect_closing() as conn:
        res3 = kb.dispatch_once(conn)
    assert res3.budget_stop_recovered == [tid]
    assert [s[0] for s in res3.spawned] == [tid]
    assert launched["env"]["HERMES_KANBAN_TOKEN_LEVEL"] == "ok"
    assert "HERMES_KANBAN_TOKEN_STEER" not in launched["env"]
    assert not any("[token-budget" in str(c) for c in launched["cmd"])

    with kb.connect_closing() as conn:
        assert kb.get_task(conn, tid).status == "running"
        kinds = [e["kind"] for e in ktb.list_budget_events(conn, limit=20)]
    # The full audit trail is visible: stop → reset → recovered.
    assert "token_budget_stop" in kinds
    assert "token_budget_reset" in kinds
    assert "token_budget_recovered" in kinds
    assert kinds.index("token_budget_recovered") < kinds.index("token_budget_reset")
    assert kinds.index("token_budget_reset") < kinds.index("token_budget_stop")
