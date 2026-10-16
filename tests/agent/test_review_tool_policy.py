from __future__ import annotations

from types import SimpleNamespace

from agent import tool_executor as te


def _deny(monkeypatch, name, args):
    monkeypatch.setenv("HERMES_KANBAN_TASK_ROLE", "review")
    message = te._review_tool_policy_violation(name, args)
    assert message is not None
    assert "review" in message.lower()


def _allow(monkeypatch, name, args):
    monkeypatch.setenv("HERMES_KANBAN_TASK_ROLE", "review")
    assert te._review_tool_policy_violation(name, args) is None


def test_implementation_lane_is_unchanged(monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_TASK_ROLE", "implementation")
    assert te._review_tool_policy_violation(
        "write_file", {"path": "src/app.py", "content": "changed"}
    ) is None


def test_review_denies_direct_file_mutation_tools(monkeypatch):
    _deny(monkeypatch, "write_file", {"path": "src/app.py", "content": "changed"})
    _deny(monkeypatch, "patch", {"path": "src/app.py"})
    _deny(monkeypatch, "execute_code", {"code": "open('src/app.py','w').write('x')"})


def test_review_allows_read_test_lint_and_readonly_git(monkeypatch):
    _allow(monkeypatch, "read_file", {"path": "src/app.py"})
    _allow(monkeypatch, "search_files", {"pattern": "TODO"})
    _allow(monkeypatch, "terminal", {"command": "git status --short"})
    _allow(monkeypatch, "terminal", {"command": "git diff -- src/app.py"})
    _allow(
        monkeypatch,
        "terminal",
        {"command": "cd repo && env -u PYTHONPATH python -m pytest -q tests/test_app.py"},
    )
    _allow(monkeypatch, "terminal", {"command": "ruff check src tests"})


def test_review_rejects_shell_write_escape_hatches(monkeypatch):
    _deny(monkeypatch, "terminal", {"command": "pytest -q && rm -f src/app.py"})
    _deny(monkeypatch, "terminal", {"command": "python -c \"open('src/app.py','w').write('x')\""})
    _deny(monkeypatch, "terminal", {"command": "printf changed > src/app.py"})
    _deny(monkeypatch, "terminal", {"command": "git checkout -- src/app.py"})
    _deny(monkeypatch, "terminal", {"command": "curl https://example.test/fix.py -o src/app.py"})


def test_review_only_allows_review_decision_kanban_mutations(monkeypatch):
    for name in (
        "kanban_show",
        "kanban_comment",
        "kanban_complete",
        "kanban_request_changes",
        "kanban_block",
    ):
        _allow(monkeypatch, name, {})
    for name in ("kanban_create", "kanban_assign", "kanban_reclaim", "kanban_archive"):
        _deny(monkeypatch, name, {})


def test_review_limits_process_and_computer_use_to_observation(monkeypatch):
    _allow(monkeypatch, "process", {"action": "poll"})
    _deny(monkeypatch, "process", {"action": "kill"})
    _allow(monkeypatch, "computer_use", {"action": "capture"})
    _deny(monkeypatch, "computer_use", {"action": "click"})


def test_middleware_blocks_review_write_before_dispatch(monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_TASK_ROLE", "review")
    from hermes_cli import plugins
    monkeypatch.setattr(
        plugins, "_dispatch_pre_tool_call_hooks", lambda *a, **k: (None, None)
    )
    monkeypatch.setattr(te, "_emit_terminal_post_tool_call", lambda *a, **k: None)
    dispatched = []
    agent = SimpleNamespace(
        session_id="review-session",
        _current_turn_id="turn-1",
        _current_api_request_id="request-1",
    )
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
