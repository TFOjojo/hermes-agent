"""Kanban Recovery Controller — lifecycle hooks and observe-only logic.

When registered, this module hooks into kanban lifecycle events to
automatically record recovery events in the audit ledger.  Currently
an **observe-only controller**: it records events but never applies
non-readonly actions (retry, reassign, etc.).

Registration::

    from plugins.kanban.recovery import lifecycle_hooks
    lifecycle_hooks.register(ctx)
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Safe categories that may produce observe-only recovery candidates.
SAFE_CATEGORIES = frozenset({"tool_loop_halt", "crashed", "rate_limited"})

# Outcomes that must NEVER produce a recovery candidate.
UNSAFE_OUTCOMES = frozenset({"needs_input", "security", "auth", "terminal_provider"})

# Mapping from recovery_class to recommended action description.
_RECOMMENDED_ACTIONS = {
    "tool_loop_halt": "observe_and_log",
    "crashed": "observe_and_log",
    "rate_limited": "backoff_and_retry_later",
}


# ---------------------------------------------------------------------------
# Pure classification functions
# ---------------------------------------------------------------------------

def classify_exit(
    *,
    exit_kind: str = "",
    outcome: str = "",
) -> dict:
    """Classify a worker exit event into a recovery candidate description.

    Returns a dict with:
      - ``recovery_class``: str or None (None = no candidate)
      - ``is_safe``: True if the class is in the observe-only safe set
      - ``automation_level``: "observe_only" | "manual" | "none"
    """
    # Unsafe outcomes always produce no candidate.
    if outcome in UNSAFE_OUTCOMES:
        return {
            "recovery_class": None,
            "is_safe": False,
            "automation_level": "manual",
        }

    # Determine recovery class from exit_kind + outcome.
    recovery_class: Optional[str] = None
    if outcome == "rate_limited" or exit_kind == "rate_limited":
        recovery_class = "rate_limited"
    elif outcome == "tool_loop_halt":
        # Explicit tool_loop_halt tag from caller.
        recovery_class = "tool_loop_halt"
    elif outcome == "crashed" or exit_kind in ("nonzero_exit", "signaled", "unknown"):
        # crashed dead PID — collapsed into "crashed" unless caller tagged tool_loop_halt
        recovery_class = "crashed"

    is_safe = recovery_class in SAFE_CATEGORIES if recovery_class else False
    automation_level = "observe_only" if is_safe else "manual"

    return {
        "recovery_class": recovery_class,
        "is_safe": is_safe,
        "automation_level": automation_level,
    }


# ---------------------------------------------------------------------------
# Fingerprint
# ---------------------------------------------------------------------------

def compute_fingerprint(
    *,
    task_id: str,
    run_id: Any,
    exit_class: str,
) -> str:
    """Compute a stable, deterministic fingerprint for a recovery event."""
    parts = f"{task_id}:{run_id}:{exit_class}"
    return hashlib.sha256(parts.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# CAS (Compare-And-Swap) snapshot
# ---------------------------------------------------------------------------

def build_cas_snapshot(task: dict) -> dict:
    """Build a CAS snapshot from the current task state.

    The snapshot records the expected state at observation time so that
    later verification can detect if the task has moved on.
    """
    return {
        "task_id": task.get("id", ""),
        "expected_status": task.get("status", ""),
        "expected_run_id": task.get("run_id"),
        "expected_latest_event_id": task.get("latest_event_id"),
        "fingerprint": compute_fingerprint(
            task_id=task.get("id", ""),
            run_id=task.get("run_id"),
            exit_class="cas_snapshot",
        ),
    }


def verify_cas(snapshot: dict, current_task: dict) -> bool:
    """Verify that the current task state matches the CAS snapshot.

    Returns True if all expected fields still match (task hasn't moved on).
    Returns False if any field has changed (stale candidate).
    """
    if snapshot.get("task_id") != current_task.get("id"):
        return False
    if snapshot.get("expected_status") != current_task.get("status"):
        return False
    if snapshot.get("expected_run_id") != current_task.get("run_id"):
        return False
    if snapshot.get("expected_latest_event_id") != current_task.get("latest_event_id"):
        return False
    return True


# ---------------------------------------------------------------------------
# Controller handlers — observe-only, never mutate tasks
# ---------------------------------------------------------------------------

def on_worker_exited(
    payload: Optional[dict] = None,
    *,
    task_reader: Optional[Callable[[str], Optional[dict]]] = None,
    cas_snapshot: Optional[dict] = None,
    **hook_payload: Any,
) -> Optional[dict]:
    """Handle an observe-only worker-exit event.

    ``payload`` preserves the pure-function test surface; real Hermes hook
    delivery supplies keyword arguments, which are merged without mutation.
    Derives candidates only for safe categories and never mutates task state.
    """
    payload = {**(payload or {}), **hook_payload}
    exit_kind = payload.get("exit_kind", "")
    outcome = payload.get("outcome", "")
    task_id = payload.get("task_id", "")
    run_id = payload.get("run_id")
    latest_event_id = payload.get("latest_event_id")

    # Classify the exit.
    classification = classify_exit(exit_kind=exit_kind, outcome=outcome)

    if not classification["is_safe"]:
        return None

    recovery_class = classification["recovery_class"]
    fingerprint = compute_fingerprint(
        task_id=task_id, run_id=run_id, exit_class=recovery_class,
    )

    candidate = {
        "task_id": task_id,
        "run_id": run_id,
        "recovery_class": recovery_class,
        "fingerprint": fingerprint,
        "action_type": "observe",
        "is_safe": True,
        "automation_level": classification["automation_level"],
    }

    # CAS verification via injected task_reader (dependency injection).
    if task_reader is not None and cas_snapshot is not None:
        current_task = task_reader(task_id)
        if current_task is not None:
            if not verify_cas(cas_snapshot, current_task):
                # Task state has moved on — reject as stale.
                candidate["rejected"] = True
                candidate["stale"] = True
                candidate["rejection_reason"] = "CAS_mismatch"
                return candidate

    return candidate


def on_dispatch_tick(_payload: Optional[dict] = None, **_hook_payload: Any) -> None:
    """Handle on_kanban_dispatch_tick lifecycle event.

    Observe-only: returns None. Future phases may add monitoring logic.
    """
    return None


# ---------------------------------------------------------------------------
# Diagnosis projection
# ---------------------------------------------------------------------------

def project_diagnostic(entry: dict) -> dict:
    """Project a recovery entry/candidate into diagnostic fields.

    Returns a dict with:
      - recovery_class
      - recommended_action
      - automation_level (observe_only / L0 / manual)
      - cooldown_active
      - cas_eligible
    """
    recovery_class = entry.get("recovery_class")
    is_safe = recovery_class in SAFE_CATEGORIES if recovery_class else False

    if is_safe:
        automation_level = "observe_only"
        recommended_action = _RECOMMENDED_ACTIONS.get(recovery_class, "observe_and_log")
    elif recovery_class is None:
        automation_level = "manual"
        recommended_action = "requires_human"
    else:
        automation_level = "manual"
        recommended_action = "requires_human"

    return {
        "recovery_class": recovery_class,
        "recommended_action": recommended_action,
        "automation_level": automation_level,
        "cooldown_active": bool(entry.get("cooldown_active", False)),
        "cas_eligible": bool(entry.get("cas_eligible", False)),
    }
