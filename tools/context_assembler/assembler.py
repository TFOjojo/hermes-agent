#!/usr/bin/env python3
"""ContextAssembler — principal-aware context assembly from layered sources.

Implements the 5-layer hierarchy from spec comment #87:

    1. current_turn          (caller-provided current message)
    2. relationship_lifeline (bounded short-term visible chat, default 7d)
    3. durable_memory        (Scope Recall promoted/current facts)
    4. session_history       (session_search, on-demand)
    5. knowledge_sources     (Obsidian/local/network, topic-gated)

Guarantees:
    - principal.authorized must be True for any read
    - results are stable-sorted (by status priority then time desc)
    - cross-layer duplicate excerpts collapse after Unicode/text normalization
      and retain the highest-confidence evidence
    - truncated predictably at max_chars — never a half evidence
    - degraded sources are recorded, never silently swallowed
"""

from __future__ import annotations

import json
import logging
import sys
import threading
import unicodedata
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple

from tools.context_assembler.models import (
    ContextEnvelope,
    ContextEvidence,
    ContextRequest,
    PrincipalContext,
)
from tools.context_assembler.sources import qq_lifeline as qq_lifeline_source
from tools.context_assembler.sources import session_search as session_search_source

logger = logging.getLogger(__name__)

# Status priority: stable ordering for evidence.  Lower = shown first.
_STATUS_ORDER = {
    "current": 0,
    "pending": 1,
    "completed": 2,
    "uncertain": 3,
    "conflicted": 4,
    "historical": 5,
    "stale": 6,
    "expired": 7,
}

# Sensitivity order for dropping (higher = more likely to be dropped when
# include_sensitive=False).
_SENSITIVITY_ORDER = {"normal": 0, "personal": 1, "sensitive": 2, "secret": 3}

# Sources that can supply each layer.  Used by the assembler's source registry.
_READERS: Dict[str, Callable] = {
    "relationship_lifeline": qq_lifeline_source.read_qq_lifeline,
    "session_history": session_search_source.read_session_history,
}

# ── Platform source allowlist ────────────────────────────────────────────
# Each platform may only read specific layers.  This prevents QQ/life
# chat from leaking work knowledge, maintenance tasks, or ops noise.
#
# "all" means no restriction (CLI/desktop context).
PLATFORM_SOURCE_ALLOWLIST: Dict[str, List[str]] = {
    "qqbot": [
        "current_turn",
        "relationship_lifeline",
        "session_history",   # filtered to same-platform by the reader
        "durable_memory",    # user-scoped, no work details
    ],
    "desktop": [
        "current_turn",
        "relationship_lifeline",
        "session_history",
        "durable_memory",
        "knowledge_sources",
    ],
    "cli": [
        "current_turn",
        "relationship_lifeline",
        "session_history",
        "durable_memory",
        "knowledge_sources",
    ],
}

# Layers that must NEVER appear in QQ girlfriend chat regardless of
# platform — these are ops/work noise that have no place in the
# relationship lifeline.
# QQ noise blocklist — keyed by REAL source_kind values readers emit.
# ("knowledge_sources" is a policy layer name; the actual evidence kind
# from the Obsidian reader is "obsidian".  Both are listed so the filter
# matches regardless of which naming a reader uses.)
_QQ_OPS_NOISE_LAYERS = frozenset({"obsidian", "knowledge_sources"})

# Sensitivity levels that are always blocked for QQ platform.
_QQ_SENSITIVITY_BLOCK = frozenset({"sensitive", "secret"})


def _dedupe_fingerprint(ev: ContextEvidence) -> str:
    """Stable identity for a piece of evidence."""
    if ev.message_id is not None:
        return f"{ev.source_kind}:{ev.source_id}:msg:{ev.message_id}"
    if ev.memory_id:
        return f"{ev.source_kind}:{ev.source_id}:mem:{ev.memory_id}"
    return f"{ev.source_kind}:{ev.source_id}:{ev.occurred_at}:{ev.excerpt[:80]}"


