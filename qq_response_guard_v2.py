#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""qq_response_guard_v2 — QQ DM response guard (semantic v2; drop-in for v0).

Deployed at the hermes-agent repo root and wired in ``gateway/run_turn.py``
(QQ guard block) as ``from qq_response_guard_v2 import guard_qq_response``.
The v0 module (``qq_response_guard.py``) stays on disk for rollback/reference.

Same public entry point and same dataclass field order as v0::

    guard_qq_response(user_text, candidate, policy=None) -> GuardDecision
    GuardDecision(action, text, reason="", dropped_paras=0, restored_paras=0)

Semantic changes vs v0
----------------------
1. **Anchor first.** Every drop decision is checked against the user's CURRENT
   turn text (``user_text``). Where that text comes from is P2's binding fix;
   this module only consumes it as a parameter.
2. **Four violation classes**, each dropped only when the anchor has no
   genuine relevance:
     - ``pressure``  催促/回调/进程点名 (qq.exe, N 小时, 吱一声, 友情提醒, 都偏西…);
     - ``dunning``   追讨/check-in (吃完没…、别忘了+旧事项);
     - ``old_ref``   旧话题指称 (上次/那本/之前…);
     - ``care``      关心建议 (喝水/眨眼/起来活动/早点睡…).
   Plain nouns (炸串/烤串/漫画/待办) are NEVER banned per se — they only ever
   appear inside the care/domain lexicons used for *anchoring*, never as drop
   triggers. v0 banned 「烤串」 globally; v2 deliberately does not.
3. **False-kill guards.** Care advice survives whenever the user opened that
   topic — including colloquial phrasing the v0 topic dictionary missed
   (「天热要怎么补水」) — because anchoring uses content bigrams + care-domain
   overlap + a user-asks-a-health-question rule, not a fixed keyword bucket.
   User-requested callbacks (「傍晚叫我一声」) are anchored by content overlap,
   so their own acknowledgment is never trimmed (v0 trimmed it, see W2).
4. **Honest accounting.** ``dropped_paras`` counts only units actually removed.
   A candidate whose final text equals the original returns ``action="pass"``
   instead of v0's misleading trim-but-identical (``all_off_topic_keep_first``
   with ``dropped_paras=1`` on a text that never changed). Units re-added by
   the never-empty contract are reported via ``restored_paras`` + ``reason``.
5. **Formatting-safe rebuild.** The output is rebuilt by removing spans from
   the original string, so single-newline layouts survive and v0's
   join-with-"\\n\\n" length growth (P0 ROOT-CAUSE 附带问题 A) cannot happen.
6. **Never-empty contract kept** (aligned with v0): if every unit is dropped,
   the first unit is restored and the decision is flagged in ``reason``
   (``all_off_topic_keep_first*``) for upstream silence handling.
7. **Exceptions propagate exactly like v0** (caller keeps its fail-open
   ``try/except``); no new silent paths.

Policy knobs (``GuardPolicy``) let the caller turn each class off without
touching code — useful while灰度量 trim rate.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional, Sequence, Set, Tuple

__all__ = [
    "GuardDecision",
    "GuardPolicy",
    "guard_qq_response",
    "PRESSURE_PATTERNS",
    "DUNNING_PATTERNS",
    "OLD_REF_PATTERNS",
    "CARE_PATTERNS",
    "NUDGE_PATTERNS",  # v0-compatible alias: union of all four classes
    "classify_unit",
    "classify_pressure",
    "classify_care",
    "classify_dunning",
    "classify_old_ref",
    "shared_tokens",
    "unit_spans",
]

# --------------------------------------------------------------------------- #
# lexicons
# --------------------------------------------------------------------------- #

# Pressure: the banned 催促/回调/催讨 voice (v0 NUDGE + URGE pressure items +
# the Chinese-numeral duration forms v0 missed, e.g. 「已经三个小时」).
PRESSURE_PATTERNS = [
    re.compile(r"(qq\.exe|chrome\.exe)", re.I),
    re.compile(r"(已经?|连续|都|都开)\s*[\d一二三四五六七八九十两]+\s*个?\s*小时"),
    re.compile(r"[\d一二三四五六七八九十两]+\s*个?小时(没动|没回|多了)"),
    re.compile(r"(吱一[声声聲]|吱个[声声聲])"),
    re.compile(r"(我还没等到|你还没回我|还没回我|怎么还不回|你还没回)"),
    re.compile(r"(催你|催一下|提醒你一下|提醒过我|友情提醒)"),
    re.compile(r"(不许报仇|不许报复|探头|抬起头|抬头看)"),
    re.compile(r"(都偏西|点啦|～啦)"),
    re.compile(r"(我在这儿等你回来|等你回来)"),
]

