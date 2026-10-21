"""W2 — process-local execution authorization, two-layer admission and interruption semantics.

Three things are pinned here, and they are the three that are easy to get wrong:

1. **Armed authorization is process-local, not persisted and not per-manager.**  The gateway
   rebuilds a ``GoalManager`` for every turn, so authorization hung off an instance would read
   as "the user reopened the session" each round and disarm a long-running goal (T07).
2. **Enqueue is not admission.**  A queued continuation is re-validated at the moment it would
   start a turn, so a user pause / clear / edit that lands meanwhile stops it before any model
   call or tool (T09, T10).  A round is billed once however many times its callback fires (T06).
3. **Normal context compression keeps running; a restart or a fork does not** (T08).
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from hermes_cli import goal_admission, goals
from hermes_cli.goals import GOAL_CONTINUATION_PREFIX


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME so SessionDB.state_meta writes stay hermetic."""
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
    goals._get_session_db()  # pre-warm so async writes never race the bootstrap window
    yield home
    try:
        reset_hermes_home_override(token)
    except Exception:
        pass
    goals._DB_CACHE.clear()


@pytest.fixture(autouse=True)
def _clean_admission_registry():
    """Authorization is process-local: no test may inherit another test's armed goal."""
    goal_admission._AUTHORIZATIONS.clear()
    yield
    goal_admission._AUTHORIZATIONS.clear()


def _sid(tag: str) -> str:
    return f"sid-w2-{tag}-{uuid.uuid4().hex}"


def _judge_says_continue():
    return patch.object(
        goals, "judge_goal",
        return_value=("continue", "more steps to take", False, None, False),
    )


def _armed_key(goal_id: str) -> tuple[str, str]:
    return (goal_admission.canonical_profile_home(), goal_id)


def _set_goal(sid: str, text: str = "ship the benchmark", max_turns: int = 5):
    mgr = goals.GoalManager(sid)
    mgr.set(text, max_turns=max_turns)
    return mgr


# ──────────────────────────────────────────────────────────────────────
# T07 — the gateway rebuilds the manager every round; authorization survives
# ──────────────────────────────────────────────────────────────────────


class TestManagerRebuildNeverDisarms:
    def test_rebuilding_the_manager_neither_arms_nor_disarms(self, hermes_home):
        sid = _sid("rebuild")
        mgr = _set_goal(sid)
        goal_id = mgr.state.goal_id
        epoch = goal_admission.armed_epoch(goal_id)
        assert epoch, "an explicit user /goal set must arm execution authorization"

        # Every surface rebuilds managers constantly: the gateway builds one per turn, the CLI
        # rebuilds when session_id changes, /goal status builds a throwaway one. None of those
        # reads may arm, and none may disarm.
        for _ in range(4):
            rebuilt = goals.GoalManager(sid)
            assert rebuilt.is_active()
            assert goal_admission.armed_epoch(goal_id) == epoch

        with _judge_says_continue():
            decision = goals.GoalManager(sid).evaluate_after_turn("made progress")
        assert decision["should_continue"] is True
        assert decision["verdict"] == "continue"

    def test_a_fresh_manager_cannot_arm_by_reading(self, hermes_home):
        """No authorization exists until a user creates or explicitly resumes the goal."""
        sid = _sid("readonly")
        mgr = _set_goal(sid)
        goal_admission.disarm(mgr.state.goal_id, reason="simulated stop")

        # Read-only paths (status, show, a rebuilt manager) must not re-arm it.
        goals.GoalManager(sid).status_line()
        goals.GoalManager(sid).state
        assert goal_admission.is_armed(mgr.state.goal_id) is False

        judge = MagicMock(return_value=("continue", "more", False, None, False))
        with patch.object(goals, "judge_goal", judge):
            decision = goals.GoalManager(sid).evaluate_after_turn("kept working")
        judge.assert_not_called()
        assert decision["should_continue"] is False
        assert decision["verdict"] == "needs_reauth"

    def test_explicit_resume_rearms_with_a_fresh_epoch(self, hermes_home):
        sid = _sid("resume")
        mgr = _set_goal(sid)
        goal_id = mgr.state.goal_id
        stale_epoch = goal_admission.armed_epoch(goal_id)

        mgr.pause(reason="user-paused")
        assert goal_admission.is_armed(goal_id) is False

        mgr.resume()
        assert goal_admission.armed_epoch(goal_id) not in (None, stale_epoch)
        with _judge_says_continue():
            assert goals.GoalManager(sid).evaluate_after_turn("back to work")["should_continue"] is True

    def test_resume_with_no_budget_left_arms_nothing(self, hermes_home):
        sid = _sid("nobudget")
        mgr = _set_goal(sid, max_turns=2)
        goal_id = mgr.state.goal_id
        with _judge_says_continue():
            mgr.evaluate_after_turn("one")
            mgr.evaluate_after_turn("two")
        assert mgr.state.status == "paused"
        assert goal_admission.is_armed(goal_id) is False

        mgr.state.turns_used = mgr.state.max_turns  # resume without a budget reset
        mgr.resume(reset_budget=False)
        assert mgr.state.status == "active"
        assert goal_admission.is_armed(goal_id) is False, (
            "an exhausted goal has nothing left to spend, so nothing may be authorized"
        )