def _dedupe_key(ev: ContextEvidence) -> str:
    """Return the key used to collapse evidence duplicates.

    Equivalent normalized excerpts use a content key so the same fact from
    different layers or lineage records collapses to one evidence item.
    Empty excerpts fall back to explicit source identity.
    """
    normalized = _normalize_excerpt(ev.excerpt)
    if normalized:
        return f"content:{normalized}"
    return f"identity:{_dedupe_fingerprint(ev)}"


def _normalize_excerpt(excerpt: str) -> str:
    """Normalize equivalent user-visible text before cross-layer matching."""
    return " ".join(unicodedata.normalize("NFKC", excerpt).split()).casefold()


def _dedupe(evidence: List[ContextEvidence]) -> List[ContextEvidence]:
    """Remove duplicate evidence, retaining the highest confidence item.

    The first item's position is stable even when a later duplicate replaces
    it, so downstream sorting and pagination remain deterministic.
    """
    positions: Dict[str, int] = {}
    out: List[ContextEvidence] = []
    for ev in evidence:
        key = _dedupe_key(ev)
        position = positions.get(key)
        if position is not None:
            if ev.confidence > out[position].confidence:
                out[position] = ev
            continue
        positions[key] = len(out)
        out.append(ev)
    return out


def stable_sort_key(ev: ContextEvidence) -> Tuple[int, float, str]:
    """Stable sort: status priority, then recency desc, then excerpt."""
    status_rank = _STATUS_ORDER.get(ev.status, 50)
    return (status_rank, -ev.occurred_at, ev.excerpt)


def _filter_sensitive(
    evidence: List[ContextEvidence], request: ContextRequest
) -> Tuple[List[ContextEvidence], Dict[str, int]]:
    """Drop evidence above the sensitivity allowance; record dropped counts.

    The ``include_sensitive`` flag is force-cleared at the assemble_context
    entry point, so this filter never trusts it: even if a caller bypasses
    the entry point and passes include_sensitive=True directly, elevated
    sensitivity levels are still dropped here.  Model input can never
    re-open the sensitive channel.
    """
    dropped: Dict[str, int] = {}
    out: List[ContextEvidence] = []
    for ev in evidence:
        if _SENSITIVITY_ORDER.get(ev.sensitivity, 0) > 0:
            dropped[ev.sensitivity] = dropped.get(ev.sensitivity, 0) + 1
            continue
        out.append(ev)
    return out, dropped


# Mapping from evidence source_kind → layer name for allowlist checks.
# Readers produce source_kind values that differ from layer names.
_SOURCE_KIND_TO_LAYER: Dict[str, str] = {
    "qq_lifeline": "relationship_lifeline",
    "session": "session_history",
    "memory": "durable_memory",
    "obsidian": "knowledge_sources",
}


def _enforce_platform_allowlist(
    evidence: List[ContextEvidence],
    principal: PrincipalContext,
) -> Tuple[List[ContextEvidence], List[str]]:
    """Drop evidence from sources the platform is not allowed to read.

    Returns (filtered_evidence, warnings).
    """
    allowed = PLATFORM_SOURCE_ALLOWLIST.get(principal.platform)
    if allowed is None:
        # Unknown platform — deny everything except current_turn.
        allowed = ["current_turn"]
    allowed_set = set(allowed)
    warnings: List[str] = []
    out: List[ContextEvidence] = []
    for ev in evidence:
        # current_turn is always allowed (it's caller-provided)
        if ev.source_kind == "current_turn":
            out.append(ev)
            continue
        # Map source_kind to layer name for allowlist check
        layer = _SOURCE_KIND_TO_LAYER.get(ev.source_kind, ev.source_kind)
        if layer not in allowed_set:
            warnings.append(
                f"Dropped {ev.source_kind} evidence: not in {principal.platform} allowlist"
            )
            continue
        out.append(ev)
    return out, warnings


