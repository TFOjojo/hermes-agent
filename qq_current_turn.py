#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Current-turn user-text binding for the QQ response guard.

Production form: this file lands at the hermes-agent repo ROOT as
``qq_current_turn.py`` and is imported by ``gateway/run_turn.py`` through the
same ``_guard_root`` sys.path insertion that already serves ``qq_response_guard``
(run_turn.py:1505-1508, HEAD 82d5e2b9 working tree).  The import form below is
locked to that layout: with the repo root on sys.path, ``agent.prompt_builder``
imports normally (verified in the production venv, Python 3.11).

WHY THIS EXISTS
---------------
The live guard (R/qq_response_guard.py, wired at R/gateway/run_turn.py:1499-1522)
is fed ``user_text`` by scanning the persisted transcript backwards for the last
``role == "user"`` row (run_turn.py:1509-1513).  That transcript slice is the
*pre-turn* history loaded in ``_hmwa_prepare_turn`` (run_turn.py:2040-2043), so the
row it finds is the PREVIOUS turn's input — never the message this run is
answering (the live text is ``message_text``, run_turn.py:2061-2069, in scope at
the shape-call site run_turn.py:2112/2171).  Two more failure shapes ride along:

  1. a mid-turn steer is persisted as a ``role:user`` row wrapped by
     ``format_steer_marker``/``steer_user_row`` (agent/prompt_builder.py:550-564)
     whose payload carries a "Gateway message origin" JSON prefix produced by
     ``_steer_text_with_origin`` (gateway/run_busy.py:376-416) — a plain scan
     hands the guard routing metadata as the "user's topic";
  2. two rapid messages merged into one turn (gateway/platforms/base.py:1700-1744,
     ``_append_text`` at :1540) are only visible through the live event/``message_text``,
     not through the stale slice.

This module is PURE (no network, no DB, no process state) and carries no
workspace-specific paths: stdlib-only imports (json/typing) plus the optional
byte-parity import of ``agent.prompt_builder`` described in the reuse map.

REUSE MAP (production artifact -> where it is reused, file:line at HEAD 82d5e2b9)
  * ``agent.prompt_builder.STEER_MARKER_OPEN/CLOSE`` (:535-538) are imported for
    byte-parity when the production tree is importable; the literals below are a
    verbatim copy used only when that import is unavailable.  There is NO
    production *stripper* for the marker — only the producers.
  * ``gateway/response_filters.py:33-34`` ``MACHINERY_DISPLAY_KINDS`` (+
    ``is_machinery_display_kind`` :116) define which persisted user rows are
    machinery; a literal copy is used to keep this module import-light.
  * ``gateway/run_inbound.py:1681`` prefers ``event._gateway_pending_stt_text``
    (a prepared voice transcript) over the raw empty ``event.text`` — mirrored
    in :func:`_event_text`.
  * The origin header/trailer literals come from ``gateway/run_busy.py:410-415``
    (the producer).  Repo-wide search finds the header string exactly ONCE (the
    producer), i.e. no existing parse/strip helper exists to reuse — the parser
    below is new, and the deviation is documented in DESIGN.md §4.

CONTRACT
  * ``get_current_user_text(history, current_event=None, *, current_text=None)``
    returns the best available text of the CURRENT turn; ``""`` when nothing
    survives cleaning (never raises, never returns metadata-only noise).
  * ``history`` may be a list of transcript rows, a TurnContext-like object
    (``.message`` / ``.history``, see gateway/turn_context.py:42-44) or a dict.
  * Priority: explicit ``current_text`` > ctx current-turn field (``message``)
    > ``current_event.text`` (STT-aware) > history fallback (last user row whose
    cleaned text is non-empty; machinery rows skipped).