# ──────────────────────────────────────────────────────────────────────
# T08 — compression continues; restart / fork does not
# ──────────────────────────────────────────────────────────────────────


class TestCompressionVersusProcessBoundary:
    def test_compression_keeps_the_authorization_across_the_new_session_id(self, hermes_home):
        parent, child = _sid("compress-parent"), _sid("compress-child")
        mgr = _set_goal(parent, "finish the integration")
        goal_id = mgr.state.goal_id

        assert goals.migrate_goal_to_session(parent, child, reason="compression") is True
        child_mgr = goals.GoalManager(child)
        assert child_mgr.state.goal == "finish the integration"
        # Compression mints a NEW session id but keeps the goal identity, which is exactly what
        # the authorization is keyed on — so the loop keeps running without a re-authorization.
        assert child_mgr.state.goal_id == goal_id
        assert goal_admission.is_armed(goal_id)

        with _judge_says_continue():
            decision = child_mgr.evaluate_after_turn("kept working across the boundary")
        assert decision["should_continue"] is True

    def test_a_fresh_process_reports_needs_reauthorization(self, hermes_home):
        """A REAL process boundary, not a simulation.

        The goal is persisted, so a fresh interpreter reads it fine — but the armed
        authorization is process-local, so it must refuse to continue and say why.
        """
        sid = _sid("restart")
        _set_goal(sid, "keep going")
        repo_root = Path(goals.__file__).resolve().parents[1]
        env = dict(os.environ)
        env["HERMES_HOME"] = str(hermes_home)
        env["PYTHONPATH"] = str(repo_root) + os.pathsep + env.get("PYTHONPATH", "")
        code = (
            "import json,sys;"
            "from hermes_cli.goals import GoalManager;"
            "from hermes_cli import goal_admission as ga;"
            "s=GoalManager(sys.argv[1]).state;"
            "print(json.dumps({'goal': s.goal if s else None,"
            " 'goal_id': s.goal_id if s else None,"
            " 'armed': ga.is_armed(s.goal_id) if s else None}))"
        )
        proc = subprocess.run(
            [sys.executable, "-c", code, sid], capture_output=True, text=True, env=env,
            cwd=str(repo_root), timeout=180, check=False,
        )
        assert proc.returncode == 0, proc.stderr
        payload = json.loads(proc.stdout.strip().splitlines()[-1])
        assert payload["goal"] == "keep going", "the goal itself must survive the restart"
        assert payload["armed"] is False, (
            "a fresh process must NOT inherit the previous process's execution authorization"
        )

    def test_a_fork_child_does_not_inherit_the_parent_authorization(self, hermes_home, monkeypatch):
        sid = _sid("fork")
        mgr = _set_goal(sid)
        goal_id = mgr.state.goal_id
        armed_pid = goal_admission._AUTHORIZATIONS[_armed_key(goal_id)].owner_pid

        # A forked child runs under a new pid and inherits this table copy-on-write. The pid
        # guard — the same boundary os.register_at_fork's after_in_child reset enforces — must
        # drop the parent's authorization rather than let the child keep executing.
        monkeypatch.setattr(goal_admission.os, "getpid", lambda: armed_pid + 1)
        assert goal_admission.is_armed(goal_id) is False

        judge = MagicMock(return_value=("continue", "more", False, None, False))
        with patch.object(goals, "judge_goal", judge):
            decision = goals.GoalManager(sid).evaluate_after_turn("kept working")
        judge.assert_not_called()
        assert decision["verdict"] == "needs_reauth"
        assert "re-authorization" in decision["message"]