def _validate_evidence_ownership(
    evidence: List[ContextEvidence],
    principal: PrincipalContext,
) -> Tuple[List[ContextEvidence], List[str]]:
    """Reject evidence not bound to this principal's canonical user.

    Platform equality is not authorization. User-scoped sources must match
    the canonical user; chat-scoped sources must also match the chat. When a
    principal is explicitly session-bound, session evidence must match it.
    """
    warnings: List[str] = []
    out: List[ContextEvidence] = []
    expected_canonical = (principal.canonical_user_id or "").strip()
    expected_platform_user = (principal.platform_user_id or "").strip()
    # SessionDB.user_id is the platform identity; Hermes identity is
    # canonical_user_id.  Either stamp is the same authorised principal.
    # Foreign ids (and unstamped rows when the principal *does* carry an
    # id) still fail closed.
    principal_users = {u for u in (expected_canonical, expected_platform_user) if u}
    principal_scope = {
        "user": expected_canonical or expected_platform_user,
        "chat": (principal.chat_id or "").strip(),
        "session": (principal.session_id or "").strip(),
    }
    for ev in evidence:
        if not ev.authorized:
            warnings.append(
                f"Dropped evidence not authorized: {ev.source_kind}:{ev.source_id}"
            )
            continue

        # current_turn is caller-provided for this very turn — exempt.
        if ev.source_kind == "current_turn":
            out.append(ev)
            continue
        required_dimensions = []
        evidence_user = (ev.owner_user_id or "").strip()
        if principal_users:
            required_dimensions.append("user")
        if ev.source_kind == "qq_lifeline":
            required_dimensions.append("chat")
        elif ev.source_kind == "session":
            principal_chat = (principal.chat_id or "").strip()
            if principal_chat:
                required_dimensions.append("chat")
                if principal.session_id:
                    required_dimensions.append("session")

        evidence_scope = {
            "user": evidence_user,
            "chat": (
                ev.owner_chat_id
                or (ev.source_id if ev.source_kind == "qq_lifeline" else "")
                or ""
            ).strip(),
            "session": (ev.owner_session_id or "").strip(),
        }
        rejected = False
        for dimension in required_dimensions:
            actual = evidence_scope[dimension]
            if dimension == "user":
                if actual not in principal_users:
                    warnings.append(
                        f"Rejected {ev.source_kind} evidence: owner user "
                        f"'{actual or '<unstamped>'}' is not principal user "
                        f"'{principal_scope['user'] or '<unstamped>'}'"
                    )
                    rejected = True
                    break
                continue
            expected = principal_scope[dimension]
            if not expected or actual != expected:
                warnings.append(
                    f"Rejected {ev.source_kind} evidence: owner {dimension} "
                    f"'{actual or '<unstamped>'}' is not principal {dimension} "
                    f"'{expected or '<unstamped>'}'"
                )
                rejected = True
                break
        if rejected:
            continue
        out.append(ev)
    return out, warnings


def _filter_qq_ops_noise(
    evidence: List[ContextEvidence],
    principal: PrincipalContext,
) -> Tuple[List[ContextEvidence], List[str]]:
    """For QQ platform: drop ops/work content and elevated sensitivity.

    QQ girlfriend chat must never see maintenance tasks, work knowledge,
    or sensitive/secret evidence.
    """
    if principal.platform != "qqbot":
        return evidence, []
    warnings: List[str] = []
    out: List[ContextEvidence] = []
    for ev in evidence:
        # Block ops/work layers using the same source-kind-to-layer mapping as
        # the platform allowlist. Readers may emit a source kind that differs
        # from the policy layer name.
        layer = _SOURCE_KIND_TO_LAYER.get(ev.source_kind, ev.source_kind)
        if layer in _QQ_OPS_NOISE_LAYERS:
            warnings.append(f"QQ noise filter: dropped {ev.source_kind}")
            continue
        # Block elevated sensitivity for QQ
        if ev.sensitivity in _QQ_SENSITIVITY_BLOCK:
            warnings.append(f"QQ sensitivity filter: dropped {ev.sensitivity} evidence")
            continue
        out.append(ev)
    return out, warnings


