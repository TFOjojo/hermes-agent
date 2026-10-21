"""Persistent session goals — the Ralph loop for Hermes.

A goal is a free-form objective that stays active across turns; after each turn an auxiliary-model
judge decides whether it is satisfied. The continuation prompt is a normal user message appended via
``run_conversation`` (no system-prompt mutation or toolset swap — prompt caching stays intact). Judge
failures are fail-OPEN (``continue``); the turn budget is the backstop.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field, asdict, replace
from datetime import datetime, timezone, UTC
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import uuid

from hermes_cli._subprocess_compat import noninteractive_git_env
from hermes_cli import goal_admission
from hermes_cli.goal_authorization import GoalAuthorizationMixin
from hermes_cli.goal_gates import (
    DEFAULT_GATE_MAX_RETRIES, DEFAULT_GATE_TIMEOUT_SECONDS, GoalGate, _gate_workspace, run_gate,
)
from hermes_cli.goal_kanban_loop import (
    KANBAN_GOAL_CONTINUATION_TEMPLATE, KANBAN_GOAL_FINALIZE_TEMPLATE, run_kanban_goal_loop,
)
from hermes_time import safe_strftime

logger = logging.getLogger(__name__)


# ── Constants & defaults ──────────────────────────────────────────────

DEFAULT_MAX_TURNS = 20
DEFAULT_JUDGE_TIMEOUT = 30.0
# Judge output budget. Reasoning models burn hidden-reasoning tokens before the visible one-line
# JSON verdict; 200 (the original) reliably truncated it and tripped the auto-pause. 4096 covers
# every model live-tested; override via auxiliary.goal_judge.max_tokens.
DEFAULT_JUDGE_MAX_TOKENS = 4096
# Cap how much of the last response we send to the judge.
_JUDGE_RESPONSE_SNIPPET_CHARS = 4000
# Consecutive judge *parse* failures (empty / non-JSON) before the loop auto-pauses and points at
# the goal_judge config. API/transport errors do NOT count — those are tracked separately below.
# Guards against small models that cannot follow the strict JSON contract burning the whole budget.
DEFAULT_MAX_CONSECUTIVE_PARSE_FAILURES = 3
# Consecutive transport failures (401, timeout, DNS) before auto-pause: a broken API key returns
# 401 every call and must not spend every turn on an unreachable judge.
DEFAULT_MAX_CONSECUTIVE_TRANSPORT_FAILURES = 5

# Quality gates: deterministic shell commands that must pass before the judge may declare DONE. A
# failed gate short-circuits the judge — its output IS the continuation prompt, so the agent works
# on concrete evidence instead of a vibe check. The record, its defaults and the runner live in
# ``goal_gates.py``; only the defaults are re-imported here for ``add_gate``'s optional arguments.
# Longest a pid/session wait barrier may hold the loop before judging resumes. Timed barriers
# (``waiting_until``) carry their own deadline and are exempt.
_MAX_BARRIER_WAIT_S = 30 * 60


CONTINUATION_PROMPT_TEMPLATE = (
    "[Continuing toward your standing goal]\n"
    "Goal: {goal}\n\n"
    "Continue working toward this goal. Take the next concrete step. "
    "If you believe the goal is complete, state so explicitly and stop. "
    "If you are blocked and need input from the user, say so clearly and stop."
)

# With a completion contract: the block tells the agent what "done" means, how to prove it, what
# not to break, scope, and when to stop — so it targets the verification surface.
CONTINUATION_PROMPT_WITH_CONTRACT_TEMPLATE = (
    "[Continuing toward your standing goal]\n"
    "Goal: {goal}\n\n"
    "Completion contract:\n"
    "{contract_block}\n\n"
    "Continue working toward the outcome above. Take the next concrete step. "
    "Stay within the stated boundaries and do not violate the constraints. "
    "Before claiming the goal is done, satisfy the Verification criterion and "
    "show the concrete evidence (command output, file contents, test result). "
    "If you hit the stated stop condition or are otherwise blocked and need "
    "user input, say so clearly and stop."
)

# With /subgoal criteria: surfaced verbatim to the agent and to the judge.
CONTINUATION_PROMPT_WITH_SUBGOALS_TEMPLATE = (
    "[Continuing toward your standing goal]\n"
    "Goal: {goal}\n\n"
    "Additional criteria the user added mid-loop:\n"
    "{subgoals_block}\n\n"
    "Continue working toward the goal AND all additional criteria. Take "
    "the next concrete step. If you believe the goal and every "
    "additional criterion are complete, state so explicitly and stop. "
    "If you are blocked and need input from the user, say so clearly "
    "and stop."
)

# Fed back when a quality gate fails: bounded output is the evidence to repair against (no judge).
CONTINUATION_PROMPT_GATE_FAILED_TEMPLATE = (
    "[Continuing toward your standing goal — a quality gate failed]\n"
    "Goal: {goal}\n\n"
    "The quality gate command below must pass before this goal can be "
    "declared done, and it just failed (attempt {attempt}/{max_retries}):\n"
    "  $ {command}\n"
    "Exit code: {exit_code}\n"
    "Output (tail):\n"
    "```\n"
    "{output}\n"
    "```\n\n"
    "Fix the underlying problem so this gate passes, then re-run it to "
    "confirm. Do not declare the goal complete while any gate fails. If the "
    "gate itself is wrong or cannot pass, say so clearly and stop."
)

# The marker every synthetic /goal continuation prompt starts with. Surfaces use it to tell an
# automatic continuation apart from a real user message — pending-continuation cleanup on
# pause/clear, and the drain-time admission check (W2).
GOAL_CONTINUATION_PREFIX = "[Continuing toward your standing goal]"

JUDGE_SYSTEM_PROMPT = (
    "You are a strict judge evaluating whether an autonomous agent has "
    "achieved a user's stated goal. You receive the goal text, the agent's "
    "most recent response, and — when present — a list of background "
    "processes the agent has running. Decide one of four verdicts.\n\n"
    "DONE — the goal is fully satisfied:\n"
    "- The response explicitly confirms the goal was completed, OR\n"
    "- The response clearly shows the final deliverable was produced.\n"
    "DONE requires the deliverable to actually exist. If the response only "
    "explains why the goal cannot be reached, the verdict is BLOCKED, not "
    "DONE.\n\n"
    "BLOCKED — the goal cannot be satisfied as stated:\n"
    "- The response explains the goal is genuinely unachievable (impossible, "
    "out of scope, no valid path to the deliverable), or refuses to "
    "fabricate a deliverable that cannot exist, OR\n"
    "- The response explains progress is blocked and the next step needs "
    "user input to proceed.\n"
    "Return BLOCKED with the reason describing what is blocking. BLOCKED is "
    "a refusal, not a completion — never return BLOCKED for a goal that "
    "was achieved.\n"
    "When the block is an error the agent hit (an HTTP status, an API, "
    "sign-in or token failure), quote the error text verbatim in the reason "
    "and attribute it only to a provider, service or credential the response "
    "itself names. Never infer one the response does not name — an unnamed "
    "401 belongs to the model provider the agent was calling, not to some "
    "other service's token.\n\n"
    "WAIT — the goal is NOT done, but the next step is to wait for async "
    "work to finish rather than act again. Choose this ONLY when the agent's "
    "progress is genuinely gated on something running on its own:\n"
    "- A background process listed below is still running AND the response "
    "shows the agent is waiting on its result (e.g. a CI poller, build, "
    "test run, deploy). If the process has a session id, return it in "
    "``wait_on_session`` — that releases when the process exits OR its "
    "watch_patterns trigger fires (use this for a long-lived watcher that "
    "signals mid-run and may never exit). Otherwise return its pid in "
    "``wait_on_pid`` (releases on exit only).\n"
    "- The agent says it is rate-limited / backing off / must wait a fixed "
    "period — return seconds in ``wait_for_seconds``.\n"
    "- The agent has delegated subagents still running (stated below as "
    "active delegations) and the response says it is waiting on them with "
    "nothing else dispatchable — return ``wait_for_seconds`` between 600 and "
    "1800. Their results wake the agent on their own; re-poking it now only "
    "produces a status recap.\n"
    "Picking WAIT parks the loop without burning a turn; it resumes "
    "automatically when the pid exits or the time elapses. Do NOT pick WAIT "
    "just because work remains — only when re-poking now would be pure "
    "busy-work because the agent can't progress until the async thing "
    "finishes.\n\n"
    "CONTINUE — not done, and there is a concrete next step the agent can "
    "take right now. This is the default when in doubt.\n\n"
    "Reply ONLY with a single JSON object on one line. Shapes:\n"
    '{"verdict": "done", "reason": "<one sentence>"}\n'
    '{"verdict": "blocked", "reason": "<one sentence>"}\n'
    '{"verdict": "continue", "reason": "<one sentence>"}\n'
    '{"verdict": "wait", "wait_on_session": "<id>", "reason": "<one sentence>"}\n'
    '{"verdict": "wait", "wait_on_pid": <int>, "reason": "<one sentence>"}\n'
    '{"verdict": "wait", "wait_for_seconds": <int>, "reason": "<one sentence>"}\n'
    "The legacy shape {\"done\": <true|false>, \"reason\": \"...\"} is still "
    "accepted (true=done, false=continue)."
)

# Judge prompt line for live delegated subagents (WAIT-for-seconds vs CONTINUE).
JUDGE_DELEGATIONS_BLOCK_TEMPLATE = (
    "Active delegations: the agent has {count} delegated subagent batch(es) still running; "
    "their results are delivered to it automatically when they finish.\n\n"
)

# Judge prompt block listing running background processes (WAIT vs CONTINUE, which pid).
JUDGE_BACKGROUND_BLOCK_TEMPLATE = (
    "Background processes the agent currently has running (it may be waiting "
    "on one of these):\n{background_lines}\n\n"
)

JUDGE_USER_PROMPT_TEMPLATE = (
    "Goal:\n{goal}\n\n"
    "Agent's most recent response:\n{response}\n\n"
    "{background_block}"
    "Current time: {current_time}\n\n"
    "Is the goal satisfied — done, blocked, continue, or wait?"
)

# With /subgoal criteria: the judge must see ALL of them met, not just the original goal.
JUDGE_USER_PROMPT_WITH_SUBGOALS_TEMPLATE = (
    "Goal:\n{goal}\n\n"
    "Additional criteria the user added mid-loop (all must also be "
    "satisfied for the goal to be DONE):\n{subgoals_block}\n\n"
    "Agent's most recent response:\n{response}\n\n"
    "{background_block}"
    "Current time: {current_time}\n\n"
    "Decision: For each numbered criterion above, find concrete "
    "evidence in the agent's response that the criterion is "
    "satisfied. Do not accept generic phrases like 'all requirements "
    "met' or 'implying it was done' — require specific evidence (a "
    "file contents excerpt, an output line, a command result). If "
    "ANY criterion lacks specific evidence in the response, the goal "
    "is NOT done — return CONTINUE (or WAIT if blocked on a listed "
    "background process).\n\n"
    "Is the goal AND every additional criterion satisfied?"
)

# With a contract: DONE strictly against the Verification criterion; a violated constraint refuses.
JUDGE_USER_PROMPT_WITH_CONTRACT_TEMPLATE = (
    "Goal:\n{goal}\n\n"
    "Completion contract (the authoritative definition of done):\n"
    "{contract_block}\n\n"
    "Agent's most recent response:\n{response}\n\n"
    "{background_block}"
    "Current time: {current_time}\n\n"
    "Decision rules:\n"
    "- The goal is DONE only when the Verification criterion is satisfied AND "
    "the response shows concrete evidence of it (a command result, file "
    "contents excerpt, test/benchmark output) — not a claim like 'done' or "
    "'all tests pass' without evidence.\n"
    "- If any stated Constraint was violated, the goal is NOT done — CONTINUE.\n"
    "- If the response shows the agent is waiting on a listed background "
    "process to satisfy the Verification criterion (e.g. CI is the "
    "verification and it's still running), return WAIT on that process "
    "instead of re-poking — re-poking now would be pure busy-work.\n"
    "- If the response explains the work is genuinely unachievable or hits "
    "the stated Stop condition and needs user input, the goal is NOT done — "
    "return BLOCKED with the reason describing the block.\n"
    "- Otherwise the goal is NOT done — CONTINUE.\n\n"
    "Is the goal satisfied per its completion contract — done, blocked, continue, or wait?"
)

# /goal draft: turn a plain objective into a reviewable contract (after Codex's "draft the goal").
DRAFT_CONTRACT_SYSTEM_PROMPT = (
    "You turn a user's plain-language objective into a structured completion "
    "contract for an autonomous coding agent. The contract has five fields:\n"
    "- outcome: the single end state that must be true when done\n"
    "- verification: the specific test / command / artifact that PROVES the "
    "outcome (must be concrete and checkable)\n"
    "- constraints: what must NOT change or regress\n"
    "- boundaries: which files, dirs, tools, or systems are in scope\n"
    "- stop_when: the condition under which the agent should stop and ask "
    "for human input instead of pushing on\n\n"
    "Infer sensible, specific values from the objective and any project "
    "context implied by it. Prefer concrete verification (a named test "
    "command, a build, a benchmark) over vague phrases. Keep each field to "
    "one or two sentences. If a field genuinely cannot be inferred, use an "
    "empty string for it.\n\n"
    "Reply ONLY with a single JSON object on one line:\n"
    '{"outcome": "...", "verification": "...", "constraints": "...", '
    '"boundaries": "...", "stop_when": "..."}'
)


# ── Completion contract ───────────────────────────────────────────────

# The five contract fields, in display order (after OpenAI Codex's "strong goal" guidance: what
# "done" means, how to prove it, what must not regress, what is in bounds, when to stop and ask).
# A bare free-form goal stays fully supported — empty fields are omitted from every prompt.
_CONTRACT_FIELDS = ("outcome", "verification", "constraints", "boundaries", "stop_when")

_CONTRACT_LABELS = {
    "outcome": "Outcome", "verification": "Verification", "constraints": "Constraints",
    "boundaries": "Boundaries", "stop_when": "Stop when blocked",
}

# Inline-input aliases the user may type before a value (`verify: tests pass`, `done when: ...`).
_CONTRACT_ALIASES = {
    "outcome": "outcome", "goal": "outcome", "done": "outcome", "done when": "outcome",
    "verification": "verification", "verify": "verification", "verified by": "verification",
    "evidence": "verification", "proof": "verification",
    "constraints": "constraints", "constraint": "constraints", "preserve": "constraints",
    "must not": "constraints", "do not change": "constraints",
    "boundaries": "boundaries", "boundary": "boundaries", "scope": "boundaries",
    "allowed": "boundaries", "files": "boundaries",
    "stop when": "stop_when", "stop_when": "stop_when", "blocked": "stop_when",
    "stop if blocked": "stop_when", "give up when": "stop_when",
}


@dataclass
class GoalContract:
    """Optional structured completion contract; empty fields are omitted everywhere."""
    outcome: str = ""
    verification: str = ""
    constraints: str = ""
    boundaries: str = ""
    stop_when: str = ""

    def is_empty(self) -> bool:
        return not any(getattr(self, f).strip() for f in _CONTRACT_FIELDS)

    def to_dict(self) -> dict[str, str]:
        return {f: getattr(self, f) for f in _CONTRACT_FIELDS}

    @classmethod
    def from_dict(cls, data: Optional[dict[str, Any]]) -> GoalContract:
        if not isinstance(data, dict):
            return cls()
        return cls(**{f: str(data.get(f) or "").strip() for f in _CONTRACT_FIELDS})

    def render_block(self) -> str:
        """Non-empty fields as a labelled block; empty contract → empty string."""
        return "\n".join(f"- {_CONTRACT_LABELS[f]}: {getattr(self, f).strip()}" for f in _CONTRACT_FIELDS if getattr(self, f).strip())


def parse_contract(text: str) -> tuple[str, GoalContract]:
    """Split user-typed goal text into a headline + contract from inline ``field: value`` lines.

    A headline without an explicit ``outcome:`` IS the outcome — it is not duplicated into the
    contract block (the goal text already carries it), so outcome stays empty in that case.
    """
    if not text:
        return "", GoalContract()
    headline_parts: list[str] = []
    fields: dict[str, list[str]] = {f: [] for f in _CONTRACT_FIELDS}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if ":" in line:
            prefix, _, value = line.partition(":")
            key = _CONTRACT_ALIASES.get(prefix.strip().lower())
            if key is not None and value.strip():
                fields[key].append(value.strip())
                continue
        headline_parts.append(line)
    contract = GoalContract(**{f: " ".join(v).strip() for f, v in fields.items()})
    return " ".join(headline_parts).strip(), contract


def _render_extra_criteria(subgoals: list[str]) -> str:
    return "\n".join(f"- Extra criterion {i}: {text}" for i, text in enumerate(subgoals, start=1))



# ── Goal state ────────────────────────────────────────────────────────

# Snapshot format version. Rows written before versioning carry no ``schema_version`` and load
# as compatible legacy rows (fields filled in on their first authorized change); a row declaring
# a version this build does not know is refused rather than misinterpreted (T02).
GOAL_STATE_SCHEMA_VERSION = 1
# A goal row in one of these states is a live goal: creating another one over it is a conflict.
_LIVE_GOAL_STATUSES = frozenset({"active", "paused"})


class GoalStoreError(RuntimeError):
    """A goal persistence failure. ``kind`` classifies it — none of these means "no goal"."""

    kind = "store-error"


class GoalStoreUnavailable(GoalStoreError):
    """The session store could not be read or written (bootstrap window, lock, I/O)."""

    kind = "unavailable"


class GoalStateCorrupt(GoalStoreError):
    """A goal row exists but cannot be interpreted (broken JSON, unknown schema_version).
    The original bytes are always preserved."""

    kind = "corrupt"


class GoalConflict(GoalStoreError):
    """The stored goal moved on: it no longer matches the goal_id/revision the writer read.
    ``current`` carries the snapshot that won, when it could still be read."""

    kind = "conflict"

    def __init__(self, message: str, *, current: Optional[GoalState] = None):
        super().__init__(message)
        self.current = current


@dataclass(frozen=True)
class GoalRef:
    """Compare-and-swap handle for one persisted goal snapshot."""

    goal_id: str
    revision: int


@dataclass
class GoalState:
    """Serializable goal state stored per session.

    Versioned snapshot: ``schema_version`` guards forward compatibility, ``goal_id`` names one
    logical goal across clear/recreate, and ``revision`` is a per-goal monotonic counter —
    together the CAS handle every mutation commits against. Rows written before versioning load
    with ``goal_id=""`` / ``revision=0`` and gain real values on their first authorized change.
    """
    goal: str
    status: str = "active"          # active | paused | done | cleared
    turns_used: int = 0
    max_turns: int = DEFAULT_MAX_TURNS
    created_at: float = 0.0
    last_turn_at: float = 0.0
    last_verdict: Optional[str] = None        # "done" | "blocked" | "continue" | "wait" | "skipped"
    last_reason: Optional[str] = None
    paused_reason: Optional[str] = None       # why we auto-paused (budget, etc.)
    consecutive_parse_failures: int = 0       # judge-output parse failures in a row
    # Tracked separately from parse failures: a broken API key returns 401 every call and must
    # auto-pause instead of burning the budget on an unreachable judge.
    consecutive_transport_failures: int = 0   # judge API/transport errors in a row
    # User-added criteria (/subgoal). Both the judge and continuation prompts include them.
    subgoals: list[str] = field(default_factory=list)
    # Wait barrier (judge ``wait`` verdict or ``/goal wait``): parks the loop instead of re-poking the
    # agent into busy-work. pid → until exit; session → until that process_registry session's OWN
    # trigger fires (exit OR watch_patterns match — preferred for watchers that signal mid-run);
    # until → wall-clock deadline. While ANY is active evaluate_after_turn returns
    # should_continue=False without burning a turn; cleared lazily when satisfied or by unwait/pause/
    # resume/clear. Defaults empty so old state_meta rows load unchanged.
    waiting_on_pid: Optional[int] = None
    waiting_on_session: Optional[str] = None
    waiting_until: float = 0.0
    # Live delegation batches when a timed WAIT was set because of them; the barrier lifts as soon
    # as that count drops (a batch returned), not only when the timer runs out.
    waiting_on_delegations: int = 0
    waiting_reason: Optional[str] = None
    waiting_since: float = 0.0
    contract: GoalContract = field(default_factory=GoalContract)
    # /goal gate add <cmd>: ALL must pass before the judge may declare done.
    gates: list[GoalGate] = field(default_factory=list)
    # ── Versioned snapshot (W1) ──
    schema_version: int = GOAL_STATE_SCHEMA_VERSION
    goal_id: str = ""                 # one logical goal; a recreated goal gets a new one
    revision: int = 0                 # per-goal monotonic counter (CAS handle with goal_id)

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_json(cls, raw: str) -> GoalState:
        data = json.loads(raw)
        raw_subgoals = data.get("subgoals") or []
        ints = {k: int(data.get(k) or 0) for k in ("turns_used", "consecutive_parse_failures", "consecutive_transport_failures", "waiting_on_delegations", "revision")}
        floats = {k: float(data.get(k) or 0.0) for k in ("created_at", "last_turn_at", "waiting_until", "waiting_since")}
        return cls(
            goal=data.get("goal", ""),
            status=data.get("status", "active"),
            max_turns=int(data.get("max_turns") or DEFAULT_MAX_TURNS),
            last_verdict=data.get("last_verdict"),
            last_reason=data.get("last_reason"),
            paused_reason=data.get("paused_reason"),
            subgoals=[str(s).strip() for s in raw_subgoals if str(s).strip()] if isinstance(raw_subgoals, list) else [],
            waiting_on_pid=(int(data["waiting_on_pid"]) if data.get("waiting_on_pid") else None),
            waiting_on_session=(str(data["waiting_on_session"]) if data.get("waiting_on_session") else None),
            waiting_reason=data.get("waiting_reason"),
            contract=GoalContract.from_dict(data.get("contract")),
            gates=[
                GoalGate.from_dict(g) for g in (data.get("gates") or [])
                if isinstance(g, dict) and str(g.get("command") or "").strip()
            ],
            schema_version=int(data.get("schema_version") or GOAL_STATE_SCHEMA_VERSION),
            goal_id=str(data.get("goal_id") or ""),
            **ints, **floats,
        )

    def has_contract(self) -> bool:
        return self.contract is not None and not self.contract.is_empty()

    def render_subgoals_block(self) -> str:
        """Numbered ``- N. text`` block; empty when there are no subgoals."""
        return "\n".join(f"- {i}. {text}" for i, text in enumerate(self.subgoals, start=1))

    def clear_wait(self) -> None:
        self.waiting_on_pid = None
        self.waiting_on_session = None
        self.waiting_until = 0.0
        self.waiting_on_delegations = 0
        self.waiting_reason = None
        self.waiting_since = 0.0


# ── Persistence (SessionDB state_meta) ────────────────────────────────

def _meta_key(session_id: str) -> str:
    return f"goal:{session_id}"


_DB_CACHE: dict[str, Any] = {}
_DB_BOOTSTRAP_LOCK = threading.Lock()
_DB_BOOTSTRAP_INFLIGHT: dict[str, threading.Event] = {}

# How long a loop-thread caller waits for an ALREADY-RUNNING bootstrap before degrading to None.
# Normal SessionDB init is ~10-100ms so a mid-bootstrap call usually picks the cached instance up;
# a contended init (locked state.db mid-migration) exceeds it and degrades. Far under the
# watchdog's probe window.
_DB_BOOTSTRAP_LOOP_WAIT_S = 0.25

# The call that STARTS the bootstrap (cold cache) waits this long instead. A fresh state.db init
# (schema DDL, FTS tables, first hermes_cli.config import) measures ~300ms warm and more on slow
# CI — well past 0.25s, which used to drop the first /goal write ("Goal set" but nothing
# persisted). Only the kick call pays this one-time stall; later calls keep the short window.
_DB_BOOTSTRAP_INIT_WAIT_S = 1.5


def _bootstrap_session_db(home: str, done: threading.Event) -> None:
    """Construct SessionDB off-loop and populate the cache (worker thread)."""
    try:
        db = _acquire_session_db(home)
    except Exception as exc:  # pragma: no cover
        logger.debug("GoalManager: background SessionDB() raised (%s)", exc)
        db = None
    with _DB_BOOTSTRAP_LOCK:
        if db is not None and home not in _DB_CACHE:
            _DB_CACHE[home] = db
            db = None
        _DB_BOOTSTRAP_INFLIGHT.pop(home, None)
    if db is not None:  # lost the race; drop our reference
        _release_session_db(db)
    done.set()


def _get_session_db() -> Optional[Any]:
    """Cached SessionDB per HERMES_HOME (profile switches pick the right DB); None on any failure.

    Never constructs SessionDB on an event-loop thread: a cache miss there kicks a one-shot background
    bootstrap and waits a bounded grace window (the kick call waits ``_DB_BOOTSTRAP_INIT_WAIT_S`` so a
    healthy cold init completes and the first write isn't dropped).
    """
    try:
        from hermes_constants import get_hermes_home

        home = str(get_hermes_home())
    except Exception as exc:  # pragma: no cover
        logger.debug("GoalManager: SessionDB bootstrap failed (%s)", exc)
        return None

    cached = _DB_CACHE.get(home)
    if cached is not None and _registry_tore_down(cached):
        # ``hermes profile delete`` force-closes every handle under the profile home
        # (``hermes_state_registry.close_all_under``) before rmtree; a same-name recreate in this
        # process must acquire a fresh handle, not keep writing into the torn-down one.
        with _DB_BOOTSTRAP_LOCK:
            if _DB_CACHE.get(home) is cached:
                del _DB_CACHE[home]
        cached = None
    if cached is not None:
        return cached

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        on_loop_thread = False
    else:
        on_loop_thread = True

    if on_loop_thread:
        with _DB_BOOTSTRAP_LOCK:
            # Re-check under the lock: a bootstrap may have finished since the unlocked read.
            cached = _DB_CACHE.get(home)
            if cached is not None:
                return cached
            done = _DB_BOOTSTRAP_INFLIGHT.get(home)
            wait = _DB_BOOTSTRAP_LOOP_WAIT_S   # already running: brief grace window only
            if done is None:
                done = _DB_BOOTSTRAP_INFLIGHT[home] = threading.Event()
                threading.Thread(target=_bootstrap_session_db, args=(home, done), name="goals-sessiondb-bootstrap", daemon=True).start()
                wait = _DB_BOOTSTRAP_INIT_WAIT_S   # kick call pays the one-time init cost
        done.wait(wait)
        return _DB_CACHE.get(home)

    try:
        db = _acquire_session_db(home)
    except Exception as exc:  # pragma: no cover
        logger.debug("GoalManager: SessionDB() raised (%s)", exc)
        return None
    with _DB_BOOTSTRAP_LOCK:
        existing = _DB_CACHE.get(home)
        if existing is not None:
            # A concurrent bootstrap won the race; drop our reference so connections don't leak.
            _release_session_db(db)
            return existing
        _DB_CACHE[home] = db
    return db


def _acquire_session_db(home: str):
    """The registry's shared handle for ``home/state.db``. A bare ``SessionDB()`` here was a SECOND
    writer per profile beside the gateway's registry handle — its own token-writer thread and
    close-time checkpoint (the #90837 corruption shape), doubled under multiplexing."""
    from hermes_state_registry import acquire
    return acquire(Path(home) / "state.db")


def _release_session_db(db) -> None:
    from hermes_state_registry import release_or_close
    try:
        release_or_close(db)
    except Exception:
        pass


def _registry_tore_down(db) -> bool:
    """True once the registry force-closed *db* (``close_all`` / ``close_all_under`` clear the
    shared-owned flag at teardown); every handle cached here was acquired through the registry, so a
    cleared flag means the connection is gone and the cache entry is stale."""
    return getattr(db, "_shared_registry_owned", True) is False


def _warn_dropped_write(manager: str, kind: str, session_id: str) -> None:
    """WARN on a dropped state write — the reply already told the user the state was set. One shared
    message keeps goal, loop and heartbeat logs greppable as one bug class."""
    logger.warning(
        "%s: %s for %s not persisted — session DB unavailable "
        "(bootstrap window exceeded, in-memory state still active)",
        manager, kind, session_id,
    )


def _read_meta(db, key: str) -> Optional[str]:
    """One ``state_meta`` read; a store failure is "unavailable", never "no row"."""
    try:
        return db.get_meta(key)
    except Exception as exc:
        raise GoalStoreUnavailable(f"goal read failed for {key}: {exc}") from exc


def _classify_row(raw: Optional[str]) -> tuple[str, Optional[GoalState]]:
    """``(kind, state)`` for a stored goal row: ``absent`` / ``ok`` / ``corrupt``.

    Never raises and never rewrites: a corrupt or unknown-schema row is reported so the caller
    can refuse (and keep the original bytes) instead of treating it as "no goal".
    """
    if raw is None:
        return "absent", None
    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("goal row is not a JSON object")
        version = data.get("schema_version", GOAL_STATE_SCHEMA_VERSION)
        if version != GOAL_STATE_SCHEMA_VERSION:
            raise ValueError(f"unsupported goal schema_version {version!r}")
        return "ok", GoalState.from_json(raw)
    except Exception as exc:  # health: allow BLE001 -- the contract of this classifier is to translate ANY parse failure into "corrupt"; catching less would let malformed rows raise out of a function documented as never raising
        return "corrupt", GoalStateCorrupt(f"stored goal is unreadable: {exc}")


def _read_goal_row(session_id: str) -> tuple[str, Optional[GoalState]]:
    """Classify a session's goal row. Raises GoalStoreUnavailable when the store itself cannot
    be read — distinct from an absent row."""
    if not session_id:
        return "absent", None
    db = _get_session_db()
    if db is None:
        raise GoalStoreUnavailable(f"session store unavailable for {session_id}")
    return _classify_row(_read_meta(db, _meta_key(session_id)))


def load_goal(session_id: str) -> Optional[GoalState]:
    """Load the goal for a session; ``None`` means there is no goal ROW.

    An unreadable row raises GoalStateCorrupt (never "no goal") and an unreadable store raises
    GoalStoreUnavailable — callers that must degrade catch GoalStoreError and keep the failure
    visible (``GoalManager.load_error``) instead of silently dropping the user's goal.
    """
    kind, state = _read_goal_row(session_id)
    if kind == "corrupt":
        raise state
    return state


def _new_goal_id() -> str:
    return uuid.uuid4().hex


def _next_snapshot(next_state: GoalState, goal_id: str, revision: int) -> GoalState:
    """The snapshot to persist: ``next_state``'s content under a stamped identity."""
    return replace(
        next_state, goal_id=goal_id, revision=revision, schema_version=GOAL_STATE_SCHEMA_VERSION,
    )


def _plan_commit(
    expected: Optional[GoalRef], kind: str, current: Optional[GoalState], *, new_goal: bool,
) -> tuple[str, int]:
    """``(goal_id, base_revision)`` for the next snapshot of this goal.

    ``expected=None`` is a create: it may only land when no LIVE goal is stored (an absent or
    terminal row, or an unreadable one — an explicit set is the recovery path for a row this
    build cannot interpret). A ``GoalRef`` must match the stored row exactly, which is what
    makes every edit a compare-and-swap. ``new_goal`` is a create-or-replace: the guard still
    applies, but the snapshot gets a fresh identity and its own revision counter, so nothing
    holding the old reference — a cleared goal's tombstone, or a goal this set replaces — can
    hit it.
    """
    if expected is None:
        if kind == "ok" and current.status in _LIVE_GOAL_STATUSES:
            raise GoalConflict("a live goal already exists for this session", current=current)
        return _new_goal_id(), 0
    if kind == "corrupt":
        raise current
    if kind != "ok" or (current.goal_id, current.revision) != (expected.goal_id, expected.revision):
        raise GoalConflict(
            f"stored goal is ({kind}, {getattr(current, 'goal_id', None)}, "
            f"{getattr(current, 'revision', None)}), expected ({expected.goal_id}, {expected.revision})",
            current=current,
        )
    if new_goal:
        return _new_goal_id(), 0
    # A pre-versioning row has no identity yet: its first authorized change mints one.
    return (expected.goal_id or _new_goal_id()), current.revision


def commit_goal(
    session_id: str, expected: Optional[GoalRef], next_state: GoalState, *, new_goal: bool = False,
) -> GoalState:
    """Atomically persist ``next_state`` as the session's goal, or change nothing.

    Reads the stored row, checks it against ``expected`` (see ``_plan_commit``), then writes the
    new snapshot inside one guarded transaction: a row that moved in between loses the guard, so
    a late judge verdict or an old client's edit can never overwrite a newer state. On success
    ``next_state`` itself is stamped with the committed goal_id/revision (memory adopts the
    commit, never the reverse) and returned.

    Raises GoalConflict (stored goal moved on), GoalStateCorrupt (row unreadable) or
    GoalStoreUnavailable (store read/write failed) — none of which means "no goal".
    """
    if not session_id:
        raise GoalStoreUnavailable("no session id to commit a goal for")
    db = _get_session_db()
    if db is None:
        raise GoalStoreUnavailable(f"session store unavailable for {session_id}")
    key = _meta_key(session_id)
    raw = _read_meta(db, key)
    kind, current = _classify_row(raw)
    goal_id, base_revision = _plan_commit(expected, kind, current, new_goal=new_goal)
    snapshot = _next_snapshot(next_state, goal_id, base_revision + 1)
    try:
        outcome = db.exchange_meta({key: raw}, {key: snapshot.to_json()})
    except Exception as exc:
        raise GoalStoreUnavailable(f"goal commit failed for {session_id}: {exc}") from exc
    if outcome != "committed":
        try:
            _, winner = _classify_row(_read_meta(db, key))
        except GoalStoreError:
            winner = None
        raise GoalConflict("stored goal changed during commit", current=winner)
    next_state.goal_id, next_state.revision = goal_id, base_revision + 1
    return next_state


def save_goal(session_id: str, state: GoalState) -> None:
    """Persist a goal snapshot without a compare-and-swap (seeding / legacy callers).

    Stamps the version fields — a snapshot without a ``goal_id`` is a new goal and mints one —
    and stamps ``state`` in place. Raises GoalStoreUnavailable when the store cannot be written
    and GoalConflict when a snapshot carrying a goal_id would land over a DIFFERENT stored goal
    (an old client's edit of a goal that has since been cleared or replaced). Production
    mutations go through ``commit_goal``; this path stays for compatibility.
    """
    if not session_id:
        return
    db = _get_session_db()
    if db is None:
        raise GoalStoreUnavailable(f"session store unavailable for {session_id}")
    key = _meta_key(session_id)
    kind, current = _classify_row(_read_meta(db, key))
    if state.goal_id:
        if kind == "corrupt":
            raise current
        if kind != "ok" or (current.goal_id, current.revision) != (state.goal_id, state.revision):
            raise GoalConflict("stale goal snapshot cannot overwrite the stored goal", current=current)
        goal_id, revision = state.goal_id, current.revision + 1
    else:
        goal_id, revision = _new_goal_id(), 1
    snapshot = _next_snapshot(state, goal_id, revision)
    try:
        db.set_meta(key, snapshot.to_json())
    except Exception as exc:
        raise GoalStoreUnavailable(f"goal write failed for {session_id}: {exc}") from exc
    state.goal_id, state.revision = goal_id, revision


def clear_goal(session_id: str) -> None:
    """Archive the goal as a tombstone: same goal_id, new revision, status ``cleared``.

    The row is kept for audit, and because a tombstone is a terminal state a later create over
    it is allowed while a live goal is not. No-op when there is no goal row; an unreadable row
    raises GoalStateCorrupt and keeps its bytes.
    """
    state = load_goal(session_id)
    if state is None:
        return
    commit_goal(session_id, GoalRef(state.goal_id, state.revision), replace(state, status="cleared"))


def move_goal(
    old_session_id: str, new_session_id: str, expected: Optional[GoalRef], *, reason: str = "",
) -> Optional[GoalState]:
    """Transfer the goal row to another session as ONE transaction (compression rotation).

    Writes the child row and archives the parent row inside a single guarded exchange, so there
    is no window in which the goal exists twice or nowhere. Returns the migrated snapshot, or
    ``None`` when nothing changed: invalid ids, no live goal on the parent, a ``expected`` that
    no longer matches, a live (or unreadable) goal already on the child, or a store failure —
    never a half transfer. The goal keeps its ``goal_id``; the parent's tombstone moves the
    revision on.
    """
    if not old_session_id or not new_session_id or old_session_id == new_session_id:
        return None
    db = _get_session_db()
    if db is None:
        raise GoalStoreUnavailable(f"session store unavailable for {old_session_id}")
    old_key, new_key = _meta_key(old_session_id), _meta_key(new_session_id)
    old_raw = _read_meta(db, old_key)
    kind, parent = _classify_row(old_raw)
    if kind != "ok" or parent.status == "cleared":
        return None
    if expected is not None and (parent.goal_id, parent.revision) != (expected.goal_id, expected.revision):
        return None
    new_raw = _read_meta(db, new_key)
    child_kind, child = _classify_row(new_raw)
    if child_kind == "corrupt" or (child_kind == "ok" and child.status in _LIVE_GOAL_STATUSES):
        return None
    migrated_id = parent.goal_id or _new_goal_id()
    # The goal itself did not change — it moved — so it keeps its identity AND its revision; the
    # parent's archived row is the new snapshot of that goal.
    migrated = _next_snapshot(parent, migrated_id, parent.revision)
    tombstone = replace(parent, goal_id=migrated_id, revision=parent.revision + 1,
                        status="cleared", schema_version=GOAL_STATE_SCHEMA_VERSION)
    try:
        outcome = db.exchange_meta(
            {old_key: old_raw, new_key: new_raw},
            {new_key: migrated.to_json(), old_key: tombstone.to_json()},
        )
    except Exception as exc:
        raise GoalStoreUnavailable(f"goal move {old_session_id} -> {new_session_id} failed: {exc}") from exc
    if outcome != "committed":
        return None
    logger.debug("GoalManager: moved goal %s -> %s (%s)", old_session_id, new_session_id, reason or "rotation")
    return migrated


def migrate_goal_to_session(old_session_id: str, new_session_id: str, *, reason: str = "") -> bool:
    """Carry a persistent /goal from a parent session to its compression continuation.

    Best-effort and never raises — a refusal here must not block compression itself. Returns
    True only when the goal row moved atomically; on any refusal the parent keeps its goal
    (recoverable, and no automatic continuation runs on the rotated session id).
    """
    try:
        parent = load_goal(old_session_id)
        if parent is None or parent.status == "cleared":
            return False
        moved = move_goal(old_session_id, new_session_id, GoalRef(parent.goal_id, parent.revision), reason=reason)
        return moved is not None
    except GoalStoreError as exc:
        logger.warning(
            "GoalManager: goal migration %s -> %s refused (%s: %s) — the goal stays on the parent "
            "session and no automatic continuation runs on the child",
            old_session_id, new_session_id, exc.kind, exc,
        )
        return False


# ── Admission (W2) ─────────────────────────────────────────────────────

def goal_continuation_admitted(session_id: str) -> tuple[bool, str]:
    """Layer 2 — may the /goal continuation queued for ``session_id`` start a turn right now?

    Every surface calls this immediately before a synthetic continuation becomes a real turn:
    the classic CLI's ``_pending_input`` drain, the gateway FIFO drain, the TUI follow-up
    chain.  It re-reads the persisted goal and re-validates the ticket that was queued for it,
    so a user pause / clear / edit that landed while the continuation sat in the queue stops it
    before any model call or tool execution.  Enqueue is not admission.
    """
    try:
        state = GoalManager(session_id).state
    except Exception as exc:  # health: allow BLE001 -- fail-closed admission gate: any unreadable state (store down, corrupt row) refuses the turn rather than starting work on a guess
        logger.debug("goal admission: state for %s unreadable: %s", session_id, exc)
        return False, "goal state could not be read"
    if state is None or not state.goal_id:
        return False, "no goal is stored for this session"
    return goal_admission.admit_queued(
        state.goal_id, revision=state.revision, status=state.status)


# ── Judge ─────────────────────────────────────────────────────────────

def _truncate(text: str, limit: int) -> str:
    if not text:
        return ""
    return text if len(text) <= limit else text[:limit] + "… [truncated]"


def _pid_alive(pid: int) -> bool:
    """Liveness via ``gateway.status._pid_exists`` (psutil + ctypes/POSIX fallback). Never uses
    ``os.kill(pid, 0)``: on Windows that routes to CTRL_C_EVENT and hard-kills the target's console
    group (bpo-14484)."""
    if not pid or pid <= 0:
        return False
    try:
        from gateway.status import _pid_exists

        return bool(_pid_exists(int(pid)))
    except Exception:
        pass
    try:
        import psutil  # type: ignore

        return bool(psutil.pid_exists(int(pid)))
    except Exception:
        return False


def _session_waiting(session_id: str) -> bool:
    """True while the process_registry session is running and its trigger hasn't fired. Fail-safe:
    any import/registry error yields False so a stale barrier can never wedge the loop."""
    if not session_id:
        return False
    try:
        from tools.process_registry import process_registry

        return bool(process_registry.is_session_waiting(session_id))
    except Exception:
        return False


_JSON_OBJECT_RE = re.compile(r"\{.*?\}", re.DOTALL)


def _goal_judge_setting(key: str, default, cast):
    """Resolve ``auxiliary.goal_judge.<key>``; non-positive/garbage falls back to ``default``
    rather than crashing the loop. ``load_config()`` is cached on (mtime, size) so this is cheap."""
    try:
        from hermes_cli.config import load_config

        value = cast((load_config().get("auxiliary") or {}).get("goal_judge", {}).get(key, default))
        if value > 0:
            return value
    except Exception:
        pass
    return default


def _goal_judge_max_tokens() -> int:
    return _goal_judge_setting("max_tokens", DEFAULT_JUDGE_MAX_TOKENS, int)


def _goal_judge_timeout() -> float:
    return _goal_judge_setting("timeout", DEFAULT_JUDGE_TIMEOUT, float)


def _extract_json_object(raw: str) -> Optional[dict[str, Any]]:
    """Best-effort: strip code fences, parse the blob, else pull the first ``{...}`` out."""
    if not raw:
        return None
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        nl = text.find("\n")   # peel off leading json/JSON tag
        if nl != -1:
            text = text[nl + 1:]
    try:
        data = json.loads(text)
    except Exception:
        match = _JSON_OBJECT_RE.search(text)
        if not match:
            return None
        try:
            data = json.loads(match.group(0))
        except Exception:
            return None
    return data if isinstance(data, dict) else None


def _parse_judge_response(raw: str) -> tuple[str, str, bool, Optional[dict[str, Any]]]:
    """Parse the judge's reply, fail-open. Returns ``(verdict, reason, parse_failed, wait_directive)``.

    ``parse_failed`` flags non-JSON output so callers can auto-pause after N in a row.
    ``wait_directive`` is ``{"session_id"}`` / ``{"pid"}`` / ``{"seconds"}`` for a ``wait``
    verdict; a wait with no target is downgraded to ``continue``. Accepts ``{"verdict": ...}`` and
    the legacy ``{"done": <bool>}`` shape.
    """
    if not raw:
        return "continue", "judge returned empty response", True, None
    data = _extract_json_object(raw)
    if data is None:
        return "continue", f"judge reply was not JSON: {_truncate(raw, 200)!r}", True, None

    reason = str(data.get("reason") or "").strip() or "no reason provided"
    verdict_raw = data.get("verdict")
    if isinstance(verdict_raw, str):
        verdict = verdict_raw.strip().lower()
    else:
        done_val = data.get("done")
        done = done_val.strip().lower() in {"true", "yes", "1", "done"} if isinstance(done_val, str) else bool(done_val)
        verdict = "done" if done else "continue"
    if verdict not in {"done", "blocked", "continue", "wait"}:
        verdict = "continue"
    if verdict != "wait":
        return verdict, reason, False, None

    def _first_int(*keys: str) -> Optional[int]:
        for k in keys:
            try:
                iv = int(data[k]) if data.get(k) is not None else 0
            except (TypeError, ValueError):
                continue
            if iv > 0:
                return iv
        return None

    # Prefer session (releases on the process's own trigger), then pid (exit only), then seconds.
    sess = data.get("wait_on_session") or data.get("session_id") or data.get("wait_session")
    if isinstance(sess, str) and sess.strip():
        return "wait", reason, False, {"session_id": sess.strip()}
    pid = _first_int("wait_on_pid", "pid", "wait_pid")
    if pid is not None:
        return "wait", reason, False, {"pid": pid}
    seconds = _first_int("wait_for_seconds", "seconds", "wait_seconds")
    if seconds is not None:
        return "wait", reason, False, {"seconds": seconds}
    return "continue", f"{reason} (wait verdict had no target — continuing)", False, None


def _render_background_block(background_processes: Optional[list[dict[str, Any]]]) -> str:
    """Render RUNNING ``process_registry.list_sessions()`` entries for the judge prompt. Empty string
    when nothing is running, so the prompt stays byte-identical to the no-background case."""
    lines: list[str] = []
    for p in background_processes or []:
        if not isinstance(p, dict) or p.get("status") == "exited" or not p.get("pid"):
            continue
        cmd = _truncate(str(p.get("command") or "").replace("\n", " ").strip(), 120)
        tail = _truncate(str(p.get("output_preview") or "").replace("\n", " ").strip(), 120)
        line = f"- pid {p['pid']}"
        if p.get("session_id"):
            line += f" / session {p['session_id']}"
        line += f": {cmd}"
        if p.get("uptime_seconds") is not None:
            line += f" (running {p['uptime_seconds']}s)"
        # Surface the process's own trigger so the judge can wait on a mid-run signal, not just exit.
        wps = p.get("watch_patterns")
        if wps:
            hit = " [already matched]" if p.get("watch_hit") else ""
            line += f" | watch_patterns={wps}{hit}"
        elif p.get("notify_on_complete"):
            line += " | notify_on_complete"
        if tail:
            line += f" | recent output: {tail}"
        lines.append(line)
    if not lines:
        return ""
    return JUDGE_BACKGROUND_BLOCK_TEMPLATE.format(background_lines="\n".join(lines))


def _call_goal_judge_llm(call_llm, system_prompt: str, user_prompt: str, timeout: Optional[float]) -> str:
    """Route through call_llm so auxiliary.goal_judge.* config (provider/model, extra_body,
    reasoning_effort, retries) all apply. Returns the raw reply text."""
    # See #35566.
    # Route through call_llm — same #35566 fix as the judge call above.
    resp = call_llm(
        task="goal_judge",
        messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}],
        temperature=0, max_tokens=_goal_judge_max_tokens(), timeout=timeout,
    )
    try:
        return resp.choices[0].message.content or ""
    except Exception:
        return ""


