# -*- coding: utf-8 -*-
"""Relationship evidence classification policy (Plan B).

Splits WHO said it (role), WHAT it is (kind), WHETHER it is actionable, and
HOW FRESH it is — the four axes the old single-keyword status regexes
confused. Pure functions, no I/O, no LLM.

Design contract (plan §4.1):
  - assistant self-nudges are never user obligations (never pending);
  - explicit negation / cancellation wins over stale keyword matches;
  - explicit future requests survive ephemeral TTL;
  - quoted third-party speech is not the user's own request;
  - "还没X" is not completion even though DONE matches "X";
  - meal-type observations still age out to stale after the TTL.
"""
from __future__ import annotations

import re
from typing import Dict, Optional

__all__ = ["classify_message", "RelationshipItem", "SEMANTIC_KINDS"]

# ── Patterns (order matters within each family) ───────────────────────────

# Negation/cancellation of a category of proactive nudges. Extracted before
# any completion matching so "不要再提醒我吃午饭" can never look like "done".
CANCEL = re.compile(r"(不要再|别再|不用|不用再|不需要|不要|停止|别|取消|关掉|关闭)\s*(提醒|催|说|问|发|提)", re.I)

# Explicit future-anchored request from the user.
REQUEST = re.compile(r"(提醒我|记得让我|到时(候)?(记得|提醒)|到点(记得|提醒))", re.I)
REQUEST_TIME = re.compile(r"(明天|后天|下周|等下|待会儿|晚上|下午|上午|中午|早上|周[一二三四五六日末])")

# User commitment / promise (self-declared obligation).
COMMIT = re.compile(r"(等我|我(会|要|打算|想)|下次|之后|改天|回头)")

# Completion, guarded against "还没" style negation.
DONE = re.compile(r"(已经|已經|完成了|做完了|吃过了|吃過了|看过了|看過了|洗完了|回来了|回來了|结束了|結束了|解决了|解決了|收到)")
NOT_YET = re.compile(r"(还没|還沒|尚未|没做完|沒做完|还没完|還沒完)")

# Quoted third-party speech: the user reporting someone else's words.
QUOTED = re.compile(r"(他说|她他说|她[说讲]|别人[说讲]|妈妈[说叫]|爸[爸说](说)?|领导[说让])")

# Assistant-originated nudges/invitations (background only, never pending).
ASSISTANT_NUDGE = re.compile(r"(记得|休息|喝水|早点睡|睡觉|吃饭|等(你|我)|别熬夜|起来动)", re.I)
ASSISTANT_HOURS = re.compile(r"(\d+\s*(个)?小时|都\d+点)", re.I)

# Ephemeral meal/rest observations: age out after TTL regardless of wording.
EPHEMERAL = re.compile(r"(刚吃|剛吃|吃完饭|吃完飯|午饭|午飯|晚饭|晚飯|早饭|早飯|胃有点|消食|刚休息|刚睡醒)")
# "刚吃完X" style completions the DONE list misses (no 已经/了 keywords).
EPHEMERAL_COMPLETION = re.compile(r"(刚|剛|已)\s*(吃完|吃完饭|吃完飯|吃好|吃饱|到家|睡醒|洗完|看完)")

# Preference statements about channel/behavior ("以后别在QQ发").
PREFERENCE = re.compile(r"(以后|以后|之后)(别|不要|不)|别在|不想收到|太吵|少发点")

TTL_SECONDS = 3 * 60 * 60

SEMANTIC_KINDS = (
    "nudge",            # assistant-originated reminder (background only)
    "invitation",       # assistant invitation (background only)
    "request",          # explicit user request for a future action
    "commitment",       # user's own promise
    "preference",       # standing behavior preference
    "preference_cancel",# cancellation of a nudge category
    "observation",      # state report ("刚吃完午饭")
    "affection",
    "other",
)


class RelationshipItem(dict):
    """Dict-based item; keys documented in plan §4.1."""

    __slots__ = ()


def _freshness(occurred_at: float, now: float) -> str:
    if not occurred_at or occurred_at <= 0:
        return "unknown"
    age = now - occurred_at
    if age < 0:
        return "unknown"
    if age <= 24 * 3600:
        return "fresh"
    return "stale"


def classify_message(
    role: str,
    text: str,
    occurred_at: float,
    now: float,
) -> Dict:
    """Classify one chat line into the plan's RelationshipItem dict.

    Keys: role, kind, status, actionable, freshness, banned_topic (optional),
    owner ("user"|"assistant"), note.
    """
    text = (text or "").strip()
    role = (role or "").strip().lower()
    is_user = role == "user"
    out = {
        "role": role,
        "kind": "other",
        "status": "current",
        "actionable": False,
        "freshness": _freshness(occurred_at, now),
        "owner": "user" if is_user else "assistant",
    }
    if not text:
        out["status"] = "current"
        return out

    # Assistant lines: nudges/invitations/hour assertions are background.
    if not is_user:
        if ASSISTANT_NUDGE.search(text) or ASSISTANT_HOURS.search(text):
            out["kind"] = "nudge"
            out["status"] = "background"
            return out
        out["kind"] = "other"
        out["status"] = "current"
        return out

    # User lines — negation/cancellation first.
    m_cancel = CANCEL.search(text)
    if m_cancel:
        topic = _cancel_topic(text)
        out["kind"] = "preference_cancel"
        out["status"] = "cancelled"
        out["actionable"] = False
        if topic:
            out["banned_topic"] = topic
        return out

    # Quoted third-party speech: never the user's own request.
    if QUOTED.search(text):
        out["status"] = "uncertain"
        return out

    # Explicit future request → pending, survives TTL.
    if REQUEST.search(text) and (REQUEST_TIME.search(text) or "提醒" in text):
        out["kind"] = "request"
        out["status"] = "pending"
        out["actionable"] = ["user"]
        out["actionable"] = True
        return out

    # User's own commitment → pending.
    if COMMIT.search(text):
        out["kind"] = "commitment"
        out["status"] = "pending"
        out["actionable"] = True
        return out

    # Completion, guarded by negation.
    if NOT_YET.search(text):
        out["kind"] = "observation"
        out["status"] = "pending" if REQUEST.search(text) else "uncertain"
        return out
    if DONE.search(text) or EPHEMERAL_COMPLETION.search(text):
        out["kind"] = "observation"
        out["status"] = "completed"
        # Ephemeral completions age out to stale after TTL.
        if EPHEMERAL.search(text):
            if now - (occurred_at or 0) >= TTL_SECONDS:
                out["status"] = "stale"
        return out

    if PREFERENCE.search(text):
        out["kind"] = "preference"
        return out

    out["kind"] = "other"
    out["status"] = "current"
    return out


def _cancel_topic(text: str) -> str:
    """Best-effort topic key for a cancellation ('午饭' from 别再提醒我吃午饭)."""
    for kw in ("午饭", "晚饭", "早饭", "饭", "喝水", "休息", "睡", "走动", "运动", "视频"):
        if kw in text:
            return kw
    return ""
