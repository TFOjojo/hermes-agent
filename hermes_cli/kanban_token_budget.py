"""Per-agent token budget control for the kanban dispatcher.

Reference design: ``munder-difflin`` (breaker.ts / control.ts) — a
per-agent ``ControlRegistry`` that escalates operator pressure from a soft
nudge to a hard kill. This module ports the same ladder onto *token cost*
rather than manual operator input, using telemetry Hermes already collects
(``state.db`` → ``session_model_usage``).

The three-level escalation:

  * **steer**     — inject a reminder into the worker's prompt (soft nudge).
  * **constrain** — the reminder turns into an instruction to truncate long
                    work, and the worker is marked ``constrain`` so runtime /
                    iteration caps can be tightened.
  * **stop**      — refuse to spawn the profile's workers and kill any that
                    are already running (hard cut).

Design goals:

  * **Zero interference when disabled.** If ``kanban.token_budgets.enabled``
    is false (the default) or a profile has no configured budget, every call
    is a cheap no-op and no worker is ever modified.
  * **Pure, unit-testable core.** ``decide`` / ``aggregate`` take plain
    values; the only I/O (config read, state.db read, worker kill) is
    isolated behind small functions the tests can stub.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional

__all__ = [
    "BudgetLevel",
    "TokenBudget",
    "ProfileUsage",
    "BudgetDecision",
    "TokenBudgetStop",
    "TokenBudgetReject",
    "load_token_budget_settings",
    "load_profile_token_budget",
    "resolve_profile_budget",
    "get_usage",
    "get_budget",
    "aggregate_profile_usage",
    "decide",
    "build_steer_note",
    "apply_budget_decision",
    "check_profile_budget",
    "constrain_reject_reason",
    "list_budget_snapshots",
    "list_budget_events",
    "tighten_turn_limit",
    "record_budget_transition",
    "stop_running_workers_for_profile",
    "load_budget_baselines",
    "reset_profile_baseline",
    "effective_usage",
]


class BudgetLevel(str, Enum):
    OK = "ok"
    STEER = "steer"
    CONSTRAIN = "constrain"
    STOP = "stop"


@dataclass(frozen=True)
class TokenBudget:
    daily: Optional[int] = None
    weekly: Optional[int] = None

    @property
    def any(self) -> bool:
        return (self.daily or 0) > 0 or (self.weekly or 0) > 0


@dataclass(frozen=True)
class ProfileUsage:
    daily_tokens: int = 0
    weekly_tokens: int = 0
    daily_api_calls: int = 0
    weekly_api_calls: int = 0


@dataclass(frozen=True)
class TokenBudgetSettings:
    enabled: bool = False
    steer_pct: float = 0.80
    constrain_pct: float = 1.00
    stop_pct: float = 1.20
    profiles: dict[str, TokenBudget] = field(default_factory=dict)

    def budget_for(self, profile: str) -> Optional[TokenBudget]:
        if not self.enabled:
            return None
        prof = self.profiles.get(profile)
        if prof is None:
            prof = self.profiles.get("default")
        if prof is None or not prof.any:
            return None
        return prof


@dataclass(frozen=True)
class BudgetDecision:
    profile: str
    level: BudgetLevel
    pct: float
    usage: ProfileUsage
    budget: TokenBudget


class TokenBudgetStop(Exception):
    """Raised by the spawn path when a profile is hard-over its token budget."""

    def __init__(self, decision: BudgetDecision):
        self.decision = decision
        super().__init__(
            f"token budget stop: profile={decision.profile!r} pct={decision.pct:.2%}"
        )


#: At ``constrain`` level a task whose body exceeds this many characters is
#: rejected at spawn (the over-budget profile shouldn't be handed an expensive
#: long spec). ~8k chars is roughly a 2-3k token task card; normal cards sit
#: in the hundreds-to-low-thousands range and pass untouched.
CONSTRAIN_MAX_TASK_BODY_CHARS = 8000


class TokenBudgetReject(Exception):
    """Raised by the spawn path when a ``constrain``-level task is over-long.

    Unlike :class:`TokenBudgetStop` this rejects ONE task without touching any
    other worker of the profile — constrain is the "degrade, don't kill"
    rung of the ladder (design §4).
    """

    def __init__(
        self,
        decision: BudgetDecision,
        body_len: int,
        limit: int = CONSTRAIN_MAX_TASK_BODY_CHARS,
        reason: Optional[str] = None,
    ):
        self.decision = decision
        self.body_len = body_len
        self.limit = limit
        self.reason = reason or (
            f"task body is {body_len} chars, over the constrain-level limit "
            f"of {limit}"
        )
        super().__init__(
            f"token budget constrain reject: profile={decision.profile!r} "
            f"body={body_len} chars > limit={limit}"
        )


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------


def _coerce_float(value, default: float) -> float:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return default
    return f if f > 0 else default


def _coerce_budget(raw) -> TokenBudget:
    if isinstance(raw, TokenBudget):
        return raw
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return TokenBudget(daily=int(raw))
    if not isinstance(raw, dict):
        return TokenBudget()

    def _int(key: str) -> Optional[int]:
        v = raw.get(key)
        try:
            n = int(v)
        except (TypeError, ValueError):
            return None
        return n if n > 0 else None

    return TokenBudget(daily=_int("daily"), weekly=_int("weekly"))


def load_token_budget_settings(cfg: Optional[dict]) -> TokenBudgetSettings:
    """Parse ``kanban.token_budgets`` from a config dict (safe: no-op if absent)."""
    section = (cfg or {}).get("kanban", {}).get("token_budgets") or {}
    if not isinstance(section, dict):
        return TokenBudgetSettings()
    profiles: dict[str, TokenBudget] = {}
    raw_profiles = section.get("profiles")
    if isinstance(raw_profiles, dict):
        for name, raw in raw_profiles.items():
            budget = _coerce_budget(raw)
            if budget.any:
                profiles[str(name).strip()] = budget
    return TokenBudgetSettings(
        enabled=bool(section.get("enabled", False)),
        steer_pct=_coerce_float(section.get("steer_pct"), 0.80),
        constrain_pct=_coerce_float(section.get("constrain_pct"), 1.00),
        stop_pct=_coerce_float(section.get("stop_pct"), 1.20),
        profiles=profiles,
    )


def load_profile_token_budget(profile_home) -> Optional[TokenBudget]:
    """Read ``<profile_home>/config.yaml`` → ``kanban.token_budgets.{daily,weekly}``.

    This is the highest-precedence budget layer (design §2.1): the profile's
    own runtime config overrides the root ``profiles[name]`` /
    ``profiles["default"]`` entries. The section only carries ``{daily,
    weekly}`` caps — ``enabled`` and the threshold percentages stay root-only.

    Read strategy (design §6 #3): fresh read on every call via
    ``read_user_config_raw``, which takes an explicit path and performs no
    caching and no defaults-merge — so an edited profile config is picked up
    on the next spawn with no process-level staleness. Missing file /
    unreadable / absent section → ``None`` (fail-open: the caller falls back
    to the root config). Never raises.
    """
    if not profile_home:
        return None
    try:
        from hermes_cli.config import read_user_config_raw

        cfg = read_user_config_raw(Path(profile_home) / "config.yaml")
    except Exception:
        return None
    section = (cfg or {}).get("kanban", {}).get("token_budgets")
    if not isinstance(section, dict):
        return None
    budget = _coerce_budget(section)
    return budget if budget.any else None


def resolve_profile_budget(
    profile: str,
    profile_home,
    settings: TokenBudgetSettings,
) -> Optional[TokenBudget]:
    """Effective budget for ``profile`` under the full merge order (§2.1):

    1. ``<profile_home>/config.yaml`` → ``kanban.token_budgets.{daily,weekly}``
    2. root config → ``kanban.token_budgets.profiles[profile]``
    3. root config → ``kanban.token_budgets.profiles["default"]``
    4. none of the above → ``None`` (profile is bypassed, zero interference)

    Also returns ``None`` when the feature is disabled. ``profile_home`` may
    be ``None``/missing; the per-profile layer is then skipped and the root
    fallback chain applies.
    """
    if not settings.enabled:
        return None
    per_profile = load_profile_token_budget(profile_home)
    if per_profile is not None:
        return per_profile
    return settings.budget_for(profile)


# ---------------------------------------------------------------------------
# Token aggregation (state.db → session_model_usage)
# ---------------------------------------------------------------------------

_DAILY_SECONDS = 24 * 3600
_WEEKLY_SECONDS = 7 * 24 * 3600


def _profile_state_db_path(profile_home) -> Optional[Path]:
    home = Path(profile_home) if profile_home else None
    if home is None:
        return None
    path = home / "state.db"
    return path if path.is_file() else None


def _aggregate_from_db(db_path: Path, now: float) -> ProfileUsage:
    """Sum input+output tokens over rolling daily/weekly windows.

    ``session_model_usage`` is cumulative per (session, model, provider, …)
    key; rows are attributed to a window by ``last_seen`` (the last time that
    key was active). This yields a rolling snapshot — "tokens touched by
    sessions active in the window" — not a strict wall-clock delta, which is
    the best the existing telemetry supports without a time-series table.
    """
    uri = f"file:{db_path.as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=5)
    try:
        daily_cut = now - _DAILY_SECONDS
        weekly_cut = now - _WEEKLY_SECONDS
        # NULL columns (rows written before these columns existed) → COALESCE
        # to 0. A schema where the columns are missing entirely raises
        # sqlite3.Error, which aggregate_profile_usage catches and degrades to
        # empty usage (never breaks the dispatcher). reasoning/cache tokens
        # are real model usage but tracked separately; include them so a
        # heavy-reasoning worker can't hide under a bare in/out count.
        # cache_write_tokens is the newest column (budget-breaker integration
        # G1): read it when the schema has it, and fall back to the legacy
        # column set when it doesn't (older state.db), so aggregation keeps
        # working instead of degrading an otherwise-readable DB to empty.
        query_new = (
            "SELECT last_seen, api_call_count, "
            "COALESCE(input_tokens,0), COALESCE(output_tokens,0), "
            "COALESCE(cache_read_tokens,0), COALESCE(cache_write_tokens,0), "
            "COALESCE(reasoning_tokens,0) "
            "FROM session_model_usage WHERE last_seen IS NOT NULL"
        )
        query_legacy = (
            "SELECT last_seen, api_call_count, "
            "COALESCE(input_tokens,0), COALESCE(output_tokens,0), "
            "COALESCE(cache_read_tokens,0), 0, "
            "COALESCE(reasoning_tokens,0) "
            "FROM session_model_usage WHERE last_seen IS NOT NULL"
        )
        try:
            rows = conn.execute(query_new).fetchall()
        except sqlite3.OperationalError:
            rows = conn.execute(query_legacy).fetchall()
        daily_tok = weekly_tok = 0
        daily_calls = weekly_calls = 0
        for last_seen, calls, inp, outp, cache, cache_write, reasoning in rows:
            try:
                seen = float(last_seen)
            except (TypeError, ValueError):
                continue
            tok = int(inp) + int(outp) + int(cache) + int(cache_write) + int(reasoning)
            if seen >= weekly_cut:
                weekly_tok += tok
                weekly_calls += int(calls or 0)
            if seen >= daily_cut:
                daily_tok += tok
                daily_calls += int(calls or 0)
        return ProfileUsage(
            daily_tokens=daily_tok,
            weekly_tokens=weekly_tok,
            daily_api_calls=daily_calls,
            weekly_api_calls=weekly_calls,
        )
    finally:
        conn.close()


def aggregate_profile_usage(
    profile_home, *, now: Optional[float] = None
) -> ProfileUsage:
    """Return a profile's rolling token usage, or an empty usage when the
    profile has no readable ``state.db`` (never raise — a missing/busy DB must
    not break the dispatcher)."""
    if now is None:
        now = time.time()
    db = _profile_state_db_path(profile_home)
    if db is None:
        return ProfileUsage()
    try:
        return _aggregate_from_db(db, now)
    except sqlite3.Error:
        return ProfileUsage()


# ---------------------------------------------------------------------------
# Level decision + steer note
# ---------------------------------------------------------------------------


def decide(
    usage: ProfileUsage,
    budget: TokenBudget,
    *,
    steer_pct: float = 0.80,
    constrain_pct: float = 1.00,
    stop_pct: float = 1.20,
    profile: str = "",
) -> BudgetDecision:
    """Map usage vs budget to a BudgetLevel (OK → STEER → CONSTRAIN → STOP).

    Uses the *higher* of the daily / weekly ratios (whichever is closer to
    / further past its limit wins). A budget axis with no limit (``None``)
    contributes 0% and never drives escalation on its own.
    """
    ratios: list[float] = []
    if budget.daily:
        ratios.append(usage.daily_tokens / budget.daily)
    if budget.weekly:
        ratios.append(usage.weekly_tokens / budget.weekly)
    pct = max(ratios) if ratios else 0.0

    if pct >= stop_pct:
        level = BudgetLevel.STOP
    elif pct >= constrain_pct:
        level = BudgetLevel.CONSTRAIN
    elif pct >= steer_pct:
        level = BudgetLevel.STEER
    else:
        level = BudgetLevel.OK
    return BudgetDecision(
        profile=profile,
        level=level,
        pct=pct,
        usage=usage,
        budget=budget,
    )


def _fmt_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def build_steer_note(decision: BudgetDecision, *, constrain: bool = False) -> str:
    """Human-facing reminder injected into the worker's prompt."""
    d = decision
    if constrain:
        verb = "constrain"
        action = (
            "You are OVER this profile's token budget. Truncate long work: "
            "skip non-essential verification, shorten your output, and wrap up "
            "within a bounded number of steps. Do not start new sub-tasks."
        )
    else:
        verb = "steer"
        action = (
            "You are approaching this profile's token budget. Prefer the "
            "shortest correct path and avoid unnecessary tool calls."
        )
    parts = [f"[token-budget {verb}] profile={d.profile or '(unknown)'}"]
    if d.budget.daily:
        parts.append(
            f"daily={_fmt_tokens(d.usage.daily_tokens)}/{_fmt_tokens(d.budget.daily)}"
        )
    if d.budget.weekly:
        parts.append(
            f"weekly={_fmt_tokens(d.usage.weekly_tokens)}/"
            f"{_fmt_tokens(d.budget.weekly)}"
        )
    parts.append(f"({d.pct:.0%})")
    return " ".join(parts) + "\n" + action


def apply_budget_decision(decision: BudgetDecision, prompt: str, env: dict) -> str:
    """Apply a budget decision to the worker launch (steer/constrain/stop).

    Mutates ``env`` (token-level + steer note) and returns the (possibly
    amended) prompt. Raises :class:`TokenBudgetStop` for the stop level. This
    is the single function the spawn path calls — kept pure so the whole
    ladder is unit-testable without launching a subprocess.

    ``OK`` level is the zero-interference path (design §4.5): it only sets the
    observable ``HERMES_KANBAN_TOKEN_LEVEL=ok`` marker and leaves the prompt
    and ``HERMES_KANBAN_TOKEN_STEER`` untouched — a profile under budget must
    never have its workers' prompts amended.
    """
    env["HERMES_KANBAN_TOKEN_LEVEL"] = decision.level.value
    if decision.level == BudgetLevel.STOP:
        raise TokenBudgetStop(decision)
    if decision.level == BudgetLevel.OK:
        return prompt
    constrain = decision.level == BudgetLevel.CONSTRAIN
    note = build_steer_note(decision, constrain=constrain)
    env["HERMES_KANBAN_TOKEN_STEER"] = note
    return f"{prompt}\n\n{note}"


def constrain_reject_reason(
    decision: BudgetDecision,
    body: Optional[str],
    *,
    limit: int = CONSTRAIN_MAX_TASK_BODY_CHARS,
) -> Optional[str]:
    """Return a human-readable rejection reason when a task should be refused.

    Only the ``constrain`` level can reject (design §4: steer keeps spawning,
    stop has its own hard-cut path). A task is rejected when its body exceeds
    ``limit`` characters — an over-budget profile must not be handed an
    expensive long spec. ``None`` body (or a body under the limit) returns
    ``None``: the spawn proceeds with the constrain note injected by
    :func:`apply_budget_decision`.
    """
    if decision.level != BudgetLevel.CONSTRAIN:
        return None
    body_len = len(body or "")
    if body_len <= limit:
        return None
    return (
        f"task body is {body_len} chars, over the constrain-level limit of "
        f"{limit}; refusing to spawn while profile {decision.profile!r} is at "
        f"{decision.pct:.0%} of its token budget"
    )


# ---------------------------------------------------------------------------
# Manual reset baseline (design §4.2: "预算滚动恢复或操作员提额后重 spawn")
# ---------------------------------------------------------------------------

#: Baselines live in ``<kanban_home>/token_budget_baselines.json`` — one file
#: per board root, mapping profile → the token usage snapshot *at reset time*.
#: The effective usage is ``aggregate(profile_home) - baseline``, floored at
#: 0 per axis, so a manual reset re-opens the ladder without waiting for the
#: rolling 24h/7d window to decay. This mirrors the documented recovery path
#: (提额 = operator raises the limit; 复位 = operator re-zeroes the baseline)
#: and keeps ``decide()`` a pure function over plain values.
_BASELINES_FILENAME = "token_budget_baselines.json"


def _baselines_path() -> Optional[Path]:
    """Location of the shared baseline file under the kanban home."""
    try:
        from hermes_cli.kanban_db import kanban_home

        return kanban_home() / _BASELINES_FILENAME
    except Exception:
        return None


def _parse_baseline(raw) -> dict:
    """One baseline entry: ``{tokens, api_calls, at}`` with sane coercion."""
    if not isinstance(raw, dict):
        return {}
    out: dict = {}
    for key in ("tokens", "api_calls", "at"):
        try:
            value = float(raw.get(key))
        except (TypeError, ValueError):
            continue
        if value >= 0:
            out[key] = value
    return out


def load_budget_baselines(path: Optional[Path] = None) -> dict[str, dict]:
    """Read the shared baseline file ({} when absent/corrupt — never raises)."""
    if path is None:
        path = _baselines_path()
    if path is None or not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, dict] = {}
    for name, raw in data.items():
        entry = _parse_baseline(raw)
        if entry:
            out[str(name)] = entry
    return out


