"""Tests for per-agent token budget (cost circuit breaker)."""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

from hermes_cli.kanban_token_budget import (
    CONSTRAIN_TURN_FACTOR,
    CONSTRAIN_UNLIMITED_TURN_CAP,
    BudgetDecision,
    BudgetLevel,
    ProfileUsage,
    TokenBudget,
    TokenBudgetSettings,
    TokenBudgetStop,
    aggregate_profile_usage,
    apply_budget_decision,
    build_steer_note,
    check_profile_budget,
    decide,
    effective_usage,
    floor_summary,
    get_budget,
    get_usage,
    list_budget_events,
    list_budget_snapshots,
    load_budget_baselines,
    load_profile_token_budget,
    load_token_budget_settings,
    record_budget_transition,
    resolve_profile_budget,
    stop_running_workers_for_profile,
    tighten_turn_limit,
)


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------


def test_settings_disabled_by_default():
    assert load_token_budget_settings({}).enabled is False
    assert load_token_budget_settings(None).enabled is False
    assert load_token_budget_settings({"kanban": {}}).enabled is False


def test_settings_parses_profiles_and_thresholds():
    cfg = {
        "kanban": {
            "token_budgets": {
                "enabled": True,
                "steer_pct": 0.5,
                "constrain_pct": 0.9,
                "stop_pct": 1.1,
                "profiles": {
                    "dsh-worker": {"daily": 1000, "weekly": 5000},
                    "qianhua": {"daily": 500},
                    "default": {"daily": 100},
                },
            }
        }
    }
    s = load_token_budget_settings(cfg)
    assert s.enabled is True
    assert s.steer_pct == 0.5
    assert s.constrain_pct == 0.9
    assert s.stop_pct == 1.1
    assert s.budget_for("dsh-worker") == TokenBudget(daily=1000, weekly=5000)
    assert s.budget_for("qianhua") == TokenBudget(daily=500, weekly=None)
    # default fallback for unlisted profile
    assert s.budget_for("cc-worker") == TokenBudget(daily=100, weekly=None)


def test_settings_budget_for_disabled_returns_none():
    s = TokenBudgetSettings(enabled=False, profiles={"x": TokenBudget(daily=1)})
    assert s.budget_for("x") is None


def test_settings_skips_zero_budgets():
    cfg = {
        "kanban": {
            "token_budgets": {
                "enabled": True,
                "profiles": {"zero": {"daily": 0}, "ok": {"daily": 10}},
            }
        }
    }
    s = load_token_budget_settings(cfg)
    assert s.budget_for("zero") is None
    assert s.budget_for("ok") == TokenBudget(daily=10, weekly=None)


# ---------------------------------------------------------------------------
# decide()
# ---------------------------------------------------------------------------


def test_decide_ok_steer_constrain_stop():
    budget = TokenBudget(daily=100)
    assert decide(ProfileUsage(daily_tokens=0), budget).level == BudgetLevel.OK
    assert decide(ProfileUsage(daily_tokens=79), budget).level == BudgetLevel.OK
    assert decide(ProfileUsage(daily_tokens=80), budget).level == BudgetLevel.STEER
    assert decide(ProfileUsage(daily_tokens=99), budget).level == BudgetLevel.STEER
    assert decide(ProfileUsage(daily_tokens=100), budget).level == BudgetLevel.CONSTRAIN
    assert decide(ProfileUsage(daily_tokens=119), budget).level == BudgetLevel.CONSTRAIN
    assert decide(ProfileUsage(daily_tokens=120), budget).level == BudgetLevel.STOP


def test_decide_higher_of_daily_weekly_wins():
    budget = TokenBudget(daily=100, weekly=1000)
    # daily 90% (> steer) vs weekly 50% (< steer) → STEER
    assert (
        decide(ProfileUsage(daily_tokens=90, weekly_tokens=500), budget).level
        == BudgetLevel.STEER
    )
    # daily 50% vs weekly 120% → STOP
    assert (
        decide(ProfileUsage(daily_tokens=50, weekly_tokens=1200), budget).level
        == BudgetLevel.STOP
    )


def test_decide_single_axis():
    # only weekly configured → daily ratio never contributes
    budget = TokenBudget(daily=None, weekly=100)
    assert (
        decide(ProfileUsage(daily_tokens=999999, weekly_tokens=50), budget).level
        == BudgetLevel.OK
    )


# ---------------------------------------------------------------------------
# steer note + apply_budget_decision
# ---------------------------------------------------------------------------


def test_build_steer_note_contains_numbers():
    d = decide(
        ProfileUsage(daily_tokens=90, weekly_tokens=500),
        TokenBudget(daily=100, weekly=1000),
        profile="dsh-worker",
    )
    note = build_steer_note(d)
    assert "token-budget steer" in note
    assert "dsh-worker" in note
    assert "90" in note and "100" in note


def test_build_steer_note_constrain():
    d = decide(
        ProfileUsage(daily_tokens=110), TokenBudget(daily=100), profile="qianhua"
    )
    note = build_steer_note(d, constrain=True)
    assert "token-budget constrain" in note
    assert "OVER" in note


def test_apply_budget_decision_steer_amends_prompt_and_env():
    d = decide(ProfileUsage(daily_tokens=85), TokenBudget(daily=100), profile="alpha")
    env = {}
    prompt = apply_budget_decision(d, "work kanban task t1", env)
    assert prompt.startswith("work kanban task t1\n\n")
    assert env["HERMES_KANBAN_TOKEN_LEVEL"] == "steer"
    assert "token-budget steer" in env["HERMES_KANBAN_TOKEN_STEER"]


def test_apply_budget_decision_stop_raises():
    d = decide(ProfileUsage(daily_tokens=150), TokenBudget(daily=100), profile="alpha")
    with pytest.raises(TokenBudgetStop):
        apply_budget_decision(d, "work kanban task t1", {})


def test_apply_budget_decision_ok_leaves_prompt_and_env_untouched():
    """Regression for design §4.5: OK must not amend the prompt or set the
    steer note — only the observable level marker is allowed."""
    d = decide(ProfileUsage(daily_tokens=0), TokenBudget(daily=100), profile="alpha")
    env = {}
    prompt = apply_budget_decision(d, "work kanban task t1", env)
    assert prompt == "work kanban task t1"
    assert env["HERMES_KANBAN_TOKEN_LEVEL"] == "ok"
    assert "HERMES_KANBAN_TOKEN_STEER" not in env


# ---------------------------------------------------------------------------
# aggregation
# ---------------------------------------------------------------------------


def _make_state_db(path, now):
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE session_model_usage ("
        " last_seen REAL, api_call_count INTEGER, input_tokens INTEGER,"
        " output_tokens INTEGER, cache_read_tokens INTEGER, reasoning_tokens INTEGER)"
    )
    rows = [
        (now - 3600, 10, 100, 50, 0, 0),  # within daily + weekly → 150
        (now - 2 * 86400, 5, 200, 100, 0, 0),  # within weekly only → 300
        (now - 30 * 86400, 1, 1000, 1000, 0, 0),  # outside both → excluded
    ]
    conn.executemany("INSERT INTO session_model_usage VALUES (?,?,?,?,?,?)", rows)
    conn.commit()
    conn.close()


def test_aggregate_profile_usage_windows(tmp_path):
    now = 2_000_000_000.0
    _make_state_db(tmp_path / "state.db", now)
    usage = aggregate_profile_usage(tmp_path, now=now)
    assert usage.daily_tokens == 150
    assert usage.weekly_tokens == 450  # 150 + 300
    assert usage.daily_api_calls == 10
    assert usage.weekly_api_calls == 15