# Dunning (追讨): check-in questions + reminders that carry an OLD item.
# A bare 「别忘了带伞」 (situational, no old item) is deliberately NOT in this
# class — see GAP-REPORT §风险.
_DUNNING_CHECKIN = re.compile(
    r"(吃完没|吃了没|喝完了?没|喝了没|睡了没|写完没|看完没|弄完没|干完没|"
    r"做完了吗|回了没|看完了吗|交了吗)"
)
_OLD_ITEM = r"(待办|本子|本儿|上次|之前|那[本条份件台部支]|复习|作业|报告|任务|进度|计划|安排|收拾)"
_DUNNING_REMINDER_OLD = re.compile(
    rf"(?:{_OLD_ITEM})[^。！？!?～]{{0,12}}?(?:别忘了|别忘啦|别忘记|不要忘)"
    rf"|(?:别忘了|别忘啦|别忘记|不要忘)[^。！？!?～]{{0,12}}?(?:{_OLD_ITEM})"
)
DUNNING_PATTERNS = [_DUNNING_CHECKIN, _DUNNING_REMINDER_OLD]

# Old-thread references: a definite pointer at an old item/thread. Only drops
# when the current turn does not share content/domain with it.
OLD_REF_PATTERNS = [
    re.compile(r"(上次|早前|之前|去年|前几天|前两天|那[本条份件台部支])"),
]

# Care: advice/reminder-of-care voice (kept whenever the user opened the topic).
CARE_PATTERNS = [
    re.compile(r"(喝水|喝点水|多喝水|喝口?热水|补水|渴了|口渴)"),
    re.compile(r"(记得吃|吃饭|吃了吗|饿着|别空着肚子|空着肚子|午饭点|晚饭点|别饿)"),
    re.compile(r"(伸[个一]?懒腰|眨眨眼|眨眨眼睛|眨眼)"),
    re.compile(r"(看看窗外|抬头看|眺望|望远)"),
    re.compile(r"(眼睛要抗议|护眼|闭眼休息|休息一下眼睛)"),
    re.compile(r"(起来活动|活动活动|走两步|别久坐|久坐|一直坐着|别一直坐|起来动动)"),
    re.compile(r"(记得休息|好好休息|注意休息|早点睡|早睡|别熬夜|少熬夜|熬夜不好)"),
    re.compile(r"(别吃太多|吃太多|少吃点|多穿点|加件衣服)"),
]

# v0-compatible alias (some callers/monitors iterate the old name).
NUDGE_PATTERNS: List[re.Pattern] = (
    PRESSURE_PATTERNS + DUNNING_PATTERNS + OLD_REF_PATTERNS + CARE_PATTERNS
)

# Topics/domains used ONLY for anchoring (is this paragraph about something the
# user actually raised?). Never used to ban a noun.
_DOMAINS = {
    "water": re.compile(r"(水|喝|渴|补水|饮料)"),
    "meal": re.compile(r"(饭|吃|饿|外卖|烤串|炸串|肚子|零食)"),
    "eyes": re.compile(r"(眼|酸|屏幕|看太久|视力|近视)"),
    "rest": re.compile(r"(睡|困|熬夜|休息|累|倦|精神)"),
    "body": re.compile(r"(坐|站|活动|动一|腰|肩|脖|腿|走两步|久|僵)"),
    "screen": re.compile(r"(屏幕|电脑|手机|游戏|进程|浏览器|qq|chrome|软件|程序|关机|开着|后台)", re.I),
    "work": re.compile(r"(工作|上班|报告|作业|任务|写|忙|项目|开会|加班|进度|计划)"),
}

_USER_BAN = re.compile(r"(别再|不要再|不用再|不需要再|别提醒|别催)")
_QUESTION = re.compile(r"(？|\?|怎么办|怎么|怎样|如何|为什么|要不要|能不能|可不可以|该不该|需要|吗|呢)")

# Bigram stop list: generic connectives that must not create a false anchor.
_STOP_TOKENS = frozenset({
    "什么", "怎么", "这个", "那个", "哪个", "我们", "你们", "他们", "咱们",
    "就是", "还是", "可能", "已经", "一直", "一下", "不是", "没有", "这样",
    "那么", "真的", "好的", "可以", "时候", "现在", "觉得", "应该", "不过",
    "但是", "因为", "所以", "如果", "然后", "而且", "可是", "只是", "还有",
    "一起", "好像", "有点", "一点", "每天", "一天", "今天", "明天", "昨天",
    "你好", "您好", "谢谢", "哈哈", "嘿嘿", "呵呵", "嗯嗯", "哦哦", "对了",
    "好呀", "好嘞", "记得", "别忘了", "别忘啦", "已经",
})

