"""W2 on the messaging-gateway surface.

The gateway is the surface that makes process-local authorization necessary: it builds a fresh
``GoalManager`` for EVERY turn (``_post_turn_manager`` -> ``factory(sid)``).  If armed
authorization lived on the manager instance, every round would look like "the user reopened the
session" and disarm a long-running goal (T07).

Also pinned here: one round is enqueued and billed once however many times its post-turn hook
fires (T06), a user ``/goal stop`` drops the queued next round before any model call (T09), and
the FIFO ordering that lets a simultaneous real user message win (T10).
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.session import SessionEntry, SessionSource, build_session_key
from hermes_cli import goal_admission, goals


@pytest.fixture()
def hermes_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    from hermes_constants import (
        reset_hermes_home_override,
        set_hermes_home_override,
    )

    token = set_hermes_home_override(str(home))
    goals._DB_CACHE.clear()
    goals._get_session_db()
    yield home
    try:
        reset_hermes_home_override(token)
    except Exception:
        pass
    goals._DB_CACHE.clear()


@pytest.fixture(autouse=True)
def _clean_admission_registry():
    goal_admission._AUTHORIZATIONS.clear()
    yield
    goal_admission._AUTHORIZATIONS.clear()


def _make_source() -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM,
        user_id="u1",
        chat_id="c1",
        user_name="tester",
        chat_type="dm",
    )


class _RecordingAdapter:
    """Minimal adapter: records send() and holds the pending-message FIFO slot."""

    def __init__(self) -> None:
        self._pending_messages: dict = {}
        self.sends: list[dict] = []

    async def send(self, chat_id: str, content: str, reply_to=None, metadata=None):
        self.sends.append({"chat_id": chat_id, "content": content})

        class _R:
            success = True
            message_id = "mock-msg"

        return _R()


def _make_runner(session_id: str | None = None):
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="***")},
    )
    runner.adapters = {}
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._queued_events = {}

    src = _make_source()
    session_entry = SessionEntry(
        session_key=build_session_key(src),
        session_id=session_id or f"gw-w2-{uuid.uuid4().hex}",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="dm",
    )
    runner.session_store = MagicMock()
    runner.session_store.get_or_create_session.return_value = session_entry
    runner.session_store._generate_session_key.return_value = build_session_key(src)

    adapter = _RecordingAdapter()
    runner.adapters[Platform.TELEGRAM] = adapter
    return runner, adapter, session_entry, src


async def _drain_until(condition, timeout=5.0):
    deadline = asyncio.get_event_loop().time() + timeout
    while not condition() and asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(0.01)


def _continue_judge():
    return patch(
        "hermes_cli.goals.judge_goal",
        return_value=("continue", "still needs work", False, None, False),
    )


# ──────────────────────────────────────────────────────────────────────
# T07 — a per-round manager rebuild must not disarm the goal
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_fresh_manager_per_round_keeps_the_goal_running(hermes_home):
    """The gateway builds a NEW GoalManager inside every post-turn hook. Authorization must
    survive that, or a long-running goal stalls after exactly one round."""
    runner, adapter, session_entry, src = _make_runner()
    goals.GoalManager(session_entry.session_id).set("polish the docs", max_turns=5)

    with _continue_judge():
        # Round 1 and round 2, each through its own freshly-built manager.
        await runner._post_turn_goal_continuation(
            session_entry=session_entry, source=src, final_response="partial edit 1",
            run_generation=1,
        )
        await _drain_until(lambda: adapter._pending_messages)
        first = adapter._pending_messages[build_session_key(src)]
        assert first.text.startswith(goals.GOAL_CONTINUATION_PREFIX)

        # Drain the slot the way the turn loop does, then run round 2.
        adapter._pending_messages.pop(build_session_key(src), None)
        await runner._post_turn_goal_continuation(
            session_entry=session_entry, source=src, final_response="partial edit 2",
            run_generation=2,
        )
        await _drain_until(lambda: adapter._pending_messages)
        second = adapter._pending_messages[build_session_key(src)]
        assert second.text.startswith(goals.GOAL_CONTINUATION_PREFIX)

    state = goals.GoalManager(session_entry.session_id).state
    assert state.status == "active"
    assert state.turns_used == 2
    assert goal_admission.is_armed(state.goal_id)


@pytest.mark.asyncio
async def test_a_fresh_process_does_not_silently_resume(hermes_home):
    """Same DB, no authorization: the gateway must say so instead of spending budget."""
    runner, adapter, session_entry, src = _make_runner()
    goals.GoalManager(session_entry.session_id).set("polish the docs", max_turns=5)
    goal_id = goals.GoalManager(session_entry.session_id).state.goal_id
    goal_admission.disarm(goal_id, reason="simulated restart")

    judge = MagicMock(return_value=("continue", "still needs work", False, None, False))
    with patch("hermes_cli.goals.judge_goal", judge):
        await runner._post_turn_goal_continuation(
            session_entry=session_entry, source=src, final_response="partial edit",
            run_generation=1,
        )
        await _drain_until(lambda: adapter.sends)

    judge.assert_not_called()
    assert not adapter._pending_messages, "an unauthorized round must not enqueue a turn"
    assert any("re-authorization" in s["content"] for s in adapter.sends), adapter.sends


@pytest.mark.asyncio
async def test_the_loop_hook_gets_its_own_signature_not_the_goal_hook_kwargs(hermes_home):
    """Regression guard for a real bug: ``_run_post_turn_hooks`` drives TWO different signatures
    in one list.  Handing the loop hook the goal hook's ``run_generation`` raises TypeError, which
    the per-hook ``except Exception -> debug`` guard then swallows — so an in-flight /loop tick is
    never released, ``awaiting_response`` sticks at True, and the wakeup watcher stops
    rescheduling.  The guard must stay (a goal failure must not block loop completion); what must
    not happen is the loop hook receiving a parameter it does not accept.
    """
    runner, _adapter, _session_entry, src = _make_runner()
    seen: dict[str, dict] = {}

    async def _loop_hook(**kwargs):
        seen["loop"] = kwargs

    async def _goal_hook(**kwargs):
        seen["goal"] = kwargs

    runner._post_turn_loop_completion = _loop_hook
    runner._post_turn_goal_continuation = _goal_hook

    await runner._run_post_turn_hooks(
        agent_result={"final_response": "a real reply"}, source=src, is_internal=True,
        run_generation=42,
    )

    assert seen["goal"]["run_generation"] == 42, "the goal hook needs the round identity"
    assert "run_generation" not in seen["loop"], (
        "the loop hook has no run_generation parameter; passing it raises a TypeError that the "
        "per-hook guard swallows to DEBUG, silently stalling every /loop tick"
    )


@pytest.mark.asyncio
async def test_the_loop_hook_still_runs_when_the_goal_hook_raises(hermes_home):
    """The other half of the contract the guard above must not weaken."""
    runner, _adapter, _session_entry, src = _make_runner()
    ran: list[str] = []

    async def _loop_hook(**kwargs):
        ran.append("loop")

    async def _goal_hook(**kwargs):
        raise RuntimeError("judge failed")

    runner._post_turn_loop_completion = _loop_hook
    runner._post_turn_goal_continuation = _goal_hook

    await runner._run_post_turn_hooks(
        agent_result={"final_response": "a real reply"}, source=src, is_internal=True,
        run_generation=42,
    )
    assert ran == ["loop"], "a failing goal hook must never block loop completion"


# ──────────────────────────────────────────────────────────────────────
# T06 — one round, one enqueue, one billing
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_duplicate_post_turn_hook_for_one_turn_enqueues_once(hermes_home):
    runner, adapter, session_entry, src = _make_runner()
    goals.GoalManager(session_entry.session_id).set("polish the docs", max_turns=5)
    key = build_session_key(src)

    judge = MagicMock(return_value=("continue", "still needs work", False, None, False))
    with patch("hermes_cli.goals.judge_goal", judge):
        # The SAME turn's run generation delivered twice (a retried post-turn hook).
        for _ in range(2):
            await runner._post_turn_goal_continuation(
                session_entry=session_entry, source=src, final_response="partial edit",
                run_generation=7,
            )
            await _drain_until(lambda: adapter.sends)
        adapter._pending_messages.pop(key, None)

    judge.assert_called_once()
    state = goals.GoalManager(session_entry.session_id).state
    assert state.turns_used == 1, "a duplicate callback must not bill a second turn"


# ──────────────────────────────────────────────────────────────────────
# T09 — a user stop drops the queued next round
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_user_stop_drops_the_queued_round(hermes_home):
    runner, adapter, session_entry, src = _make_runner()
    goals.GoalManager(session_entry.session_id).set("polish the docs", max_turns=5)

    with _continue_judge():
        await runner._post_turn_goal_continuation(
            session_entry=session_entry, source=src, final_response="partial edit",
            run_generation=1,
        )
        await _drain_until(lambda: adapter._pending_messages)
    pending = adapter._pending_messages[build_session_key(src)]
    assert pending.text.startswith(goals.GOAL_CONTINUATION_PREFIX)

    # `/goal stop` while the continuation is still sitting in the FIFO.
    goals.GoalManager(session_entry.session_id).clear()

    # Layer 2 at the gateway's drain boundary.
    assert runner._is_goal_continuation_event(pending) is True
    assert runner._goal_still_active_for_session(session_entry.session_id) is False
    assert goals.goal_continuation_admitted(session_entry.session_id)[0] is False


@pytest.mark.asyncio
async def test_a_user_pause_drops_the_queued_round(hermes_home):
    runner, adapter, session_entry, src = _make_runner()
    goals.GoalManager(session_entry.session_id).set("polish the docs", max_turns=5)
    with _continue_judge():
        await runner._post_turn_goal_continuation(
            session_entry=session_entry, source=src, final_response="partial edit",
            run_generation=1,
        )
        await _drain_until(lambda: adapter._pending_messages)

    goals.GoalManager(session_entry.session_id).pause(reason="user-paused")
    assert runner._goal_still_active_for_session(session_entry.session_id) is False
    assert goal_admission.is_armed(
        goals.GoalManager(session_entry.session_id).state.goal_id) is False


@pytest.mark.asyncio
async def test_a_healthy_queued_round_is_still_admitted(hermes_home):
    """The gate must not become a new way to lose a goal: nothing happened, so it runs."""
    runner, adapter, session_entry, src = _make_runner()
    goals.GoalManager(session_entry.session_id).set("polish the docs", max_turns=5)
    with _continue_judge():
        await runner._post_turn_goal_continuation(
            session_entry=session_entry, source=src, final_response="partial edit",
            run_generation=1,
        )
        await _drain_until(lambda: adapter._pending_messages)

    assert runner._goal_still_active_for_session(session_entry.session_id) is True


# ──────────────────────────────────────────────────────────────────────
# T10 — a simultaneous real user message wins
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_real_user_message_is_never_refused_by_the_gate(hermes_home):
    """Only synthetic continuations go through admission; a human turn is untouched."""
    runner, _adapter, session_entry, _src = _make_runner()
    goals.GoalManager(session_entry.session_id).set("polish the docs", max_turns=5)

    from gateway.platforms.event import MessageEvent, MessageType

    human = MessageEvent(text="actually, do this instead", message_type=MessageType.TEXT,
                         source=_make_source())
    assert runner._is_goal_continuation_event(human) is False
    # Even with the goal stopped, the gate is about tickets, not about people.
    goals.GoalManager(session_entry.session_id).clear()
    assert runner._goal_still_active_for_session(session_entry.session_id) is False


@pytest.mark.asyncio
async def test_the_continuation_lands_behind_an_already_queued_user_message(hermes_home):
    """FIFO order is the T10 mechanism on this surface: the human message occupies the slot and
    the synthetic continuation is staged behind it, so the user's turn runs first."""
    runner, adapter, session_entry, src = _make_runner()
    goals.GoalManager(session_entry.session_id).set("polish the docs", max_turns=5)
    key = build_session_key(src)

    from gateway.platforms.event import MessageEvent, MessageType

    human = MessageEvent(text="actually, do this instead", message_type=MessageType.TEXT,
                         source=src)
    runner._enqueue_fifo(key, human, adapter)

    with _continue_judge():
        await runner._post_turn_goal_continuation(
            session_entry=session_entry, source=src, final_response="partial edit",
            run_generation=1,
        )
        await _drain_until(lambda: adapter.sends)

    # The human message keeps the slot; the continuation waits behind it in the overflow FIFO.
    assert adapter._pending_messages[key].text == "actually, do this instead"
    assert runner._is_goal_continuation_event(adapter._pending_messages[key]) is False
    assert len(runner._overflow_queue(key)) == 1
    assert runner._is_goal_continuation_event(runner._overflow_queue(key)[0]) is True
