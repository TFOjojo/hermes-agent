# -*- coding: utf-8 -*-
"""qq_lifeline 三层话题分级（vendored：tools/context_assembler/sources/qq_topic_layering.py）。

CURRENT_TURN / ACTIVE_THREAD / BACKGROUND_ONLY —— 背景层只能维持语气与风格，
不得作为话题素材。纯 stdlib、纯函数：无 I/O、无网络、无模型调用、不 import
任何生产模块；消费者是 sources/qq_lifeline.py 里的 apply_to_evidence() 接入点。

问题（2026-09-19 QQ 私聊旧话题倾倒）：
    7 天 lifeline 里的旧助手话题（炸串 / 漫画 / 待办本 / 傍晚 / 吱一声）被当成
    当前话题延续。现有 `background_only_evidence()` 只覆盖「助手催促 + 年龄 >= 2h」
    一种显式模式，普通旧话题名词没有任何分级。

判定规则（详见 P3/DESIGN.md）：

  行级（role=user）：
    1. 与当前输入同一轮（文本相等/包含，或最近一条用户行且 <= current_turn_sec）
       → CURRENT_TURN；
    2. 当前输入含「让它过去」信号（翻篇/别提了/别聊了…）且该行与当前输入无主题
       关联 → BACKGROUND_ONLY；
    3. 其余 → ACTIVE_THREAD（用户 authored 行永不降级：他说过的话是他的上下文）。

  行级（role=assistant）：
    4. 催促语态（pressure：吱一声 / N 小时没回 / qq.exe 运行时长 / 还没回我 /
       不许报仇）→ BACKGROUND_ONLY（无论年龄、无论当前输入，永不作话题素材）；
    5. 关心语态（care：喝水/伸懒腰/看看窗外/久坐提醒…）：
         年龄 >= nudge_background_after_sec(2h) 或 与当前输入无主题关联 → BACKGROUND_ONLY；
         既新（<2h）且与当前输入同主题 → ACTIVE_THREAD（用户此刻就在这个话题上）；
    6. 与当前输入主题重叠（关键词双字/词元交集）→ ACTIVE_THREAD（旧话题被当前输入
       明确重提时升级回 ACTIVE；不受轮次/年龄限制 → 保护长工作上下文）；
    7. 最近（<= active_window_sec 且位于末尾 active_last_n 行内，或 <= fresh_grace_sec）
       → ACTIVE_THREAD（进行中的对话）；
    8. 其余 → BACKGROUND_ONLY。

  其它（L3 承诺/情绪余韵等无角色前缀的证据）按 user 处理：不降级（fail-open）。

主题关联用「双字/词元交集 + 停用词表」，不搞 embedding。催促正则覆盖生产
`_NUDGE` 的变体（生产对 "连续 5 个小时"、"伸个懒腰" 这类插入量词不命中）。
注意：炸串 / 烤串 / 漫画等是**话题名词**，不是催促 —— 用户重提时允许激活。
"""
from __future__ import annotations

import re
import time
import unicodedata
from dataclasses import dataclass, field
from typing import Dict, FrozenSet, List, Optional, Sequence, Tuple

__all__ = [
    "LAYER_CURRENT_TURN",
    "LAYER_ACTIVE_THREAD",
    "LAYER_BACKGROUND_ONLY",
    "LAYER_USAGE_NOTES",
    "TAG_BACKGROUND",
    "LifelineLine",
    "LayerPolicy",
    "LayeredLine",
    "LayeredContext",
    "classify_lines",
    "classify_one",
    "nudge_class",
    "is_nudge",
    "topic_tokens",
    "shared_topic_tokens",
    "shares_topic",
    "is_current_turn",
    "is_recent",
    "parse_excerpt",
    "apply_to_evidence",
    "render_prompt_block",
    "DEFAULT_POLICY",
]

# ── 层名与用途限制 ──────────────────────────────────────────────────────────

LAYER_CURRENT_TURN = "CURRENT_TURN"
LAYER_ACTIVE_THREAD = "ACTIVE_THREAD"
LAYER_BACKGROUND_ONLY = "BACKGROUND_ONLY"

#: 背景层摘录前缀（excerpt-only 消费者——如 scripts/qq_context_snippet.py——
#: 只打印 excerpt，看不到 status，标注必须落在文本里）。
TAG_BACKGROUND = "〔仅背景·勿作话题〕"

LAYER_USAGE_NOTES: Dict[str, str] = {
    LAYER_CURRENT_TURN: "本轮输入直接相关：优先回应，不要扯开去讲别的。",
    LAYER_ACTIVE_THREAD: "与当前输入有主题关联：可自然续接，最多续一项，不整段复述。",
    LAYER_BACKGROUND_ONLY: "仅可用于语气/情绪/称呼的延续，不得作为话题素材：不得重提、不得追问、不得当待办。",
}