def test_aggregate_profile_usage_includes_cache_and_reasoning(tmp_path):
    now = 2_000_000_000.0
    conn = sqlite3.connect(tmp_path / "state.db")
    conn.execute(
        "CREATE TABLE session_model_usage ("
        " last_seen REAL, api_call_count INTEGER, input_tokens INTEGER,"
        " output_tokens INTEGER, cache_read_tokens INTEGER, reasoning_tokens INTEGER)"
    )
    conn.execute(
        "INSERT INTO session_model_usage VALUES (?,?,?,?,?,?)",
        (now - 10, 1, 100, 50, 30, 20),
    )
    conn.commit()
    conn.close()
    assert aggregate_profile_usage(tmp_path, now=now).daily_tokens == 200


def test_aggregate_profile_usage_includes_cache_write_tokens(tmp_path):
    """G1: cache_write_tokens counts toward the rolling usage total."""
    now = 2_000_000_000.0
    conn = sqlite3.connect(tmp_path / "state.db")
    conn.execute(
        "CREATE TABLE session_model_usage ("
        " last_seen REAL, api_call_count INTEGER, input_tokens INTEGER,"
        " output_tokens INTEGER, cache_read_tokens INTEGER,"
        " cache_write_tokens INTEGER, reasoning_tokens INTEGER)"
    )
    conn.execute(
        "INSERT INTO session_model_usage VALUES (?,?,?,?,?,?,?)",
        (now - 10, 1, 100, 50, 30, 40, 20),
    )
    conn.commit()
    conn.close()
    # 100 + 50 + 30 (cache read) + 40 (cache write) + 20 (reasoning) = 240
    assert aggregate_profile_usage(tmp_path, now=now).daily_tokens == 240


def test_aggregate_profile_usage_cache_write_null_coerced_to_zero(tmp_path):
    """G1: rows written before the column existed (NULL cache_write) still
    aggregate with the legacy five-token total."""
    now = 2_000_000_000.0
    conn = sqlite3.connect(tmp_path / "state.db")
    conn.execute(
        "CREATE TABLE session_model_usage ("
        " last_seen REAL, api_call_count INTEGER, input_tokens INTEGER,"
        " output_tokens INTEGER, cache_read_tokens INTEGER,"
        " cache_write_tokens INTEGER, reasoning_tokens INTEGER)"
    )
    conn.execute(
        "INSERT INTO session_model_usage VALUES (?,?,?,?,?,?,?)",
        (now - 10, 1, 100, 50, 30, None, 20),
    )
    conn.commit()
    conn.close()
    assert aggregate_profile_usage(tmp_path, now=now).daily_tokens == 200


def test_aggregate_profile_usage_legacy_schema_skips_cache_write(tmp_path):
    """G1 fallback: an older state.db without the cache_write_tokens column
    keeps aggregating (legacy total) instead of degrading to empty usage."""
    now = 2_000_000_000.0
    conn = sqlite3.connect(tmp_path / "state.db")
    conn.execute(
        "CREATE TABLE session_model_usage ("
        " last_seen REAL, api_call_count INTEGER, input_tokens INTEGER,"
        " output_tokens INTEGER, cache_read_tokens INTEGER, reasoning_tokens INTEGER)"
    )
    conn.execute(
        "INSERT INTO session_model_usage VALUES (?,?,?,?,?,?)",
        (now - 10, 1, 100, 50, 30, 20),
    )
    conn.commit()
    conn.close()
    usage = aggregate_profile_usage(tmp_path, now=now)
    assert usage.daily_tokens == 200
    assert usage.daily_api_calls == 1



def test_aggregate_profile_usage_missing_db_returns_empty(tmp_path):
    assert aggregate_profile_usage(tmp_path) == ProfileUsage()


def _make_single_row_db(path, now, last_seen, calls=1, tokens=100):
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE session_model_usage ("
        " last_seen REAL, api_call_count INTEGER, input_tokens INTEGER,"
        " output_tokens INTEGER, cache_read_tokens INTEGER, reasoning_tokens INTEGER)"
    )
    conn.execute(
        "INSERT INTO session_model_usage VALUES (?,?,?,?,?,?)",
        (last_seen, calls, tokens, 0, 0, 0),
    )
    conn.commit()
    conn.close()


def test_aggregate_profile_usage_daily_boundary_inclusive(tmp_path):
    """Rolling windows are `last_seen >= now - window`, so a row exactly at
    the cut belongs to the window; one tick before does not (cross-day)."""
    now = 2_000_000_000.0
    _make_single_row_db(
        tmp_path / "state.db", now, last_seen=now - 24 * 3600, tokens=100
    )
    usage = aggregate_profile_usage(tmp_path, now=now)
    assert usage.daily_tokens == 100
    assert usage.weekly_tokens == 100


def test_aggregate_profile_usage_daily_boundary_exclusive_just_before(tmp_path):
    now = 2_000_000_000.0
    _make_single_row_db(
        tmp_path / "state.db", now, last_seen=now - 24 * 3600 - 1, tokens=100
    )
    usage = aggregate_profile_usage(tmp_path, now=now)
    assert usage.daily_tokens == 0
    assert usage.weekly_tokens == 100  # still inside the 7d window


def test_aggregate_profile_usage_weekly_boundary_inclusive(tmp_path):
    now = 2_000_000_000.0
    _make_single_row_db(
        tmp_path / "state.db", now, last_seen=now - 7 * 24 * 3600, tokens=100
    )
    usage = aggregate_profile_usage(tmp_path, now=now)
    assert usage.daily_tokens == 0
    assert usage.weekly_tokens == 100


def test_aggregate_profile_usage_weekly_boundary_exclusive_just_before(tmp_path):
    """A session last seen just before the 7d cut (cross-week) is excluded
    from both windows."""
    now = 2_000_000_000.0
    _make_single_row_db(
        tmp_path / "state.db", now, last_seen=now - 7 * 24 * 3600 - 1, tokens=100
    )
    usage = aggregate_profile_usage(tmp_path, now=now)
    assert usage.daily_tokens == 0
    assert usage.weekly_tokens == 0
    assert usage.weekly_api_calls == 0


def test_aggregate_profile_usage_missing_columns_degrades_to_empty(tmp_path):
    """A schema without cache/reasoning columns is a read failure → the
    dispatcher degrades to empty usage rather than raising (§3.3)."""
    now = 2_000_000_000.0
    conn = sqlite3.connect(tmp_path / "state.db")
    conn.execute(
        "CREATE TABLE session_model_usage ("
        " last_seen REAL, api_call_count INTEGER, input_tokens INTEGER,"
        " output_tokens INTEGER)"
    )
    conn.execute(
        "INSERT INTO session_model_usage VALUES (?,?,?,?)",
        (now - 10, 2, 60, 40),
    )
    conn.commit()
    conn.close()
    assert aggregate_profile_usage(tmp_path, now=now) == ProfileUsage()


# ---------------------------------------------------------------------------
# check_profile_budget (the gate)
# ---------------------------------------------------------------------------


def test_check_profile_budget_disabled_returns_none(tmp_path):
    s = TokenBudgetSettings(enabled=False, profiles={"alpha": TokenBudget(daily=1)})
    assert check_profile_budget("alpha", tmp_path, settings=s) is None


def test_check_profile_budget_no_budget_returns_none(tmp_path):
    s = TokenBudgetSettings(enabled=True, profiles={})
    assert check_profile_budget("alpha", tmp_path, settings=s) is None


