"""Process-local execution authorization and continuation-ticket admission for /goal (W2).

Two concepts live here and nowhere else, because they answer two different questions:

**Armed authorization** — "may *this process* keep running turns for this goal without a new
user command?"  It is deliberately NOT persisted, and deliberately NOT stored on a
``GoalManager``.  Both alternatives are the bug this module exists to prevent: the CLI, the
gateway and the TUI rebuild ``GoalManager`` objects constantly — the gateway builds a fresh
one for every turn — so instance state would read as "the user just reopened the session"
each round and disarm a long-running goal mid-flight.  A record is keyed by
``(canonical_profile_home, goal_id)`` and stamped with the pid that armed it, so it dies with
its process.  That is exactly why a restart or a fork shows "needs re-authorization" while an
ordinary context compression — which mints a NEW session_id but keeps the same goal_id —
keeps running.

**Continuation tickets** — every automatic continuation carries its identity
(``goal_id`` / ``expected_revision`` / ``owner_epoch`` / ``round_id`` / ``source_kind``) and is
validated twice: once before it is queued, once immediately before it starts a turn.  A stale
ticket is discarded without calling the model and without running a tool.  A round is
accounted on first admission only, so a duplicate queue callback cannot bill a round twice.

Enqueue is not admission: a ticket sitting in a queue is a *request*, and every request is
re-checked against freshly-read persisted state at the moment it would spend budget.  A
late judge verdict therefore can never overwrite a pause / edit / clear the user performed
while it was in flight.

The Kanban dispatcher is deliberately absent: its goal-mode workers drive their own turns
through ``run_kanban_goal_loop`` (no ``GoalManager``), so a re-claimed card keeps the existing
claim/run identity with no per-card human confirmation gate.
"""

from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

# Surfaced wherever a continuation was refused because this host holds no live authorization.
NEEDS_REAUTH_MESSAGE = (
    "⏸ Goal needs re-authorization in this session — run /goal resume to continue."
)

# Rounds remembered per goal. Bounded so a goal that runs for days cannot grow the record
# without limit; the oldest rounds are evicted FIFO (a round that old is long finished).
_MAX_REMEMBERED_ROUNDS = 128

_LOCK = threading.RLock()
# (canonical_profile_home, goal_id) -> _Authorization.  Process-local by design: a fresh
# process — restart, fork child, new CLI attach — starts with an empty table and therefore
# shows "needs re-authorization" instead of silently resuming.
_AUTHORIZATIONS: OrderedDict[tuple[str, str], _Authorization] = OrderedDict()


# ── identity ───────────────────────────────────────────────────────────


def canonical_profile_home() -> str:
    """Case- and separator-normalised absolute profile home: one key per profile, per host.

    A multiplexed host runs many profiles inside ONE process, so the profile — not the
    process — is part of the authorization identity.  Resolution failure yields ``""``, which
    is a key nothing was ever armed under, so the caller fails closed.
    """
    try:
        from hermes_constants import get_hermes_home

        return os.path.normcase(os.path.abspath(str(get_hermes_home())))
    except Exception as exc:  # health: allow BLE001 -- resolution failure yields "", a key nothing was armed under, so the caller fails closed
        logger.debug("goal admission: profile home unresolved (%s)", exc)
        return ""


@dataclass(frozen=True)
class GoalTicket:
    """One automatic continuation request, carrying everything needed to re-check it."""

    goal_id: str
    expected_revision: int
    owner_epoch: str
    round_id: str
    source_kind: str


@dataclass
class _Authorization:
    """The armed authorization for one goal in this process, plus its round ledger."""

    goal_id: str
    owner_epoch: str
    owner_pid: int
    armed_at: float
    source_kind: str
    # The continuation this host last queued for the goal.  Text-only queues (the classic
    # CLI's ``_pending_input``) cannot carry an object, so the drain-time check resolves it
    # here rather than off the queued payload.
    pending: Optional[GoalTicket] = None
    # Rounds already billed (turn budget + judge call) and rounds already started.
    counted: OrderedDict[str, None] = field(default_factory=OrderedDict)
    started: OrderedDict[str, None] = field(default_factory=OrderedDict)


def _key(goal_id: str) -> tuple[str, str]:
    return (canonical_profile_home(), str(goal_id or ""))


def _live(goal_id: str) -> Optional[_Authorization]:
    """The armed record for ``goal_id``, or None.

    A record stamped with another pid is dropped on sight: a forked child inherits this table
    copy-on-write, and the parent's authorization is not the child's.
    """
    key = _key(goal_id)
    with _LOCK:
        rec = _AUTHORIZATIONS.get(key)
        if rec is None:
            return None
        if rec.owner_pid != os.getpid():
            _AUTHORIZATIONS.pop(key, None)
            logger.info(
                "Goal authorization for %s dropped: armed by pid %s, now running as pid %s",
                goal_id, rec.owner_pid, os.getpid(),
            )
            return None
        return rec


# ── arming ─────────────────────────────────────────────────────────────


def arm(goal_id: str, *, source_kind: str = "user") -> str:
    """Authorize this process to continue ``goal_id``; returns the new owner epoch.

    Every explicit user authorization mints a FRESH epoch, which is what makes every ticket
    issued under the previous one stale at once.
    """
    goal_id = str(goal_id or "")
    if not goal_id:
        return ""
    rec = _Authorization(
        goal_id=goal_id, owner_epoch=uuid.uuid4().hex, owner_pid=os.getpid(),
        armed_at=time.time(), source_kind=source_kind,
    )
    with _LOCK:
        _AUTHORIZATIONS[_key(goal_id)] = rec
    return rec.owner_epoch


