"""Phase 2 diagnostics test: tool_loop_halt_guidance rule."""

from __future__ import annotations

import time

from hermes_cli import kanban_diagnostics as kd


def _task(**overrides):
    base = {
        "id": "t_halt00",
        "title": "halt test task",
        "assignee": "workhorse",
        "status": "blocked",
        "consecutive_failures": 1,
        "last_failure_error": "Tool guardrail halted terminal: same_tool_failure_halt (count=8)",
    }
    base.update(overrides)
    return base


def _gave_up_event(
    *,
    tool_name="terminal",
    reason_code="same_tool_failure_halt",
    count=8,
    fingerprint="abc123def4567890",
    ts=None,
):
    return {
        "kind": "gave_up",
        "created_at": int(ts or time.time()),
        "payload": {
            "failure_class": "tool_loop_halt",
            "exit_class": "tool_loop_halt",
            "tool_name": tool_name,
            "reason_code": reason_code,
            "count": count,
            "failure_fingerprint": fingerprint,
            "handoff_required": True,
            "recommended_action": "change-strategy-or-request-review",
        },
    }


def test_tool_loop_halt_guidance_fires_on_blocked_task():
    now = 100_000
    task = _task(status="blocked")
    events = [_gave_up_event(ts=now)]

    diags = kd.compute_task_diagnostics(task, events, [], now=now)
    halt_diags = [d for d in diags if d.kind == "tool_loop_halt_guidance"]
    assert len(halt_diags) == 1
    d = halt_diags[0]
    assert d.severity == "error"
    assert "terminal" in d.title
    assert d.data["tool_name"] == "terminal"
    assert d.data["reason_code"] == "same_tool_failure_halt"
    assert d.data["count"] == 8
    assert d.data["failure_fingerprint"] == "abc123def4567890"
    assert d.first_seen_at == now
    assert d.last_seen_at == now
    assert d.count == 1


def test_tool_loop_halt_guidance_does_not_fire_on_done_task():
    now = 100_000
    task = _task(status="done")
    events = [_gave_up_event(ts=now)]

    diags = kd.compute_task_diagnostics(task, events, [], now=now)
    halt_diags = [d for d in diags if d.kind == "tool_loop_halt_guidance"]
    assert len(halt_diags) == 0


def test_tool_loop_halt_clears_after_completion():
    """A clean completion after the halt event suppresses the diagnostic."""
    now = 100_000
    task = _task(status="blocked")
    events = [
        _gave_up_event(ts=now - 100),
        {"kind": "completed", "created_at": now, "payload": None},
    ]

    diags = kd.compute_task_diagnostics(task, events, [], now=now)
    halt_diags = [d for d in diags if d.kind == "tool_loop_halt_guidance"]
    assert len(halt_diags) == 0


def test_tool_loop_halt_ignores_non_tool_loop_halt_gave_up():
    """A generic gave_up (not tool_loop_halt) should not trigger this rule."""
    now = 100_000
    task = _task(status="blocked")
    events = [
        {
            "kind": "gave_up",
            "created_at": now,
            "payload": {
                "failure_class": "transient_provider",
                "trigger_outcome": "timed_out",
            },
        }
    ]

    diags = kd.compute_task_diagnostics(task, events, [], now=now)
    halt_diags = [d for d in diags if d.kind == "tool_loop_halt_guidance"]
    assert len(halt_diags) == 0


def test_tool_loop_halt_guidance_has_suggested_action():
    now = 100_000
    task = _task(status="blocked")
    events = [_gave_up_event(ts=now)]

    diags = kd.compute_task_diagnostics(task, events, [], now=now)
    halt_diags = [d for d in diags if d.kind == "tool_loop_halt_guidance"]
    assert len(halt_diags) == 1
    actions = halt_diags[0].actions
    assert len(actions) >= 1
    suggested = [a for a in actions if a.suggested]
    assert len(suggested) == 1
    assert suggested[0].kind == "cli_hint"
