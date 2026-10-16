"""Spawn-gate coverage for the steer level (task t_64a99696).

Complements test_default_spawn_constrain_overlong_raises_reject /
test_default_spawn_constrain_short_body_proceeds_to_launch: the same
``_default_spawn`` budget gate, exercised at ``steer`` so the four
acceptance scenarios (steer injection / constrain compression /
constrain over-limit reject / no-budget profile untouched) are all
covered through the real dispatch spawn path.
"""

import os
import sys
import tempfile

import pytest

from hermes_cli.kanban_token_budget import (
    BudgetDecision,
    BudgetLevel,
    ProfileUsage,
    TokenBudget,
)


@pytest.fixture()
def isolated_kanban_home(monkeypatch):
    """Local copy of the fixture in test_kanban_token_budget.py (module-local
    there, so not importable) — same purge semantics: fresh HERMES_HOME and a
    clean hermes_cli module cache so kanban_db re-resolves its paths."""
    test_home = tempfile.mkdtemp(prefix="kanban_token_budget_test_")
    for prof in ("alpha", "beta", "default"):
        os.makedirs(os.path.join(test_home, "profiles", prof), exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", test_home)
    for mod in list(sys.modules.keys()):
        if (
            mod.startswith("hermes_cli")
            or mod.startswith("hermes_state")
            or mod == "hermes_constants"
        ):
            del sys.modules[mod]
    from hermes_cli import kanban_db

    yield kanban_db


def _steer_decision():
    return BudgetDecision(
        profile="alpha",
        level=BudgetLevel.STEER,
        pct=0.85,
        usage=ProfileUsage(daily_tokens=85),
        budget=TokenBudget(daily=100),
    )


def _steer_task(kb):
    from hermes_cli.kanban_db import Task

    return Task(
        id="t_steer_gate",
        title="steer gate check",
        # Even a long body must NOT be rejected at steer level — the
        # constrain-only body limit is the entire point of the ladder.
        body="x" * 9000,
        assignee="alpha",
        status="running",
        priority=0,
        created_by="test",
        created_at=0,
        started_at=None,
        completed_at=None,
        workspace_kind="scratch",
        workspace_path=None,
        claim_lock=None,
        claim_expires=None,
        tenant=None,
    )


def test_default_spawn_steer_injects_note_and_launches(isolated_kanban_home, monkeypatch):
    """Steer: task spawns normally with the reminder injected (no reject)."""
    kb = isolated_kanban_home
    import hermes_cli.kanban_token_budget as budget_mod

    monkeypatch.setattr(
        budget_mod,
        "check_profile_budget",
        lambda profile, home, **kw: _steer_decision(),
    )

    task = _steer_task(kb)
    workspace = kb.tempfile.mkdtemp(prefix="budget_steer_ws_") if hasattr(
        kb, "tempfile"
    ) else None
    if workspace is None:
        import tempfile

        workspace = tempfile.mkdtemp(prefix="budget_steer_ws_")

    launched = {}

    class _FakeProc:
        pid = 99998

    import subprocess as _sp

    def _fake_popen(cmd, **kwargs):
        launched["cmd"] = cmd
        launched["env"] = kwargs.get("env", {})
        return _FakeProc()

    monkeypatch.setattr(_sp, "Popen", _fake_popen)
    pid = kb._default_spawn(task, workspace)
    assert pid == 99998
    # Level marker + steer reminder are injected into the worker env...
    assert launched["env"].get("HERMES_KANBAN_TOKEN_LEVEL") == "steer"
    assert "token-budget steer" in launched["env"].get("HERMES_KANBAN_TOKEN_STEER", "")
    # ...and the reminder is appended to the quiet-mode prompt itself
    # (cmd ends with [..., "chat", "-q", <prompt>]).
    prompt = launched["cmd"][-1]
    assert isinstance(prompt, str)
    assert "token-budget steer" in prompt


def test_default_spawn_no_budget_profile_zero_interference(isolated_kanban_home, monkeypatch):
    """No configured budget → the gate must be an early-exit no-op.

    check_profile_budget returning None (feature disabled / profile has no
    budget) means _default_spawn must reach Popen with NO token-budget env
    markers and an unamended prompt — identical to the pre-feature path.
    """
    kb = isolated_kanban_home
    import hermes_cli.kanban_token_budget as budget_mod

    monkeypatch.setattr(
        budget_mod, "check_profile_budget", lambda profile, home, **kw: None
    )

    task = _steer_task(kb)
    task.id = "t_no_budget"
    task.body = "plain spec"
    import tempfile

    workspace = tempfile.mkdtemp(prefix="budget_none_ws_")

    launched = {}

    class _FakeProc:
        pid = 99997

    import subprocess as _sp

    def _fake_popen(cmd, **kwargs):
        launched["cmd"] = cmd
        launched["env"] = kwargs.get("env", {})
        return _FakeProc()

    monkeypatch.setattr(_sp, "Popen", _fake_popen)
    pid = kb._default_spawn(task, workspace)
    assert pid == 99997
    # Zero-interference: no level marker, no steer note, prompt untouched
    # (the prompt _default_spawn itself builds — no budget suffix appended).
    assert "HERMES_KANBAN_TOKEN_LEVEL" not in launched["env"]
    assert "HERMES_KANBAN_TOKEN_STEER" not in launched["env"]
    assert "token-budget" not in launched["cmd"][-1]
