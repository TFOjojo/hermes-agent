"""Compression must carry an active /goal onto the child session — and say so when refused.

Compression mints a fresh child session id while ``load_goal`` does a flat per-session
lookup with no parent walk, so an untransferred goal dies at the boundary (#33618). Since
0.22.x upstream performs the transfer inside ``agent.compression_automation``; the local
requirement is that a REFUSED transfer is not silent (W2): the goal stays on the parent
session, so the user has to be told instead of watching it disappear.
"""

from __future__ import annotations

import sys
import types

from agent.conversation_compression_goal_carry import carry_goal_to_child


class _Agent:
    def __init__(self, warnings=None):
        self.session_id = "s_child"
        self.warnings = warnings if warnings is not None else []
        self._session_db = object()

    def _emit_warning(self, message):
        self.warnings.append(message)


def _stub_goals(monkeypatch, *, result):
    calls = []

    module = types.ModuleType("hermes_cli.goals")

    def migrate_goal_to_session(old_session_id, new_session_id, *, reason=""):
        calls.append((old_session_id, new_session_id, reason))
        return result

    module.migrate_goal_to_session = migrate_goal_to_session
    monkeypatch.setitem(sys.modules, "hermes_cli.goals", module)
    return calls


def test_carry_goal_migrates_with_compression_reason(monkeypatch):
    calls = _stub_goals(monkeypatch, result=True)
    agent = _Agent()
    carry_goal_to_child(agent, "s_parent")
    assert calls == [("s_parent", "s_child", "compression")]
    assert agent.warnings == []


def test_refused_carry_warns_instead_of_vanishing(monkeypatch):
    _stub_goals(monkeypatch, result=False)
    agent = _Agent()
    carry_goal_to_child(agent, "s_parent")
    assert len(agent.warnings) == 1
    warning = agent.warnings[0]
    assert "/goal" in warning
    assert "not carried" in warning
    assert "/goal resume" in warning


def test_compression_automation_routes_the_goal_through_the_warning_path(monkeypatch):
    """The boundary hook must call carry_goal_to_child, not a silent migrate()."""
    import agent.compression_automation as automation
    import agent.conversation_compression_goal_carry as carry_mod

    seen = []

    def fake_carry(agent, old_session_id):
        seen.append((agent.session_id, old_session_id))

    monkeypatch.setattr(carry_mod, "carry_goal_to_child", fake_carry)
    monkeypatch.setattr(automation, "_swallow_stub", None, raising=False)

    class _Swallow:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    import agent.conversation_compression as compression
    monkeypatch.setattr(compression, "_swallow", _Swallow)

    for mod_name, fn_name in (
        ("hermes_cli.heartbeat", "migrate_heartbeat_to_session"),
        ("hermes_cli.loops", "migrate_loop_to_session"),
    ):
        stub = types.ModuleType(mod_name)
        setattr(stub, fn_name, lambda *a, **k: True)
        monkeypatch.setitem(sys.modules, mod_name, stub)

    agent = _Agent()
    automation.carry_compression_automation(agent, "s_parent")
    assert seen == [("s_child", "s_parent")]