def _apply_budget(
    evidence: List[ContextEvidence], max_chars: int, max_items: int
) -> Tuple[List[ContextEvidence], Dict[str, Any]]:
    """Slice evidence up to max_items and max_chars, never splitting an item.

    Total ``used_chars`` is the sum of complete kept excerpts. Evidence that
    exceeds the whole page budget is consumed without output so pagination
    cannot stall on an item that will never fit.
    """
    kept: List[ContextEvidence] = []
    skipped: List[ContextEvidence] = []
    used = 0
    for ev in evidence:
        if len(kept) >= max_items:
            break
        excerpt_len = len(ev.excerpt)
        # An item that can never fit this page must be consumed as a whole;
        # otherwise pagination would return the same item forever at the next
        # offset.  Items that only miss the remaining budget still stop the
        # page and remain available to a later page.
        if excerpt_len > max_chars:
            skipped.append(ev)
            continue
        if used + excerpt_len > max_chars:
            break
        kept.append(ev)
        used += excerpt_len
    budget: Dict[str, Any] = {
        "max_chars": max_chars,
        "used_chars": used,
        "truncated": len(evidence) - len(kept) > 0,
        "kept_items": len(kept),
        "dropped_items": len(evidence) - len(kept),
        "consumed_items": len(kept) + len(skipped),
        "skipped_items": len(skipped),
        "dropped_by_source": {},
    }
    kept_ids = {id(ev) for ev in kept}
    for ev in evidence:
        if id(ev) not in kept_ids:
            budget["dropped_by_source"][ev.source_kind] = (
                budget["dropped_by_source"].get(ev.source_kind, 0) + 1
            )
    return kept, budget


def _parse_lifeline_payload(payload: str) -> str:
    """Best-effort human formatting of a raw lifeline snippet for evidence."""
    return payload.strip()


def _providers_from_module(module) -> list:
    """Collect live provider instances a module exposes.

    Scope Recall publishes ``_PROVIDER_REGISTRY`` as a ``weakref.WeakSet``, not a
    dict, so a dict-only check silently finds nothing and the recall path degrades
    to "provider not loaded in this process".
    """
    found: list = []
    registry = getattr(module, "_PROVIDER_REGISTRY", None)
    if isinstance(registry, dict):
        found.extend(registry.values())
    elif registry is not None:
        try:
            found.extend([item for item in registry if item is not None])
        except TypeError:
            pass
    provider = getattr(module, "provider", None)
    if provider is not None:
        found.append(provider)
    return found


def _live_scope_provider_containers():
    """Yield already-imported modules that may expose a live Scope Recall provider.

    User-installed providers are imported as
    ``_hermes_user_memory.<name>__source_<digest>`` (see plugins/memory _module_name),
    so probing exact names such as ``_hermes_user_memory.scope-recall.provider``
    can never match. Probe the explicit names first, then match by namespace suffix.
    """
    names = (
        "_hermes_user_memory.scope-recall.provider",
        "_hermes_user_memory.scope_recall.provider",
        "_hermes_user_memory.scope-recall",
        "_hermes_user_memory.scope_recall",
    )
    for name in names:
        module = sys.modules.get(name)
        if module is not None:
            yield module
    for name, module in list(sys.modules.items()):
        if not name.startswith(("_hermes_user_memory.", "plugins.memory.")):
            continue
        lowered = name.lower()
        if "scope" in lowered and "recall" in lowered:
            yield module


def _scope_provider_modules():
    """Backwards-compatible alias for the container probe."""
    return _live_scope_provider_containers()