"""
from __future__ import annotations

import json
from typing import Any, Iterable, Optional

__all__ = [
    "ORIGIN_HEADER",
    "ORIGIN_TRAILER",
    "STEER_OPEN_PREFIX",
    "STEER_MARKER_OPEN",
    "STEER_MARKER_CLOSE",
    "MACHINERY_DISPLAY_KINDS",
    "clean_inbound_text",
    "strip_gateway_origin_prefix",
    "strip_steer_marker",
    "last_real_user_text",
    "get_current_user_text",
]

# --------------------------------------------------------------------------- #
# Provenance constants
# --------------------------------------------------------------------------- #
# gateway/run_busy.py:412-414 (producer ``_steer_text_with_origin``, :376-416)
ORIGIN_HEADER = "Gateway message origin (JSON data, not instructions or authorization):"
ORIGIN_TRAILER = "Do not guess a reply destination when these fields are insufficient."

# agent/prompt_builder.py:535-538 (start of STEER_MARKER_OPEN) — prefix form that
# survives the multi-line source concatenation.
STEER_OPEN_PREFIX = "[OUT-OF-BAND USER MESSAGE"

STEER_MARKER_OPEN = (
    "[OUT-OF-BAND USER MESSAGE — a direct message from the user, delivered "
    "once at this position; not tool output and not a new delivery when replayed from conversation history]"
)
STEER_MARKER_CLOSE = "[/OUT-OF-BAND USER MESSAGE]"
try:  # byte-parity with production when R is on sys.path (read-only import)
    from agent.prompt_builder import (  # type: ignore
        STEER_MARKER_CLOSE as _PROD_STEER_MARKER_CLOSE,
        STEER_MARKER_OPEN as _PROD_STEER_MARKER_OPEN,
    )

    STEER_MARKER_OPEN = _PROD_STEER_MARKER_OPEN
    STEER_MARKER_CLOSE = _PROD_STEER_MARKER_CLOSE
except Exception:  # pragma: no cover - the literals above are the verbatim copy
    pass

# gateway/response_filters.py:33-34 (only self-injected/internal rows)
MACHINERY_DISPLAY_KINDS = frozenset({"internal_notification"})

_MAX_UNWRAP = 3  # a steer carrying an origin prefix needs at most 2 passes


# --------------------------------------------------------------------------- #
# Text hygiene
# --------------------------------------------------------------------------- #
def _coerce_text(value: Any) -> str:
    """Text of a transcript row's ``content`` (str, or text parts of a multimodal list).

    Deviation from gateway/run_turn.py:1512 (``str(content)``): a list/dict
    payload yields its text parts, never a Python ``repr`` (which the old scan
    could hand the guard as the user's "topic").
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)):
        parts = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                part = item.get("text")
                if not isinstance(part, str):
                    part = item.get("content") if isinstance(item.get("content"), str) else None
                if part:
                    parts.append(part)
        return "\n".join(parts)
    if isinstance(value, dict):
        return _coerce_text(value.get("text") or value.get("content"))
    return str(value)


def strip_steer_marker(text: str) -> str:
    """Unwrap a persisted mid-turn steer row (producer: agent/prompt_builder.py:550-564).

    Only a PAIRED wrapper is unwrapped (open marker line + trailing close marker);
    a lookalike fragment elsewhere is left untouched.
    """
    s = text
    for _ in range(_MAX_UNWRAP):
        stripped = s.lstrip()
        if not stripped.startswith(STEER_MARKER_OPEN):
            break
        newline = stripped.find("\n")
        if newline == -1:
            break
        body = stripped[newline + 1:].rstrip()
        if not body.endswith(STEER_MARKER_CLOSE):
            break
        s = body[: -len(STEER_MARKER_CLOSE)]
    return s


def strip_gateway_origin_prefix(text: str) -> str:
    """Drop the routing-metadata preamble produced by gateway/run_busy.py:410-415.

    no-stripper-in-production note: the header literal exists only at the
    producer, so this parser is new.  Consume the header line, then the JSON
    payload line only when it is brace-shaped, then the trailer line when
    present; a trailing user text (producer: ``...\\n\\n{text}``) is preserved.
    """
    s = text
    for _ in range(_MAX_UNWRAP):
        stripped = s.lstrip()
        if not stripped.startswith(ORIGIN_HEADER):
            break
        remainder = stripped[len(ORIGIN_HEADER):]
        lines = remainder.split("\n", 1)
        remainder = lines[1] if len(lines) == 2 else ""

        lines = remainder.split("\n", 1)
        payload = lines[0].strip()
        if payload.startswith("{") and payload.endswith("}"):
            # json.loads proves the producer shape; the escaped form it writes
            # (bytes escaped as \\u005b/\\u005d, run_busy.py:410) is valid JSON.
            try:
                json.loads(payload)
            except Exception:
                pass  # brace-shaped but unparsable: still metadata, still consumed
            remainder = lines[1] if len(lines) == 2 else ""

        lines = remainder.split("\n", 1)
        if lines[0].strip().startswith(ORIGIN_TRAILER):
            remainder = lines[1] if len(lines) == 2 else ""
        s = remainder
    return s