# ── 判定用词表 ──────────────────────────────────────────────────────────────

#: 催促-施压族：索取回应/监控在线时长，永不作话题素材（用户已明确反感）。
_PRESSURE = re.compile(
    r"(qq\.exe|chrome\.exe)"
    r"|((已经?|连续|都)\s*\d+\s*(个)?\s*(小时|钟头))"
    r"|(吱一声|吱个声)"
    r"|(我还没等到|你还没回我|还没回我|一直没回|怎么还不回)"
    r"|(不许报仇|不许报复)",
    re.I,
)

#: 催促-关心族：喝水/休息/久坐/护眼等提醒（旧催促必须留在背景层）。
_CARE = re.compile(
    r"(喝水|喝口水|多喝水|补点水)"
    r"|(伸个?懒腰|眨眨眼|眨眨眼睛|看看窗外|抬头看|眼睛要抗议|眼睛受不了)"
    r"|(午饭点|晚饭点|早饭点|别空着肚子|该吃饭|记得吃饭)"
    r"|(记得休息|该休息|早点睡|别熬夜|起来活动|活动一下|站起来|动一动|别久坐|久坐了)"
    r"|(我在这里等你|等你回来|我等你)",
    re.I,
)

_URGE_FRAME = re.compile(
    r"(记得|该|快|赶紧|别忘|要)[^，。！？；~～]{0,6}(喝水|休息|睡觉|吃饭|活动|动一动|护眼|站起来)"
)

#: 「让它过去」信号：用户明确把某话题丢掉。
_LET_GO = re.compile(r"(翻篇|别提了|不聊这个|别聊了|都过去了|过去的事|不提了|算了吧)")

#: 双字停用表：高频功能双字不构成主题关联。
_STOP_TOKENS = frozenset(
    {
        "今天", "明天", "昨天", "后天", "现在", "刚才", "然后", "就是", "还是",
        "不是", "没有", "我们", "你们", "他们", "这个", "那个", "哪个", "什么",
        "怎么", "为什", "干什", "时候", "地方", "东西", "一下", "一点", "有点",
        "知道", "觉得", "应该", "可以", "不要", "不用", "不能", "到了", "来了",
        "好了", "对吧", "是吧", "哈哈", "好的", "行了", "真的", "好像", "大概",
        "其实", "反正", "而且", "另外", "要是", "如果", "因为", "所以", "但是",
        "虽然", "之后", "之前", "以后", "以前", "起来", "出去", "回来", "看到",
        "听到", "说的", "做了", "吃了", "看了", "我的", "你的", "他的", "她的",
        "记得", "一些", "很多", "还有", "还要", "不去", "不想",
    }
)

_CJK_RUN = re.compile(r"[\u4e00-\u9fff]+")
_WORD = re.compile(r"[a-zA-Z][a-zA-Z0-9_]{1,}")


# ── 数据结构 ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class LifelineLine:
    """一行 lifeline 历史：角色、文本、发生时间。"""

    role: str  # "user" | "assistant" | "unknown"
    text: str
    occurred_at: float


@dataclass(frozen=True)
class LayerPolicy:
    """分层阈值（默认值与生产 2h 催促规则、7 天窗口相容）。"""

    current_turn_sec: float = 300.0        # 最近一条用户行 <=5min 视为本轮
    fresh_grace_sec: float = 900.0         # <=15min 一律算「新」，不因位置降级
    active_window_sec: float = 6 * 3600.0  # 进行中窗口
    active_last_n: int = 8                 # 末尾 N 行内视为进行中
    nudge_background_after_sec: float = 2 * 3600.0  # 旧催促阈值（=生产 BACKGROUND_AFTER_SEC）
    release_window_sec: float = 48 * 3600.0


DEFAULT_POLICY = LayerPolicy()


@dataclass
class LayeredLine:
    line: LifelineLine
    layer: str
    reason: str = ""


@dataclass
class LayeredContext:
    current_turn: List[LayeredLine] = field(default_factory=list)
    active_thread: List[LayeredLine] = field(default_factory=list)
    background_only: List[LayeredLine] = field(default_factory=list)
    policy: Optional[LayerPolicy] = None

    def all(self) -> List[LayeredLine]:
        return list(self.current_turn) + list(self.active_thread) + list(self.background_only)

    def counts(self) -> Dict[str, int]:
        return {
            LAYER_CURRENT_TURN: len(self.current_turn),
            LAYER_ACTIVE_THREAD: len(self.active_thread),
            LAYER_BACKGROUND_ONLY: len(self.background_only),
        }

    def render_prompt_block(self) -> str:
        return render_prompt_block(self)


