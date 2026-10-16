#!/usr/bin/env python3
"""Context Assembler — unified, testable, principal-aware context assembly.

Contracts (per spec comment #87 on t_05baeb16):
    PrincipalContext, ContextRequest, ContextEvidence, ContextEnvelope

Usage:
    from tools.context_assembler import assemble_context, PrincipalContext, ContextRequest

    principal = PrincipalContext(
        profile_id="default",
        agent_identity="workhorse",
        canonical_user_id="jojo",
        platform="qqbot",
        platform_user_id="JoJo",
        chat_id="...",
        chat_type="private",
        session_id="...",
        authorized=True,
        authorization_basis="pairing",
    )
    request = ContextRequest(purpose="conversation", max_chars=3000)
    envelope = assemble_context(principal, request)
    # envelope.evidence, envelope.degraded_sources, envelope.budget
"""

from __future__ import annotations

from tools.context_assembler.assembler import (
    assemble_context,
    format_envelope_for_prompt,
    PLATFORM_SOURCE_ALLOWLIST,
    _enforce_platform_allowlist,
    _validate_evidence_ownership,
    _filter_qq_ops_noise,
)
from tools.context_assembler.models import (
    ContextEnvelope,
    ContextEvidence,
    ContextRequest,
    PrincipalContext,
)

__all__ = [
    "assemble_context",
    "format_envelope_for_prompt",
    "PLATFORM_SOURCE_ALLOWLIST",
    "_enforce_platform_allowlist",
    "_validate_evidence_ownership",
    "_filter_qq_ops_noise",
    "PrincipalContext",
    "ContextRequest",
    "ContextEvidence",
    "ContextEnvelope",
]