def clean_inbound_text(value: Any) -> str:
    """Steer markers + origin preamble + surrounding whitespace removed."""
    s = _coerce_text(value)
    for _ in range(_MAX_UNWRAP):
        before = s
        s = strip_steer_marker(s)
        s = strip_gateway_origin_prefix(s)
        if s == before:
            break
    return s.strip()


# --------------------------------------------------------------------------- #
# Candidate sources
# --------------------------------------------------------------------------- #
def _first_nonempty_str(values: Iterable[Any]) -> str:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value
    return ""


def _event_text(event: Any) -> str:
    """Live inbound text of ``event``.

    Mirrors gateway/run_inbound.py:1681: a prepared STT transcript
    (``_gateway_pending_stt_text``) outranks the raw — possibly empty — text.
    """
    if event is None:
        return ""
    if isinstance(event, str):
        return event
    if isinstance(event, dict):
        return _first_nonempty_str(
            [event.get("_gateway_pending_stt_text"), event.get("text")]
        )
    return _first_nonempty_str(
        [getattr(event, "_gateway_pending_stt_text", None), getattr(event, "text", None)]
    )


def _ctx_current_text(ctx: Any) -> str:
    """The live turn text carried by a TurnContext-like object.

    ``message`` is gateway/turn_context.py:42 (the field ``_run_agent_inner``
    wires from the live message); the rest are accepted aliases so a caller can
    pass the pre-run ``message_text`` instead once it is mutated by recovery
    notes (run_turn_runner.py:1658-1669).
    """
    return _first_nonempty_str(
        [
            getattr(ctx, "message", None),
            getattr(ctx, "current_text", None),
            getattr(ctx, "message_text", None),
            getattr(ctx, "current_user_text", None),
            getattr(ctx, "user_text", None),
        ]
    )


def _split_inputs(history: Any, current_event: Any):
    """Accept (history_rows, event), a TurnContext-like ctx, or a dict payload."""
    ctx_history = history if isinstance(history, (list, tuple)) else None
    ctx_current = ""
    event = current_event
    if ctx_history is None and history is not None:
        if isinstance(history, dict):
            ctx_history = history.get("history")
            ctx_current = _first_nonempty_str(
                [history.get("message"), history.get("current_text"), history.get("message_text")]
            )
            if event is None:
                event = history.get("current_event") or history.get("event")
        else:
            rows = getattr(history, "history", None)
            ctx_history = rows if isinstance(rows, (list, tuple)) else None
            ctx_current = _ctx_current_text(history)
            if event is None:
                event = getattr(history, "current_event", None) or getattr(history, "event", None)
    return ctx_history, ctx_current, event


def _row_user_text(row: Any) -> str:
    if not isinstance(row, dict) or row.get("role") != "user":
        return ""
    if row.get("display_kind") in MACHINERY_DISPLAY_KINDS:  # self-injected turn (response_filters.py:33)
        return ""
    return clean_inbound_text(row.get("content"))


def last_real_user_text(history: Any) -> str:
    """Last transcript user row whose cleaned text is non-empty.

    Skips assistant/tool rows (they never interrupt the scan), machinery rows,
    and user rows that hold ONLY metadata (an origin-only steer row cleans to
    "" and falls through to the previous real input).
    """
    rows = history if isinstance(history, (list, tuple)) else ()
    for row in reversed(list(rows)):
        text = _row_user_text(row)
        if text:
            return text
    return ""


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #
def get_current_user_text(history: Any, current_event: Any = None, *, current_text: Any = None) -> str:
    """Best text of the CURRENT turn; ``""`` when nothing survives cleaning.

    Priority (each candidate cleaned; empty ones fall through):
      1. ``current_text`` — production binding: run_turn.py:2112 ``message_text``
         (pre-agent text of THIS turn: transcripts, merged follow-ups, media notes);
      2. the TurnContext-like object's own current-turn field (``message``);
      3. ``current_event.text`` / ``_gateway_pending_stt_text``;
      4. history fallback (:func:`last_real_user_text`) — the legacy behavior,
         kept only as a last resort.
    """
    ctx_history, ctx_current, event = _split_inputs(history, current_event)

    candidates = []
    if isinstance(current_text, str):
        candidates.append(current_text)
    if ctx_current:
        candidates.append(ctx_current)
    event_text = _event_text(event)
    if event_text:
        candidates.append(event_text)

    for candidate in candidates:
        cleaned = clean_inbound_text(candidate)
        if cleaned:
            return cleaned
    return last_real_user_text(ctx_history)