def _scope_serves_prefetch() -> bool:
    """True when Scope Recall's recall arrives through ``prefetch()``, not ``search_memories()``.

    ``agent.memory_provider.MemoryProvider`` exposes ``prefetch`` and has never carried
    ``search_memories``; the provider package itself exports only ``register`` /
    ``register_adapter``, so no instance is reachable from the module namespace. The
    host injects the prefetch envelope into the turn, so this channel has nothing to
    fetch — but reporting that as "provider not loaded" made working memory look broken.
    """
    if next(_live_scope_provider_containers(), None) is None:
        return False
    try:
        from agent.memory_provider import MemoryProvider
    except Exception:  # pragma: no cover - interface import is best effort
        return False
    return callable(getattr(MemoryProvider, "prefetch", None))


def _active_scope_search(query: str):
    """Use an already-loaded Scope Recall runtime. Never construct a second one."""
    seen_mods: set[int] = set()
    candidates = []
    for module in _scope_provider_modules():
        if id(module) in seen_mods:
            continue
        seen_mods.add(id(module))
        candidates.extend(_providers_from_module(module))
    seen: set[int] = set()
    for cand in candidates:
        ident = id(cand)
        if ident in seen:
            continue
        seen.add(ident)
        search = getattr(cand, "search_memories", None)
        if callable(search):
            return search(query)
    return None


def _read_durable_memory(
    principal: PrincipalContext,
    request: ContextRequest,
) -> Tuple[List[ContextEvidence], List[Dict[str, str]]]:
    """Read Scope Recall durable memory through its own API (read-only)."""
    degraded: List[Dict[str, str]] = []
    evidence: List[ContextEvidence] = []
    # Durably gated: if there is no query, we don't proactively blast the
    # whole memory into the envelope.  Only memory management requests pass
    # an explicit recall query.
    query = (request.query or "").strip()
    if not query and request.purpose != "memory_management":
        return evidence, degraded
    try:
        results = _active_scope_search(query)
    except Exception as exc:
        logger.debug("scope provider search failed: %s", exc)
        degraded.append({"source": "durable_memory", "reason": f"provider unavailable: {exc}"})
        return evidence, degraded
    if results is None:
        if _scope_serves_prefetch():
            # The provider recalls through its own prefetch() and the host injects that
            # envelope into the turn; this channel has nothing to add and is NOT degraded.
            logger.debug("durable_memory: supplied by the provider's prefetch channel")
            return evidence, degraded
        degraded.append(
            {
                "source": "durable_memory",
                "reason": "provider not loaded in this process",
            }
        )
        return evidence, degraded
    if not results:
        return evidence, degraded
    memories = results if isinstance(results, list) else results.get("results", [])
    for mem in memories[: request.max_items]:
        content = mem.get("content", mem.get("text", "")) if isinstance(mem, dict) else str(mem)
        if not content:
            continue
        evidence.append(
            ContextEvidence(
                source_kind="memory",
                source_id=str(mem.get("id", "") if isinstance(mem, dict) else "memory"),
                platform="scope_recall",
                occurred_at=mem.get("updated_at", mem.get("created_at", 0)) or 0,
                excerpt=" ".join(str(content).split())[:300],
                status="current" if mem.get("status", "candidate") == "current" else "historical",
                confidence=float(mem.get("confidence", 1.0) or 1.0),
                sensitivity="normal",
                authorized=True,
                memory_id=str(mem.get("id", "") if isinstance(mem, dict) else None),
                owner_user_id=(principal.canonical_user_id or "").strip() or None,
            )
        )
    return evidence, degraded