# ── 主题关联（关键词重叠，无 embedding） ────────────────────────────────────


def _normalize(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text or "").casefold().split())


def topic_tokens(text: str) -> FrozenSet[str]:
    """主题词元：CJK 双字组 + 长度>=2 的拉丁词，剔除停用双字。"""
    norm = _normalize(text)
    tokens = set()
    for run in _CJK_RUN.findall(norm):
        if len(run) < 2:
            continue
        for i in range(len(run) - 1):
            bigram = run[i : i + 2]
            if bigram not in _STOP_TOKENS:
                tokens.add(bigram)
    for word in _WORD.findall(norm):
        tokens.add(word)
    return frozenset(tokens)


def shared_topic_tokens(a: str, b: str) -> FrozenSet[str]:
    return topic_tokens(a) & topic_tokens(b)


def shares_topic(a: str, b: str) -> bool:
    return bool(shared_topic_tokens(a, b))


# ── 催促判定 ────────────────────────────────────────────────────────────────


def nudge_class(text: str) -> Optional[str]:
    """「pressure」= 索取回应的施压；「care」= 关心式提醒；None = 非催促。"""
    text = text or ""
    if _PRESSURE.search(text):
        return "pressure"
    if _CARE.search(text) or _URGE_FRAME.search(text):
        return "care"
    return None


def is_nudge(text: str) -> bool:
    return nudge_class(text) is not None


# ── 单行与整体判定 ──────────────────────────────────────────────────────────


def _age(occurred_at: float, now: float) -> float:
    if not occurred_at or occurred_at <= 0:
        return float("inf")
    return max(0.0, now - float(occurred_at))


def is_current_turn(
    line: LifelineLine,
    current_input: str,
    age: float,
    is_last_user_line: bool,
    policy: LayerPolicy,
) -> bool:
    if (line.role or "").strip().lower() == "assistant":
        return False
    current = _normalize(current_input)
    text = _normalize(line.text)
    if not current or not text:
        return False
    if len(current) >= 4 and len(text) >= 4:
        if text == current or text in current or current in text:
            return True
    return bool(is_last_user_line and age <= policy.current_turn_sec)


def is_recent(age: float, pos_from_end: int, policy: LayerPolicy) -> bool:
    if age <= policy.fresh_grace_sec:
        return True
    return age <= policy.active_window_sec and pos_from_end < policy.active_last_n


def classify_one(
    line: LifelineLine,
    *,
    current_input: str = "",
    now: float,
    policy: LayerPolicy,
    is_last_user_line: bool = False,
    pos_from_end: int = 0,
) -> Tuple[str, str]:
    """纯判定：返回 (layer, reason)。"""
    age = _age(line.occurred_at, now)
    role = (line.role or "").strip().lower()
    text = line.text or ""

    # 用户（含未知角色，fail-open）行：永不因为“旧”降级。
    if role != "assistant":
        if is_current_turn(line, current_input, age, is_last_user_line, policy):
            return LAYER_CURRENT_TURN, "same-turn user line"
        if _LET_GO.search(current_input or "") and not shares_topic(text, current_input):
            return LAYER_BACKGROUND_ONLY, "user released this topic (let-go signal)"
        return LAYER_ACTIVE_THREAD, "user-authored line is never demoted"

    # 助手行。
    n_class = nudge_class(text)
    if n_class == "pressure":
        return LAYER_BACKGROUND_ONLY, "assistant pressure nudge is never topic material"
    if n_class == "care":
        if age >= policy.nudge_background_after_sec or not shares_topic(text, current_input):
            return LAYER_BACKGROUND_ONLY, "old/unrelated care nudge stays background"
        return LAYER_ACTIVE_THREAD, "fresh care line tied to the current topic"

    if shares_topic(text, current_input):
        return LAYER_ACTIVE_THREAD, "topic overlap with current input"
    if is_recent(age, pos_from_end, policy):
        return LAYER_ACTIVE_THREAD, "recent thread"
    return LAYER_BACKGROUND_ONLY, "old assistant line unrelated to current input"


def _coerce(item) -> LifelineLine:
    if isinstance(item, LifelineLine):
        return item
    if isinstance(item, dict):
        return LifelineLine(
            str(item.get("role", "assistant")),
            str(item.get("text", "")),
            float(item.get("occurred_at", 0.0) or 0.0),
        )
    role, text, ts = item  # type: ignore[misc]
    return LifelineLine(str(role), str(text), float(ts))


