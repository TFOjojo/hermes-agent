"""Wire contracts for the Cortico↔Hermes request bridge (``tui_gateway/methods_bridge.py``).

These five methods are the ONLY surface a broker connection may call. They are registered
alongside the ordinary methods, but the admission gate in ``rpc_dispatch`` refuses anything
outside this whitelist for a bridge-authenticated transport — so a broker can never reach
``shell.exec``, ``prompt.submit``, ``session.resume`` or any other method even if it asks.
"""

from __future__ import annotations

from pydantic import Field

from .base import JsonValue, Params, Result
from .registry import method


class BridgeCapabilitiesParams(Params):
    """No parameters: capabilities are a property of the server, not the caller."""


class BridgeCapabilitiesResult(Result):
    protocol_version: str = "bridge-v1"
    targeted_cancel: bool = False
    persistent_dispatch: bool = False
    events: bool = False
    tools: list[str] = Field(default_factory=list)


class BridgeTaskSubmitParams(Params):
    """Every field is required: a dispatch receipt is all-or-nothing."""

    dispatch_id: str
    task_id: str
    revision: int
    policy_digest: str = Field(min_length=64, max_length=64)
    nonce: str
    policy_ref: dict[str, JsonValue]


class BridgeTaskSubmitResult(Result):
    ok: bool
    state: str = ""
    attempt: int = 0
    error: dict[str, str] | None = None


class BridgeTaskGetParams(Params):
    dispatch_id: str


class BridgeTaskGetResult(Result):
    ok: bool
    dispatch_id: str = ""
    task_id: str = ""
    state: str = ""
    attempt: int = 0
    result: dict[str, JsonValue] | None = None
    error_code: str | None = None
    error: dict[str, str] | None = None


class BridgeTaskCancelParams(Params):
    dispatch_id: str
    reason: str = "operator cancel"


class BridgeTaskCancelResult(Result):
    ok: bool
    state: str = ""
    cancelled: bool = False
    error: dict[str, str] | None = None


class BridgeTaskEventsParams(Params):
    after_cursor: int = 0


class BridgeTaskEvent(Result):
    cursor: int
    dispatch_id: str = ""
    task_id: str = ""
    event_type: str
    payload: dict[str, JsonValue] = Field(default_factory=dict)


class BridgeTaskEventsResult(Result):
    ok: bool
    events: list[BridgeTaskEvent] = Field(default_factory=list)
    next_cursor: int = 0


method("bridge.capabilities", params=BridgeCapabilitiesParams,
       result=BridgeCapabilitiesResult,
       doc="Broker capability probe: what this Hermes instance actually supports.")

method("bridge.task.submit", params=BridgeTaskSubmitParams,
       result=BridgeTaskSubmitResult,
       doc="Broker submits a dispatch receipt; idempotent on dispatch_id.")

method("bridge.task.get", params=BridgeTaskGetParams,
       result=BridgeTaskGetResult,
       doc="Broker reads dispatch state (owner-scoped: only its own principal).")

method("bridge.task.cancel", params=BridgeTaskCancelParams,
       result=BridgeTaskCancelResult,
       doc="Broker cancels one dispatch; never touches user sessions or global TTS.")

method("bridge.task.events", params=BridgeTaskEventsParams,
       result=BridgeTaskEventsResult,
       doc="Broker drains the dispatch event cursor for its own principal.")
