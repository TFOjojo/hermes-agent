"""Goal execution authorization and continuation tickets — the W2 sibling of ``goals.py``.

Split out of the facade for the same reason W1 split ``goal_gates.py``, ``goal_kanban_loop.py``
and ``hermes_state_meta_txn.py``: ``goals.py`` carries a 2000-line ratchet, and this behaviour
is a coherent unit that other packages import directly rather than through the facade.

Everything here answers ONE question: may this process spend another turn on this goal, and may
the continuation it queued still start?  ``goal_admission`` owns the process-local registry; this
module owns the ``GoalManager``-facing surface over it — arming on an explicit user action,
disarming on every stop, and minting a ticket for every continuation handed out.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from hermes_cli import goal_admission

logger = logging.getLogger(__name__)


class GoalAuthorizationMixin:
    """Arm / disarm / ticket behaviour mixed into ``GoalManager``.

    A mixin rather than free functions because every method reads ``self._state`` and calls
    ``self._save()``: the authorization is a property of the goal the manager holds, not of a
    detached snapshot.
    """

    def _disarm(self) -> None:
        """Drop this goal's process-local execution authorization (no-op when unarmed)."""
        state = self._state
        if state is not None and state.goal_id:
            goal_admission.disarm(state.goal_id, reason="goal stopped")

    def _arm_if_budget(self, source_kind: str) -> None:
        """Arm execution authorization for the goal this manager holds.

        An explicit user create / resume is the ONLY thing that grants authorization, and it
        grants no more than the remaining turn budget: resuming a goal with nothing left to
        spend re-authorizes nothing.  Reading state (a fresh manager, a status query) neither
        arms nor disarms — which is what lets the gateway rebuild the manager every round
        without disarming a long-running goal.
        """
        state = self._state
        if state is None or state.status != "active" or not state.goal_id:
            return
        if state.turns_used >= state.max_turns:
            return
        goal_admission.arm(state.goal_id, source_kind=source_kind)

    def _authorization_gate(self) -> Optional[dict[str, Any]]:
        """The refusal decision when this process may NOT run another round, else None.

        A goal that looks perfectly healthy in the DB can still be unauthorized: a fresh
        process, a fork child, or a goal created before this build has no process-local
        authorization.  That is reported, never silently continued.
        """
        state = self._state
        if state is None or state.status != "active":
            return None  # the caller's own inactive check already answered this
        if not state.goal_id:
            # A pre-versioning row has no identity to authorize against. Its first authorized
            # change mints one (W1); doing it here means an old goal needs exactly one
            # /goal resume rather than a manual re-set.
            try:
                self._save()
            except Exception as exc:  # health: allow BLE001 -- store errors are unenumerable here; any failure fails closed to needs_reauth rather than continuing on an unidentified goal
                # The store refused, or the row moved under us. Fail closed: spending a judge
                # call on a goal whose identity could not be established buys nothing.
                logger.debug("goal authorization: identity mint failed for %s: %s",
                             getattr(self, "session_id", "?"), exc)
                return self._unauthorized_decision()
            state = self._state
            if state is None or state.status != "active":
                return None
        if goal_admission.is_armed(state.goal_id):
            return None
        return self._unauthorized_decision()

    def _unauthorized_decision(self) -> dict[str, Any]:
        from hermes_cli.goals import _decision

        return _decision(
            "active", False, None, "needs_reauth",
            "execution authorization is not armed in this process",
            goal_admission.NEEDS_REAUTH_MESSAGE,
        )

    def issue_continuation_ticket(self, source_kind: str, *, round_id: str = ""):
        """Mint the ticket for a continuation minted OUTSIDE the post-turn judge.

        An explicit ``/goal resume`` kickoff, a parked-goal barrier lift and the TUI's
        compression-exhaustion retry all hand out a continuation prompt directly. They carry the
        same admission contract as a judged one, so they get a ticket too — otherwise the
        drain-time check would refuse a turn the user explicitly asked for.
        """
        state = self._state
        if state is None or state.status != "active" or not state.goal_id:
            return None
        return goal_admission.issue_ticket(
            state.goal_id, expected_revision=state.revision, source_kind=source_kind,
            round_id=round_id)

    def _ticket_continuation(self, decision: dict[str, Any], round_id: str) -> dict[str, Any]:
        """Layer 1 — mint the ticket for a continuation this judge just authorized.

        Every decision that does NOT continue (paused, parked, done, budget exhausted) returns
        untouched, so a stop never leaves a live ticket behind for the drain-time check to find.
        The ticket rides on the decision as ``ticket`` for the surfaces that carry objects, and
        in the admission registry for the text-only ones.
        """
        if not decision.get("should_continue") or not decision.get("continuation_prompt"):
            return decision
        state = self._state
        if state is None or not state.goal_id:
            return decision
        ticket = self.issue_continuation_ticket("auto", round_id=round_id)
        if ticket is not None:
            decision["ticket"] = ticket
        return decision