def classify_lines(
    lines: Sequence,
    current_input: str = "",
    now: Optional[float] = None,
    policy: Optional[LayerPolicy] = None,
) -> LayeredContext:
    """把 lifeline 行列表分成三层。输入不被改写；同输入同输出（纯函数）。"""
    policy = policy or DEFAULT_POLICY
    now = float(now) if now else time.time()
    coerced = [_coerce(item) for item in lines]
    last_user_idx = -1
    for idx, line in enumerate(coerced):
        if (line.role or "").strip().lower() != "assistant":
            last_user_idx = idx
    total = len(coerced)
    res = LayeredContext(policy=policy)
    for idx, line in enumerate(coerced):
        layer, reason = classify_one(
            line,
            current_input=current_input,
            now=now,
            policy=policy,
            is_last_user_line=(idx == last_user_idx),
            pos_from_end=total - 1 - idx,
        )
        entry = LayeredLine(line, layer, reason)
        if layer == LAYER_CURRENT_TURN:
            res.current_turn.append(entry)
        elif layer == LAYER_ACTIVE_THREAD:
            res.active_thread.append(entry)
        else:
            res.background_only.append(entry)
    return res


# ── 渲染 ────────────────────────────────────────────────────────────────────


def render_prompt_block(layered: LayeredContext) -> str:
    """渲染成 prompt 块：三层分区 + 用途限制说明。"""
    parts: List[str] = []
    sections = (
        (layered.current_turn, LAYER_CURRENT_TURN, "【当前回合】"),
        (layered.active_thread, LAYER_ACTIVE_THREAD, "【进行中话题】"),
        (layered.background_only, LAYER_BACKGROUND_ONLY, "【仅背景·语气用】"),
    )
    for entries, layer, title in sections:
        if not entries:
            continue
        parts.append(f"{title}（{LAYER_USAGE_NOTES[layer]}）")
        parts.extend(f"  - {entry.line.text}" for entry in entries)
    return "\n".join(parts)


# ── 与生产证据结构的接点（只读适配，不改生产文件） ──────────────────────────


def parse_excerpt(excerpt: str) -> Tuple[str, str]:
    """把 lifeline 摘录解析成 (role, text)。

    生产行格式：``思远（3小时前）：…`` / ``千花（72小时前）：…``。
    其余（L3 承诺 / 情绪余韵等附录）→ role="unknown"（按用户行处理，不降级）。
    """
    text = excerpt or ""
    if text.startswith("思远"):
        role = "user"
    elif text.startswith("千花"):
        role = "assistant"
    else:
        role = "unknown"
    if "：" in text:
        text = text.split("：", 1)[1]
    return role, text


#: 只有这些 status 允许被降级为 background_only（与生产口径一致）。
_ACTIONABLE_STATUSES = frozenset({"pending", "current", "completed", "uncertain"})


def apply_to_evidence(
    evidence: Sequence,
    current_input: str = "",
    now: Optional[float] = None,
    policy: Optional[LayerPolicy] = None,
    tag_background: bool = True,
    tag: str = TAG_BACKGROUND,
) -> Sequence:
    """给生产 ContextEvidence 列表打层：加 ``layer`` 属性 + 背景层降级。

    - ``ev.layer`` / ``ev.layer_reason``：分层结果（新增属性，不影响既有字段）；
    - 背景层且 status 可操作 → ``ev.status = "background_only"``；
    - ``tag_background=True`` 时给背景层摘录加前缀（excerpt-only 消费者可见）；
    - 幂等：重复调用不重复改写摘录（prompt 前缀稳定）。
    """
    policy = policy or DEFAULT_POLICY
    now = float(now) if now else time.time()
    parsed: List[LifelineLine] = []
    for ev in evidence:
        role, text = parse_excerpt(getattr(ev, "excerpt", "") or "")
        parsed.append(LifelineLine(role, text, float(getattr(ev, "occurred_at", 0.0) or 0.0)))
    last_user_idx = -1
    for idx, line in enumerate(parsed):
        if line.role != "assistant":
            last_user_idx = idx
    total = len(parsed)
    for idx, (ev, line) in enumerate(zip(evidence, parsed)):
        layer, reason = classify_one(
            line,
            current_input=current_input,
            now=now,
            policy=policy,
            is_last_user_line=(idx == last_user_idx),
            pos_from_end=total - 1 - idx,
        )
        try:
            setattr(ev, "layer", layer)
            setattr(ev, "layer_reason", reason)
        except Exception:
            pass  # frozen evidence: rendering still uses status/excerpt
        if layer != LAYER_BACKGROUND_ONLY:
            continue
        try:
            if getattr(ev, "status", "") in _ACTIONABLE_STATUSES:
                ev.status = "background_only"
            if tag_background:
                excerpt = getattr(ev, "excerpt", "") or ""
                if not excerpt.startswith(tag):
                    ev.excerpt = tag + excerpt
        except Exception:
            pass  # immutable evidence: leave as-is, caller renders
    return evidence
