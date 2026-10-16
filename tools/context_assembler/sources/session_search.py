#!/usr/bin/env python3
"""Session-history source adapter: wraps ``session_search_tool``.

Returns evidence from the Hermes state.db session store, filtered by
principal platform and source policy.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, List, Optional, Tuple

from tools.context_assembler.models import ContextEvidence, ContextRequest, PrincipalContext

logger = logging.getLogger(__name__)

# Source types to exclude from session history results
_HIDDEN_SOURCES = ("kanban", "subagent", "tool", "cron")

# Sessions older than this many days are excluded from "relationship_lifeline"
_LIFELINE_DAYS = 7


def _call_session_search(
    query: str,
    limit: int,
    sort: Optional[str] = None,
    profile: Optional[str] = None,
    db=None,
) -> Dict[str, Any]:
    """Call the session_search tool's implementation and return its JSON result."""
    from tools.session_search_tool import session_search

    raw = session_search(
        query=query,
        limit=limit,
        sort=sort,
        profile=profile,
        db=db,
        current_session_id=None,
    )
    try:
        return json.loads(raw) if isinstance(raw, str) else raw
    except (json.JSONDecodeError, TypeError):
        logger.warning("session_search returned non-JSON: %r", raw[:200] if raw else "empty")
        return {"success": False, "mode": "error", "error": "non-json response"}


def _session_to_evidence(
    session: Dict[str, Any],
    principal: PrincipalContext,
    session_meta: Dict[str, Any],
    status: str = "historical",
) -> Optional[ContextEvidence]:
    """Convert a session_search result entry to a ContextEvidence."""
    source = session_meta.get("source", "unknown") or "unknown"
    if source in _HIDDEN_SOURCES:
        return None

    snippet = session.get("snippet", "") or ""
    title = session.get("title", "")
    excerpt = title + ": " + snippet if title else snippet
    excerpt = " ".join(excerpt.split())[:500]

    return ContextEvidence(
        source_kind="session",
        source_id=session_meta.get("id", ""),
        platform=source,
        occurred_at=session.get("when", 0) or 0,
        excerpt=excerpt,
        status=status,
        confidence=0.8,
        sensitivity="normal",
        authorized=True,
        session_id=session_meta.get("id"),
        message_id=session.get("match_message_id"),
        owner_user_id=(session_meta.get("user_id") or "").strip() or None,
        owner_chat_id=(session_meta.get("chat_id") or "").strip() or None,
        owner_session_id=(session_meta.get("id") or "").strip() or None,
    )


def read_session_history(
    principal: PrincipalContext,
    request: ContextRequest,
    db=None,
) -> Tuple[List[ContextEvidence], List[Dict[str, str]]]:
    """Read session history evidence for the given principal and request.

    ALWAYS filters to the principal's own platform sessions — platform
    scoping is a security boundary, not a purpose-specific convenience.
    For ``relationship_lifeline`` purposes, additionally marks sessions
    older than the 7-day window as expired.
    """
    degraded: List[Dict[str, str]] = []
    evidence: List[ContextEvidence] = []
    owned_db = None

    chat_id = (principal.chat_id or "").strip()

    try:
        if db is None:
            from hermes_state import SessionDB

            if principal.profile_id != "default":
                from hermes_cli import profiles as profiles_mod

                db = SessionDB(
                    db_path=profiles_mod.get_profile_dir(principal.profile_id) / "state.db",
                    read_only=True,
                )
            else:
                db = SessionDB(read_only=True)
            owned_db = db

        try:
            page_size = max(1, int(request.max_items or 10))
            page_offset = max(0, int(request.offset or 0))
        except (TypeError, ValueError):
            page_size, page_offset = 10, 0
        fetch_limit = min(max(page_size + page_offset + 1, 10), 50)
        result = _call_session_search(
            query=request.query,
            limit=fetch_limit,
            sort="newest" if request.purpose == "relationship_lifeline" else None,
            profile=principal.profile_id if principal.profile_id != "default" else None,
            db=db,
        )
    except Exception as exc:
        logger.warning("session_search failed: %s", exc, exc_info=True)
        degraded.append({"source": "session_history", "reason": str(exc)})
        if owned_db is not None:
            owned_db.close()
        return evidence, degraded

    if not result.get("success"):
        degraded.append({"source": "session_history", "reason": result.get("error", "unknown failure")})
        if owned_db is not None:
            owned_db.close()
        return evidence, degraded

    sessions = result.get("results") or []
    now = time.time()

    for s in sessions:
        session_id = (s.get("session_id") or "").strip()
        if not session_id or db is None or not hasattr(db, "get_session"):
            continue
        try:
            session_meta = db.get_session(session_id) or {}
        except Exception:
            logger.warning("session metadata lookup failed for %s", session_id, exc_info=True)
            continue

        # Authorization comes from the canonical session row — never from
        # the search result payload.  Source and chat scope must match.
        if (session_meta.get("source") or "") != principal.platform:
            continue
        principal_chat_id = (principal.chat_id or "").strip()
        if principal_chat_id and (session_meta.get("chat_id") or "") != principal_chat_id:
            continue
        if principal.session_id and (session_meta.get("id") or "") != principal.session_id:
            continue

        # Platform user check: search results must NOT be trusted for
        # user ownership.  The session row must carry a resolvable user_id;
        # without one the evidence cannot be bound to a principal and must
        # be dropped (fail closed).
        db_platform_user = (session_meta.get("user_id") or "").strip()
        if not db_platform_user:
            logger.debug(
                "session %s has no user_id in DB; dropping evidence to avoid unstamped leak",
                session_id,
            )
            continue
        principal_platform_user = (principal.platform_user_id or "").strip()
        if not principal_platform_user or db_platform_user != principal_platform_user:
            logger.debug(
                "session %s owner %s != principal %s; dropping foreign evidence",
                session_id,
                db_platform_user,
                principal.platform_user_id,
            )
            continue

        ev = _session_to_evidence(s, principal, session_meta)
        if ev is None:
            continue

        # Platform filter applies for ALL purposes: session history from a
        # different platform is out of scope for this principal, regardless
        # of why we're reading.
        if ev.platform != principal.platform:
            continue

        # For relationship lifeline, sessions outside the 7-day window are
        # marked expired (kept visible as expired rather than dropped).
        if request.purpose == "relationship_lifeline":
            if ev.occurred_at and ev.occurred_at < now - _LIFELINE_DAYS * 86400:
                ev.status = "expired"

        evidence.append(ev)

    if owned_db is not None:
        owned_db.close()
    return evidence, degraded


def read_current_lifeline(
    principal: PrincipalContext,
    request: ContextRequest,
    db=None,
) -> Tuple[List[ContextEvidence], List[Dict[str, str]]]:
    """Read the bounded relationship lifeline from the session DB.

    This is a purpose-built version of session_history that only reads
    user-visible messages from the principal's platform chat, excluding
    tool/ops noise.
    """
    from tools.context_assembler.sources.qq_lifeline import read_qq_lifeline

    return read_qq_lifeline(principal, request, db=db)