"""Carry the persistent /goal across a context-compression rotation.

Compression mints a fresh child session id while ``load_goal`` does a flat
per-session lookup with no parent walk, so without an explicit transfer an
active goal silently dies at the boundary (#33618). A refusal is NOT silent:
the goal stays on the parent session, so the user is told — otherwise they
watch a live goal disappear (W2).
"""

from __future__ import annotations

from typing import Any


def carry_goal_to_child(agent: Any, old_session_id: str) -> None:
    """Migrate the goal; warn the user when the move is refused."""
    from hermes_cli.goals import migrate_goal_to_session

    if not migrate_goal_to_session(old_session_id, agent.session_id, reason="compression"):
        agent._emit_warning(
            "⚠️ Your active /goal was not carried into this session (another goal already "
            "exists here, or the session store refused the move). It stays on the previous "
            "session and will NOT continue automatically. Run /goal resume here once that "
            "goal is cleared, or /goal <text> to start a fresh one in this session."
        )