def test_check_profile_budget_returns_level(tmp_path):
    now = 2_000_000_000.0
    _make_state_db(tmp_path / "state.db", now)
    s = TokenBudgetSettings(enabled=True, profiles={"alpha": TokenBudget(daily=200)})
    decision = check_profile_budget("alpha", tmp_path, settings=s, now=now)
    assert decision is not None
    # 150 tokens vs daily 200 → 0.75 → below steer (0.80) → OK
    assert decision.level == BudgetLevel.OK
    assert decision.usage.daily_tokens == 150


# ---------------------------------------------------------------------------
# Per-profile config layer + merge order (design §2.1 / §6 #1)
# ---------------------------------------------------------------------------


def _write_profile_config(profile_home, text):
    (Path(profile_home) / "config.yaml").write_text(text, encoding="utf-8")


def test_load_profile_token_budget_reads_profile_config(tmp_path):
    _write_profile_config(
        tmp_path,
        "kanban:\n  token_budgets:\n    daily: 2000000\n    weekly: 8000000\n",
    )
    assert load_profile_token_budget(tmp_path) == TokenBudget(
        daily=2000000, weekly=8000000
    )


def test_load_profile_token_budget_missing_or_empty_returns_none(tmp_path):
    # no config.yaml at all
    assert load_profile_token_budget(tmp_path) is None
    # config.yaml present but no kanban.token_budgets section
    _write_profile_config(tmp_path, "model:\n  default: something\n")
    assert load_profile_token_budget(tmp_path) is None
    # section present but zero caps → treated as "no budget"
    _write_profile_config(tmp_path, "kanban:\n  token_budgets:\n    daily: 0\n")
    assert load_profile_token_budget(tmp_path) is None
    # unparseable YAML must not raise
    _write_profile_config(tmp_path, "kanban: [unclosed\n")
    assert load_profile_token_budget(tmp_path) is None


def test_check_profile_budget_profile_config_beats_root_profiles_name(tmp_path):
    """Merge order: profile config.yaml > root profiles[name] (§2.1)."""
    now = 2_000_000_000.0
    _make_state_db(tmp_path / "state.db", now)  # daily usage = 150
    _write_profile_config(
        tmp_path,
        "kanban:\n  token_budgets:\n    daily: 1000\n    weekly: 5000\n",
    )
    # Root config lists alpha with a much tighter daily cap: 150/200 → steer
    # under root-only merging, but the profile's own config must win (150/1000
    # → ok).
    s = TokenBudgetSettings(enabled=True, profiles={"alpha": TokenBudget(daily=200)})
    decision = check_profile_budget("alpha", tmp_path, settings=s, now=now)
    assert decision is not None
    assert decision.level == BudgetLevel.OK
    assert decision.budget == TokenBudget(daily=1000, weekly=5000)


def test_check_profile_budget_profile_config_wins_even_over_root_default(tmp_path):
    now = 2_000_000_000.0
    _make_state_db(tmp_path / "state.db", now)  # daily usage = 150
    _write_profile_config(tmp_path, "kanban:\n  token_budgets:\n    daily: 150\n")
    # Root default is generous (150/1000 → ok); profile config (150/150 →
    # constrain) must win.
    s = TokenBudgetSettings(enabled=True, profiles={"default": TokenBudget(daily=1000)})
    decision = check_profile_budget("gamma", tmp_path, settings=s, now=now)
    assert decision is not None
    assert decision.budget == TokenBudget(daily=150, weekly=None)
    assert decision.level == BudgetLevel.CONSTRAIN


def test_check_profile_budget_root_profiles_name_fallback(tmp_path):
    """No profile config.yaml → root profiles[name] applies."""
    now = 2_000_000_000.0
    _make_state_db(tmp_path / "state.db", now)
    s = TokenBudgetSettings(enabled=True, profiles={"alpha": TokenBudget(daily=100)})
    decision = check_profile_budget("alpha", tmp_path, settings=s, now=now)
    assert decision is not None
    assert decision.budget == TokenBudget(daily=100)
    assert decision.level == BudgetLevel.STOP  # 150/100 = 1.5 ≥ 1.2


def test_check_profile_budget_root_default_fallback(tmp_path):
    """No profile config.yaml, no profiles[name] → profiles.default applies."""
    now = 2_000_000_000.0
    _make_state_db(tmp_path / "state.db", now)
    s = TokenBudgetSettings(enabled=True, profiles={"default": TokenBudget(daily=100)})
    decision = check_profile_budget("unlisted", tmp_path, settings=s, now=now)
    assert decision is not None
    assert decision.budget == TokenBudget(daily=100)


def test_check_profile_budget_no_budget_anywhere_bypasses(tmp_path):
    """Profile config.yaml exists but lacks token_budgets; root has neither
    name nor default → None (zero interference)."""
    now = 2_000_000_000.0
    _make_state_db(tmp_path / "state.db", now)
    _write_profile_config(tmp_path, "model:\n  default: something\n")
    s = TokenBudgetSettings(enabled=True, profiles={})
    assert check_profile_budget("alpha", tmp_path, settings=s, now=now) is None


def test_check_profile_budget_hot_reload(tmp_path):
    """Per-profile config is re-read per spawn (no process-level staleness)."""
    now = 2_000_000_000.0
    _make_state_db(tmp_path / "state.db", now)  # daily usage = 150
    s = TokenBudgetSettings(enabled=True, profiles={})
    _write_profile_config(tmp_path, "kanban:\n  token_budgets:\n    daily: 1000\n")
    first = check_profile_budget("alpha", tmp_path, settings=s, now=now)
    assert first.budget == TokenBudget(daily=1000, weekly=None)
    assert first.level == BudgetLevel.OK
    # Tighten the cap on disk without any cache invalidation call.
    _write_profile_config(tmp_path, "kanban:\n  token_budgets:\n    daily: 100\n")
    second = check_profile_budget("alpha", tmp_path, settings=s, now=now)
    assert second.budget == TokenBudget(daily=100, weekly=None)
    assert second.level == BudgetLevel.STOP  # 150/100 = 1.5 ≥ 1.2


# ---------------------------------------------------------------------------
# get_usage / get_budget public API (design §6 #2)
# ---------------------------------------------------------------------------


def test_get_usage_with_explicit_home(tmp_path):
    now = 2_000_000_000.0
    _make_state_db(tmp_path / "state.db", now)
    usage = get_usage("alpha", profile_home=tmp_path, now=now)
    assert usage == ProfileUsage(
        daily_tokens=150, weekly_tokens=450, daily_api_calls=10, weekly_api_calls=15
    )


def test_get_usage_unknown_profile_returns_empty():
    # resolve_profile_env raises for a nonexistent profile → degraded empty
    assert get_usage("no-such-profile-xyz") == ProfileUsage()


def test_get_usage_missing_db_returns_empty(tmp_path):
    assert get_usage("alpha", profile_home=tmp_path) == ProfileUsage()


def test_get_budget_full_merge_order(tmp_path):
    _write_profile_config(
        tmp_path,
        "kanban:\n  token_budgets:\n    daily: 42\n",
    )
    s = TokenBudgetSettings(
        enabled=True,
        profiles={
            "alpha": TokenBudget(daily=100),
            "default": TokenBudget(daily=1000),
        },
    )
    # profile config layer wins
    assert get_budget("alpha", profile_home=tmp_path, settings=s) == TokenBudget(
        daily=42, weekly=None
    )


def test_get_budget_disabled_returns_none(tmp_path):
    _write_profile_config(tmp_path, "kanban:\n  token_budgets:\n    daily: 42\n")
    s = TokenBudgetSettings(enabled=False, profiles={})
    assert get_budget("alpha", profile_home=tmp_path, settings=s) is None


def test_get_budget_no_config_returns_none(tmp_path):
    s = TokenBudgetSettings(enabled=True, profiles={})
    assert get_budget("alpha", profile_home=tmp_path, settings=s) is None


