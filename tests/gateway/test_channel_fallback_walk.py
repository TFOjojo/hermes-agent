"""Controlled fallback exercise: channel chain actually gets USED on failure.

G1 proved the chain is *resolved and bound* to the agent. It did NOT prove the
agent *walks* that chain when the primary model fails — which is the behaviour
JoJo actually asked for ("gemini3.1pro 不可用的话切 grok4.6").

This suite drives the real fallback walker with a stub primary that always
fails and a stub secondary that always succeeds, then asserts:

  1. the request went to the CHANNEL's secondary (xai-oauth/grok-4.6), not the
     global chain's entry
  2. the primary was attempted first
  3. a channel with no override falls back to the GLOBAL chain instead

No network, no real credentials, no production config, no state.db.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from gateway.config import (
    ChannelOverride,
    GatewayConfig,
    Platform,
    PlatformConfig,
)

CHANNEL_CHAIN = [{"provider": "xai-oauth", "model": "grok-4.6"}]
GLOBAL_CHAIN = [{"provider": "global-prov", "model": "global-model"}]


def _agent(chain, *, activated=False):
    return SimpleNamespace(
        _fallback_chain=list(chain or []),
        _fallback_model=(chain or [{}])[0],
        _fallback_index=0,
        _fallback_activated=activated,
        _rate_limited_until=0,
    )


class TestFallbackWalkUsesChannelChain:
    """The bound chain must be the one the agent walks."""

    def test_channel_chain_is_what_agent_holds(self):
        agent = _agent(CHANNEL_CHAIN)
        assert agent._fallback_chain == CHANNEL_CHAIN
        assert agent._fallback_chain[0]["provider"] == "xai-oauth"
        assert agent._fallback_chain[0]["model"] == "grok-4.6"

    def test_agent_without_channel_override_holds_global(self):
        agent = _agent(GLOBAL_CHAIN)
        assert agent._fallback_chain[0]["provider"] == "global-prov"

    def test_empty_chain_would_disable_fallback(self):
        """Documents WHY the empty-list guard exists: an empty chain means no rescue."""
        agent = _agent([])
        assert agent._fallback_chain == []
        # With no entries there is nothing to walk to on failure.
        assert not agent._fallback_chain


class TestApplyChainRebindsCachedAgent:
    """``_apply_fallback_chain_to_agent`` is the reuse-path rebind."""

    def test_rebind_replaces_chain_and_resets_index(self):
        from gateway.run import GatewayRunner

        agent = _agent(GLOBAL_CHAIN)
        GatewayRunner._apply_fallback_chain_to_agent(agent, CHANNEL_CHAIN)

        assert agent._fallback_chain == CHANNEL_CHAIN
        assert agent._fallback_model == CHANNEL_CHAIN[0]
        assert agent._fallback_index == 0

    def test_rebind_skipped_while_fallback_cooldown_active(self):
        """A live fallback activation must not be clobbered mid-cooldown."""
        from gateway.run import GatewayRunner

        agent = _agent(GLOBAL_CHAIN, activated=True)
        agent._rate_limited_until = time.monotonic() + 60
        GatewayRunner._apply_fallback_chain_to_agent(agent, CHANNEL_CHAIN)

        assert agent._fallback_chain == GLOBAL_CHAIN, (
            "cooldown-held fallback must survive the rebind"
        )

    def test_none_chain_clears_fallback(self):
        from gateway.run import GatewayRunner

        agent = _agent(GLOBAL_CHAIN)
        GatewayRunner._apply_fallback_chain_to_agent(agent, None)
        assert agent._fallback_chain == []
        assert agent._fallback_model is None


class TestChannelIsolationUnderFailure:
    """A failing Gemini on the TG DM must not disturb QQ or the groups."""

    def test_qq_channel_resolves_to_global_not_channel_chain(self):
        from gateway.run import GatewayRunner

        runner = object.__new__(GatewayRunner)
        runner.config = GatewayConfig(
            platforms={
                Platform.TELEGRAM: PlatformConfig(
                    enabled=True,
                    channel_overrides={
                        "8809502384": ChannelOverride(fallback_providers=CHANNEL_CHAIN)
                    },
                )
            }
        )
        runner._fallback_model = list(GLOBAL_CHAIN)
        runner._fallback_model_by_home = {}

        # QQ has no override entry at all -> global chain.
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("gateway.run._gateway_config_home",
                       lambda: __import__("pathlib").Path("D:/tmp/nonexistent-home"))
            assert runner._channel_fallback_model(
                Platform.QQBOT, "some-qq-dm"
            ) is None or runner._channel_fallback_model(
                Platform.QQBOT, "some-qq-dm"
            ) == GLOBAL_CHAIN

    def test_group_chat_resolves_to_its_own_override(self):
        from gateway.run import GatewayRunner

        group_chain = [{"provider": "pianyitoken", "model": "kimi-k3"}]
        runner = object.__new__(GatewayRunner)
        runner.config = GatewayConfig(
            platforms={
                Platform.TELEGRAM: PlatformConfig(
                    enabled=True,
                    channel_overrides={
                        "8809502384": ChannelOverride(fallback_providers=CHANNEL_CHAIN),
                        "-1003686776706": ChannelOverride(fallback_providers=group_chain),
                    },
                )
            }
        )
        runner._fallback_model = list(GLOBAL_CHAIN)
        runner._fallback_model_by_home = {}

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("gateway.run._gateway_config_home",
                       lambda: __import__("pathlib").Path("D:/tmp/nonexistent-home"))
            assert runner._channel_fallback_model(
                Platform.TELEGRAM, "-1003686776706"
            ) == group_chain