# ──────────────────────────────────────────────────────────────────────
# T06 — one round is admitted, tool-run and billed exactly once
# ──────────────────────────────────────────────────────────────────────


class TestRoundAccounting:
    def test_a_repeated_post_turn_callback_bills_one_round(self, hermes_home):
        sid = _sid("dup-hook")
        mgr = _set_goal(sid, max_turns=5)
        judge = MagicMock(return_value=("continue", "more to do", False, None, False))
        with patch.object(goals, "judge_goal", judge):
            first = mgr.evaluate_after_turn("worked a bit", round_id="round-1")
            second = mgr.evaluate_after_turn("worked a bit", round_id="round-1")

        assert first["should_continue"] is True
        assert second["should_continue"] is False
        assert second["verdict"] == "duplicate_round"
        judge.assert_called_once(), "a duplicate callback must not spend a second judge call"
        assert mgr.state.turns_used == 1, "a duplicate callback must not bill a second turn"

    def test_a_repeated_queue_promotion_starts_one_turn(self, hermes_home):
        sid = _sid("dup-queue")
        mgr = _set_goal(sid, max_turns=5)
        with _judge_says_continue():
            decision = mgr.evaluate_after_turn("worked a bit", round_id="round-1")
        assert decision["should_continue"] is True
        assert decision["ticket"].round_id == "round-1"

        first_admit, first_why = goals.goal_continuation_admitted(sid)
        second_admit, second_why = goals.goal_continuation_admitted(sid)
        assert (first_admit, first_why) == (True, "")
        assert second_admit is False
        assert "already started" in second_why

    def test_a_new_round_is_admitted_after_the_previous_one_ran(self, hermes_home):
        sid = _sid("next-round")
        mgr = _set_goal(sid, max_turns=5)
        with _judge_says_continue():
            mgr.evaluate_after_turn("one", round_id="round-1")
        assert goals.goal_continuation_admitted(sid)[0] is True

        # The next round is a different turn, so it gets its own round id and its own admission.
        with _judge_says_continue():
            second = mgr.evaluate_after_turn("two", round_id="round-2")
        assert second["should_continue"] is True
        assert goals.goal_continuation_admitted(sid)[0] is True

    def test_a_round_is_not_billed_while_the_goal_is_parked(self, hermes_home):
        sid = _sid("parked")
        mgr = _set_goal(sid, max_turns=5)
        mgr.wait_for_seconds(600, reason="cooling off")
        judge = MagicMock(return_value=("continue", "more", False, None, False))
        with patch.object(goals, "judge_goal", judge):
            first = mgr.evaluate_after_turn("still waiting", round_id="round-1")
            again = mgr.evaluate_after_turn("still waiting", round_id="round-1")
        judge.assert_not_called()
        assert first["verdict"] == "waiting"
        # The round was still admitted once, so a repeated callback is a duplicate — but the
        # parked goal burned no turn and made no judge call either way.
        assert again["verdict"] == "duplicate_round"
        assert mgr.state.turns_used == 0


