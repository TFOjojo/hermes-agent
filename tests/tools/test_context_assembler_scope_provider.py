"""The durable_memory channel must not report a provider that is working as missing.

Scope Recall recalls through ``MemoryProvider.prefetch()``.  Its provider package
exports only ``register`` / ``register_adapter`` and the recall interface has never
carried ``search_memories``, so a resolver that looks for a live provider instance
finds nothing and the channel reported "provider not loaded in this process" on every
turn while memory was arriving normally.

The contract: no provider reachable *and* nothing serving prefetch -> report the
degradation; a Scope Recall container present, whose interface serves prefetch ->
this channel is quiet.  A provider that really does expose ``search_memories`` still
wins over both.
"""

import sys
import types
import weakref

import pytest

from tools.context_assembler import assembler

CONTAINER = "_hermes_user_memory.scope-recall__source_deadbeefdeadbeef"
REASON = "provider not loaded in this process"


def _principal():
    return types.SimpleNamespace(canonical_user_id="test-user")


def _request():
    return types.SimpleNamespace(query="test query", purpose="chat", max_items=8)


def _degradations():
    return assembler._read_durable_memory(_principal(), _request())[1]


@pytest.fixture
def container():
    """A module shaped like the real provider package: no registry, no instance."""
    module = types.ModuleType(CONTAINER)

    def register(*_args, **_kwargs):
        return None

    module.register = register
    module.register_adapter = register
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setitem(sys.modules, CONTAINER, module)
    try:
        yield module
    finally:
        monkeypatch.undo()


def test_prefetch_interface_is_recognised():
    from agent.memory_provider import MemoryProvider

    assert callable(getattr(MemoryProvider, "prefetch", None))


def test_missing_provider_is_reported():
    """Nothing loaded at all is a real degradation and must stay visible."""
    assert assembler._scope_serves_prefetch() is False
    reasons = [item.get("reason") for item in _degradations()]
    assert REASON in reasons


def test_provider_package_serving_prefetch_is_not_degraded(container):
    assert assembler._scope_serves_prefetch() is True
    assert _degradations() == []


def test_degradation_returns_when_prefetch_interface_is_absent(container, monkeypatch):
    """Red control: without the interface the old alarm comes back."""
    monkeypatch.setattr(assembler, "_scope_serves_prefetch", lambda: False)
    reasons = [item.get("reason") for item in _degradations()]
    assert REASON in reasons


def test_live_search_memories_provider_still_wins(container):
    class Legacy:
        def search_memories(self, query):
            return [{"content": "legacy:" + query}]

    live = Legacy()
    registry = weakref.WeakSet()
    registry.add(live)
    container._PROVIDER_REGISTRY = registry
    # WeakSet holds no strong reference; keep our provider alive for the assertion
    assert assembler._active_scope_search("q") == [{"content": "legacy:q"}]
    assert live is not None