def effective_usage(
    usage: ProfileUsage,
    baseline: Optional[dict],
) -> ProfileUsage:
    """Subtract a stored baseline from a usage snapshot (pure, floored at 0).

    ``baseline`` is one entry of :func:`load_budget_baselines` output —
    ``{tokens, api_calls, at}`` captured at reset time. The same subtraction
    is applied to both the daily and the weekly axis: the baseline row was
    inside both windows when it was recorded, and the rolling semantics of
    ``_aggregate_from_db`` (attribute the whole cumulative row to
    ``last_seen``) mean a per-axis split would be fiction.
    """
    if not baseline:
        return usage
    try:
        tokens = int(baseline.get("tokens", 0) or 0)
    except (TypeError, ValueError):
        return usage
    if tokens <= 0:
        return usage
    return ProfileUsage(
        daily_tokens=max(0, usage.daily_tokens - tokens),
        weekly_tokens=max(0, usage.weekly_tokens - tokens),
        daily_api_calls=usage.daily_api_calls,
        weekly_api_calls=usage.weekly_api_calls,
    )


def reset_profile_baseline(profile: str, profile_home=None) -> dict:
    """Manually reset a profile's budget baseline (the 复位 action).

    Captures the profile's *current* rolling usage as the new baseline so the
    effective usage drops to ~0 and the ladder re-opens (stop → dispatchable).
    Writes the shared JSON file atomically (tmp + replace). Returns the stored
    entry ``{tokens, api_calls, at}`` — callers surface it in the reset event
    payload and the UI toast.
    """
    now = time.time()
    usage = get_usage(profile, profile_home, now=now)
    entry = {
        "tokens": usage.daily_tokens,
        "api_calls": usage.daily_api_calls,
        "at": now,
    }
    path = _baselines_path()
    if path is None:
        raise RuntimeError("kanban home unavailable; cannot store budget baseline")
    baselines = load_budget_baselines(path)
    baselines[profile] = entry
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(baselines, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    os.replace(tmp, path)
    return entry


def check_profile_budget(
    profile: str,
    profile_home,
    *,
    settings: Optional[TokenBudgetSettings] = None,
    now: Optional[float] = None,
) -> Optional[BudgetDecision]:
    """The single gate the spawn path calls.

    Returns ``None`` (no action) when the feature is disabled or the profile
    has no budget — this is the "zero interference" path. Otherwise returns a
    :class:`BudgetDecision` for the highest-reached level.
    """
    settings = settings or load_token_budget_settings_from_config()
    budget = resolve_profile_budget(profile, profile_home, settings)
    if budget is None:
        return None
    usage = aggregate_profile_usage(profile_home, now=now)
    # Manual-reset baseline: subtract the recorded snapshot so the ladder
    # re-opens after an operator 复位 (see reset_profile_baseline).
    baselines = load_budget_baselines()
    usage = effective_usage(usage, baselines.get(profile))
    return decide(
        usage,
        budget,
        steer_pct=settings.steer_pct,
        constrain_pct=settings.constrain_pct,
        stop_pct=settings.stop_pct,
        profile=profile,
    )


# ---------------------------------------------------------------------------
# Stable read-only public API (design §6 #2: getUsage / getBudget)
# ---------------------------------------------------------------------------


def _resolve_profile_home(profile: str, profile_home=None) -> Optional[str]:
    """Resolve a profile name to its HERMES_HOME path string.

    Prefers an explicit ``profile_home`` (already resolved by the caller —
    the spawn gate passes the profile-scoped HERMES_HOME it injects into the
    worker env). Otherwise resolves via ``resolve_profile_env``; any failure
    (unknown / missing profile) returns ``None`` so read-only callers degrade
    to empty usage / no budget instead of raising.
    """
    if profile_home:
        return str(profile_home)
    try:
        from hermes_cli.profiles import resolve_profile_env

        return resolve_profile_env(profile)
    except Exception:
        return None


def get_usage(
    profile: str, profile_home=None, *, now: Optional[float] = None
) -> ProfileUsage:
    """Stable read-only usage API (design §6 #2 ``getUsage(profile)``).

    Resolves the profile name → home when ``profile_home`` isn't supplied,
    then aggregates ``state.db`` over the rolling daily/weekly windows.
    Never raises — a missing/busy DB returns an empty :class:`ProfileUsage`.
    """
    home = _resolve_profile_home(profile, profile_home)
    if home is None:
        return ProfileUsage()
    return aggregate_profile_usage(home, now=now)


def get_budget(
    profile: str,
    profile_home=None,
    *,
    settings: Optional[TokenBudgetSettings] = None,
) -> Optional[TokenBudget]:
    """Stable read-only budget API (design §6 #2 ``getBudget(profile)``).

    Returns the profile's *effective* budget under the full merge order
    (profile config > root ``profiles[name]`` > root ``profiles.default``),
    or ``None`` when the feature is disabled or the profile has no budget.
    Never raises.
    """
    settings = settings or load_token_budget_settings_from_config()
    home = _resolve_profile_home(profile, profile_home)
    return resolve_profile_budget(profile, home, settings)


def load_token_budget_settings_from_config() -> TokenBudgetSettings:
    """Best-effort live config read; returns the disabled default on failure."""
    try:
        from hermes_cli.config import load_config_readonly

        return load_token_budget_settings(load_config_readonly() or {})
    except Exception:
        return TokenBudgetSettings()


# ---------------------------------------------------------------------------
# Monitoring-view snapshot
# ---------------------------------------------------------------------------


def list_budget_snapshots(
    profiles: dict[str, str],
    *,
    settings: Optional[TokenBudgetSettings] = None,
    now: Optional[float] = None,
) -> list[dict]:
    """Per-profile usage vs budget for the dashboard / CLI table.

    ``profiles`` maps profile name → HERMES_HOME path. Returns one dict per
    profile that has a configured budget (disabled/unlisted → empty list).
    """
    settings = settings or load_token_budget_settings_from_config()
    if not settings.enabled:
        return []
    out: list[dict] = []
    baselines = load_budget_baselines()
    for name, home in profiles.items():
        budget = resolve_profile_budget(name, home, settings)
        if budget is None:
            continue
        usage = aggregate_profile_usage(home, now=now)
        # Show the *effective* usage (baseline-subtracted) so the panel and
        # the spawn gate never disagree after a manual reset.
        usage = effective_usage(usage, baselines.get(name))
        decision = decide(
            usage,
            budget,
            steer_pct=settings.steer_pct,
            constrain_pct=settings.constrain_pct,
            stop_pct=settings.stop_pct,
            profile=name,
        )
        out.append({
            "profile": name,
            "level": decision.level.value,
            "pct": round(decision.pct, 4),
            "daily_tokens": usage.daily_tokens,
            "weekly_tokens": usage.weekly_tokens,
            "daily_api_calls": usage.daily_api_calls,
            "weekly_api_calls": usage.weekly_api_calls,
            "daily_limit": budget.daily,
            "weekly_limit": budget.weekly,
        })
    return out


def floor_summary(snapshots: list[dict]) -> dict:
    """Team-floor totals derived from ``list_budget_snapshots`` rows.

    Budget-breaker integration G2: the monitoring view's floor line ("总计:
    日 X / 周 Y tokens · Z 个已配置预算成员") is a pure aggregation over the
    snapshot rows the backend already produces — no extra state.db reads, no
    new dispatch-path bookkeeping. Profiles without a configured budget never
    appear in ``snapshots`` (they render as 未限额 and contribute nothing).

    ``daily_limit_total``/``weekly_limit_total`` sum only the non-null axes:
    an axis nobody configures stays ``None`` (rendered ∞) instead of a
    misleading 0.
    """
    daily = sum(int(s.get("daily_tokens") or 0) for s in snapshots)
    weekly = sum(int(s.get("weekly_tokens") or 0) for s in snapshots)
    daily_calls = sum(int(s.get("daily_api_calls") or 0) for s in snapshots)
    weekly_calls = sum(int(s.get("weekly_api_calls") or 0) for s in snapshots)
    daily_limits = [s.get("daily_limit") for s in snapshots if s.get("daily_limit")]
    weekly_limits = [s.get("weekly_limit") for s in snapshots if s.get("weekly_limit")]
    return {
        "profiles": len(snapshots),
        "daily_tokens": daily,
        "weekly_tokens": weekly,
        "daily_api_calls": daily_calls,
        "weekly_api_calls": weekly_calls,
        "daily_limit_total": sum(daily_limits) if daily_limits else None,
        "weekly_limit_total": sum(weekly_limits) if weekly_limits else None,
    }


# ---------------------------------------------------------------------------
# Constrain-level runtime tightening (design §6 #6)
# ---------------------------------------------------------------------------

#: Scale factor applied to a finite turn/iteration cap when the worker runs
#: at ``constrain`` level. Halving keeps the worker useful but bounded.
CONSTRAIN_TURN_FACTOR = 0.5

#: When no explicit turn/iteration cap is configured (``None`` / "unlimited"),
#: a constrain-level worker gets this finite cap instead of an effectively
#: infinite budget. Picked to be enough for a focused single-shot task with
#: a couple of retries, small enough to actually cut runaway cost.
CONSTRAIN_UNLIMITED_TURN_CAP = 24

#: Caps at or above this many iterations are treated as "unlimited"
#: (``sys.maxsize`` and "none"/"unlimited" config values land here).
_UNLIMITED_TURN_THRESHOLD = 1_000_000


def tighten_turn_limit(limit, level: str = "") -> Optional[int]:
    """Apply constrain-level tightening to a turn/iteration cap (design §6 #6).

    Any level other than ``constrain`` (including a missing marker) returns
    ``limit`` unchanged — the zero-interference guarantee. For ``constrain``:

      * a finite limit is scaled by :data:`CONSTRAIN_TURN_FACTOR`, floored at 1;
      * an absent / unparseable / "unlimited" limit is replaced with
        :data:`CONSTRAIN_UNLIMITED_TURN_CAP`.

    Pure and side-effect free so the whole ladder stays unit-testable. The
    worker consumes this via ``HERMES_KANBAN_TOKEN_LEVEL`` (set by the
    dispatcher's spawn gate); nothing else reads the marker.
    """
    if level != BudgetLevel.CONSTRAIN.value:
        return limit
    if limit is None:
        return CONSTRAIN_UNLIMITED_TURN_CAP
    try:
        n = int(limit)
    except (TypeError, ValueError):
        return CONSTRAIN_UNLIMITED_TURN_CAP
    if n >= _UNLIMITED_TURN_THRESHOLD:
        return CONSTRAIN_UNLIMITED_TURN_CAP
    return max(1, int(n * CONSTRAIN_TURN_FACTOR))


# ---------------------------------------------------------------------------
# Budget trigger events (design §6 #4/#7: per-level audit trail)
# ---------------------------------------------------------------------------

_BUDGET_EVENT_KINDS = (
    "token_budget_steer",
    "token_budget_constrain",
    "token_budget_stop",
    "token_budget_reset",
    "token_budget_recovered",
    "token_budget_constrain_reject",
)


def record_budget_transition(conn, task_id: str, decision: BudgetDecision) -> Optional[str]:
    """Record a steer/constrain trigger as a task event, deduped by level.

    Writes ``token_budget_steer`` / ``token_budget_constrain`` events so the
    monitoring view can show *when* a profile crossed each threshold, with
    level, profile, usage, limits and a timestamp (design §6 #4). To avoid
    event spam while a profile sits over budget across spawn ticks, only a
    *level change* is recorded: when the profile's most recent budget event
    already carries the same level, this is a no-op and returns ``None``.

    ``stop`` is deliberately not handled here — the dispatch loop records
    the richer ``token_budget_stop`` event (with ``killed_workers``) through
    its own path. The event is appended through the dispatcher's own
    ``write_txn`` so the insert is atomic under WAL.
    """
    if decision.level not in (BudgetLevel.STEER, BudgetLevel.CONSTRAIN):
        return None
    profile = decision.profile or ""
    # Find this profile's most recent budget-trigger level (any kind).
    rows = conn.execute(
        f"SELECT kind, payload FROM task_events "
        f"WHERE kind IN ({','.join('?' * len(_BUDGET_EVENT_KINDS))}) "
        f"ORDER BY id DESC LIMIT 50",
        _BUDGET_EVENT_KINDS,
    ).fetchall()
    last_level: Optional[str] = None
    for kind, payload in rows:
        try:
            pl = json.loads(payload) if payload else {}
        except (TypeError, ValueError):
            pl = {}
        if not isinstance(pl, dict):
            continue
        if pl.get("assignee") == profile:
            last_level = pl.get("level") or str(kind).removeprefix("token_budget_")
            break
    if last_level == decision.level.value:
        return None
    kind = f"token_budget_{decision.level.value}"
    payload = {
        "assignee": profile,
        "level": decision.level.value,
        "pct": round(decision.pct, 4),
        "daily_tokens": decision.usage.daily_tokens,
        "daily_limit": decision.budget.daily,
        "weekly_tokens": decision.usage.weekly_tokens,
        "weekly_limit": decision.budget.weekly,
    }
    from hermes_cli import kanban_db as kb

    with kb.write_txn(conn):
        kb._append_event(conn, task_id, kind, payload)
    return kind


def list_budget_events(
    conn,
    *,
    limit: int = 50,
    profile: Optional[str] = None,
) -> list[dict]:
    """Recent token-budget trigger events, newest first (monitoring view).

    Returns one dict per ``token_budget_{steer,constrain,stop,reset,
    recovered,constrain_reject}`` event with ``task_id``, ``kind``,
    ``level``, ``profile``, ``pct``, daily/weekly usage + limits,
    ``killed_workers`` (stop only) and ``created_at`` (unix seconds).
    ``profile`` optionally filters to one assignee. ``token_budget_
    constrain_reject`` (constrain + over-long body) carries the same
    ``level=constrain`` payload as a constrain trigger, so the panel
    renders it as a constrained event without a schema change.
    """
    rows = conn.execute(
        f"SELECT task_id, kind, payload, created_at FROM task_events "
        f"WHERE kind IN ({','.join('?' * len(_BUDGET_EVENT_KINDS))}) "
        f"ORDER BY id DESC LIMIT ?",
        (*_BUDGET_EVENT_KINDS, max(1, int(limit))),
    ).fetchall()
    out: list[dict] = []
    for task_id, kind, payload, created_at in rows:
        try:
            pl = json.loads(payload) if payload else {}
        except (TypeError, ValueError):
            pl = {}
        if not isinstance(pl, dict):
            pl = {}
        if profile and pl.get("assignee") != profile:
            continue
        level = pl.get("level") or str(kind).removeprefix("token_budget_")
        out.append({
            "task_id": task_id,
            "kind": kind,
            "level": "reset" if kind == "token_budget_reset" else level,
            "profile": pl.get("assignee"),
            "pct": pl.get("pct"),
            "daily_tokens": pl.get("daily_tokens"),
            "daily_limit": pl.get("daily_limit"),
            "weekly_tokens": pl.get("weekly_tokens"),
            "weekly_limit": pl.get("weekly_limit"),
            "killed_workers": pl.get("killed_workers"),
            "baseline_tokens": pl.get("baseline_tokens"),
            "created_at": created_at,
        })
    return out


# ---------------------------------------------------------------------------
# Hard-stop enforcement (kill running workers)
# ---------------------------------------------------------------------------


def stop_running_workers_for_profile(
    conn,
    profile: str,
    *,
    signal_fn=None,
) -> list[str]:
    """Kill every currently-running worker of ``profile`` (host-local claims).

    Thin wrapper over :func:`kanban_db.terminate_profile_workers`: each
    worker goes through the SIGTERM→SIGKILL ladder plus the orphan sweep,
    and — once confirmed gone — its task is parked in ``budget_stopped``
    (claim released, run closed, event recorded). Returns the task ids
    whose workers were terminated (survivors keep their claim and are
    excluded — they are retried on the next stop trigger).
    """
    from hermes_cli import kanban_db as kb

    details = kb.terminate_profile_workers(
        conn, profile, signal_fn=signal_fn
    )
    return [
        d["task_id"] for d in details
        if not d.get("survived") and d.get("marked_stopped")
    ]