def judge_goal(
    goal: str,
    last_response: str,
    *,
    timeout: Optional[float] = None,
    subgoals: Optional[list[str]] = None,
    background_processes: Optional[list[dict[str, Any]]] = None,
    contract: Optional[GoalContract] = None,
    active_delegations: int = 0,
) -> tuple[str, str, bool, Optional[dict[str, Any]], bool]:
    """Ask the auxiliary model whether the goal is satisfied.

    Returns ``(verdict, reason, parse_failed, wait_directive, transport_failed)``; verdict is done /
    blocked / continue / wait / skipped. ``parse_failed`` means unusable output; transport errors
    set ``transport_failed`` instead and fail-open to ``continue``.
    """
    if not goal.strip():
        return "skipped", "empty goal", False, None, False
    if not last_response.strip():
        return "continue", "empty response (nothing to evaluate)", False, None, False
    if timeout is None:
        timeout = _goal_judge_timeout()   # the declared default is the config key, not the constant

    try:
        from agent.auxiliary_client import call_llm
        from agent.auxiliary_unavailable import AuxiliaryClientUnavailable
    except Exception as exc:
        logger.debug("goal judge: auxiliary client import failed: %s", exc)
        return "continue", "auxiliary client unavailable", False, None, False

    # Prompt priority: contract > subgoals > plain. With both, subgoals fold into the contract
    # block as extra criteria so the judge sees a single source of truth.
    clean_subgoals = [s.strip() for s in (subgoals or []) if s and s.strip()]
    common = dict(
        goal=_truncate(goal, 2000),
        response=_truncate(last_response, _JUDGE_RESPONSE_SNIPPET_CHARS),
        background_block=_render_background_block(background_processes)
        + (JUDGE_DELEGATIONS_BLOCK_TEMPLATE.format(count=active_delegations) if active_delegations > 0 else ""),
        current_time=safe_strftime(datetime.now(tz=UTC).astimezone(), "%Y-%m-%d %H:%M:%S %Z"),
    )
    if contract is not None and not contract.is_empty():
        contract_block = contract.render_block()
        if clean_subgoals:
            contract_block = f"{contract_block}\n{_render_extra_criteria(clean_subgoals)}"
        prompt = JUDGE_USER_PROMPT_WITH_CONTRACT_TEMPLATE.format(contract_block=_truncate(contract_block, 2500), **common)
    elif clean_subgoals:
        subgoals_block = "\n".join(f"- {i}. {text}" for i, text in enumerate(clean_subgoals, start=1))
        prompt = JUDGE_USER_PROMPT_WITH_SUBGOALS_TEMPLATE.format(subgoals_block=_truncate(subgoals_block, 2000), **common)
    else:
        prompt = JUDGE_USER_PROMPT_TEMPLATE.format(**common)

    try:
        raw = _call_goal_judge_llm(call_llm, JUDGE_SYSTEM_PROMPT, prompt, timeout)
    except AuxiliaryClientUnavailable as exc:
        # No client at all (e.g. a dead Nous refresh token): name the cause so the user is sent to
        # re-authenticate, not to context-length / model debugging (#42177). Still fails open.
        logger.info("goal judge: auxiliary client unavailable (%s) — falling through to continue", exc)
        return "continue", f"goal_judge auxiliary client unavailable: {exc}", False, None, True
    except Exception as exc:
        logger.info("goal judge: API call failed (%s) — falling through to continue", exc)
        return "continue", f"judge error: {type(exc).__name__}", False, None, True

    verdict, reason, parse_failed, wait_directive = _parse_judge_response(raw)
    logger.info("goal judge: verdict=%s reason=%s%s", verdict, _truncate(reason, 120),
                f" wait={wait_directive}" if wait_directive else "")
    return verdict, reason, parse_failed, wait_directive, False