_CJK_RUN = re.compile(r"[\u3400-\u9fff]+")
_LATIN_RUN = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.\-]{1,}")


@dataclass(frozen=True)
class GuardPolicy:
    """Tunable thresholds; defaults are the recommended ones (see GAP-REPORT)."""

    long_exempt_chars: int = 400
    short_input_max_chars: int = 12
    max_unanchored_for_short_input: int = 3
    drop_unanchored_pressure: bool = True
    drop_unanchored_dunning: bool = True
    drop_unanchored_care: bool = True
    drop_unanchored_old_refs: bool = True


@dataclass
class GuardDecision:
    action: str            # "pass" | "trim"
    text: str              # final outbound text
    reason: str = ""       # structured audit reason
    dropped_paras: int = 0  # units ACTUALLY removed
    restored_paras: int = 0  # units re-added by the never-empty contract


# --------------------------------------------------------------------------- #
# text units (paragraph -> sentence two-level split, with spans)
# --------------------------------------------------------------------------- #

def _para_spans(text: str) -> List[Tuple[int, int]]:
    spans: List[Tuple[int, int]] = []
    pos = 0
    cur: Optional[List[int]] = None
    for line in text.split("\n"):
        s, e = pos, pos + len(line)
        pos += len(line) + 1
        if line.strip():
            if cur is None:
                cur = [s, e]
            else:
                cur[1] = e
        else:
            if cur is not None:
                spans.append((cur[0], cur[1]))
                cur = None
    if cur is not None:
        spans.append((cur[0], cur[1]))
    return spans


def unit_spans(text: str) -> List[Tuple[int, int]]:
    """Two-level split (blank-line paragraphs, then sentences inside any
    paragraph that carries newlines or runs long) — same granularity as v0,
    but returning character spans instead of stripped strings."""
    out: List[Tuple[int, int]] = []
    for s, e in _para_spans(text):
        para = text[s:e]
        if "\n" in para or len(para) > 120:
            for m in re.finditer(r"[^。！？!?～]*(?:[。！？!?～]+|$)", para):
                seg = m.group(0)
                if not seg.strip():
                    continue
                lead = len(seg) - len(seg.lstrip())
                trail = len(seg) - len(seg.rstrip())
                out.append((s + m.start() + lead, s + m.start() + len(seg) - trail))
        else:
            out.append((s, e))
    return out


def _rebuild(text: str, units: Sequence[Tuple[int, int]], keep: Sequence[int]) -> str:
    """Reassemble by removing the dropped spans from the ORIGINAL string, so
    untouched layout (single newlines, bullets) is preserved verbatim."""
    parts: List[str] = []
    prev_end: Optional[int] = None
    for i in keep:
        s, e = units[i]
        if prev_end is not None:
            window = text[prev_end:s]
            if "\n" in window:
                join = "\n\n" if re.search(r"\n[ \t]*\n", window) else "\n"
            else:
                join = window if window.strip() == "" else ""
            parts.append(join)
        parts.append(text[s:e])
        prev_end = e
    out = "".join(parts)
    out = "\n".join(line.rstrip() for line in out.split("\n")).strip()
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out


# --------------------------------------------------------------------------- #
# classification & anchoring
# --------------------------------------------------------------------------- #

def classify_pressure(unit: str) -> bool:
    return any(p.search(unit) for p in PRESSURE_PATTERNS)


def classify_dunning(unit: str) -> bool:
    return any(p.search(unit) for p in DUNNING_PATTERNS)


def classify_old_ref(unit: str) -> bool:
    return any(p.search(unit) for p in OLD_REF_PATTERNS)


def classify_care(unit: str) -> bool:
    return any(p.search(unit) for p in CARE_PATTERNS)


def classify_unit(unit: str) -> Optional[str]:
    """Return the unit's violation class (priority order), or None."""
    for name, fn in (
        ("pressure", classify_pressure),
        ("dunning", classify_dunning),
        ("old_ref", classify_old_ref),
        ("care", classify_care),
    ):
        if fn(unit):
            return name
    return None