# ──────────────────────────────────────────────────────────────────────
# T09 / T10 — interruption semantics at the drain boundary
# ──────────────────────────────────────────────────────────────────────


class TestUserStopDropsTheQueuedRound:
    def _cli(self, sid: str):
        from cli import HermesCLI

        cli = HermesCLI.__new__(HermesCLI)
        cli._pending_input = queue.Queue()
        cli._last_turn_interrupted = False
        cli.conversation_history = [{"role": "assistant", "content": "did some work"}]
        cli.session_id = sid
        cli.agent = MagicMock()
        cli.agent.session_id = sid
        cli._voice_lock = threading.Lock()
        cli._voice_mode = False
        cli._voice_continuous = False
        cli._goal_manager = _set_goal(sid, "long running task")
        cli.chat = MagicMock(return_value="a fresh model turn")
        return cli

    def test_user_stop_drops_the_queued_next_round(self, hermes_home):
        sid = _sid("cli-stop")
        cli = self._cli(sid)
        with _judge_says_continue():
            cli._maybe_continue_goal_after_turn()
        queued = cli._pending_input.get_nowait()
        assert queued.startswith(GOAL_CONTINUATION_PREFIX)
        # Before the stop the ticket holds.
        assert cli._goal_continuation_refused(queued) is False

        # `/goal stop` (== /goal clear) while the continuation is still queued.
        cli._goal_manager.clear()
        assert cli._goal_continuation_refused(queued) is True
        assert goals.goal_continuation_admitted(sid)[0] is False

        # And the drain itself must not reach the model or any tool.
        cli._tui_process_one_input(queued)
        cli.chat.assert_not_called()

    def test_user_pause_drops_the_queued_next_round(self, hermes_home):
        sid = _sid("cli-pause")
        cli = self._cli(sid)
        with _judge_says_continue():
            cli._maybe_continue_goal_after_turn()
        queued = cli._pending_input.get_nowait()

        cli._goal_manager.pause(reason="user-paused")
        assert cli._goal_continuation_refused(queued) is True
        cli._tui_process_one_input(queued)
        cli.chat.assert_not_called()

    def test_a_queued_round_survives_an_unrelated_user_edit(self, hermes_home):
        """Editing the goal (a subgoal, a contract) is not a stop: the round keeps its ticket."""
        sid = _sid("cli-edit")
        cli = self._cli(sid)
        with _judge_says_continue():
            cli._maybe_continue_goal_after_turn()
        queued = cli._pending_input.get_nowait()

        cli._goal_manager.add_subgoal("and cover the edge cases")
        # A user edit moves the revision, so the OLD ticket is stale by design...
        assert cli._goal_continuation_refused(queued) is True
        # ...but the goal is still authorized, so the next judged round runs normally.
        with _judge_says_continue():
            decision = cli._goal_manager.evaluate_after_turn("covered them")
        assert decision["should_continue"] is True

    def test_a_real_user_message_is_never_refused(self, hermes_home):
        sid = _sid("cli-realmsg")
        cli = self._cli(sid)
        cli._goal_manager.clear()
        assert cli._goal_continuation_refused("please also fix the typo") is False


