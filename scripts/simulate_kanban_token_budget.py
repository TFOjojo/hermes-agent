#!/usr/bin/env python3
"""Simulate kanban token-budget levels by injecting usage telemetry.

Acceptance harness for the per-agent token budget (cost circuit breaker).
It seeds a fake profile ``state.db`` with injected token usage, then drives
the REAL decision / spawn-gate / enforcement code through the three levels
(steer / constrain / stop) and shows the zero-interference path for an
unconfigured profile. The event trail (level, profile, usage, timestamp) is
recorded into a scratch ``task_events`` table through the same
``record_budget_transition`` the dispatcher uses.

Usage:
    python scripts/simulate_kanban_token_budget.py \\
        --profile dsh-worker \\
        --budget-daily 1000000 \\
        --daily-tokens 1500000
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def _make_state_db(home: Path, daily_tokens: int, now: float) -> None:
    db_path = home / "state.db"
    if db_path.exists():
        db_path.unlink()
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE session_model_usage ("
        " last_seen REAL, api_call_count INTEGER, input_tokens INTEGER,"
        " output_tokens INTEGER, cache_read_tokens INTEGER, reasoning_tokens INTEGER)"
    )
    if daily_tokens:
        conn.execute(
            "INSERT INTO session_model_usage VALUES (?,?,?,?,?,?)",
            (now - 60, 1, daily_tokens, 0, 0, 0),
        )
    conn.commit()
    conn.close()


def _make_events_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE task_events ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, run_id INTEGER,"
        " kind TEXT, payload TEXT, created_at INTEGER)"
    )
    return conn


def _run_level(
    profile: str,
    home: Path,
    settings,
    *,
    daily_tokens: int,
    task_id: str,
    conn: sqlite3.Connection,
    now: float,
) -> None:
    from hermes_cli.kanban_token_budget import (
        BudgetLevel,
        TokenBudgetStop,
        apply_budget_decision,
        check_profile_budget,
        list_budget_events,
        record_budget_transition,
        tighten_turn_limit,
    )

    _make_state_db(home, daily_tokens, now)
    decision = check_profile_budget(profile, str(home), settings=settings, now=now)
    if decision is None:
        print("  level: UNCONFIGURED (bypassed, zero interference)")
        return
    print(
        f"  level: {decision.level.value}  pct={decision.pct:.2%}  "
        f"daily={decision.usage.daily_tokens}/{decision.budget.daily}"
    )
    prompt, env = "work kanban task t-sim", {}
    try:
        prompt = apply_budget_decision(decision, prompt, env)
    except TokenBudgetStop:
        print("  stop: TokenBudgetStop raised → dispatcher refuses to spawn + kills workers")
        return
    if decision.level == BudgetLevel.OK:
        assert prompt == "work kanban task t-sim", "OK level must not touch the prompt"
        assert "HERMES_KANBAN_TOKEN_STEER" not in env
        print("  ok: prompt/env untouched (zero interference)")
        return
    print(f"  env: HERMES_KANBAN_TOKEN_LEVEL={env.get('HERMES_KANBAN_TOKEN_LEVEL')!r}")
    if "HERMES_KANBAN_TOKEN_STEER" in env:
        first_line = env["HERMES_KANBAN_TOKEN_STEER"].splitlines()[0]
        print(f"  steer note injected into prompt: {first_line[:70]}...")
    if decision.level == BudgetLevel.CONSTRAIN:
        print(
            "  constrain tightening: max_turns 500 -> "
            f"{tighten_turn_limit(500, 'constrain')}, unlimited -> "
            f"{tighten_turn_limit(None, 'constrain')}, "
            f"unconfigured(steer) 500 -> {tighten_turn_limit(500, 'steer')}"
        )
    kind = record_budget_transition(conn, task_id, decision)
    print(f"  event recorded: {kind}")
    trail = [e["kind"] for e in list_budget_events(conn, limit=10)]
    print(f"  event trail (newest first): {trail}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--profile", default="sim-worker")
    ap.add_argument("--budget-daily", type=int, default=1_000_000)
    ap.add_argument("--budget-weekly", type=int, default=5_000_000)
    ap.add_argument("--daily-tokens", type=int, default=0,
                    help="inject this many daily tokens (overrides the ladder)")
    ap.add_argument("--steer-pct", type=float, default=0.80)
    ap.add_argument("--constrain-pct", type=float, default=1.00)
    ap.add_argument("--stop-pct", type=float, default=1.20)
    args = ap.parse_args()

    from hermes_cli.kanban_token_budget import TokenBudget, TokenBudgetSettings

    settings = TokenBudgetSettings(
        enabled=True,
        steer_pct=args.steer_pct,
        constrain_pct=args.constrain_pct,
        stop_pct=args.stop_pct,
        profiles={
            args.profile: TokenBudget(daily=args.budget_daily, weekly=args.budget_weekly)
        },
    )

    home = Path(tempfile.mkdtemp(prefix="kbtb_sim_"))
    home.mkdir(parents=True, exist_ok=True)
    conn = _make_events_db()
    now = time.time()

    print(f"profile={args.profile}  budget daily={args.budget_daily} "
          f"weekly={args.budget_weekly}")
    print(f"thresholds: steer={args.steer_pct:.0%} "
          f"constrain={args.constrain_pct:.0%} stop={args.stop_pct:.0%}")
    print()

    if args.daily_tokens:
        print(f"[ladder] injected usage {args.daily_tokens} tokens")
        _run_level(
            args.profile, home, settings,
            daily_tokens=args.daily_tokens,
            task_id="t-sim-injected", conn=conn, now=now,
        )
    else:
        # Full ladder: walk one scenario per level by injecting usage directly.
        print("[ladder] ok (below steer)")
        _run_level(args.profile, home, settings,
                   daily_tokens=int(args.budget_daily * 0.5),
                   task_id="t-sim-ok", conn=conn, now=now)
        print("\n[ladder] steer")
        _run_level(args.profile, home, settings,
                   daily_tokens=int(args.budget_daily * 0.9),
                   task_id="t-sim-steer", conn=conn, now=now)
        print("\n[ladder] constrain")
        _run_level(args.profile, home, settings,
                   daily_tokens=int(args.budget_daily * 1.1),
                   task_id="t-sim-constrain", conn=conn, now=now)
        print("\n[ladder] stop")
        _run_level(args.profile, home, settings,
                   daily_tokens=int(args.budget_daily * 1.5),
                   task_id="t-sim-stop", conn=conn, now=now)

    print("\n[zero interference] unconfigured profile with heavy usage")
    _run_level("no-budget-profile", home, settings,
               daily_tokens=10_000_000,
               task_id="t-sim-unconfigured", conn=conn, now=now)
    return 0


if __name__ == "__main__":
    sys.exit(main())
