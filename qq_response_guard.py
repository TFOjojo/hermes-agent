#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""QQ DM response guard — Layer D of the topic-flood repair (plan 2026-09-19).

Last-line-of-defense filter for NORMAL inbound replies on the QQ channel
(the proactive pipeline has its own governance). Pure functions, injected
clock where needed, no network, no DB.

Contract:
  - "pass": reply unchanged (normal case — guard must be invisible);
  - "trim": reply contains off-topic nudge paragraphs; return only the
    paragraphs tied to the user's current message;
  - never returns empty on a user turn: if everything is off-topic, keep
    the FIRST paragraph (topic acknowledgment) rather than swallowing;
  - parse errors degrade to "pass" (fail-open) — the fix lives upstream in
    prompt/evidence layers; this guard only catches rare regressions;
  - long work/technical replies (>400 chars) are never trimmed.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional

__all__ = ["GuardDecision", "guard_qq_response", "NUDGE_PATTERNS"]

# Off-topic nudge patterns the user has explicitly banned as unprompted
# re-introductions (screenshot evidence 2026-09-18/19).
NUDGE_PATTERNS = [
    re.compile(r"(qq\.exe|chrome\.exe)", re.I),
    re.compile(r"(已经?|连续)\s*\d+\s*(个)?小时"),
    re.compile(r"吱一声"),
    re.compile(r"(喝水|伸懒腰|眨眨眼|眨眨眼睛)"),
    re.compile(r"(看看窗外|抬头看|眼睛要抗议)"),
    re.compile(r"(不许报仇|不许报复|烤串)"),
    re.compile(r"(午饭点|晚饭点|别空着肚子)"),
    re.compile(r"(我还没等到|你还没回我)"),
    re.compile(r"(起来活动|久坐)"),
]

# Urge-tone patterns: even when the topic overlaps the user's words, these
# push-style framings are the banned "nudge" voice (screenshot evidence
# 2026-09-18/19: 友情提醒/探头/抬头/不许报仇/都偏西了…).
URGE_PATTERNS = [
    re.compile(r"(友情提醒|探头|抬起头|抬头看|看看窗外|不许报仇|不许报复)"),
    re.compile(r"(记得喝水|记得吃饭|别空着肚子|我在这儿等你回来)"),
    re.compile(r"(都偏西了|点啦|～啦)"),
]

# Topics the user may legitimately raise; a paragraph mentioning them is
# on-topic ONLY when the user's own message mentions the same topic.
_TOPIC_KEYS = {
    "rest": re.compile(r"(累|休息|睡|困|熬夜|久坐|坐了|眼睛|酸|眨眼|伸懒腰|活动|窗外|喝水|渴)"),
    "meal": re.compile(r"(饭|吃|饿了|午饭|晚饭|早饭|烤串|外卖)"),
    "reply_due": re.compile(r"(回|吱一声|在吗|还在|等你)"),
    "screen": re.compile(r"(屏幕|电脑|手机|游戏|qq|chrome|浏览器)"),
}

_USER_BAN = re.compile(r"(别再|不要再|不用再|不需要再|别提醒|别催)")

# Short-affection input: one topic continuation at most.
_SHORT_INPUT_MAX_PARAS = 6


@dataclass
class GuardDecision:
    action: str            # "pass" | "trim"
    text: str              # final outbound text
    reason: str = ""       # structured audit reason
    dropped_paras: int = 0


def _paragraphs(text: str) -> List[str]:
    """Two-level split: blank-line paragraphs, then sentences inside any
    paragraph that still contains newlines or runs long (chat replies glue
    nudge sentences to normal ones with single newlines)."""
    parts = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    out: List[str] = []
    for para in parts:
        if "\n" in para or len(para) > 120:
            out.extend(s.strip() for s in re.split(r"(?<=[。！？!?～])\s*", para) if s.strip())
        else:
            out.append(para)
    if not out:
        out = [s.strip() for s in re.split(r"(?<=[。！？!?～])\s*", text) if s.strip()]
    return out


def _is_nudge(para: str) -> bool:
    return (any(p.search(para) for p in NUDGE_PATTERNS)
            or any(p.search(para) for p in URGE_PATTERNS))


def _user_topics(user_text: str) -> set:
    topics = set()
    for name, rx in _TOPIC_KEYS.items():
        if rx.search(user_text or ""):
            topics.add(name)
    return topics


def _para_topics(para: str) -> set:
    topics = set()
    for name, rx in _TOPIC_KEYS.items():
        if rx.search(para):
            topics.add(name)
    return topics


def _on_topic(para: str, user_topics: set) -> bool:
    """A nudge paragraph is acceptable only when its topic was user-raised."""
    pt = _para_topics(para)
    if not pt:
        return True  # neutral affection/chat paragraph — keep
    return bool(pt & user_topics)


def guard_qq_response(user_text: str, candidate: str) -> GuardDecision:
    """Decide whether the candidate reply may go out as-is on the QQ channel."""
    if not candidate or not candidate.strip():
        return GuardDecision("pass", candidate)
    # Long/technical replies are exempt ONLY when they carry no nudge
    # patterns; a long flood (screenshot case: 790 chars of qq.exe/chrome.exe
    # paras) is still subject to trimming.
    if len(candidate) > 400 and not any(p.search(candidate) for p in NUDGE_PATTERNS):
        return GuardDecision("pass", candidate)
    # No nudge pattern anywhere → invisible.
    if not any(p.search(candidate) for p in NUDGE_PATTERNS):
        return GuardDecision("pass", candidate)

    user_topics = _user_topics(user_text)
    # User explicitly banned reminders AND the reply still nudges → hard trim.
    banned_now = bool(_USER_BAN.search(user_text or ""))

    paras = _paragraphs(candidate)
    kept: List[str] = []
    dropped = 0
    for para in paras:
        if _is_nudge(para) and (banned_now or not _on_topic(para, user_topics)):
            dropped += 1
            continue
        kept.append(para)

    if dropped == 0:
        return GuardDecision("pass", candidate)
    if not kept:
        # Never swallow a user turn entirely: keep the first paragraph.
        kept = [paras[0]]
        return GuardDecision("trim", kept[0], reason="all_off_topic_keep_first", dropped_paras=dropped)

    out = "\n\n".join(kept)
    # Short affection input: cap continuations after the first on-topic paragraph.
    if len(user_text or "") <= 12 and len(kept) > _SHORT_INPUT_MAX_PARAS:
        out = "\n\n".join(kept[:_SHORT_INPUT_MAX_PARAS])
    return GuardDecision("trim", out, reason=f"dropped_{dropped}_nudge_paras", dropped_paras=dropped)
