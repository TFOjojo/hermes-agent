"""Regression test: the review read-only verdict must actually block.

A prior refactor carried scope/plugin/pruned-arg verdicts through ``block_body``
but left the review verdict in ``block_message`` only. The guardrail gate
(``if block_body is None``) then still consulted the loop guardrails, and an
allow decision let the call dispatch with the review violation silently
dropped — the write tool executed under a read-only review lane.

These tests assert the effect (no dispatch, blocked result, error type), not the
refactor shape, so the same class of regression cannot pass again.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from agent import tool_executor as te


def _agent(monkeypatch, *, guardrails):
    """Minimal AIAgent-like stub: only collaborators the dispatch path touches."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "")
    monkeypatch.setenv("HERMES_INFERENCE_PROVIDER", "")
    agent = SimpleNamespace(
        session_id="review-session",
        _current_turn_id="turn-1",
        _current_api_request_id="request-1",
        quiet_mode=True,
        verbose_logging=False,
        log_prefix_chars=200,
        tool_progress_callback=None,
        tool_start_callback=None,
        tool_complete_callback=None,
        tool_progress_mode="off",
        _checkpoint_mgr=SimpleNamespace(enabled=False),
        _current_tool=None,
        _touch_activity=lambda *a, **k: None,
        _tool_guardrails=guardrails,
    )
    agent._guardrail_block_result = lambda decision: json.dumps({"error": "guardrail"})
    return agent


class _PermissiveGuardrails:
    """A loop guardrail that allows everything — but records that it was consulted."""

    def __init__(self):
        self.calls = []

    def before_call(self, name, args):
        self.calls.append((name, dict(args or {})))
        return SimpleNamespace(action="allow", code="allow", allows_execution=True, message=None)

    def after_call(self, *a, **k):
        return None


def test_review_write_is_blocked_even_when_loop_guardrails_allow(monkeypatch):
    """A review-lane write tool must not dispatch merely because guardrails allow it."""
    monkeypatch.setenv("HERMES_KANBAN_TASK_ROLE", "review")
    guardrails = _PermissiveGuardrails()
    agent = _agent(monkeypatch, guardrails=guardrails)
    monkeypatch.setattr(te, "_emit_terminal_post_tool_call", lambda *a, **k: None)

    dispatched: list[dict] = []
    managed = te._run_agent_tool_execution_middleware(
        agent,
        function_name="patch",
        function_args={"path": "src/app.py", "new_string": "changed"},
        effective_task_id="t_review",
        tool_call_id="call-1",
        execute=lambda args: dispatched.append(args),
    )

    assert managed.blocked is True
    assert dispatched == []
    assert "read-only" in str(managed.result)
    assert "patch" in str(managed.result)


def test_review_block_never_consults_loop_guardrails(monkeypatch):
    """A blocked call must not spend guardrail state: mirrors the scope/plugin contract."""
    monkeypatch.setenv("HERMES_KANBAN_TASK_ROLE", "review")
    guardrails = _PermissiveGuardrails()
    agent = _agent(monkeypatch, guardrails=guardrails)
    monkeypatch.setattr(te, "_emit_terminal_post_tool_call", lambda *a, **k: None)

    te._run_agent_tool_execution_middleware(
        agent,
        function_name="write_file",
        function_args={"path": "src/app.py", "content": "changed"},
        effective_task_id="t_review",
        tool_call_id="call-2",
        execute=lambda args: None,
    )

    assert guardrails.calls == []


def test_implementation_lane_still_consults_guardrails_and_dispatches(monkeypatch):
    """The fix must not widen the short-circuit: implementation keeps the full chain."""
    monkeypatch.setenv("HERMES_KANBAN_TASK_ROLE", "implementation")
    guardrails = _PermissiveGuardrails()
    agent = _agent(monkeypatch, guardrails=guardrails)
    monkeypatch.setattr(te, "_emit_terminal_post_tool_call", lambda *a, **k: None)

    dispatched: list[dict] = []
    managed = te._run_agent_tool_execution_middleware(
        agent,
        function_name="write_file",
        function_args={"path": "src/app.py", "content": "changed"},
        effective_task_id="t_impl",
        tool_call_id="call-3",
        execute=lambda args: dispatched.append(args),
    )

    assert managed.blocked is False
    assert dispatched == [{"path": "src/app.py", "content": "changed"}]
    assert guardrails.calls == [("write_file", {"path": "src/app.py", "content": "changed"})]


def test_review_readonly_tool_still_passes_through_guardrails(monkeypatch):
    """An allowlisted read-only tool must reach the guardrails, not be blanket-blocked."""
    monkeypatch.setenv("HERMES_KANBAN_TASK_ROLE", "review")
    guardrails = _PermissiveGuardrails()
    agent = _agent(monkeypatch, guardrails=guardrails)
    monkeypatch.setattr(te, "_emit_terminal_post_tool_call", lambda *a, **k: None)

    dispatched: list[dict] = []
    managed = te._run_agent_tool_execution_middleware(
        agent,
        function_name="read_file",
        function_args={"path": "src/app.py"},
        effective_task_id="t_review",
        tool_call_id="call-4",
        execute=lambda args: dispatched.append(args) or "file contents",
    )

    assert managed.blocked is False
    assert managed.result == "file contents"
    assert guardrails.calls == [("read_file", {"path": "src/app.py"})]