def disarm(goal_id: str, *, reason: str = "") -> None:
    """Revoke execution permission and invalidate every outstanding ticket for this goal."""
    goal_id = str(goal_id or "")
    if not goal_id:
        return
    with _LOCK:
        _AUTHORIZATIONS.pop(_key(goal_id), None)


def armed_epoch(goal_id: str) -> Optional[str]:
    """The epoch this process currently holds for ``goal_id``, or None when unarmed."""
    rec = _live(goal_id)
    return rec.owner_epoch if rec is not None else None


def is_armed(goal_id: str) -> bool:
    return _live(goal_id) is not None


# ── tickets ────────────────────────────────────────────────────────────


def issue_ticket(
    goal_id: str, *, expected_revision: int, source_kind: str, round_id: str = "",
) -> Optional[GoalTicket]:
    """Mint the ticket for one continuation, or None when this host is not authorized.

    The minted ticket also becomes the goal's pending ticket so a drain-time check in another
    stack frame (the queue drain, not the judge) can re-validate the very same request.
    """
    rec = _live(goal_id)
    if rec is None:
        return None
    ticket = GoalTicket(
        goal_id=goal_id, expected_revision=int(expected_revision),
        owner_epoch=rec.owner_epoch, round_id=round_id or uuid.uuid4().hex,
        source_kind=source_kind,
    )
    with _LOCK:
        rec.pending = ticket
    return ticket


def validate_ticket(
    ticket: Optional[GoalTicket], *, goal_id: str, revision: int, status: str,
) -> str:
    """Re-check a ticket against freshly-read state; "" when it may run, else why not.

    Shared by both layers: enqueue-time and start-time ask the same question, and differ only
    in how fresh ``revision`` / ``status`` are.
    """
    if ticket is None:
        return "no continuation ticket"
    goal_id = str(goal_id or "")
    if not ticket.goal_id or ticket.goal_id != goal_id:
        return "ticket names a different goal"
    if ticket.owner_epoch != armed_epoch(ticket.goal_id):
        return "execution authorization is not armed in this process"
    if ticket.expected_revision != int(revision):
        return "the goal changed after this continuation was queued"
    if status != "active":
        return f"the goal is {status}"
    return ""


def admit_queued(goal_id: str, *, revision: int, status: str) -> tuple[bool, str]:
    """Layer 2 — may the continuation queued for ``goal_id`` start a turn right now?

    Called by every surface immediately before a synthetic continuation becomes a real turn.
    It reads no state itself: the caller passes freshly-read persisted state, which is what
    makes a user pause / clear / edit that landed while the ticket sat in the queue stop it
    before any model call or tool execution.
    """
    rec = _live(goal_id)
    if rec is None:
        return False, "execution authorization is not armed in this process"
    ticket = rec.pending
    if ticket is None:
        return False, "no continuation is queued for this goal"
    if reason := validate_ticket(ticket, goal_id=goal_id, revision=revision, status=status):
        with _LOCK:
            if rec.pending is ticket:
                rec.pending = None
        return False, reason
    if not claim_start(goal_id, ticket.owner_epoch, ticket.round_id):
        return False, f"round {ticket.round_id} already started"
    return True, ""


# ── round ledger ───────────────────────────────────────────────────────


def _claim(seen: OrderedDict[str, None], round_id: str) -> bool:
    """True iff ``round_id`` is new; records it. Bounded so the ledger cannot grow."""
    if round_id in seen:
        seen.move_to_end(round_id)
        return False
    seen[round_id] = None
    while len(seen) > _MAX_REMEMBERED_ROUNDS:
        seen.popitem(last=False)
    return True


def _claim_under(goal_id: str, owner_epoch: str, round_id: str,
                 ledger: str) -> bool:
    """Record ``round_id`` in one of the goal's ledgers; True only the first time.

    ``ledger`` is ``"counted"`` (layer 1: the round was billed) or ``"started"`` (layer 2: the
    round began a turn).  A round the current epoch does not own is never claimable.
    """
    if not round_id:
        return False
    rec = _live(goal_id)
    if rec is None or rec.owner_epoch != owner_epoch:
        return False
    with _LOCK:
        return _claim(getattr(rec, ledger), round_id)


def claim_round(goal_id: str, owner_epoch: str, round_id: str) -> bool:
    """Layer 1 — account one continuation round.  True only the FIRST time it is claimed, so a
    repeated post-turn callback cannot bill the same round (judge call + turn budget) twice."""
    return _claim_under(goal_id, owner_epoch, round_id, "counted")


def claim_start(goal_id: str, owner_epoch: str, round_id: str) -> bool:
    """Layer 2 — mark one round as actually started.  True only the first time, so a duplicate
    queue promotion cannot start a second turn for the same round."""
    return _claim_under(goal_id, owner_epoch, round_id, "started")


def _fork_child_reset() -> None:
    """A forked child inherits the parent's table copy-on-write; none of it is the child's."""
    with _LOCK:
        _AUTHORIZATIONS.clear()


if hasattr(os, "register_at_fork"):  # pragma: no cover - POSIX only
    os.register_at_fork(after_in_child=_fork_child_reset)
