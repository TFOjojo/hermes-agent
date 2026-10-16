"""``_mark_connected`` must clear stale reconnect-escalation fields.

Regression test for the 2026-09-07..09-12 api_server case: a platform that
reconnected via ``_mark_connected()`` kept ``needs_attention=True`` and a
days-old ``retrying_since`` in ``gateway_state.json`` because
``write_runtime_status``'s ``_apply_set_fields`` only updates explicitly
passed fields — the connected write omitted both, so the stale escalation
survived a successful connect. The dedicated reconnect-success path
(gateway.run_adapters) passed them explicitly; ``_mark_connected`` did not.
"""

from unittest.mock import patch

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter


class _MinimalAdapter(BasePlatformAdapter):
    """Concrete-enough adapter: connect() is never called; we only test the
    runtime-status write performed by ``_mark_connected``."""

    async def connect(self, *, is_reconnect: bool = False) -> bool:  # pragma: no cover
        return True

    async def disconnect(self) -> None:  # pragma: no cover
        pass

    async def send(self, *args, **kwargs):  # pragma: no cover
        raise NotImplementedError

    async def get_chat_info(self, *args, **kwargs):  # pragma: no cover
        raise NotImplementedError


def test_mark_connected_clears_needs_attention_and_retrying_since():
    adapter = _MinimalAdapter(PlatformConfig(), Platform.LOCAL)
    captured = {}

    def fake_write(context, **kwargs):
        captured.update(kwargs)

    with patch.object(adapter, "_write_runtime_status_safe", side_effect=fake_write):
        adapter._mark_connected()

    assert captured.get("platform_state") == "connected"
    # The regression: these two must be explicitly cleared, not left unset
    # (unset fields keep their previous value in gateway_state.json).
    assert captured.get("needs_attention") is False
    assert captured.get("retrying_since") is None