def test_resolve_profile_budget_merge_order_direct(tmp_path):
    home_alpha = tmp_path / "alpha"
    home_alpha.mkdir()
    _write_profile_config(
        home_alpha,
        "kanban:\n  token_budgets:\n    weekly: 777\n",
    )
    s = TokenBudgetSettings(
        enabled=True,
        profiles={"alpha": TokenBudget(daily=100), "default": TokenBudget(daily=999)},
    )
    # profile config layer wins over root profiles[name]
    assert resolve_profile_budget("alpha", home_alpha, s) == TokenBudget(
        daily=None, weekly=777
    )
    # beta has no profile config → root profiles[beta] missing → default
    assert resolve_profile_budget("beta", tmp_path, s) == TokenBudget(daily=999)
    # disabled → None regardless of layers
    assert resolve_profile_budget("alpha", home_alpha, TokenBudgetSettings()) is None


def test_list_budget_snapshots_disabled(tmp_path):
    s = TokenBudgetSettings(enabled=False, profiles={"a": TokenBudget(daily=1)})
    assert list_budget_snapshots({"a": str(tmp_path)}, settings=s) == []


def test_list_budget_snapshots_shape(tmp_path):
    now = 2_000_000_000.0
    _make_state_db(tmp_path / "state.db", now)
    s = TokenBudgetSettings(enabled=True, profiles={"alpha": TokenBudget(daily=100)})
    snaps = list_budget_snapshots({"alpha": str(tmp_path)}, settings=s, now=now)
    assert len(snaps) == 1
    assert snaps[0]["profile"] == "alpha"
    assert snaps[0]["level"] == "stop"  # 150/100 = 1.5 → stop
    assert snaps[0]["daily_tokens"] == 150
    assert snaps[0]["daily_limit"] == 100


# ---------------------------------------------------------------------------
# floor_summary (budget-breaker integration G2)
# ---------------------------------------------------------------------------


def test_floor_summary_sums_tokens_calls_and_profiles():
    snaps = [
        {"profile": "a", "daily_tokens": 100, "weekly_tokens": 400,
         "daily_api_calls": 10, "weekly_api_calls": 40,
         "daily_limit": 1000, "weekly_limit": 5000},
        {"profile": "b", "daily_tokens": 50, "weekly_tokens": 200,
         "daily_api_calls": 5, "weekly_api_calls": 20,
         "daily_limit": 1000, "weekly_limit": 5000},
    ]
    f = floor_summary(snaps)
    assert f["profiles"] == 2
    assert f["daily_tokens"] == 150
    assert f["weekly_tokens"] == 600
    assert f["daily_api_calls"] == 15
    assert f["weekly_api_calls"] == 60
    assert f["daily_limit_total"] == 2000
    assert f["weekly_limit_total"] == 10000


def test_floor_summary_empty():
    assert floor_summary([]) == {
        "profiles": 0,
        "daily_tokens": 0,
        "weekly_tokens": 0,
        "daily_api_calls": 0,
        "weekly_api_calls": 0,
        "daily_limit_total": None,
        "weekly_limit_total": None,
    }


def test_floor_summary_unlimited_axis_stays_none():
    """An axis nobody configures stays None (rendered ∞), not a fake 0."""
    snaps = [
        {"profile": "a", "daily_tokens": 100, "weekly_tokens": 400,
         "daily_api_calls": 10, "weekly_api_calls": 40,
         "daily_limit": 1000, "weekly_limit": None},
    ]
    f = floor_summary(snaps)
    assert f["daily_limit_total"] == 1000
    assert f["weekly_limit_total"] is None


def test_floor_summary_mixed_limits_sum_configured_only():
    snaps = [
        {"profile": "a", "daily_tokens": 100, "weekly_tokens": 400,
         "daily_api_calls": 10, "weekly_api_calls": 40,
         "daily_limit": 1000, "weekly_limit": 5000},
        {"profile": "b", "daily_tokens": 50, "weekly_tokens": 200,
         "daily_api_calls": 5, "weekly_api_calls": 20,
         "daily_limit": None, "weekly_limit": None},
    ]
    f = floor_summary(snaps)
    assert f["daily_tokens"] == 150
    assert f["daily_limit_total"] == 1000
    assert f["weekly_limit_total"] == 5000



# ---------------------------------------------------------------------------
# stop_running_workers_for_profile + dispatcher integration
# ---------------------------------------------------------------------------


@pytest.fixture()
def isolated_kanban_home(monkeypatch):
    test_home = tempfile.mkdtemp(prefix="kanban_token_budget_test_")
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


def test_stop_running_workers_for_profile_reclaims(isolated_kanban_home):
    kb = isolated_kanban_home
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        a = kb.create_task(conn, title="running alpha", assignee="alpha")
        b = kb.create_task(conn, title="running beta", assignee="beta")
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status='running', claim_lock='h:1', "
                "worker_pid=12345 WHERE id IN (?, ?)",
                (a, b),
            )
        killed = stop_running_workers_for_profile(
            conn, "alpha", signal_fn=lambda *a, **k: None
        )
        assert killed == [a]
        # alpha reclaimed (not running), beta untouched
        assert kb.get_task(conn, a).status != "running"
        assert kb.get_task(conn, b).status == "running"


def test_dispatch_stop_releases_and_records(isolated_kanban_home):
    kb = isolated_kanban_home
    # Re-import AFTER the fixture purges hermes_cli modules, so the raised
    # exception is the same class kanban_db._dispatch_once_locked catches.
    from hermes_cli.kanban_token_budget import TokenBudgetStop

    decision = BudgetDecision(
        profile="alpha",
        level=BudgetLevel.STOP,
        pct=1.5,
        usage=ProfileUsage(daily_tokens=150),
        budget=TokenBudget(daily=100),
    )

    def _stop_spawn(*args, **kwargs):
        raise TokenBudgetStop(decision)

    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        tid = kb.create_task(conn, title="over budget", assignee="alpha")
        res = kb.dispatch_once(conn, spawn_fn=_stop_spawn)

    assert res.token_budget_stopped == [tid]
    assert res.spawned == []
    with kb.connect_closing() as conn:
        task = kb.get_task(conn, tid)
        assert task.status != "running"
        events = kb.list_events(conn, tid)
        assert any(e.kind == "token_budget_stop" for e in events)


def test_dispatch_normal_zero_interference(isolated_kanban_home):
    """A normal spawn_fn is unaffected — no token_budget_stopped, no event."""
    kb = isolated_kanban_home
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        tid = kb.create_task(conn, title="normal work", assignee="alpha")
        res = kb.dispatch_once(conn, spawn_fn=lambda *a, **k: 4242)
    assert res.token_budget_stopped == []
    assert [s[0] for s in res.spawned] == [tid]
    assert [s[1] for s in res.spawned] == ["alpha"]
    with kb.connect_closing() as conn:
        events = kb.list_events(conn, tid)
        assert not any(e.kind == "token_budget_stop" for e in events)


# ---------------------------------------------------------------------------
# stop → budget_stopped parking + worker-process termination detail
# ---------------------------------------------------------------------------


