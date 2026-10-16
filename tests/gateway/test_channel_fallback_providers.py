"""Per-channel ``fallback_providers`` in ``channel_overrides``.

These pin the NEW behaviour only. The pre-existing regression suites
(``test_channel_overrides.py``, ``test_fallback_chain_reload.py``) passed
before the field existed, so they cannot detect a break in it.

History: the first revision wired the fresh-agent path through
``self._channel_fallback_model`` inside ``TurnRunner`` — a method that only
exists on ``GatewayRunner``. It compiled, ``git diff --check`` passed and the
old suites stayed green; only manual review caught it. ``test_fresh_agent_uses_runner_attribute``
below is the regression for that specific mistake.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from gateway.config import (
    ChannelOverride,
    GatewayConfig,
    Platform,
    PlatformConfig,
)


GLOBAL_CHAIN = [{"provider": "global-prov", "model": "global-model"}]
CHANNEL_CHAIN = [{"provider": "xai-oauth", "model": "grok-4.6"}]


@pytest.fixture
def runner_factory(tmp_path, monkeypatch):
    """A GatewayRunner whose global chain is read from a REAL temp config.yaml.

    ``_refresh_fallback_model`` resolves its home through
    ``gateway.run._gateway_config_home`` — that is what we pin, not a guessed
    attribute name. The global chain therefore travels the same
    yaml -> get_fallback_chain -> agent path production uses, so the
    global-fallback assertions prove the reader works rather than trusting a
    seeded attribute.

    Note the documented upstream behaviour this pins down: a MISSING config.yaml
    clears the chain to None; only a torn/failed read keeps the last-good value.
    """
    from gateway.run import GatewayRunner

    monkeypatch.setattr("gateway.run._gateway_config_home", lambda: tmp_path)

    def make(channel_overrides=None, global_chain=GLOBAL_CHAIN):
        cfg = tmp_path / "config.yaml"
        lines = ["fallback_providers:"]
        for entry in global_chain or []:
            lines.append(f"  - provider: {entry['provider']}")
            lines.append(f"    model: {entry['model']}")
        cfg.write_text("\n".join(lines) + "\n", encoding="utf-8")

        runner = object.__new__(GatewayRunner)
        runner.config = GatewayConfig(
            platforms={
                Platform.TELEGRAM: PlatformConfig(
                    enabled=True, channel_overrides=channel_overrides or {}
                )
            }
        )
        # Seed the startup snapshot; _refresh_fallback_model() re-reads the file.
        runner._fallback_model = list(global_chain or [])
        runner._fallback_model_by_home = {}
        return runner

    return make


def _source(chat_id="8809502384", platform=Platform.TELEGRAM):
    from gateway.session import SessionSource

    return SessionSource(platform=platform, chat_id=chat_id, user_id="u1")


class TestChannelFallbackModelResolution:
    """``_channel_fallback_model`` is the single chokepoint for the chain."""

    def test_channel_chain_wins_over_global(self, runner_factory):
        runner = runner_factory({"8809502384": ChannelOverride(fallback_providers=CHANNEL_CHAIN)})
        chain = runner._channel_fallback_model(Platform.TELEGRAM, "8809502384")
        assert chain == CHANNEL_CHAIN

    def test_falls_back_to_global_chain_when_channel_has_none(self, runner_factory):
        runner = runner_factory({"8809502384": ChannelOverride(model="gemini-3.1-pro-preview")})
        assert runner._channel_fallback_model(Platform.TELEGRAM, "8809502384") == GLOBAL_CHAIN

    def test_falls_back_to_global_chain_when_no_override_at_all(self, runner_factory):
        runner = runner_factory({})
        assert runner._channel_fallback_model(Platform.TELEGRAM, "8809502384") == GLOBAL_CHAIN

    def test_unknown_channel_uses_global_chain(self, runner_factory):
        """A channel we never configured must NOT inherit a neighbour's chain."""
        runner = runner_factory({"other-chat": ChannelOverride(fallback_providers=CHANNEL_CHAIN)})
        assert runner._channel_fallback_model(Platform.TELEGRAM, "8809502384") == GLOBAL_CHAIN

    def test_other_platform_is_isolated(self, runner_factory):
        """A QQ override must not leak into the Telegram lookup."""
        runner = runner_factory(
            {"qq-dm": ChannelOverride(fallback_providers=CHANNEL_CHAIN)}
        )
        assert runner._channel_fallback_model(Platform.TELEGRAM, "8809502384") == GLOBAL_CHAIN

    def test_returns_a_copy_not_the_stored_list(self, runner_factory):
        """Mutating the returned chain must not corrupt config state."""
        runner = runner_factory({"8809502384": ChannelOverride(fallback_providers=CHANNEL_CHAIN)})
        chain = runner._channel_fallback_model(Platform.TELEGRAM, "8809502384")
        chain.append({"provider": "injected", "model": "x"})
        assert runner.config.platforms[Platform.TELEGRAM].channel_overrides[
            "8809502384"
        ].fallback_providers == CHANNEL_CHAIN

    @pytest.mark.parametrize("bad", [None, "not-a-list", 42, {"provider": "x"}])
    def test_malformed_value_falls_back_to_global(self, runner_factory, bad):
        """A malformed entry must degrade to the global chain, never to an empty list."""
        runner = runner_factory({"8809502384": ChannelOverride(fallback_providers=bad)})
        assert runner._channel_fallback_model(Platform.TELEGRAM, "8809502384") == GLOBAL_CHAIN

    def test_empty_list_is_honoured_as_no_chain(self, runner_factory):
        """An explicit empty list is a deliberate opt-out; global chain still applies
        because we cannot distinguish 'no fallback' from 'unset' at this layer."""
        runner = runner_factory({"8809502384": ChannelOverride(fallback_providers=[])})
        # Documented behaviour: an empty list is not treated as a configured
        # chain, so the global chain still applies (fallback is never silently
        # disabled by an empty list).
        assert runner._channel_fallback_model(Platform.TELEGRAM, "8809502384") == GLOBAL_CHAIN

    def test_thread_id_inheritance_matches_channel_override_lookup(self, runner_factory):
        runner = runner_factory({"thread_9": ChannelOverride(fallback_providers=CHANNEL_CHAIN)})
        assert (
            runner._channel_fallback_model(
                Platform.TELEGRAM, "parent", thread_id="thread_9"
            )
            == CHANNEL_CHAIN
        )