class TestUserMessageBeatsAutomaticContinuation:
    def test_a_queued_user_message_preempts_judging(self, hermes_home):
        """T10: the post-turn hook defers to a real user message already in the queue."""
        sid = _sid("preempt")
        from cli import HermesCLI

        cli = HermesCLI.__new__(HermesCLI)
        cli._pending_input = queue.Queue()
        cli._last_turn_interrupted = False
        cli.conversation_history = [{"role": "assistant", "content": "did some work"}]
        cli.session_id = sid
        cli.agent = MagicMock()
        cli.agent.session_id = sid
        cli._goal_manager = _set_goal(sid, "long running task")

        cli._pending_input.put("actually, please do this instead")
        judge = MagicMock(return_value=("continue", "more", False, None, False))
        with patch.object(goals, "judge_goal", judge):
            cli._maybe_continue_goal_after_turn()
        judge.assert_not_called(), "a real user message owns the next turn; the judge waits"
        assert cli._pending_input.qsize() == 1
        assert cli._pending_input.get_nowait() == "actually, please do this instead"

    def test_the_displaced_automatic_ticket_cannot_be_replayed(self, hermes_home):
        """T10, other half: the round the user's message displaced is stale and is discarded."""
        sid = _sid("expire")
        mgr = _set_goal(sid, "long running task")
        with _judge_says_continue():
            mgr.evaluate_after_turn("worked a bit", round_id="round-1")
        assert goals.goal_continuation_admitted(sid)[0] is True

        # Replaying the displaced round must not start a second turn for it.
        admitted, why = goals.goal_continuation_admitted(sid)
        assert admitted is False
        assert "already started" in why

        # The user's own turn then advances the goal and gets its own round.
        with _judge_says_continue():
            users = mgr.evaluate_after_turn("actually, do this instead", round_id="round-2")
        assert users["should_continue"] is True
        assert users["ticket"].round_id == "round-2"
        assert goals.goal_continuation_admitted(sid)[0] is True


# ──────────────────────────────────────────────────────────────────────
# Stop semantics on the manager itself
# ──────────────────────────────────────────────────────────────────────


class TestStopRevokesAuthorization:
    @pytest.mark.parametrize("verb", ["pause", "clear"])
    def test_manual_stop_revokes_immediately(self, hermes_home, verb):
        sid = _sid(f"stop-{verb}")
        mgr = _set_goal(sid)
        goal_id = mgr.state.goal_id
        with _judge_says_continue():
            decision = mgr.evaluate_after_turn("worked a bit")
        assert decision["should_continue"] is True
        assert goals.goal_continuation_admitted(sid)[0] is True

        getattr(mgr, verb)()
        assert goal_admission.is_armed(goal_id) is False
        admitted, why = goals.goal_continuation_admitted(sid)
        assert admitted is False
        assert "not armed" in why

    def test_auto_pause_also_revokes(self, hermes_home):
        sid = _sid("autopause")
        mgr = _set_goal(sid, max_turns=1)
        goal_id = mgr.state.goal_id
        with _judge_says_continue():
            decision = mgr.evaluate_after_turn("the only turn")
        assert decision["status"] == "paused"
        assert goal_admission.is_armed(goal_id) is False

    def test_replacing_the_goal_revokes_the_old_one(self, hermes_home):
        sid = _sid("replace")
        mgr = _set_goal(sid, "the first goal")
        old_goal_id = mgr.state.goal_id
        mgr.set("the second goal")
        assert mgr.state.goal_id != old_goal_id
        assert goal_admission.is_armed(old_goal_id) is False
        assert goal_admission.is_armed(mgr.state.goal_id) is True

    def test_a_done_goal_revokes(self, hermes_home):
        sid = _sid("done")
        mgr = _set_goal(sid)
        goal_id = mgr.state.goal_id
        mgr.mark_done("finished by hand")
        assert goal_admission.is_armed(goal_id) is False


# ──────────────────────────────────────────────────────────────────────
# Ticket validation against stale state
# ──────────────────────────────────────────────────────────────────────