def count_active_delegations(session_id: Optional[str]) -> int:
    """Live async delegation batches spawned by this session (fail-safe 0)."""
    if not session_id:
        return 0
    try:
        from tools.async_delegation import _LIVE_STATES, _session_records
        return len(_session_records(_LIVE_STATES, "", "", str(session_id)))
    except Exception:
        return 0


# `/goal <text>` kicks the loop by sending the goal as the next user turn. When that text IS what
# the user just said (a pasted handoff note, a plan the agent already has), re-sending it makes the
# agent spend a turn deciding it is a replay (11 API calls, 6 min, in one run) and duplicates ~2k
# tokens of context. The pointer is used only when the goal is substantially the WHOLE last
# message: a short goal that merely appears inside a longer one ("ship the API" after a message
# offering API or UI work) selects one option, and two different goals must not kick identically.
GOAL_ALREADY_SEEN_KICK = "[Goal set] Continue with the goal you were just given; there is no need to re-read it."
_GOAL_REPASTE_MIN_CHARS = 400
_GOAL_REPASTE_MIN_SHARE = 0.8


def goal_kick_prompt(goal: str, last_user_message: Any) -> str:
    """The goal text, or ``GOAL_ALREADY_SEEN_KICK`` when ``last_user_message`` is essentially that text."""
    content = last_user_message
    if isinstance(content, list):
        content = " ".join(str(b.get("text", "")) for b in content if isinstance(b, dict))
    goal_norm, last_norm = " ".join(str(goal or "").split()), " ".join(str(content or "").split())
    if (
        len(goal_norm) >= _GOAL_REPASTE_MIN_CHARS
        and goal_norm in last_norm
        and len(goal_norm) >= _GOAL_REPASTE_MIN_SHARE * len(last_norm)
    ):
        return GOAL_ALREADY_SEEN_KICK
    return goal