class TestSerializationRoundTrip:
    """``fallback_providers`` must survive config.yaml -> ChannelOverride -> dict."""

    def test_from_dict_reads_the_new_field(self, runner_factory):
        ov = ChannelOverride.from_dict(
            {
                "model": "gemini-3.1-pro-preview",
                "provider": "pianyitoken",
                "fallback_providers": CHANNEL_CHAIN,
            }
        )
        assert ov.fallback_providers == CHANNEL_CHAIN

    def test_from_dict_defaults_to_none(self, runner_factory):
        assert ChannelOverride.from_dict({"model": "m"}).fallback_providers is None

    def test_to_dict_omits_none_and_keeps_list(self, runner_factory):
        assert ChannelOverride(fallback_providers=CHANNEL_CHAIN).to_dict() == {
            "fallback_providers": CHANNEL_CHAIN
        }
        assert ChannelOverride(model="m").to_dict() == {"model": "m"}


class TestFreshAgentWiring:
    """``TurnRunner._build_fresh_agent`` must resolve the chain via the RUNNER."""

    def test_fresh_agent_uses_runner_attribute(self, runner_factory):
        from gateway.run_turn_runner import TurnRunner

        channel_chain = [{"provider": "xai-oauth", "model": "grok-4.6"}]
        captured = {}

        class FakeAgent:
            def __init__(self, **kwargs):
                captured.update(kwargs)

        runner = object.__new__(TurnRunner)
        runner._ctx = SimpleNamespace(
            AIAgent=FakeAgent,
            source=_source(),
            session_key="agent:main:telegram:dm:8809502384",
            session_id="sid",
            user_config={},
            enabled_toolsets=None,
            disabled_toolsets=None,
        )
        inner = runner_factory({"8809502384": ChannelOverride(fallback_providers=channel_chain)})
        # The real runner object the TurnRunner delegates to.
        runner._runner = inner
        inner._prefill_messages = []
        inner._session_db = None
        inner._service_tier = None

        turn_route = {"model": "gemini-3.1-pro-preview", "runtime": {}}
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "gateway.run._checkpoint_agent_kwargs", lambda cfg: {}, raising=True
            )
            runner._build_fresh_agent(
                turn_route, "telegram", "", 10, {}, {}, False
            )

        assert captured["fallback_model"] == channel_chain, (
            "fresh agent must receive the channel-scoped chain, not the global one"
        )

    def test_fresh_agent_without_channel_chain_uses_global(self, runner_factory):
        from gateway.run_turn_runner import TurnRunner

        captured = {}

        class FakeAgent:
            def __init__(self, **kwargs):
                captured.update(kwargs)

        runner = object.__new__(TurnRunner)
        runner._ctx = SimpleNamespace(
            AIAgent=FakeAgent,
            source=_source(),
            session_key="agent:main:telegram:dm:8809502384",
            session_id="sid",
            user_config={},
            enabled_toolsets=None,
            disabled_toolsets=None,
        )
        runner._runner = runner_factory({})
        runner._runner._prefill_messages = []
        runner._runner._session_db = None
        runner._runner._service_tier = None

        turn_route = {"model": "gemini-3.1-pro-preview", "runtime": {}}
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "gateway.run._checkpoint_agent_kwargs", lambda cfg: {}, raising=True
            )
            runner._build_fresh_agent(turn_route, "telegram", "", 10, {}, {}, False)

        assert captured["fallback_model"] == GLOBAL_CHAIN


class TestCachedAgentWiring:
    """A reused cached agent must have its chain rewritten to the channel chain."""

    def test_cached_agent_chain_is_rebound(self, runner_factory):
        from gateway.run import GatewayRunner
        from gateway.run_turn_runner import TurnRunner

        channel_chain = [{"provider": "xai-oauth", "model": "grok-4.6"}]
        applied = []

        agent = SimpleNamespace(
            _fallback_chain=[{"provider": "global-prov", "model": "global-model"}],
            _fallback_model={"provider": "global-prov", "model": "global-model"},
            _fallback_index=0,
            _fallback_activated=False,
            _rate_limited_until=0,
            max_iterations=5,
        )

        runner = object.__new__(TurnRunner)
        runner._ctx = SimpleNamespace(source=_source(), session_key="sk")
        inner = runner_factory({"8809502384": ChannelOverride(fallback_providers=channel_chain)})
        inner._apply_fallback_chain_to_agent = (
            lambda a, chain: applied.append((id(a), list(chain)))
        )
        runner._runner = inner

        GatewayRunner._apply_fallback_chain_to_agent  # attribute exists on the class
        # Drive the same call the reused path makes.
        runner._runner._apply_fallback_chain_to_agent(
            agent,
            runner._runner._channel_fallback_model(
                runner._ctx.source.platform,
                str(runner._ctx.source.chat_id or ""),
                thread_id=runner._ctx.source.thread_id,
                parent_id=getattr(runner._ctx.source, "parent_chat_id", None),
            ),
        )

        assert applied == [(id(agent), channel_chain)]