def test_dispatch_stop_parks_task_in_budget_stopped(isolated_kanban_home):
    """A stop-level trigger parks the task in budget_stopped, not ready."""
    kb = isolated_kanban_home
    # Re-import AFTER the fixture purges hermes_cli modules, so the raised
    # exception is the same class kanban_db._dispatch_once_locked catches.
    from hermes_cli.kanban_token_budget import TokenBudgetStop as TBS

    decision = BudgetDecision(
        profile="alpha",
        level=BudgetLevel.STOP,
        pct=1.5,
        usage=ProfileUsage(daily_tokens=150),
        budget=TokenBudget(daily=100),
    )

    def _stop_spawn(*args, **kwargs):
        raise TBS(decision)

    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        tid = kb.create_task(conn, title="over budget", assignee="alpha")
        res = kb.dispatch_once(conn, spawn_fn=_stop_spawn)

    assert res.token_budget_stopped == [tid]
    with kb.connect_closing() as conn:
        task = kb.get_task(conn, tid)
        assert task.status == "budget_stopped"
        run = conn.execute(
            "SELECT outcome, status FROM task_runs WHERE task_id = ? "
            "ORDER BY id DESC LIMIT 1",
            (tid,),
        ).fetchone()
        assert run["outcome"] == "budget_stopped"
        assert run["status"] == "budget_stopped"
        events = kb.list_events(conn, tid)
        stop_ev = [e for e in events if e.kind == "token_budget_stop"][-1]
        payload = stop_ev.payload  # list_events parses payload to a dict
        assert payload["level"] == "stop"
        assert payload["killed_workers"] == []  # no other running workers
        assert payload["workers"] == []


def test_handle_token_budget_stop_early_return_guard(
    isolated_kanban_home, monkeypatch
):
    """No-budget (None) and non-stop decisions exit before any write or kill.

    The process-management path must be byte-for-byte identical to the
    pre-feature behavior for profiles without a configured budget — the
    early-return guard in ``_handle_token_budget_stop`` makes that
    unreachability explicit and locally testable.
    """
    kb = isolated_kanban_home
    host = kb._claimer_id().split(":", 1)[0]
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        tid = kb.create_task(conn, title="claimed task", assignee="alpha")
        other = kb.create_task(conn, title="running sibling", assignee="alpha")
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status='running', claim_lock=?, "
                "worker_pid=4242 WHERE id = ?",
                (f"{host}:1", other),
            )
        before = conn.execute(
            "SELECT status FROM tasks WHERE id = ?", (tid,)
        ).fetchone()
        task = kb.get_task(conn, tid)
        result = kb.DispatchResult()
        calls = []
        monkeypatch.setattr(
            kb,
            "terminate_profile_workers",
            lambda *a, **k: calls.append((a, k)) or [],
        )

        # 1) No budget configured → decision is None: full early return.
        kb._handle_token_budget_stop(conn, task, None, result)
        # 2) A non-stop level (steer) must not reach the kill path either.
        steer = BudgetDecision(
            profile="alpha",
            level=BudgetLevel.STEER,
            pct=0.9,
            usage=ProfileUsage(daily_tokens=90),
            budget=TokenBudget(daily=100),
        )
        kb._handle_token_budget_stop(conn, task, steer, result)

        assert calls == []
        assert result.token_budget_stopped == []
        assert result.token_budget_stopped_workers == []
        # No board write on the triggering task, no stop event.
        assert conn.execute(
            "SELECT status FROM tasks WHERE id = ?", (tid,)
        ).fetchone() == before
        # The running sibling is untouched (no kill attempted).
        row = conn.execute(
            "SELECT status, worker_pid FROM tasks WHERE id = ?", (other,)
        ).fetchone()
        assert row["status"] == "running"
        assert row["worker_pid"] == 4242
        events = kb.list_events(conn, tid)
        assert not any(
            e.kind in ("token_budget_stop", "token_budget_stopped_worker")
            for e in events
        )


def test_terminate_profile_workers_detail_and_parking(
    isolated_kanban_home, monkeypatch
):
    """terminate_profile_workers parks the profile's workers and reports
    structured termination detail (pid, mode, liveness, orphan sweep)."""
    kb = isolated_kanban_home
    host = kb._claimer_id().split(":", 1)[0]
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: False)
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        a = kb.create_task(conn, title="running alpha", assignee="alpha")
        b = kb.create_task(conn, title="running beta", assignee="beta")
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status='running', claim_lock=?, "
                "worker_pid=12345 WHERE id = ?",
                (f"{host}:777", a),
            )
            conn.execute(
                "UPDATE tasks SET status='running', claim_lock=?, "
                "worker_pid=12346 WHERE id = ?",
                (f"{host}:778", b),
            )
        details = kb.terminate_profile_workers(
            conn, "alpha", signal_fn=lambda *a, **k: None
        )
    assert len(details) == 1
    d = details[0]
    assert d["task_id"] == a
    assert d["pid"] == 12345
    assert d["termination_mode"] == "graceful"
    assert d["alive_after"] is False
    assert d["survived"] is False
    assert d["marked_stopped"] is True
    assert d["orphans_found"] == []
    with kb.connect_closing() as conn:
        assert kb.get_task(conn, a).status == "budget_stopped"
        assert kb.get_task(conn, b).status == "running"  # beta untouched
        events = kb.list_events(conn, a)
        assert any(e.kind == "token_budget_stopped_worker" for e in events)


def test_terminate_profile_workers_survivor_keeps_claim(
    isolated_kanban_home, monkeypatch
):
    """A worker that survives termination keeps its claim (no duplicate
    spawn) and its task is NOT parked."""
    kb = isolated_kanban_home
    host = kb._claimer_id().split(":", 1)[0]
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(kb.time, "sleep", lambda s: None)
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        a = kb.create_task(conn, title="stubborn worker", assignee="alpha")
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status='running', claim_lock=?, "
                "worker_pid=12345 WHERE id = ?",
                (f"{host}:777", a),
            )
        details = kb.terminate_profile_workers(
            conn, "alpha", signal_fn=lambda *a, **k: None
        )
    d = details[0]
    assert d["survived"] is True
    assert d["marked_stopped"] is False
    with kb.connect_closing() as conn:
        t = kb.get_task(conn, a)
        assert t.status == "running"
        assert t.claim_lock == f"{host}:777"


def test_terminate_worker_orphan_sweep(isolated_kanban_home, monkeypatch):
    """The termination ladder sweeps orphaned descendants of a dead worker:
    children still parented to the worker are left alone, reparented ones
    are SIGTERMed and confirmed gone."""
    kb = isolated_kanban_home
    host = kb._claimer_id().split(":", 1)[0]

    class _FakeChild:
        def __init__(self, pid, ppid, running=True):
            self.pid = pid
            self._ppid = ppid
            self._running = running

        def is_running(self):
            return self._running

        def ppid(self):
            return self._ppid

    keep = _FakeChild(2001, 12345)   # still parented → not an orphan
    orphan = _FakeChild(2002, 1)     # reparented after parent died
    monkeypatch.setattr(
        kb, "_snapshot_worker_children", lambda pid: [keep, orphan]
    )
    alive = {12345: True, 2001: True, 2002: True}
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: bool(alive.get(pid)))
    signals: list[tuple] = []

    def _kill(pid, sig):
        signals.append((pid, sig))
        alive[pid] = False

    termination = kb._terminate_reclaimed_worker(
        12345, f"{host}:777", signal_fn=_kill
    )
    assert termination["termination_mode"] == "graceful"
    assert termination["terminated"] is True
    assert termination["alive_after"] is False
    assert termination["orphans_found"] == [2002]
    assert termination["orphans_terminated"] == [2002]
    assert termination["orphans_alive"] == []
    assert (12345, 15) in signals      # SIGTERM to the worker
    assert (2002, 15) in signals       # SIGTERM to the orphan
    assert not any(s[0] == 2001 for s in signals)  # kept child untouched


# ---------------------------------------------------------------------------
# budget_stopped recovery (re-gate on budget roll-over)
# ---------------------------------------------------------------------------