def _tokens(text: str) -> Set[str]:
    out: Set[str] = set()
    for m in _CJK_RUN.finditer(text):
        run = m.group(0)
        for i in range(len(run) - 1):
            out.add(run[i:i + 2])
    for m in _LATIN_RUN.finditer(text.lower()):
        out.add(m.group(0))
    return {t for t in out if t not in _STOP_TOKENS}


def shared_tokens(a: str, b: str) -> Set[str]:
    """Content tokens (CJK bigrams + latin words ≥2, stop-listed) shared by two
    texts — the anchor test's primary signal."""
    return _tokens(a) & _tokens(b)


def _domains(text: str) -> Set[str]:
    return {name for name, rx in _DOMAINS.items() if rx.search(text or "")}


def _anchored(unit: str, cls: Optional[str], user_text: str,
              user_domains: Set[str], user_care_question: bool) -> bool:
    """Is this unit genuinely tied to what the user raised right now?"""
    if shared_tokens(unit, user_text):
        return True
    unit_domains = _domains(unit)
    if unit_domains & user_domains:
        return True
    if cls == "care" and user_care_question and unit_domains:
        return True
    return False


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #

def guard_qq_response(user_text: str, candidate: str,
                      policy: Optional[GuardPolicy] = None) -> GuardDecision:
    """Decide whether the candidate reply may go out as-is on the QQ channel."""
    pol = policy or GuardPolicy()
    if not candidate or not candidate.strip():
        return GuardDecision("pass", candidate)

    units = unit_spans(candidate)
    if not units:
        return GuardDecision("pass", candidate)
    texts = [candidate[s:e] for s, e in units]
    classes = [classify_unit(t) for t in texts]

    # No violation anywhere → guard invisible (same as v0).
    if not any(classes):
        return GuardDecision("pass", candidate)

    # Long/technical replies are exempt unless they carry pressure/dunning
    # units (v0: any nudge match broke the exemption; care-only long replies
    # now pass, which protects long health/work answers).
    if len(candidate) > pol.long_exempt_chars and not any(
        c in ("pressure", "dunning") for c in classes
    ):
        return GuardDecision("pass", candidate, reason="long_exempt")

    user_text = user_text or ""
    user_domains = _domains(user_text)
    user_care_question = bool(_QUESTION.search(user_text)) and bool(user_domains)
    banned_now = bool(_USER_BAN.search(user_text))

    anchored = [
        _anchored(t, c, user_text, user_domains, user_care_question)
        for t, c in zip(texts, classes)
    ]

    drop: Set[int] = set()
    for i, (t, c) in enumerate(zip(texts, classes)):
        if c is None:
            continue
        if banned_now:
            drop.add(i)  # user explicitly banned reminders → hard drop
            continue
        if c == "pressure" and not pol.drop_unanchored_pressure:
            continue
        if c == "dunning" and not pol.drop_unanchored_dunning:
            continue
        if c == "care" and not pol.drop_unanchored_care:
            continue
        if c == "old_ref" and not pol.drop_unanchored_old_refs:
            continue
        if not anchored[i]:
            drop.add(i)

    # Short affection input: cap the number of UNANCHORED kept units.
    # Anchored content is never capped (v0's positional cap could cut it).
    capped = 0
    if len(user_text) <= pol.short_input_max_chars:
        unanchored_kept = [
            i for i, c in enumerate(classes)
            if i not in drop and not anchored[i]
        ]
        if len(unanchored_kept) > pol.max_unanchored_for_short_input:
            extra = unanchored_kept[pol.max_unanchored_for_short_input:]
            drop.update(extra)
            capped = len(extra)

    if not drop:
        return GuardDecision("pass", candidate)

    keep = [i for i in range(len(units)) if i not in drop]

    if not keep:
        # Never swallow a user turn entirely: restore the first unit.
        keep = [0]
        out = _rebuild(candidate, units, keep)
        restored = len(units) - 1
        if len(units) == 1 or out == candidate:
            return GuardDecision(
                "pass", candidate, reason="all_off_topic_keep_first_noop",
                dropped_paras=0, restored_paras=len(units),
            )
        return GuardDecision(
            "trim", out, reason="all_off_topic_keep_first",
            dropped_paras=len(units) - 1, restored_paras=1,
        )

    out = _rebuild(candidate, units, keep)
    if out == candidate:
        # Nothing the user can see changed — report a pass, not a phantom trim.
        return GuardDecision(
            "pass", candidate, reason="trim_noop_identical",
            dropped_paras=0, restored_paras=len(drop),
        )

    reason = f"dropped_{len(drop)}_nudge_paras"
    if capped:
        reason += "+short_cap"
    return GuardDecision("trim", out, reason=reason, dropped_paras=len(drop))
