"""Discord reply-context routing note (split out of run_inbound.py for FILE_LINES)."""

from __future__ import annotations

from typing import Any


def discord_triggering_note(message_id: Any) -> str:
    """Model-facing routing note for a Discord turn (rides the API-bound user message only)."""
    return (
        f"[Triggering message id: `{message_id}` — use as `message_id` for reply/react/pin "
        f"via the discord tools.]"
    )