class TestTicketStaleness:
    def test_a_ticket_whose_revision_moved_is_refused(self, hermes_home):
        sid = _sid("stale-rev")
        mgr = _set_goal(sid)
        with _judge_says_continue():
            mgr.evaluate_after_turn("one")
        assert goals.goal_continuation_admitted(sid)[0] is True

        # Something else commits on the goal (another surface's edit): the revision moves, so
        # the queued ticket no longer names the state it was judged against.
        goals.GoalManager(sid).add_subgoal("extra criterion")
        admitted, why = goals.goal_continuation_admitted(sid)
        assert admitted is False
        assert "changed after this continuation was queued" in why

    def test_a_ticket_from_a_replaced_epoch_is_refused(self, hermes_home):
        sid = _sid("stale-epoch")
        mgr = _set_goal(sid)
        with _judge_says_continue():
            ticket = mgr.evaluate_after_turn("one")["ticket"]

        # A re-authorization mints a fresh owner epoch, so every ticket issued under the
        # previous one is stale at once.
        mgr.pause(reason="user-paused")
        mgr.resume()
        assert goal_admission.armed_epoch(ticket.goal_id) != ticket.owner_epoch
        reason = goal_admission.validate_ticket(
            ticket, goal_id=ticket.goal_id, revision=mgr.state.revision, status=mgr.state.status)
        assert "not armed" in reason

    def test_a_paused_goal_refuses_the_ticket(self, hermes_home):
        """Disarming is process-local, so a pause raised by ANOTHER surface reaches this host
        only through the freshly-read status — which is why the drain-time check re-reads it."""
        sid = _sid("crossproc-pause")
        _set_goal(sid)
        # Another surface commits the pause; this host's registry is untouched by that.
        goals.GoalManager(sid).pause(reason="user-paused")
        paused = goals.GoalManager(sid).state
        assert paused.status == "paused"
        goal_admission.arm(paused.goal_id, source_kind="test")
        epoch = goal_admission.armed_epoch(paused.goal_id)

        ticket = goal_admission.GoalTicket(
            goal_id=paused.goal_id, expected_revision=paused.revision, owner_epoch=epoch,
            round_id="round-1", source_kind="auto")
        reason = goal_admission.validate_ticket(
            ticket, goal_id=ticket.goal_id, revision=ticket.expected_revision, status=paused.status)
        assert reason == "the goal is paused"

    def test_no_goal_at_all_refuses(self, hermes_home):
        admitted, why = goals.goal_continuation_admitted(_sid("empty"))
        assert admitted is False
        assert "no goal is stored" in why


# ──────────────────────────────────────────────────────────────────────
# W1 leftover — a refused compression migration must reach the user
# ──────────────────────────────────────────────────────────────────────


class TestCompressionMigrationRefusalIsSurfaced:
    def test_a_refused_migration_warns_the_user(self, hermes_home):
        from agent.conversation_compression import _carry_session_state_to_child

        parent, child = _sid("mig-parent"), _sid("mig-child")
        goals.GoalManager(parent).set("finish the migration")
        goals.GoalManager(child).set("a different goal already lives here")

        agent = SimpleNamespace(session_id=child, _emit_warning=MagicMock())
        _carry_session_state_to_child(agent, parent, None)

        warnings = [str(call.args[0]) for call in agent._emit_warning.call_args_list]
        assert any("not carried into this session" in w for w in warnings), (
            "a refused migration only logged before; the user must be told the goal needs recovery"
        )
        # The refusal is real: the parent keeps its goal and the child keeps its own.
        assert goals.GoalManager(parent).state.goal == "finish the migration"
        assert goals.GoalManager(child).state.goal == "a different goal already lives here"

    def test_a_successful_migration_says_nothing(self, hermes_home):
        from agent.conversation_compression import _carry_session_state_to_child

        parent, child = _sid("ok-parent"), _sid("ok-child")
        goals.GoalManager(parent).set("finish the migration")

        agent = SimpleNamespace(session_id=child, _emit_warning=MagicMock())
        _carry_session_state_to_child(agent, parent, None)

        warnings = [str(call.args[0]) for call in agent._emit_warning.call_args_list]
        assert not any("not carried into this session" in w for w in warnings), warnings
        assert goals.GoalManager(child).state.goal == "finish the migration"