def test_recover_budget_stopped_no_budget_profile(
    isolated_kanban_home, monkeypatch
):
    """A parked task whose profile has NO budget recovers immediately
    (zero-interference contract: unbudgeted profiles can't stay parked)."""
    kb = isolated_kanban_home
    monkeypatch.setattr(
        "hermes_cli.kanban_token_budget.check_profile_budget",
        lambda profile, home, **kw: None,
    )
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        tid = kb.create_task(conn, title="parked", assignee="alpha")
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status='budget_stopped' WHERE id = ?",
                (tid,),
            )
        recovered = kb.recover_budget_stopped_tasks(conn)
    assert recovered == [tid]
    with kb.connect_closing() as conn:
        assert kb.get_task(conn, tid).status == "ready"
        events = kb.list_events(conn, tid)
        assert any(e.kind == "token_budget_recovered" for e in events)


def test_recover_budget_stopped_still_stop_keeps_parked(
    isolated_kanban_home, monkeypatch
):
    """A parked task whose profile is STILL at stop level stays parked."""
    kb = isolated_kanban_home
    decision = BudgetDecision(
        profile="alpha",
        level=BudgetLevel.STOP,
        pct=1.5,
        usage=ProfileUsage(daily_tokens=150),
        budget=TokenBudget(daily=100),
    )
    monkeypatch.setattr(
        "hermes_cli.kanban_token_budget.check_profile_budget",
        lambda profile, home, **kw: decision,
    )
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        tid = kb.create_task(conn, title="parked", assignee="alpha")
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status='budget_stopped' WHERE id = ?",
                (tid,),
            )
        recovered = kb.recover_budget_stopped_tasks(conn)
    assert recovered == []
    with kb.connect_closing() as conn:
        assert kb.get_task(conn, tid).status == "budget_stopped"


def test_recover_budget_stopped_zero_interference(isolated_kanban_home):
    """No parked tasks → the recovery scan is a cheap no-op returning []."""
    kb = isolated_kanban_home
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        kb.create_task(conn, title="normal", assignee="alpha")
        assert kb.recover_budget_stopped_tasks(conn) == []


def test_dispatch_tick_recovers_parked_tasks(isolated_kanban_home, monkeypatch):
    """The dispatch tick re-gates parked tasks whose budget recovered."""
    kb = isolated_kanban_home
    monkeypatch.setattr(
        "hermes_cli.kanban_token_budget.check_profile_budget",
        lambda profile, home, **kw: None,
    )
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        tid = kb.create_task(conn, title="parked", assignee="alpha")
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status='budget_stopped' WHERE id = ?",
                (tid,),
            )
        res = kb.dispatch_once(conn, spawn_fn=lambda *a, **k: 4242)
    assert res.budget_stop_recovered == [tid]
    with kb.connect_closing() as conn:
        # Recovery returns the task to ready; the SAME tick's ready loop
        # immediately re-claims and spawns it — the full end-to-end loop.
        assert kb.get_task(conn, tid).status == "running"


# ---------------------------------------------------------------------------
# Constrain-level tightening (design §6 #6)
# ---------------------------------------------------------------------------


def test_tighten_turn_limit_constrain_halves_finite():
    assert tighten_turn_limit(500, "constrain") == 250
    assert tighten_turn_limit(3, "constrain") == 1  # int(3 * 0.5) = 1
    assert tighten_turn_limit(1, "constrain") == 1  # floored at 1


def test_tighten_turn_limit_constrain_unlimited_capped():
    assert tighten_turn_limit(None, "constrain") == CONSTRAIN_UNLIMITED_TURN_CAP
    assert tighten_turn_limit(sys.maxsize, "constrain") == CONSTRAIN_UNLIMITED_TURN_CAP
    assert tighten_turn_limit(10**9, "constrain") == CONSTRAIN_UNLIMITED_TURN_CAP
    assert tighten_turn_limit("unparseable", "constrain") == CONSTRAIN_UNLIMITED_TURN_CAP


def test_tighten_turn_limit_non_constrain_untouched():
    # Zero interference for every non-constrain level and a missing marker.
    for level in ("", "ok", "steer", "stop", None):
        assert tighten_turn_limit(500, level) == 500
        assert tighten_turn_limit(None, level) is None


# ---------------------------------------------------------------------------
# Budget trigger events (design §6 #4/#7): record_budget_transition +
# list_budget_events
# ---------------------------------------------------------------------------


def _budget_decision(profile, level, pct, daily_used=90, daily_cap=100):
    return BudgetDecision(
        profile=profile,
        level=level,
        pct=pct,
        usage=ProfileUsage(daily_tokens=daily_used),
        budget=TokenBudget(daily=daily_cap),
    )


def test_record_budget_transition_first_steer_writes_event(isolated_kanban_home):
    kb = isolated_kanban_home
    from hermes_cli.kanban_token_budget import record_budget_transition

    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        tid = kb.create_task(conn, title="t1", assignee="alpha")
        d = _budget_decision("alpha", BudgetLevel.STEER, 0.9)
        kind = record_budget_transition(conn, tid, d)
        assert kind == "token_budget_steer"
        events = list_budget_events(conn, limit=10)
        assert len(events) == 1
        assert events[0]["kind"] == "token_budget_steer"
        assert events[0]["level"] == "steer"
        assert events[0]["profile"] == "alpha"
        assert events[0]["pct"] == 0.9
        assert events[0]["daily_tokens"] == 90
        assert events[0]["daily_limit"] == 100
        assert events[0]["created_at"] is not None


def test_record_budget_transition_dedupes_same_level(isolated_kanban_home):
    """Sitting over budget across spawn ticks must not spam the event log."""
    kb = isolated_kanban_home
    from hermes_cli.kanban_token_budget import record_budget_transition

    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        tid = kb.create_task(conn, title="t1", assignee="alpha")
        d = _budget_decision("alpha", BudgetLevel.STEER, 0.85)
        assert record_budget_transition(conn, tid, d) == "token_budget_steer"
        # same level again (same + different tasks) → no-op
        assert record_budget_transition(conn, tid, d) is None
        tid2 = kb.create_task(conn, title="t2", assignee="alpha")
        assert record_budget_transition(conn, tid2, d) is None
        assert len(list_budget_events(conn, limit=10)) == 1


def test_record_budget_transition_level_changes_both_directions(isolated_kanban_home):
    kb = isolated_kanban_home
    from hermes_cli.kanban_token_budget import record_budget_transition

    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        tid = kb.create_task(conn, title="t1", assignee="alpha")
        steer = _budget_decision("alpha", BudgetLevel.STEER, 0.85)
        constrain = _budget_decision("alpha", BudgetLevel.CONSTRAIN, 1.05, 105)
        assert record_budget_transition(conn, tid, steer) == "token_budget_steer"
        assert record_budget_transition(conn, tid, constrain) == "token_budget_constrain"
        # budget rolled back down → steer fires again (a new trigger)
        assert record_budget_transition(conn, tid, steer) == "token_budget_steer"
        events = list_budget_events(conn, limit=10)
        assert [e["kind"] for e in events] == [
            "token_budget_steer",
            "token_budget_constrain",
            "token_budget_steer",
        ]


def test_record_budget_transition_ok_is_noop(isolated_kanban_home):
    kb = isolated_kanban_home
    from hermes_cli.kanban_token_budget import record_budget_transition

    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        tid = kb.create_task(conn, title="t1", assignee="alpha")
        d = _budget_decision("alpha", BudgetLevel.OK, 0.1, 10)
        assert record_budget_transition(conn, tid, d) is None
        assert list_budget_events(conn, limit=10) == []