def last_user_message_content(history: Any) -> Any:
    """Content of the newest ``role == "user"`` message in an OpenAI-shaped history, else ``""``."""
    for msg in reversed(history or []):
        if isinstance(msg, dict) and msg.get("role") == "user":
            return msg.get("content")
    return ""


def last_user_message_from_db(session_id: Optional[str]) -> Any:
    """Newest user message of ``session_id`` from the SessionDB (gateway/TUI surfaces have no live
    history object at slash-command time); ``""`` on any error."""
    if not session_id:
        return ""
    try:
        db = _get_session_db()
        if db is None:
            return ""
        rows = db.get_messages(str(session_id), limit=20, latest=True)
        return last_user_message_content(rows)
    except Exception:
        return ""


def gather_background_processes(task_id: Optional[str] = None, *, owner_task_id: Optional[str] = None) -> list[dict[str, Any]]:
    """Fail-safe snapshot of RUNNING ``process_registry`` sessions for the judge; ``[]`` on any error
    so the loop degrades to its pre-wait-barrier behavior.

    ``owner_task_id`` restricts the snapshot to processes the goal's OWN session spawned. The registry's
    ``task_id`` is the container key, which collapses to one value for every agent in the process, so
    without this filter a fan-out parent's judge saw every subagent's pollers and parked the goal on a
    grandchild's ``proc_*`` session (one run: 7 of 7 root verdicts were WAIT on child-owned processes;
    parked 3 h 22 min at the end while nothing of its own was running)."""
    try:
        from tools.process_registry import process_registry

        sessions = process_registry.list_sessions(task_id=task_id) or []
    except Exception as exc:
        logger.debug("gather_background_processes failed: %s", exc)
        return []
    running = [s for s in sessions if isinstance(s, dict) and s.get("status") != "exited"]
    if owner_task_id:
        running = [s for s in running if str(s.get("owner_task_id") or s.get("task_id") or "") == str(owner_task_id)]
    return running


