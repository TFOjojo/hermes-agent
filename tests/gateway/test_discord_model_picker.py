"""Regression tests for the Discord /model picker.

Uses the shared discord mock from tests/gateway/conftest.py (installed
at collection time via _ensure_discord_mock()). Previously this file
installed its own mock at module-import time and clobbered sys.modules,
breaking other gateway tests in the same process.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.platforms.base import utf16_len
from plugins.platforms.discord.adapter import ModelPickerView


@pytest.mark.asyncio
async def test_model_picker_clears_controls_before_running_switch_callback():
    events: list[object] = []

    async def on_model_selected(chat_id: str, model_id: str, provider_slug: str) -> str:
        events.append(("switch", chat_id, model_id, provider_slug))
        return "Model switched"

    async def edit_message(**kwargs):
        events.append(
            (
                "initial-edit",
                kwargs["embed"].title,
                kwargs["embed"].description,
                kwargs["view"],
            )
        )

    async def edit_original_response(**kwargs):
        events.append((
            "final-edit",
            kwargs["embed"].title,
            kwargs["embed"].description,
            kwargs["view"],
        ))

    view = ModelPickerView(
        providers=[
            {
                "slug": "copilot",
                "name": "GitHub Copilot",
                "models": ["gpt-5.4"],
                "total_models": 1,
                "is_current": True,
            }
        ],
        current_model="gpt-5-mini",
        current_provider="copilot",
        session_key="session-1",
        on_model_selected=on_model_selected,
        allowed_user_ids={"123"},  # matches the interaction user; empty = fail-closed
    )
    view._selected_provider = "copilot"

    interaction = SimpleNamespace(
        user=SimpleNamespace(id=123),
        channel_id=456,
        data={"values": ["gpt-5.4"]},
        response=SimpleNamespace(
            defer=AsyncMock(),
            send_message=AsyncMock(),
            edit_message=AsyncMock(side_effect=edit_message),
        ),
        edit_original_response=AsyncMock(side_effect=edit_original_response),
    )

    await view._on_model_selected(interaction)

    # Step 1: picking a model opens the reasoning step instead of switching immediately.
    assert events[0][0] == "initial-edit"
    assert events[0][1] == "🧠 思考强度设置"
    assert "gpt-5.4" in events[0][2]
    assert events[0][3] is view

    # Step 2: choosing "keep current" finishes the switch with no effort override.
    interaction.data = {"values": ["keep"]}
    await view._on_reasoning_selected(interaction)

    assert events[1:] == [
        ("initial-edit", "⚙ Switching Model", "Switching to `gpt-5.4`...", None),
        ("switch", "456", "gpt-5.4", "copilot"),
        ("final-edit", "⚙ Model Switched", "Model switched", None),
    ]
    interaction.response.edit_message.assert_awaited()
    interaction.response.defer.assert_not_called()
    interaction.edit_original_response.assert_awaited_once()


@pytest.mark.asyncio
async def test_reasoning_step_select_values_are_discord_legal():
    """Every reasoning-step option value must be 1..100 UTF-16 units.

    Discord rejects an out-of-range option value with 400 (50035 Invalid Form Body).
    The "keep current" option used to send value="", so after picking a model the
    picker message failed to update and eventually timed out.
    """
    view = ModelPickerView(
        providers=[
            {
                "slug": "local-hakimi",
                "name": "本地哈基米",
                "models": ["gpt-5-mini"],
                "total_models": 1,
                "is_current": True,
            }
        ],
        current_model="gpt-5-mini",
        current_provider="local-hakimi",
        session_key="session-1",
        on_model_selected=lambda *a, **k: None,
        allowed_user_ids={"123"},
    )
    view._selected_provider = "local-hakimi"

    captured: dict[str, object] = {}

    async def edit_message(**kwargs):
        captured["view"] = kwargs["view"]

    interaction = SimpleNamespace(
        user=SimpleNamespace(id=123),
        channel_id=456,
        data={"values": ["gpt-5-mini"]},
        response=SimpleNamespace(
            defer=AsyncMock(),
            send_message=AsyncMock(),
            edit_message=AsyncMock(side_effect=edit_message),
        ),
        edit_original_response=AsyncMock(),
    )

    await view._on_model_selected(interaction)

    rendered = captured["view"]
    selects = [child for child in rendered.children if getattr(child, "options", None)]
    assert selects, "reasoning step rendered no select menu"
    values = [opt.value for opt in selects[0].options]
    for value in values:
        assert 1 <= utf16_len(value) <= 100, f"illegal select value {value!r}"
    assert values[0] == "keep", "the keep-default option must still be selectable"