def test_list_budget_events_profile_filter_and_stop_visibility(isolated_kanban_home):
    """A stop event (written by the dispatch path) shows up with killed workers."""
    kb = isolated_kanban_home
    from hermes_cli.kanban_token_budget import record_budget_transition

    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        t_alpha = kb.create_task(conn, title="alpha work", assignee="alpha")
        t_beta = kb.create_task(conn, title="beta work", assignee="beta")
        record_budget_transition(
            conn, t_alpha, _budget_decision("alpha", BudgetLevel.STEER, 0.9)
        )
        record_budget_transition(
            conn, t_beta, _budget_decision("beta", BudgetLevel.CONSTRAIN, 1.1, 110)
        )
        with kb.write_txn(conn):
            kb._append_event(
                conn, t_alpha, "token_budget_stop",
                {
                    "assignee": "alpha",
                    "level": "stop",
                    "pct": 1.5,
                    "daily_tokens": 150,
                    "daily_limit": 100,
                    "killed_workers": ["t_alpha"],
                },
            )
        all_events = list_budget_events(conn, limit=10)
        assert [e["kind"] for e in all_events] == [
            "token_budget_stop",
            "token_budget_constrain",
            "token_budget_steer",
        ]
        stop = all_events[0]
        assert stop["killed_workers"] == ["t_alpha"]
        assert stop["daily_tokens"] == 150
        # per-profile filter
        alpha_events = list_budget_events(conn, limit=10, profile="alpha")
        assert [e["kind"] for e in alpha_events] == [
            "token_budget_stop",
            "token_budget_steer",
        ]


def test_list_budget_events_empty_board(isolated_kanban_home):
    kb = isolated_kanban_home
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        assert list_budget_events(conn) == []


# ---------------------------------------------------------------------------
# Constrain-level over-long task rejection (design §4: degrade, don't kill)
# ---------------------------------------------------------------------------


def test_constrain_reject_reason_only_constrain_level():
    """Steer keeps spawning and stop has its own path — only constrain rejects."""
    body = "x" * (8000 + 1)
    for level, pct in (
        (BudgetLevel.OK, 0.5),
        (BudgetLevel.STEER, 0.9),
        (BudgetLevel.STOP, 1.5),
    ):
        from hermes_cli.kanban_token_budget import constrain_reject_reason

        d = _budget_decision("alpha", level, pct, 150)
        assert constrain_reject_reason(d, body) is None


def test_constrain_reject_reason_boundary_and_none_body():
    from hermes_cli.kanban_token_budget import (
        CONSTRAIN_MAX_TASK_BODY_CHARS,
        constrain_reject_reason,
    )

    d = _budget_decision("alpha", BudgetLevel.CONSTRAIN, 1.05, 105)
    limit = CONSTRAIN_MAX_TASK_BODY_CHARS
    # exactly at the limit → pass; one over → reject with a reason
    assert constrain_reject_reason(d, "x" * limit) is None
    reason = constrain_reject_reason(d, "x" * (limit + 1))
    assert reason is not None
    assert "constrain" in reason and str(limit) in reason
    # a None body (empty spec) is never rejected
    assert constrain_reject_reason(d, None) is None
    assert constrain_reject_reason(d, "") is None
    # short bodies pass at constrain too
    assert constrain_reject_reason(d, "short spec") is None


def test_dispatch_constrain_reject_releases_and_records(isolated_kanban_home):
    """A TokenBudgetReject at spawn releases the claim and records the reason."""
    kb = isolated_kanban_home
    from hermes_cli.kanban_token_budget import TokenBudgetReject

    decision = BudgetDecision(
        profile="alpha",
        level=BudgetLevel.CONSTRAIN,
        pct=1.05,
        usage=ProfileUsage(daily_tokens=105),
        budget=TokenBudget(daily=100),
    )

    def _reject_spawn(*args, **kwargs):
        raise TokenBudgetReject(decision, 9000)

    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        tid = kb.create_task(
            conn, title="over-long spec", assignee="alpha",
            body="x" * 9000,
        )
        res = kb.dispatch_once(conn, spawn_fn=_reject_spawn)

    assert res.token_budget_rejected == [tid]
    assert res.token_budget_stopped == []
    assert res.spawned == []
    with kb.connect_closing() as conn:
        task = kb.get_task(conn, tid)
        assert task.status != "running"
        events = kb.list_events(conn, tid)
        rejects = [e for e in events if e.kind == "token_budget_constrain_reject"]
        assert len(rejects) == 1
        payload = rejects[0].payload
        assert payload["assignee"] == "alpha"
        assert payload["level"] == "constrain"
        assert payload["body_chars"] == 9000
        assert payload["killed_workers"] == []
        # the reject must also be visible in the budget UI event stream
        # (list_budget_events / GET /budget/events), rendering as a
        # constrained event — not only on the task's own event timeline.
        from hermes_cli.kanban_token_budget import list_budget_events

        budget_events = list_budget_events(conn, limit=10)
        kinds = [e["kind"] for e in budget_events]
        assert kinds == ["token_budget_constrain_reject"]
        rej = budget_events[0]
        assert rej["level"] == "constrain"
        assert rej["profile"] == "alpha"
        assert rej["pct"] == 1.05
        # profile filter includes it (assignee matches), other profile excludes
        assert len(list_budget_events(conn, limit=10, profile="alpha")) == 1
        assert list_budget_events(conn, limit=10, profile="beta") == []


def test_dispatch_constrain_reject_never_kills_other_workers(isolated_kanban_home):
    """Constrain rejects only the over-long task; other running workers live."""
    kb = isolated_kanban_home
    from hermes_cli.kanban_token_budget import TokenBudgetReject

    decision = BudgetDecision(
        profile="alpha",
        level=BudgetLevel.CONSTRAIN,
        pct=1.05,
        usage=ProfileUsage(daily_tokens=105),
        budget=TokenBudget(daily=100),
    )

    def _reject_spawn(*args, **kwargs):
        raise TokenBudgetReject(decision, 9000)

    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        running_alpha = kb.create_task(conn, title="alpha running", assignee="alpha")
        running_beta = kb.create_task(conn, title="beta running", assignee="beta")
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status='running', claim_lock='h:1', "
                "worker_pid=4242 WHERE id IN (?, ?)",
                (running_alpha, running_beta),
            )
        overlong = kb.create_task(
            conn, title="over-long spec", assignee="alpha", body="x" * 9000,
        )
        res = kb.dispatch_once(
            conn, spawn_fn=_reject_spawn, reconcile_orphans=False,
        )

    assert res.token_budget_rejected == [overlong]
    with kb.connect_closing() as conn:
        # the rejected task is back in its source phase; the running siblings
        # are untouched (no kill, no claim release) — degrade, don't kill
        assert kb.get_task(conn, overlong).status != "running"
        assert kb.get_task(conn, running_alpha).status == "running"
        assert kb.get_task(conn, running_beta).status == "running"


def test_dispatch_constrain_reject_review_lane_returns_to_review(
    isolated_kanban_home,
):
    """A constrain-rejected review task reclaims back to review, not ready."""
    kb = isolated_kanban_home
    from hermes_cli.kanban_token_budget import TokenBudgetReject

    decision = BudgetDecision(
        profile="alpha",
        level=BudgetLevel.CONSTRAIN,
        pct=1.05,
        usage=ProfileUsage(daily_tokens=105),
        budget=TokenBudget(daily=100),
    )

    def _reject_spawn(*args, **kwargs):
        raise TokenBudgetReject(decision, 9000)

    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        tid = kb.create_task(
            conn, title="review spec", assignee="alpha", body="x" * 9000,
        )
        # Put the task in the review lane directly (review claim is exercised
        # by the dispatch loop below).
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='review' WHERE id = ?", (tid,))
        res = kb.dispatch_once(
            conn, spawn_fn=_reject_spawn, reconcile_orphans=False,
        )

    assert res.token_budget_rejected == [tid]
    with kb.connect_closing() as conn:
        assert kb.get_task(conn, tid).status == "review"
        events = kb.list_events(conn, tid)
        assert any(e.kind == "token_budget_constrain_reject" for e in events)