def draft_contract(objective: str, *, timeout: Optional[float] = None) -> Optional[GoalContract]:
    """Expand a plain-language objective into a completion contract via the ``goal_judge`` auxiliary
    task (a side LLM call, not a conversation turn). None when unavailable or unparseable."""
    objective = (objective or "").strip()
    if not objective:
        return None
    if timeout is None:
        # The declared default for this path is the config key, not the module constant — see
        # _goal_judge_timeout (#91022).
        # Same config-backed default as judge_goal (#91022).
        timeout = _goal_judge_timeout()

    try:
        from agent.auxiliary_client import call_llm
    except Exception as exc:
        logger.debug("goal draft: auxiliary client import failed: %s", exc)
        return None

    try:
        raw = _call_goal_judge_llm(call_llm, DRAFT_CONTRACT_SYSTEM_PROMPT, f"Objective:\n{_truncate(objective, 4000)}", timeout)
    except Exception as exc:
        logger.info("goal draft: API call failed (%s)", exc)
        return None

    data = _extract_json_object(raw)
    if not isinstance(data, dict):
        logger.debug("goal draft: reply was not JSON: %r", _truncate(raw, 200))
        return None
    contract = GoalContract.from_dict(data)
    return None if contract.is_empty() else contract


# ── GoalManager — the orchestration surface CLI + gateway talk to ──────

