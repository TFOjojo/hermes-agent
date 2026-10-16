from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from gateway.config import Platform
from gateway.hooks import HookRegistry
from gateway.run import TurnRunner


def _ctx():
    hooks = HookRegistry()
    for event in ("tool:start", "tool:end", "tool:error"):
        hooks._handlers[event] = [lambda *_: None]
    return SimpleNamespace(
        source=SimpleNamespace(
            platform=Platform.TELEGRAM,
            user_id="u",
            chat_id="c",
            chat_type="group",
            thread_id="thread-2",
        ),
        session_id="s",
        progress_queue=None,
        log_queue=None,
        _run_still_current=lambda: True,
        _loop_for_step=MagicMock(),
        _hooks_ref=hooks,
        tool_progress_enabled=False,
        _thinking_enabled=False,
        log_mode_enabled=False,
        _live_status_adapter=None,
        needs_progress_queue=False,
    )


def test_registry_reports_only_requested_events():
    registry = HookRegistry()
    registry._handlers["gateway:startup"] = [lambda *_: None]
    assert registry.has_handlers("gateway:startup") is True
    assert registry.has_handlers("tool:start", "tool:end", "tool:error") is False
    registry._handlers["tool:error"] = [lambda *_: None]
    assert registry.has_handlers("tool:start", "tool:end", "tool:error") is True


def test_tool_hook_start_carries_call_and_conversation_ids():
    ctx = _ctx()
    runner = TurnRunner(MagicMock(), ctx)
    with patch("gateway.run.safe_schedule_threadsafe") as schedule:
        runner.progress_callback(
            "tool.started", "terminal", "run", {"command": "pwd"},
            tool_call_id="call-17",
        )
    coro, _loop = schedule.call_args.args
    locals_ = coro.cr_frame.f_locals
    context = locals_["context"]
    assert locals_["event_type"] == "tool:start"
    assert context["tool_call_id"] == "call-17"
    assert context["chat_id"] == "c"
    assert context["chat_type"] == "group"
    assert context["thread_id"] == "thread-2"
    coro.close()


def test_tool_hook_error_carries_same_call_id_error_and_duration_units():
    ctx = _ctx()
    runner = TurnRunner(MagicMock(), ctx)
    with patch("gateway.run.safe_schedule_threadsafe") as schedule:
        runner.progress_callback(
            "tool.completed", "terminal", None, None,
            tool_call_id="call-17", duration=1.25, is_error=True,
        )
    coro, _loop = schedule.call_args.args
    locals_ = coro.cr_frame.f_locals
    context = locals_["context"]
    assert locals_["event_type"] == "tool:error"
    assert context["tool_call_id"] == "call-17"
    assert context["ok"] is False
    assert context["duration"] == 1.25
    assert context["duration_seconds"] == 1.25
    coro.close()