def test_default_spawn_constrain_overlong_raises_reject(
    isolated_kanban_home, monkeypatch,
):
    """The spawn gate itself raises TokenBudgetReject before launching a worker."""
    kb = isolated_kanban_home
    import hermes_cli.kanban_token_budget as budget_mod

    decision = BudgetDecision(
        profile="alpha",
        level=BudgetLevel.CONSTRAIN,
        pct=1.05,
        usage=ProfileUsage(daily_tokens=105),
        budget=TokenBudget(daily=100),
    )
    monkeypatch.setattr(
        budget_mod, "check_profile_budget",
        lambda profile, home, **kw: decision,
    )
    from hermes_cli.kanban_db import Task
    from hermes_cli.kanban_token_budget import TokenBudgetReject

    task = Task(
        id="t_gate",
        title="gate check",
        body="x" * 9000,
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
    workspace = tempfile.mkdtemp(prefix="budget_gate_ws_")
    with pytest.raises(TokenBudgetReject) as excinfo:
        kb._default_spawn(task, workspace)
    assert excinfo.value.body_len == 9000
    assert excinfo.value.limit == 8000


def test_default_spawn_constrain_short_body_proceeds_to_launch(
    isolated_kanban_home, monkeypatch,
):
    """Under the limit at constrain, the spawn continues (prompt note injected)."""
    kb = isolated_kanban_home
    import hermes_cli.kanban_token_budget as budget_mod

    decision = BudgetDecision(
        profile="alpha",
        level=BudgetLevel.CONSTRAIN,
        pct=1.05,
        usage=ProfileUsage(daily_tokens=105),
        budget=TokenBudget(daily=100),
    )
    monkeypatch.setattr(
        budget_mod, "check_profile_budget",
        lambda profile, home, **kw: decision,
    )
    # A short body must not reject; the launch proceeds to Popen (which we
    # stub so the test asserts the prompt/env amendment instead of spawning).
    from hermes_cli.kanban_db import Task

    task = Task(
        id="t_gate_ok",
        title="gate check",
        body="short spec",
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
    workspace = tempfile.mkdtemp(prefix="budget_gate_ws_")
    launched = {}

    class _FakeProc:
        pid = 99999

    import subprocess as _sp

    def _fake_popen(cmd, **kwargs):
        launched["cmd"] = cmd
        launched["env"] = kwargs.get("env", {})
        return _FakeProc()

    monkeypatch.setattr(_sp, "Popen", _fake_popen)
    pid = kb._default_spawn(task, workspace)
    assert pid == 99999
    # constrain note injected into the worker prompt + env markers set
    assert "HERMES_KANBAN_TOKEN_LEVEL" in launched["env"]
    assert launched["env"]["HERMES_KANBAN_TOKEN_LEVEL"] == "constrain"
    assert "token-budget constrain" in launched["env"]["HERMES_KANBAN_TOKEN_STEER"]
    assert any("token-budget constrain" in c for c in launched["cmd"])

# ---------------------------------------------------------------------------
# Manual reset baseline (人工复位 — design §4.2 recovery path)
# ---------------------------------------------------------------------------


def test_effective_usage_no_baseline_is_identity():
    u = ProfileUsage(daily_tokens=150, weekly_tokens=450, daily_api_calls=2, weekly_api_calls=5)
    assert effective_usage(u, None) == u
    assert effective_usage(u, {}) == u


def test_effective_usage_subtracts_and_floors():
    u = ProfileUsage(daily_tokens=150, weekly_tokens=450, daily_api_calls=2, weekly_api_calls=5)
    out = effective_usage(u, {"tokens": 100, "api_calls": 1, "at": 1.0})
    assert out.daily_tokens == 50
    assert out.weekly_tokens == 350
    # api_calls are not deducted (the ladder only trips on tokens)
    assert out.daily_api_calls == 2
    assert out.weekly_api_calls == 5
    # floored at 0
    over = effective_usage(u, {"tokens": 999, "api_calls": 1, "at": 1.0})
    assert over.daily_tokens == 0 and over.weekly_tokens == 0


def test_load_budget_baselines_missing_or_corrupt_returns_empty(tmp_path):
    assert load_budget_baselines(tmp_path / "nope.json") == {}
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert load_budget_baselines(bad) == {}
    non_dict = tmp_path / "list.json"
    non_dict.write_text("[1,2]", encoding="utf-8")
    assert load_budget_baselines(non_dict) == {}


def test_load_budget_baselines_parses_entries(tmp_path):
    p = tmp_path / "b.json"
    p.write_text(
        json.dumps({"alpha": {"tokens": 100, "api_calls": 3, "at": 1.5}, "junk": 7}),
        encoding="utf-8",
    )
    out = load_budget_baselines(p)
    assert out == {"alpha": {"tokens": 100.0, "api_calls": 3.0, "at": 1.5}}


def test_reset_profile_baseline_reopens_ladder(monkeypatch, tmp_path):
    """Full 复位 flow: stop-level usage → reset → spawn gate returns OK/None."""
    now = 2_000_000_000.0
    home = tmp_path / "profiles" / "alpha"
    home.mkdir(parents=True)
    _make_state_db(home / "state.db", now)  # daily 150 / weekly 450

    s = TokenBudgetSettings(enabled=True, profiles={"alpha": TokenBudget(daily=100)})
    # 150/100 = 150% → stop
    d = check_profile_budget("alpha", home, settings=s, now=now)
    assert d is not None and d.level == BudgetLevel.STOP

    # Point the shared baseline file at a temp kanban home.
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    for mod in list(sys.modules.keys()):
        if mod.startswith("hermes_cli"):
            del sys.modules[mod]
    from hermes_cli import kanban_token_budget as ktb

    entry = ktb.reset_profile_baseline("alpha", home)
    # The baseline captures the profile's CURRENT rolling usage (real clock):
    # with last_seen in the "future" relative to the fake test `now`, all
    # seeded rows land in the weekly window → 150 + 300 + 2000 = 2450.
    assert entry["tokens"] == 2450
    stored = ktb.load_budget_baselines()
    assert stored["alpha"]["tokens"] == 2450

    # Same usage, now baseline-subtracted → 0/100 → OK.
    d2 = ktb.check_profile_budget("alpha", home, settings=s, now=now)
    assert d2 is not None and d2.level == BudgetLevel.OK
    assert d2.usage.daily_tokens == 0

    # Snapshots agree with the gate (no divergence after reset).
    snaps = ktb.list_budget_snapshots({"alpha": str(home)}, settings=s, now=now)
    assert snaps and snaps[0]["level"] == "ok" and snaps[0]["daily_tokens"] == 0


def test_reset_event_visible_in_list_budget_events(isolated_kanban_home):
    kb = isolated_kanban_home
    from hermes_cli.kanban_db import _append_event

    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        with kb.write_txn(conn):
            _append_event(conn, "", "token_budget_reset", {
                "assignee": "alpha", "level": "reset", "baseline_tokens": 150,
            })
            _append_event(conn, "", "token_budget_stop", {
                "assignee": "alpha", "level": "stop", "pct": 1.5,
            })
        events = list_budget_events(conn, limit=10)
        kinds = [e["kind"] for e in events]
        assert kinds == ["token_budget_stop", "token_budget_reset"]
        reset = events[1]
        assert reset["level"] == "reset"
        assert reset["profile"] == "alpha"
        assert reset["baseline_tokens"] == 150