def _decision(status, should_continue: bool, prompt: Optional[str], verdict: str, reason: str, message: str) -> dict[str, Any]:
    return {"status": status, "should_continue": should_continue, "continuation_prompt": prompt,
            "verdict": verdict, "reason": reason, "message": message}


_JUDGE_CONFIG_HINT = (
    "~/.hermes/config.yaml:\n  auxiliary:\n    goal_judge:\n      provider: {provider}\n      model: {model}\n"
    "Then /goal resume to continue."
)


class GoalManager(GoalAuthorizationMixin):
    """Per-session goal state + continuation decisions.

    The CLI and gateway each hold one per live session. ``evaluate_after_turn`` calls the judge and
    returns the decision dict that drives the next turn; ``next_continuation_prompt`` is the
    canonical user-role message to feed back into ``run_conversation``.
    """

    def __init__(self, session_id: str, *, default_max_turns: int = DEFAULT_MAX_TURNS):
        self.session_id = session_id
        self.default_max_turns = int(default_max_turns or DEFAULT_MAX_TURNS)
        self._load_error: Optional[GoalStoreError] = None
        try:
            self._state: Optional[GoalState] = load_goal(session_id)
        except GoalStoreError as exc:
            # An unreadable row or an unreadable store is NOT "no goal": the goal is kept (bytes
            # untouched) and reported, and nothing automatic may run on it.
            self._state = None
            self._load_error = exc
            if exc.kind == "corrupt":
                logger.warning("GoalManager: stored goal for %s is unreadable: %s", session_id, exc)
            else:
                # A bootstrap-window miss is expected degradation, not an operator problem.
                logger.debug("GoalManager: goal for %s not loaded (%s): %s", session_id, exc.kind, exc)

    # --- introspection ------------------------------------------------

    @property
    def state(self) -> Optional[GoalState]:
        return self._state

    @property
    def load_error(self) -> Optional[GoalStoreError]:
        """Why the persisted goal could not be loaded (unreadable row / unreadable store).
        Set instead of silently degrading to "no goal" — the goal was NOT discarded."""
        return self._load_error

    def is_active(self) -> bool:
        return self._state is not None and self._state.status == "active"

    def has_goal(self) -> bool:
        return self._state is not None and self._state.status in {"active", "paused"}

    def has_contract(self) -> bool:
        return self._state is not None and self._state.has_contract()

    def status_line(self) -> str:
        s = self._state
        if s is None:
            if self._load_error is not None:
                return (f"⚠ Goal state unreadable ({self._load_error.kind}) — the stored goal was NOT "
                        f"discarded: {self._load_error} Fix the session store, or replace the goal "
                        "with /goal <text>.")
            return "No active goal. Set one with /goal <text>."
        if s.status == "cleared":
            return "No active goal. Set one with /goal <text>."
        turns = f"{s.turns_used}/{s.max_turns} turns"
        sub = f", {len(s.subgoals)} subgoal{'s' if len(s.subgoals) != 1 else ''}" if s.subgoals else ""
        con = ", contract" if self.has_contract() else ""
        gat = f", {len(s.gates)} gate{'s' if len(s.gates) != 1 else ''}" if s.gates else ""
        meta = f"{turns}{sub}{con}{gat}"
        if s.status == "active":
            if s.waiting_on_session and _session_waiting(s.waiting_on_session):
                return f"⏳ Goal (parked on {s.waiting_reason or f'session {s.waiting_on_session}'}, {meta}): {s.goal}"
            if s.waiting_on_pid and _pid_alive(s.waiting_on_pid):
                return f"⏳ Goal (parked on {s.waiting_reason or f'pid {s.waiting_on_pid}'}, {meta}): {s.goal}"
            if s.waiting_until and time.time() < s.waiting_until:
                remaining = int(s.waiting_until - time.time())
                wr = s.waiting_reason or f"{remaining}s"
                return f"⏳ Goal (parked {remaining}s — {wr}, {meta}): {s.goal}"
            return f"⊙ Goal (active, {meta}): {s.goal}"
        if s.status == "paused":
            extra = f" — {s.paused_reason}" if s.paused_reason else ""
            return f"⏸ Goal (paused, {meta}{extra}): {s.goal}"
        if s.status == "done":
            return f"✓ Goal done ({meta}): {s.goal}"
        return f"Goal ({s.status}, {meta}): {s.goal}"

    # --- mutation -----------------------------------------------------

    def _save(self) -> Optional[GoalState]:
        """Commit the mutated in-memory snapshot through the CAS transaction.

        The stored row must still be the one this manager read (``goal_id`` + ``revision``);
        if the user paused, cleared or edited the goal while a judge was running, the commit is
        refused, the stored snapshot is adopted in memory, and the GoalConflict propagates so no
        caller can report a success that never landed.
        """
        state = self._state
        if state is None:
            return None
        try:
            return commit_goal(self.session_id, GoalRef(state.goal_id, state.revision), state)
        except GoalConflict as conflict:
            self._adopt_stored(conflict)
            raise

    def _adopt_stored(self, conflict: GoalConflict) -> None:
        """After a refused write the stored row wins: drop our stale in-memory snapshot."""
        if conflict.current is not None:
            self._state = conflict.current
            return
        try:
            self._state = load_goal(self.session_id)
        except GoalStoreError:
            self._state = None

    def _require_goal(self) -> GoalState:
        if self._state is None or not self.has_goal():
            raise RuntimeError("no active goal")
        return self._state

    def _require_active(self) -> GoalState:
        if self._state is None or self._state.status != "active":
            raise RuntimeError("no active goal to park")
        return self._state

    def _pause_state(self, reason: str) -> None:
        self._state.status = "paused"
        self._state.paused_reason = reason
        # Every pause is a stop: revoke the execution authorization and the queued ticket with
        # it, before the commit, so a refused write can never leave an armed goal behind.
        self._disarm()
        self._save()

    def _pause_decision(self, paused_reason: str, verdict: str, reason: str, message: str) -> dict[str, Any]:
        self._pause_state(paused_reason)
        return _decision("paused", False, None, verdict, reason, message)

    def set(self, goal: str, *, max_turns: Optional[int] = None, contract: Optional[GoalContract] = None) -> GoalState:
        goal = (goal or "").strip()
        if not goal:
            raise ValueError("goal text is empty")
        next_state = GoalState(
            goal=goal, status="active", turns_used=0, created_at=time.time(), last_turn_at=0.0,
            max_turns=int(max_turns) if max_turns else self.default_max_turns,
            contract=contract if contract is not None else GoalContract(),
        )
        try:
            # A create-or-replace: guarded by the ref this manager holds (a goal another surface
            # created meanwhile is a conflict) but always a NEW identity, so a cleared goal's
            # tombstone or a replaced goal cannot be hit by an old reference.
            committed = commit_goal(self.session_id, self._expected_ref(), next_state, new_goal=True)
        except GoalConflict as conflict:
            self._adopt_stored(conflict)
            raise
        replaced = self._state.goal_id if self._state is not None else ""
        self._state = committed
        if replaced and replaced != committed.goal_id:
            goal_admission.disarm(replaced, reason="goal replaced")
        self._arm_if_budget("user_set")
        return committed

    def _expected_ref(self) -> Optional[GoalRef]:
        """The CAS handle of the goal this manager holds; ``None`` when it holds none."""
        state = self._state
        return None if state is None else GoalRef(state.goal_id, state.revision)

    def set_contract(self, contract: GoalContract) -> Optional[GoalState]:
        """Attach or replace the completion contract on the active goal."""
        if self._state is None:
            return None
        self._state.contract = contract or GoalContract()
        return self._save()

    def pause(self, reason: str = "user-paused") -> Optional[GoalState]:
        if not self._state:
            return None
        self._state.status = "paused"
        self._state.paused_reason = reason
        self._state.clear_wait()   # a wait barrier is meaningless once paused
        # A manual pause is a stop: revoke first so a refused commit cannot leave the goal armed.
        self._disarm()
        return self._save()

    def resume(self, *, reset_budget: bool = True) -> Optional[GoalState]:
        if not self._state:
            return None
        self._state.status = "active"
        self._state.paused_reason = None
        self._state.clear_wait()   # resuming starts fresh
        if reset_budget:
            self._state.turns_used = 0
        committed = self._save()
        # An explicit resume re-authorizes this process, and mints a fresh owner epoch so every
        # ticket issued under the previous authorization is stale from here on. The kickoff turn
        # it queues is a continuation too, so it carries a ticket of its own.
        self._arm_if_budget("user_resume")
        self.issue_continuation_ticket("user_resume")
        return committed

    def clear(self) -> None:
        state = self._state
        if state is None:
            return
        try:
            commit_goal(self.session_id, GoalRef(state.goal_id, state.revision),
                        replace(state, status="cleared"))
        except GoalConflict as conflict:
            self._adopt_stored(conflict)
            raise
        self._disarm()
        self._state = None

    def mark_done(self, reason: str) -> None:
        if not self._state:
            return
        self._state.status = "done"
        self._state.last_verdict = "done"
        self._state.last_reason = reason
        self._disarm()
        self._save()

    # --- /subgoal user controls ---------------------------------------

    def add_subgoal(self, text: str) -> str:
        """Append a user-added criterion; raises ``RuntimeError`` without ``has_goal()``."""
        state = self._require_goal()
        text = (text or "").strip()
        if not text:
            raise ValueError("subgoal text is empty")
        state.subgoals.append(text)
        self._save()
        return text

    def _pop_item(self, attr: str, index_1based: int):
        items = getattr(self._require_goal(), attr)
        idx = int(index_1based) - 1
        if idx < 0 or idx >= len(items):
            raise IndexError(f"index out of range (1..{len(items)})")
        removed = items.pop(idx)
        self._save()
        return removed

    def _clear_items(self, attr: str) -> int:
        state = self._require_goal()
        prev = len(getattr(state, attr))
        setattr(state, attr, [])
        self._save()
        return prev

    def remove_subgoal(self, index_1based: int) -> str:
        """Remove a subgoal by 1-based index. Returns the removed text."""
        return self._pop_item("subgoals", index_1based)

    def clear_subgoals(self) -> int:
        """Wipe all subgoals. Returns the previous count."""
        return self._clear_items("subgoals")

    def render_subgoals(self) -> str:
        """Public helper for the /subgoal slash command."""
        if self._state is None:
            return "(no active goal)"
        return self._state.render_subgoals_block() or "(no subgoals — use /subgoal <text> to add criteria)"

    # --- /goal gate quality gates ---------------------------------------

    def add_gate(self, command: str, *, timeout_seconds: Optional[int] = None, max_retries: Optional[int] = None) -> GoalGate:
        """Append a quality-gate command; raises ``RuntimeError`` without ``has_goal()``."""
        state = self._require_goal()
        command = (command or "").strip()
        if not command:
            raise ValueError("gate command is empty")
        gate = GoalGate(
            command=command,
            timeout_seconds=int(timeout_seconds) if timeout_seconds else DEFAULT_GATE_TIMEOUT_SECONDS,
            max_retries=int(max_retries) if max_retries else DEFAULT_GATE_MAX_RETRIES,
        )
        state.gates.append(gate)
        self._save()
        return gate

    def remove_gate(self, index_1based: int) -> str:
        """Remove a gate by 1-based index. Returns the removed command."""
        return self._pop_item("gates", index_1based).command

    def clear_gates(self) -> int:
        """Remove all gates. Returns the previous count."""
        return self._clear_items("gates")

    def render_gates(self) -> str:
        """Public helper for the /goal gate slash command."""
        if self._state is None:
            return "(no active goal)"
        if not self._state.gates:
            return "(no quality gates — use /goal gate add <command> to require one)"
        lines = []
        for i, g in enumerate(self._state.gates, start=1):
            status = ""
            if g.last_exit_code == 0:
                status = " ✓ passing"
            elif g.last_exit_code is not None:
                status = f" ✗ failing (exit {g.last_exit_code}, attempt {g.attempts}/{g.max_retries})"
            lines.append(f"- {i}. $ {g.command}{status}")
        return "\n".join(lines)

    def _check_gates(self) -> Optional[dict[str, Any]]:
        """Run quality gates in order; return a decision dict on failure.

        Every eligible boundary re-executes a failed gate. A git HEAD+porcelain fingerprint used to
        replay the recorded failure when "nothing changed", but porcelain sees neither the contents
        of an untracked or already-modified file nor inputs outside the repo, so a repaired input
        was replayed as still-failing until retry exhaustion paused the goal (#110649). The
        retry cap below still bounds a genuinely stuck red suite.
        """
        state = self._state
        if state is None or not state.gates:
            return None

        gate_cwd, refusal = _gate_workspace()
        if refusal:
            return self._pause_decision(
                f"quality gates not run: {refusal}", "gate_failed", f"gates not run: {refusal}",
                f"⏸ Goal paused — quality gates not run: {refusal}. Fix the workspace or "
                f"/goal gate remove the gates, then /goal resume.",
            )
        for gate in state.gates:
            passed, exit_code, tail = run_gate(gate, cwd=gate_cwd)
            gate.last_exit_code = exit_code
            gate.last_output_tail = tail
            if passed:
                gate.attempts = 0
                continue

            gate.attempts += 1

            if gate.attempts > gate.max_retries:
                return self._pause_decision(
                    f"quality gate exhausted {gate.attempts - 1} retries: $ {gate.command}",
                    "gate_failed", f"gate exhausted retries: $ {gate.command}",
                    f"⏸ Goal paused — quality gate still failing after "
                    f"{gate.max_retries} retries: $ {gate.command} "
                    f"(exit {exit_code}). Fix it manually or /goal gate remove it, "
                    f"then /goal resume.",
                )

            self._save()
            prompt = CONTINUATION_PROMPT_GATE_FAILED_TEMPLATE.format(
                goal=state.goal, command=gate.command, exit_code=exit_code, attempt=gate.attempts,
                max_retries=gate.max_retries, output=tail or "(no output)",
            )
            return _decision(
                "active", True, prompt, "gate_failed",
                f"gate failed (exit {exit_code}): $ {gate.command}",
                f"✗ Quality gate failed ({state.turns_used}/{state.max_turns} turns, "
                f"attempt {gate.attempts}/{gate.max_retries}): $ {gate.command}",
            )

        self._save()
        return None

    # --- /goal wait barrier -------------------------------------------

    def _park(self, reason: str, **barrier) -> GoalState:
        state = self._require_active()
        state.clear_wait()
        for k, v in barrier.items():
            setattr(state, k, v)
        state.waiting_reason = (reason or "").strip() or None
        state.waiting_since = time.time()
        return self._save()

    def wait_on(self, pid: int, reason: str = "") -> GoalState:
        """Park the goal loop until a background PID exits (no turn burned, no judge call). For a
        process with a watch/notify trigger prefer ``wait_on_session``. Requires an active goal."""
        self._require_active()
        pid = int(pid)
        if pid <= 0:
            raise ValueError("pid must be a positive integer")
        if not _pid_alive(pid):
            raise ValueError("pid is not alive on this host")
        return self._park(reason, waiting_on_pid=pid)

    def wait_on_session(self, session_id: str, reason: str = "") -> GoalState:
        """Park on a process_registry session's OWN trigger: exit OR ``watch_patterns`` match. The
        right barrier for a long-lived watcher/poller that signals mid-run and may never exit."""
        self._require_active()
        session_id = str(session_id or "").strip()
        if not session_id:
            raise ValueError("session_id must be a non-empty string")
        return self._park(reason, waiting_on_session=session_id)

    def wait_for_seconds(self, seconds: int, reason: str = "", *, on_delegations: int = 0) -> GoalState:
        """Park until ``seconds`` from now (backoff/cooldown waits with no process to track). With
        ``on_delegations`` the wait is FOR those live delegation batches: it also lifts as soon as
        fewer are live (a batch result came back), so the loop re-judges with the result in hand
        instead of sleeping out a 20-minute timer (independent review: results arrived with 1,199 s
        left on the timer and nothing re-judged)."""
        self._require_active()
        seconds = int(seconds)
        if seconds <= 0:
            raise ValueError("seconds must be a positive integer")
        return self._park(reason, waiting_until=time.time() + seconds, waiting_on_delegations=max(0, int(on_delegations)))

    def stop_waiting(self) -> bool:
        """Clear any active wait barrier (pid / session / time). Returns True if one was cleared."""
        s = self._state
        if s is None or (s.waiting_on_pid is None and s.waiting_on_session is None and not s.waiting_until):
            return False
        s.clear_wait()
        self._save()
        return True

    def is_waiting(self) -> bool:
        """True iff a barrier is set AND not yet satisfied. A satisfied barrier is cleared here
        (lazy auto-clear) so the next evaluation resumes normal judging. A pid/session barrier
        also expires after ``_MAX_BARRIER_WAIT_S``: a watcher or poller that never exits would
        otherwise park the goal indefinitely (one run sat 3 h 22 min on a poller that outlived
        the work it was polling)."""
        s = self._state
        if s is None:
            return False
        if s.waiting_on_session is not None:
            still = _session_waiting(s.waiting_on_session)
        elif s.waiting_on_pid is not None:
            still = _pid_alive(s.waiting_on_pid)
        elif s.waiting_until:
            still = time.time() < s.waiting_until
            if still and s.waiting_on_delegations > 0:
                # Set because of live delegations: lift the moment one of them returned.
                live = count_active_delegations(self.session_id)
                if live < s.waiting_on_delegations:
                    still = False
        else:
            return False
        if still and s.waiting_since and s.waiting_until == 0.0 and time.time() - s.waiting_since > _MAX_BARRIER_WAIT_S:
            logger.info("goal %s: wait barrier on %s exceeded %ds; resuming judging",
                        self.session_id, s.waiting_on_session or s.waiting_on_pid, _MAX_BARRIER_WAIT_S)
            still = False
        if not still:
            self.stop_waiting()
        return still

    # --- the main entry point called after every turn -----------------

    def _waiting_decision(self, state: GoalState) -> dict[str, Any]:
        if state.waiting_on_session is not None:
            tgt = f"session {state.waiting_on_session}"
        elif state.waiting_on_pid is not None:
            tgt = f"pid {state.waiting_on_pid}"
        else:
            tgt = f"{max(0, int(state.waiting_until - time.time()))}s remaining"
        reason = state.waiting_reason or tgt
        return _decision("active", False, None, "waiting", reason, f"⏳ Goal parked — waiting on {tgt}: {reason}")

    def _apply_wait_directive(self, wait_directive: dict[str, Any], reason: str, *, active_delegations: int = 0) -> Optional[dict[str, Any]]:
        """Judge said WAIT: set the barrier and park. The counted turn stands (the judge ran) but no
        continuation fires; the loop resumes once the barrier clears. ``None`` = the barrier is
        unobservable here, so the caller continues instead."""
        if wait_directive.get("session_id"):
            tgt = f"session {self.wait_on_session(str(wait_directive['session_id']), reason=reason).waiting_on_session}"
        elif wait_directive.get("pid"):
            pid = int(wait_directive["pid"])
            try:
                tgt = f"pid {self.wait_on(pid, reason=reason).waiting_on_pid}"
            except ValueError:
                # A remote or already-exited pid is a barrier this host can never observe lifting
                # (#110826): the judge sees the same pid next turn and would re-park forever.
                # Catching wait_on's own liveness check (rather than probing first) closes the
                # window where the pid exits between a pre-check and the park.
                logger.info("goal judge: wait_on_pid %s is not alive on this host; continuing", pid)
                return None
        else:
            self.wait_for_seconds(int(wait_directive["seconds"]), reason=reason, on_delegations=active_delegations)
            tgt = f"{wait_directive['seconds']}s"
        return _decision("active", False, None, "wait", reason, f"⏳ Goal parked (judge) — waiting on {tgt}: {reason}")

    def _budget_pause(self, state: GoalState, verdict: str, reason: str, note: str = "") -> dict[str, Any]:
        return self._pause_decision(
            f"turn budget exhausted ({state.turns_used}/{state.max_turns})", verdict, reason,
            f"⏸ Goal paused — {state.turns_used}/{state.max_turns} turns used{note}. "
            "Use /goal resume to keep going, or /goal clear to stop.",
        )

    def evaluate_after_turn(
        self, last_response: str, *, user_initiated: bool = True,
        background_processes: Optional[list[dict[str, Any]]] = None,
        active_delegations: int = 0, round_id: str = "",
    ) -> dict[str, Any]:
        """Run gates + judge and update state. Return a decision dict (``status``, ``should_continue``,
        ``continuation_prompt``, ``verdict``, ``reason``, ``message``). Both real user prompts and our
        own continuations increment ``turns_used`` — both consume model budget.

        A store failure is a DECISION, never an exception: the judge's verdict is discarded, no new
        round starts, and the stored state wins — a pause/clear/edit that landed while the judge was
        running must not be overwritten by the late result, and a broken store must not be reported
        as a goal update (T01/T04).

        ``round_id`` is the caller's identity for the round this turn belongs to. When supplied, a
        repeated post-turn callback carrying the same id is refused here — before the gates, before
        the judge — so one round costs one judge call and one turn of budget however many times the
        hook fires.
        """
        state = self._state
        if state is None or state.status != "active":
            return _decision(state.status if state else None, False, None, "inactive", "no active goal", "")
        if (refusal := self._authorization_gate()) is not None:
            return refusal
        if round_id and not goal_admission.claim_round(
                state.goal_id, goal_admission.armed_epoch(state.goal_id) or "", round_id):
            return _decision(
                "active", False, None, "duplicate_round",
                f"round {round_id} was already admitted",
                f"↻ Skipped a duplicate continuation callback for round {round_id}.",
            )
        try:
            return self._judge_turn(
                last_response, background_processes=background_processes, active_delegations=active_delegations,
                round_id=round_id,
            )
        except GoalStoreError as exc:
            return self._store_failure_decision(exc)

    def _store_failure_decision(self, exc: GoalStoreError) -> dict[str, Any]:
        """Fail closed on a refused/failed commit: no round, no false success, stored state kept."""
        status = self._state.status if self._state is not None else "active"
        messages = {
            "conflict": ("↻ Goal changed while judging (paused / cleared / edited) — the stale "
                         "verdict was discarded. /goal status shows the current state."),
            "corrupt": ("⚠ The stored goal is unreadable, so nothing was changed and no round "
                        "started. The original row is preserved; /goal set replaces it."),
            "unavailable": ("⚠ The goal state could not be saved (session store unavailable) — no "
                            "round started. The in-memory goal stays active until the store returns."),
        }
        verdict = {"conflict": "state_changed", "corrupt": "state_corrupt"}.get(exc.kind, "state_write_failed")
        return _decision(status, False, None, verdict, str(exc), messages.get(exc.kind, str(exc)))

    def _judge_turn(
        self, last_response: str, *,
        background_processes: Optional[list[dict[str, Any]]] = None,
        active_delegations: int = 0, round_id: str = "",
    ) -> dict[str, Any]:
        decision = self._run_judge(
            last_response, background_processes=background_processes, active_delegations=active_delegations,
        )
        return self._ticket_continuation(decision, round_id)

    def _run_judge(
        self, last_response: str, *,
        background_processes: Optional[list[dict[str, Any]]] = None,
        active_delegations: int = 0,
    ) -> dict[str, Any]:
        state = self._state
        if state is None or state.status != "active":
            return _decision(state.status if state else None, False, None, "inactive", "no active goal", "")

        # Parked on a live process or an unexpired deadline: quiesce without burning a turn.
        if self.is_waiting():
            return self._waiting_decision(state)

        state.turns_used += 1
        state.last_turn_at = time.time()

        # Gates run BEFORE the judge: a failing gate is deterministic evidence the goal is not done,
        # so the judge is skipped and the gate's output drives the next turn (same turn budget).
        gate_decision = self._check_gates()
        if gate_decision is not None:
            if gate_decision.get("should_continue") and state.turns_used >= state.max_turns:
                return self._budget_pause(state, "gate_failed", gate_decision.get("reason", ""), note=" (a quality gate is still failing)")
            return gate_decision

        verdict, reason, parse_failed, wait_directive, transport_failed = judge_goal(
            state.goal, last_response, subgoals=state.subgoals or None, background_processes=background_processes,
            contract=state.contract if state.has_contract() else None, active_delegations=active_delegations,
        )
        state.last_verdict = verdict
        state.last_reason = reason
        # Parse failures reset on any usable reply INCLUDING transport errors, so a flaky network
        # doesn't trip the auto-pause meant for bad judge models; transport failures are counted
        # separately because persistent API errors (401, DNS) mean a broken config.
        state.consecutive_parse_failures = state.consecutive_parse_failures + 1 if parse_failed else 0
        state.consecutive_transport_failures = state.consecutive_transport_failures + 1 if transport_failed else 0

        if verdict == "wait" and wait_directive:
            parked = self._apply_wait_directive(wait_directive, reason, active_delegations=active_delegations)
            if parked is not None:
                return parked

        # BLOCKED is NOT done: pause so the user sees the judge's reason and can re-scope or override,
        # instead of burning turns on an unachievable goal or waving it through as complete.
        # BLOCKED verdict: the judge ruled the goal genuinely cannot be satisfied as stated (impossible, out
        # of scope, needs user input). See #100954.
        if verdict == "blocked":
            return self._pause_decision(
                f"judged unachievable: {reason}", "blocked", reason,
                f"🚫 Goal judged unachievable — paused: {reason} Re-scope with /goal set, or override with /goal resume.",
            )

        if verdict == "done":
            state.status = "done"
            self._save()
            return _decision("done", False, None, "done", reason, f"✓ Goal achieved: {reason}")

        # Persistent judge failures (API unreachable / unparseable output) auto-pause and point at the
        # goal_judge config so a broken judge can't burn the whole turn budget.
        n_tx, n_parse = state.consecutive_transport_failures, state.consecutive_parse_failures
        if n_tx >= DEFAULT_MAX_CONSECUTIVE_TRANSPORT_FAILURES:
            return self._pause_decision(
                f"judge API unreachable {n_tx} turns in a row (check auxiliary.goal_judge provider/key in config.yaml)",
                "continue", reason,
                f"⏸ Goal paused — judge API returned errors ({n_tx} turns). Check the goal_judge provider/key in "
                + _JUDGE_CONFIG_HINT.format(provider="deepseek", model="deepseek-flash"),
            )
        if n_parse >= DEFAULT_MAX_CONSECUTIVE_PARSE_FAILURES:
            return self._pause_decision(
                f"judge model returned unparseable output {n_parse} turns in a row", "continue", reason,
                f"⏸ Goal paused — the judge model ({n_parse} turns) isn't returning the required JSON verdict. "
                "Route the judge to a stricter model in "
                + _JUDGE_CONFIG_HINT.format(provider="openrouter", model="google/gemini-3-flash-preview"),
            )

        if state.turns_used >= state.max_turns:
            return self._budget_pause(state, "continue", reason)

        self._save()
        return _decision(
            "active", True, self.next_continuation_prompt(), "continue", reason,
            f"↻ Continuing toward goal ({state.turns_used}/{state.max_turns}): {reason}",
        )

    def next_continuation_prompt(self) -> Optional[str]:
        s = self._state
        if not s or s.status != "active":
            return None
        # Contract first (it carries the verification surface); subgoals fold in as extra criteria.
        if s.has_contract():
            contract_block = s.contract.render_block()
            if s.subgoals:
                contract_block = f"{contract_block}\n{_render_extra_criteria(s.subgoals)}"
            return CONTINUATION_PROMPT_WITH_CONTRACT_TEMPLATE.format(goal=s.goal, contract_block=contract_block)
        if s.subgoals:
            return CONTINUATION_PROMPT_WITH_SUBGOALS_TEMPLATE.format(goal=s.goal, subgoals_block=s.render_subgoals_block())
        return CONTINUATION_PROMPT_TEMPLATE.format(goal=s.goal)

    def render_contract(self) -> str:
        """Public helper for the /goal show + /goal draft slash commands."""
        if self._state is None:
            return "(no active goal)"
        return self._state.contract.render_block() if self._state.has_contract() else (
            "(no completion contract — set one with /goal draft <objective> or inline field: value lines)")