def _read_knowledge_sources(
    principal: PrincipalContext,
    request: ContextRequest,
) -> Tuple[List[ContextEvidence], List[Dict[str, str]]]:
    """Topic-gated Obsidian pointers (work queries only, max 3)."""
    import re
    import subprocess
    import sys
    from pathlib import Path

    degraded: List[Dict[str, str]] = []
    evidence: List[ContextEvidence] = []
    query = (request.query or "").strip()
    if not query:
        return evidence, degraded
    WORK = re.compile(r"(丰台|积滞水|防汛|内涝|亦庄|巡查|流量计|职称|申报|考公|国考|岗位|水务)")
    SECRET = re.compile(r"(密钥|token|api[_ -]?key|密码|证件|合同|简历|准考证|体检)", re.I)
    if SECRET.search(query):
        degraded.append({"source": "knowledge_sources", "reason": "sensitive-query gated"})
        return evidence, degraded
    if not WORK.search(query):
        return evidence, degraded  # ordinary chat: do not read Obsidian

    vault = Path(r"C:/Users/JoJo/Documents/Obsidian Vault/知识库")
    recall = Path(
        r"D:/Program Files (x86)/AI/hermes/skills/productivity/knowledge-base-work-recall/scripts/recall_vault.py"
    )
    if not vault.exists() or not recall.exists():
        degraded.append({"source": "knowledge_sources", "reason": "vault or recall script missing"})
        return evidence, degraded
    try:
        result = subprocess.run(
            [sys.executable, str(recall), query, "--vault", str(vault), "--limit", "3"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
    except Exception as exc:
        degraded.append({"source": "knowledge_sources", "reason": f"recall failed: {exc}"})
        return evidence, degraded
    if not (result.stdout or "").strip():
        return evidence, degraded
    evidences = [ln.strip() for ln in result.stdout.splitlines() if ln.strip()]
    for i, snippet in enumerate(evidences[:3]):
        evidence.append(
            ContextEvidence(
                source_kind="obsidian",
                source_id=f"recall:{query}",
                platform="obsidian",
                occurred_at=0,
                excerpt=snippet[:300],
                status="current",
                confidence=0.6,
                sensitivity="normal",
                authorized=True,
                path=str(vault),
                owner_user_id=(principal.canonical_user_id or "").strip() or None,
            )
        )
    return evidence, degraded


def assemble_context(
    principal: PrincipalContext,
    request: Optional[ContextRequest] = None,
    current_turn: Optional[str] = None,
    db=None,
) -> ContextEnvelope:
    """Assemble a bounded, provenance-tracked context envelope.

    Args:
        principal: The verified, authorized principal.
        request:   What to request.  Defaults to a conversation request.
        current_turn: Optional explicit current message (layer 1).
        db:        Optional pre-opened SessionDB to share with readers.
    """
    request = request or ContextRequest()
    warnings: List[str] = []
    degraded: List[Dict[str, str]] = []

    try:
        principal.assert_authorized()
    except PermissionError as exc:
        # Spec: unauthorized → empty envelope with explicit degraded marker
        return ContextEnvelope(
            principal=principal,
            query=request.query or current_turn or "",
            evidence=[],
            warnings=["Unauthorized principal; no context loaded."],
            degraded_sources=[{"source": "all", "reason": str(exc)}],
            budget={"max_chars": request.max_chars, "used_chars": 0, "truncated": False, "dropped_by_source": {}},
        )

    # Safety invariant: sensitive/candidates channels are force-disabled at
    # the boundary.  The request object is mutated so downstream filters see
    # the effective policy, and the caller gets an explicit warning.
    if request.include_sensitive or request.include_candidates:
        warnings.append(
            "include_sensitive/include_candidates are force-disabled; "
            "model input cannot enable them."
        )
        request.include_sensitive = False
        request.include_candidates = False

    evidence: List[ContextEvidence] = []

    # Layer 1: current turn (always highest priority)
    if current_turn:
        evidence.append(
            ContextEvidence(
                source_kind="current_turn",
                source_id=principal.session_id or "current",
                platform=principal.platform,
                occurred_at=datetime.now().timestamp(),
                excerpt=" ".join(current_turn.split())[:500],
                status="current",
                confidence=1.0,
                sensitivity="normal",
                authorized=True,
                session_id=principal.session_id,
            )
        )

    # Readers for the requested layers — enforce platform allowlist early
    readers: List[Tuple[str, Callable]] = []
    policy = request.source_policy or []
    platform_allowed = set(PLATFORM_SOURCE_ALLOWLIST.get(principal.platform, ["current_turn"]))
    if "relationship_lifeline" in policy and "relationship_lifeline" in platform_allowed:
        # The relationship lifeline is a QQ-only source and must be bound to a
        # concrete chat before the reader is mounted. This prevents CLI,
        # desktop, and empty-chat requests from touching the QQ database.
        chat_id = (principal.chat_id or "").strip()
        if principal.platform == "qqbot" and chat_id:
            readers.append(("relationship_lifeline", qq_lifeline_source.read_qq_lifeline))
        else:
            degraded.append(
                {
                    "source": "relationship_lifeline",
                    "reason": "QQ lifeline requires platform=qqbot and a non-empty chat_id",
                }
            )
    if "session_history" in policy and "session_history" in platform_allowed:
        readers.append(("session_history", session_search_source.read_session_history))
    if "durable_memory" in policy and "durable_memory" in platform_allowed:
        readers.append(("durable_memory", _read_durable_memory))
    if "knowledge_sources" in policy and "knowledge_sources" in platform_allowed:
        readers.append(("knowledge_sources", _read_knowledge_sources))

    for layer, reader in readers:
        try:
            if reader is qq_lifeline_source.read_qq_lifeline:
                injected_path = getattr(db, "db_path", None) if db is not None else None
                layer_evidence, layer_degraded = reader(
                    principal, request, db_path=injected_path, db=db
                )
            elif reader is session_search_source.read_session_history:
                layer_evidence, layer_degraded = reader(principal, request, db=db)
            else:
                layer_evidence, layer_degraded = reader(principal, request)
        except Exception as exc:  # noqa: BLE001
            logger.warning("reader %s failed: %s", layer, exc, exc_info=True)
            layer_evidence, layer_degraded = [], [{"source": layer, "reason": str(exc)}]
        evidence.extend(layer_evidence)
        degraded.extend(layer_degraded)

    # Authorization filters BEFORE dedupe: a higher-confidence foreign
    # duplicate must not evict a lower-confidence owned item and then
    # itself be dropped, which would erase the authorised fact.
    seen_evidence, allowlist_warns = _enforce_platform_allowlist(evidence, principal)
    warnings.extend(allowlist_warns)

    seen_evidence, ownership_warns = _validate_evidence_ownership(seen_evidence, principal)
    warnings.extend(ownership_warns)

    seen_evidence, qq_noise_warns = _filter_qq_ops_noise(seen_evidence, principal)
    warnings.extend(qq_noise_warns)

    seen_evidence, sens_dropped = _filter_sensitive(seen_evidence, request)
    if sens_dropped:
        warnings.append(
            "Dropped sensitive evidence: " + ", ".join(f"{k}={v}" for k, v in sorted(sens_dropped.items()))
        )

    seen_evidence = _dedupe(seen_evidence)
    seen_evidence.sort(key=stable_sort_key)

    # Pagination BEFORE budget: request.offset skips through the fully
    # filtered pool (sensitive/ops/ownership drops must not consume the
    # window), then the char/item budget applies to this page only.
    # has_more reflects whether evidence remains after both the caller's
    # offset and the items consumed by this budget pass.
    try:
        page_offset = max(0, int(request.offset or 0))
    except (TypeError, ValueError):
        page_offset = 0
    total_after_filters = len(seen_evidence)
    page = seen_evidence[page_offset:]

    # Budget truncation
    kept, budget = _apply_budget(page, request.max_chars, request.max_items)

    next_offset = page_offset + int(budget.get("consumed_items", len(kept)))
    pagination = {
        "offset": page_offset,
        "has_more": next_offset < total_after_filters,
        "total_after_filters": total_after_filters,
        "total_before_budget": len(page),
        "page_size": len(kept),
        "next_offset": next_offset,
    }

    return ContextEnvelope(
        principal=principal,
        query=request.query or current_turn or "",
        evidence=kept,
        warnings=warnings,
        degraded_sources=degraded,
        budget={**budget, "max_chars": request.max_chars, "pagination": pagination},
    )


def format_envelope_for_prompt(envelope: ContextEnvelope) -> str:
    """Render the envelope into a compact prompt block for a chat model."""
    return envelope.format_human()