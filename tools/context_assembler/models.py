#!/usr/bin/env python3
"""Pure-data models for the context assembly contract.

Per spec comment #87 on t_05baeb16:
    PrincipalContext, ContextRequest, ContextEvidence, ContextEnvelope
"""

from __future__ import annotations

import dataclasses
import json
import time
from typing import Any, Dict, List, Optional, Tuple


# ── PrincipalContext ──────────────────────────────────────────────────────


@dataclasses.dataclass(frozen=True)
class PrincipalContext:
    """Who is asking, on what platform, and with what authorization.

    Must be constructed from the Gateway's authorised source, never from
    model input.  All reads must verify ``authorized=True`` and that the
    canonical user matches the accessible scope.
    """

    profile_id: str
    agent_identity: str
    canonical_user_id: str
    platform: str
    platform_user_id: str
    chat_id: str
    chat_type: str  # "private" | "group" | "channel"
    session_id: str
    authorized: bool = True
    authorization_basis: str = "local"  # "pairing" | "allowlist" | "adapter_policy" | "local"

    def assert_authorized(self) -> None:
        """Raise if this principal is not authorised."""
        if not self.authorized:
            raise PermissionError(
                f"Principal {self.platform_user_id} on {self.platform} is not authorised"
            )

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)


# ── ContextRequest ────────────────────────────────────────────────────────


@dataclasses.dataclass
class ContextRequest:
    """What context is being requested, and with what budget/policy.

    Defaults are safe for general conversation.  Callers may tighten or
    relax within the authorised scope, but *include_sensitive* and
    *include_candidates* default to ``False`` and cannot be overridden by
    model input.
    """

    query: str = ""
    purpose: str = "conversation"  # "conversation" | "proactive" | "work" | "memory_management"

    # Allowed data layers (from the 5-layer hierarchy)
    source_policy: Optional[List[str]] = None

    # Time range
    time_range: Optional[Tuple[Optional[float], Optional[float]]] = None  # (start, end) unixtime

    # Pagination
    offset: int = 0

    # Budget
    max_items: int = 10
    max_chars: int = 5000

    # Safety – always default to *off*; model can never flip these.
    include_sensitive: bool = False
    include_candidates: bool = False

    def __post_init__(self) -> None:
        if self.source_policy is None:
            # Default: allow all layers
            self.source_policy = [
                "current_turn",
                "relationship_lifeline",
                "durable_memory",
                "session_history",
                "knowledge_sources",
            ]


# ── ContextEvidence ────────────────────────────────────────────────────────


_EVIDENCE_STATUSES = frozenset(
    {
        "current",
        "historical",
        "pending",
        "completed",
        "uncertain",
        "conflicted",
        "stale",
        "expired",
    }
)
_SENSITIVITY_LEVELS = frozenset({"normal", "personal", "sensitive", "secret"})


@dataclasses.dataclass
class ContextEvidence:
    """A single piece of evidence from a specific source.

    Every piece carries provenance, status, confidence, and sensitivity
    so the consumer can decide how to use it.
    """

    source_kind: str  # "session" | "memory" | "obsidian" | "current_turn" | "qq_lifeline"
    source_id: str
    platform: str
    occurred_at: float  # unixtime
    excerpt: str
    status: str = "current"
    confidence: float = 1.0
    sensitivity: str = "normal"
    authorized: bool = True
    redactions: List[str] = dataclasses.field(default_factory=list)

    # Optional provenance for deeper drill-down
    session_id: Optional[str] = None
    message_id: Optional[int] = None
    memory_id: Optional[str] = None
    path: Optional[str] = None

    # Ownership binding. Readers stamp these from verified source metadata;
    # the assembler rejects evidence outside the principal's scope.
    owner_user_id: Optional[str] = None
    owner_chat_id: Optional[str] = None
    owner_session_id: Optional[str] = None

    def __post_init__(self) -> None:
        if self.status not in _EVIDENCE_STATUSES:
            raise ValueError(f"Unknown evidence status: {self.status!r}")
        if self.sensitivity not in _SENSITIVITY_LEVELS:
            raise ValueError(f"Unknown sensitivity level: {self.sensitivity!r}")
        # occurred_at must be a float unixtime for stable sorting; readers may
        # hand over ISO strings or numeric strings from JSON/sqlite.
        if isinstance(self.occurred_at, str):
            try:
                from datetime import datetime

                parsed = datetime.fromisoformat(self.occurred_at.replace("Z", "+00:00"))
                self.occurred_at = parsed.timestamp() if parsed.tzinfo else parsed.timestamp()
            except ValueError:
                try:
                    self.occurred_at = float(self.occurred_at)
                except ValueError:
                    self.occurred_at = 0.0
        elif self.occurred_at is None:
            self.occurred_at = 0.0
        else:
            self.occurred_at = float(self.occurred_at)

    def to_dict(self) -> Dict[str, Any]:
        return {k: v for k, v in dataclasses.asdict(self).items() if v is not None}


# ── ContextEnvelope ────────────────────────────────────────────────────────


@dataclasses.dataclass
class ContextEnvelope:
    """The assembled output — a bounded, provenance-tracked context payload.

    Carries the principal, query, evidence array, warnings, degraded-source
    list, and budget tracking so the consumer can tell exactly what was
    included, what was dropped, and why.
    """

    principal: PrincipalContext
    query: str
    evidence: List[ContextEvidence]
    warnings: List[str]
    degraded_sources: List[Dict[str, str]]  # [{source: str, reason: str}]
    budget: Dict[str, Any]  # {max_chars, used_chars, truncated, dropped_by_source: {kind: count}}

    def to_dict(self) -> Dict[str, Any]:
        return {
            "principal": self.principal.to_dict(),
            "query": self.query,
            "evidence": [e.to_dict() for e in self.evidence],
            "warnings": self.warnings,
            "degraded_sources": self.degraded_sources,
            "budget": self.budget,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, default=str)

    def format_human(self) -> str:
        """Return a human-readable summary of the envelope contents."""
        lines: List[str] = []
        if self.evidence:
            lines.append(f"上下文共 {len(self.evidence)} 条证据（来自 {self.budget.get('used_chars', '?')} 字符/{self.budget.get('max_chars', '?')} 预算）：")
            for i, ev in enumerate(self.evidence, 1):
                age = _age_hint(ev.occurred_at)
                age_bit = f" {age}" if age else ""
                lines.append(f"  [{i}] {ev.source_kind}/{ev.status} [{ev.sensitivity}]{age_bit} {ev.excerpt[:120]}")
        else:
            lines.append("无上下文证据。")
        if self.degraded_sources:
            lines.append("降级源：")
            for d in self.degraded_sources:
                lines.append(f"  - {d['source']}: {d['reason']}")
        if self.warnings:
            lines.append("警告：")
            for w in self.warnings:
                lines.append(f"  - {w}")
        # Budget summary
        b = self.budget
        if b.get("truncated"):
            lines.append(f"截断：{b.get('used_chars', 0)}/{b.get('max_chars', 0)} 字符，丢失 {b.get('dropped_by_source', {})} 条")
        return "\n".join(lines)


def _age_hint(occurred_at: float) -> str:
    """Compact age for prompt rendering; skip ancient/sentinel timestamps."""
    if not occurred_at:
        return ""
    age = time.time() - float(occurred_at)
    if age < 0 or age > 30 * 86400:
        return ""
    if age < 90:
        return "刚刚"
    if age < 3600:
        return f"{int(age // 60)}分钟前"
    if age < 86400:
        return f"{int(age // 3600)}小时前"
    return f"{int(age // 86400)}天前"