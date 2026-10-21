"""Completed Discord model pickers must never be overwritten by timeout UI."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from plugins.platforms.discord.adapter import ModelPickerView


def make_picker(monkeypatch, callback=None):
    callback = callback or AsyncMock(return_value="Model switched successfully")
    view = ModelPickerView(
        providers=[{"slug": "fixture", "name": "Fixture", "models": ["fixture-model"]}],
        current_model="old-model", current_provider="fixture", session_key="fixture-session",
        on_model_selected=callback, allowed_user_ids={"123"},
    )
    view._selected_provider = "fixture"
    # The shared Discord test double has no scheduler. Observe its cancellation seam.
    monkeypatch.setattr(view, "stop", Mock(), raising=False)
    view._message = SimpleNamespace(edit=AsyncMock())
    interaction = SimpleNamespace(
        user=SimpleNamespace(id=123), channel_id=456,
        response=SimpleNamespace(edit_message=AsyncMock(), send_message=AsyncMock()),
        edit_original_response=AsyncMock(),
    )
    return view, interaction, callback


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["success", "switch_error", "cancel", "edit_error"])
async def test_terminal_picker_cannot_be_replaced_by_expired_notice(monkeypatch, outcome):
    view, interaction, callback = make_picker(monkeypatch)
    if outcome == "switch_error":
        callback.side_effect = RuntimeError("fixture switch failure")
    if outcome == "edit_error":
        interaction.response.edit_message.side_effect = RuntimeError("fixture transport failure")
        with pytest.raises(RuntimeError, match="fixture transport failure"):
            await view._switch_selected_model(interaction, "fixture-model")
    elif outcome == "cancel":
        await view._on_cancel(interaction)
    else:
        await view._switch_selected_model(interaction, "fixture-model")

    assert view.resolved
    # A timeout already queued before cancellation must also be harmless.
    await view.on_timeout()
    view._message.edit.assert_not_awaited()
    view.stop.assert_called_once()
    if outcome in {"success", "switch_error"}:
        callback.assert_awaited_once_with("456", "fixture-model", "fixture")
    else:
        callback.assert_not_awaited()


@pytest.mark.asyncio
async def test_timeout_during_switch_callback_preserves_final_result(monkeypatch):
    view, interaction, _ = make_picker(monkeypatch)

    async def switch_callback(*args):
        view.stop.assert_called_once()
        await view.on_timeout()
        view._message.edit.assert_not_awaited()
        return "Model switched successfully"

    view.on_model_selected = switch_callback
    await view._switch_selected_model(interaction, "fixture-model")
    assert interaction.edit_original_response.await_args.kwargs["embed"].description == "Model switched successfully"


@pytest.mark.asyncio
async def test_unfinished_picker_still_expires_once_without_switching(monkeypatch):
    view, interaction, callback = make_picker(monkeypatch)
    await view.on_timeout()
    assert view.resolved
    assert view.children == []
    assert "expired" in view._message.edit.await_args.kwargs["embed"].description.lower()
    callback.assert_not_awaited()
    await view.on_timeout()
    view._message.edit.assert_awaited_once()