__all__ = [
    "CONTINUATION_PROMPT_TEMPLATE",
    "CONTINUATION_PROMPT_WITH_CONTRACT_TEMPLATE",
    "CONTINUATION_PROMPT_WITH_SUBGOALS_TEMPLATE",
    "DEFAULT_MAX_TURNS",
    "DRAFT_CONTRACT_SYSTEM_PROMPT",
    "GOAL_STATE_SCHEMA_VERSION",
    "JUDGE_USER_PROMPT_TEMPLATE",
    "JUDGE_USER_PROMPT_WITH_CONTRACT_TEMPLATE",
    "JUDGE_USER_PROMPT_WITH_SUBGOALS_TEMPLATE",
    "KANBAN_GOAL_CONTINUATION_TEMPLATE",
    "KANBAN_GOAL_FINALIZE_TEMPLATE",
    "GoalConflict",
    "GoalContract",
    "GoalGate",
    "GoalManager",
    "GoalRef",
    "GoalState",
    "GoalStateCorrupt",
    "GoalStoreError",
    "GoalStoreUnavailable",
    "clear_goal",
    "commit_goal",
    "draft_contract",
    "goal_continuation_admitted",
    "judge_goal",
    "load_goal",
    "migrate_goal_to_session",
    "move_goal",
    "parse_contract",
    "run_gate",
    "run_kanban_goal_loop",
    "save_goal",
]
