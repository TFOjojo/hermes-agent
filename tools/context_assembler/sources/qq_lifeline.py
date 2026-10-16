#!/usr/bin/env python3
"""QQ relationship-lifeline source adapter: bounded user-visible chat window.

Extracts the logic formerly hardcoded in ``scripts/qq_lifeline.py`` /
``scripts/qq_context_snippet.py`` into a reusable, principal-parameterised
module.  The chat id now comes from ``PrincipalContext.chat_id``, never from
a module-level constant.
"""

from __future__ import annotations

import re
import sqlite3
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from tools.context_assembler.models import ContextEvidence, ContextRequest, PrincipalContext
from tools.context_assembler.relationship_policy import classify_message

# ── Constants formerly hardcoded in the QQ scripts ────────────────────────

# Default home for the Hermes state DB; overridable for tests.
DEFAULT_DB = Path(r"D:/Program Files (x86)/AI/hermes/state.db")

# The canonical chat id for the QQ girlfriend channel is NO LONGER used as a
# read fallback: an empty principal.chat_id degrades the read (returns no
# evidence with an explicit reason) instead of silently reading a hardcoded
# chat.  Fail closed, not fail wide.
FALLBACK_CHAT_ID = ""

NOISE = re.compile(
    r"(SENT_OK|已完成修复|修复文件|重启后台|更新提醒|监控基线|scope-recall|active model|"
    r"runtime metadata|DSH 工作间|Gateway interruption|config_drift|模型：/|\[DSH\]|【DSH|"
    r"\[Codex\]|\[Grok\]|任务失败通知|补丁告警|BLOCKED|READY|Cron failed|看板流程|额度|周额度)",
    re.I,
)
OPS_DUMP = re.compile(
    r"(DeepSeek Harness|qianhua-core|database is locked|provider=|model: /|任务失败通知|"
    r"补丁告警|\[DSH\]|【DSH|\[Codex\]|\[Grok\])",
    re.I,
)
ASSISTANT_OPS = re.compile(
    r"(DSH|Hermes|发送口|脚本|补丁|cron|重启|占位|进程|源码|看板|告警|任务|端口|模型|worker|路径|文件)",
    re.I,
)
PROMISE = re.compile(r"(等我|等你|下次|之后|晚点|有空|记得|还没|没下完|继续|重赛|一起|陪我|想和你)")
DONE = re.compile(r"(已经|已經|完成了|做完了|吃过了|吃過了|看过了|看過了|洗完了|回来了|回來了|结束了|結束了|解决了|解決了|收到)")
UNCERTAIN = re.compile(r"(好像|似乎|可能|应该|應該|记不清|記不清|不确定|不確定|改到|再看看)")
QUIET = re.compile(r"(睡了|晚安|洗澡|安静|安靜|不想说|不想說|累死|好累|别分析|別分析|先不聊)")
BUSY = re.compile(r"(正在改|先忙|手头忙|工作完|临时任务|别吵|别烦|出大问题|在改)", re.I)
AFFECTION = re.compile(r"(想你|想妳|想我|宝贝|寶貝|亲|親|抱|😘|❤️|爱你|愛你)")
DISMISSIVE = re.compile(r"^(嗯|嗯嗯|哦|哦哦|好|好的|行|收到|哈哈|哈哈哈)[!！。…~～]*$")
QUESTION = re.compile(r"(吃饭了吗|在干嘛|累不累|还好吗|忙完了吗)")
ENDING = re.compile(r"(记得休息|记得吃饭|我在这里等你|晚安|晚上见)")
EPHEMERAL = re.compile(
    r"(刚吃|剛吃|吃完饭|吃完飯|午饭|午飯|晚饭|晚飯|早饭|早飯|剩几粒|胃有点|消食)"
)
PLANNING = re.compile(
    r"(他应该到家|她应该到家|可以续上一点|不要重复说想你|想到可以聊聊|"
    r"按本次相处|只输出消息正文|相处建议|已完成/仅背景|7天外旧话题|"
    r"不要输出内心|第三人称)"
)
EPHEMERAL_TTL_SEC = 3 * 60 * 60

# Plan B: user control signals (cancel/quiet) that must survive NOISE filter.
CONTROL_SIGNAL = re.compile(r"(不要再|别再|不用再|不需要再|取消|安静|暂停|别催|太吵|少发|别发了|关掉)")


# ── Helpers ───────────────────────────────────────────────────────────────


def clean(text: str) -> str:
    """Clean a raw message for lifeline display: strip control/format junk."""
    if not text:
        return ""
    if "\x00json" in text or "data:image/" in text:
        return ""
    text = re.sub(r"\[Image attached.*?\]", " ", text, flags=re.I | re.S)
    text = re.sub("[\x00-\x1f]+", " ", text)
    return " ".join(text.split()).strip()


def age_label(occurred_at: float, now: Optional[float] = None) -> str:
    """Human age for a lifeline timestamp."""
    now_ts = time.time() if now is None else now
    age = max(0.0, now_ts - (occurred_at or 0.0))
    if age < 90:
        return "刚刚"
    if age < 3600:
        return f"{int(age // 60)}分钟前"
    if age < 86400:
        return f"{int(age // 3600)}小时前"
    return f"{int(age // 86400)}天前"


def daypart(hour: int) -> str:
    if 5 <= hour < 11:
        return "早上"
    if 11 <= hour < 14:
        return "中午"
    if 14 <= hour < 17:
        return "下午"
    if 17 <= hour < 20:
        return "傍晚"
    if 20 <= hour < 23:
        return "晚上"
    return "深夜"


def apply_ephemeral_status(
    text: str,
    status: str,
    occurred_at: float,
    now: Optional[float] = None,
) -> str:
    """Meal/just-ate facts expire in hours, not days."""
    if not EPHEMERAL.search(text or ""):
        return status
    now_ts = time.time() if now is None else now
    age = now_ts - (occurred_at or 0.0)
    if age >= EPHEMERAL_TTL_SEC:
        return "stale"
    return "completed"


def is_planning_draft(text: str) -> bool:
    """True when a generated line is internal planning, not a sendable message."""
    return bool(text and PLANNING.search(text))


def select_outbound_text(raw: str) -> str:
    """Keep the last non-planning paragraph from a generated reply."""
    if not raw:
        return ""
    chunks = [p.strip() for p in re.split(r"\n\s*\n", raw) if p.strip()]
    if len(chunks) <= 1:
        chunks = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    good = [c for c in chunks if c and not is_planning_draft(c)]
    return good[-1] if good else ""


def _resolve_chat_id(principal: PrincipalContext) -> Optional[str]:
    """The chat id comes ONLY from the principal — no constant fallback.

    Returns ``None`` when the principal carries no usable chat_id; callers
    must treat that as a degraded read (no evidence), never as "read the
    default chat".
    """
    chat_id = (principal.chat_id or "").strip()
    return chat_id or None


def _open_db(db_path: Optional[Path] = None) -> sqlite3.Connection:
    path = db_path or DEFAULT_DB
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=3)
    conn.row_factory = sqlite3.Row
    return conn


# ── Lifetime evidence read (the old qq_lifeline.py logic) ─────────────────


def read_qq_lifeline(
    principal: PrincipalContext,
    request: ContextRequest,
    db_path: Optional[Path] = None,
    db=None,
) -> Tuple[List[ContextEvidence], List[Dict[str, str]]]:
    """Return bounded, user-visible QQ conversation lines as evidence.

    Filters tool/ops noise, deduplicates, applies the 7-day window, and
    classifies pending/completed/uncertain.
    """
    degraded: List[Dict[str, str]] = []
    chat_id = _resolve_chat_id(principal)
    if not chat_id:
        # Fail closed: without a principal-bound chat_id we cannot scope the
        # read, so return no evidence rather than falling back to any
        # hardcoded chat.
        degraded.append(
            {
                "source": "relationship_lifeline",
                "reason": "principal has no chat_id; refusing to read QQ lifeline",
            }
        )
        return [], degraded
    # Over-fetch a bounded window so the assembler can apply offset/max_items.
    # Readers must not pre-slice to one page or has_more can never be true.
    try:
        page_size = max(2, int(request.max_items or 10))
        page_offset = max(0, int(request.offset or 0))
    except (TypeError, ValueError):
        page_size, page_offset = 10, 0
    fetch_limit = min(max(page_size + page_offset + 1, 8), 40)
    cutoff = time.time() - 7 * 24 * 60 * 60

    try:
        resolved_path = db_path
        if resolved_path is None and db is not None:
            injected = getattr(db, "db_path", None)
            if injected is not None:
                resolved_path = Path(injected)
        conn = _open_db(resolved_path)
        try:
            rows = conn.execute(
                "SELECT m.role, m.content, m.timestamp FROM messages m "
                "JOIN sessions s ON s.id=m.session_id "
                "WHERE s.source='qqbot' AND s.chat_id=? AND m.role IN ('user','assistant') "
                "AND (s.archived IS NULL OR s.archived=0) AND s.ended_at IS NULL "
                "AND COALESCE(m.timestamp, 0) >= ? ORDER BY m.id DESC LIMIT ?",
                (chat_id, cutoff, fetch_limit * 4),
            ).fetchall()
        except sqlite3.Error as exc:
            degraded.append({"source": "relationship_lifeline", "reason": f"sqlite error: {exc}"})
            return [], degraded
        finally:
            conn.close()
    except (sqlite3.Error, OSError) as exc:
        degraded.append({"source": "relationship_lifeline", "reason": f"open failed: {exc}"})
        return [], degraded

    lines: List[tuple] = []
    seen: set = set()
    for row in reversed(rows):
        text = clean(row["content"] or "")
        if not text or NOISE.search(text):
            continue
        if row["role"] != "user" and (
            OPS_DUMP.search(text) or ASSISTANT_OPS.search(text) or PLANNING.search(text)
        ):
            continue
        # Quiet/cancel control signals must survive the ops-noise filter:
        # extract them from the raw user line BEFORE dropping it.
        if row["role"] == "user" and CONTROL_SIGNAL.search(text):
            item = classify_message(
                "user", text, row["timestamp"] or time.time(), time.time()
            )
            if item.get("kind") in ("preference_cancel", "preference"):
                lines.append((row["role"], text, row["timestamp"] or time.time()))
                continue
        key = (row["role"], text)
        if key in seen:
            continue
        seen.add(key)
        if len(text) > 260:
            text = text[:260].rstrip() + "..."
        lines.append((row["role"], text, row["timestamp"] or time.time()))

    recent = lines[-fetch_limit:]
    now = time.time()
    evidence: List[ContextEvidence] = []
    PASSTHROUGH_STATUSES = ("pending", "completed", "uncertain", "current", "stale")

    for role, text, ts in recent:
        # Plan B: classify via the relationship policy (role/negation/
        # request/TTL aware) instead of the old single-keyword statuses.
        # Domain statuses (background/cancelled) map onto the generic
        # vocabulary: both mean "never actionable, background only".
        try:
            item = classify_message(role, text, ts, now)
            status = item["status"]
            if status == "background":
                status = "current"
            elif status == "cancelled":
                status = "uncertain"
            if status not in PASSTHROUGH_STATUSES:
                status = "current"
        except Exception:
            status = "current"
        who = "思远" if role == "user" else "千花"
        evidence.append(
            ContextEvidence(
                source_kind="qq_lifeline",
                source_id=chat_id,
                platform="qqbot",
                occurred_at=ts,
                excerpt=f"{who}（{age_label(ts, now)}）：{text}",
                status=status,
                confidence=0.9 if role == "user" else 0.8,
                sensitivity="normal",
                authorized=True,
                owner_user_id=(principal.canonical_user_id or "").strip() or None,
            )
        )
    # L3 relationship ledger appendix (T2): commitments + emotion residue.
    # Fail-open: an empty/missing table yields no extra evidence; L2 output
    # above is unchanged.
    try:
        cmts = read_l3_commitments(chat_id, db_path=resolved_path, now=now)
    except Exception:
        cmts = []
    try:
        emos = read_l3_emotions(chat_id, db_path=resolved_path, now=now)
    except Exception:
        emos = []
    if cmts:
        n_jojo = sum(1 for c in cmts if c["maker"] == "jojo")
        n_chika = len(cmts) - n_jojo
        lines_l3 = [
            "  - [%s]%s %s（%s，%s）" % (
                c["status"], "（已过期）" if c["overdue"] else "", c["content"], c["age"], c["maker"],
            )
            for c in cmts
        ]
        # Plan B: the appendix represents ORIGINAL proposal time, not the read
        # time — otherwise every assembly refreshes stale commitments.
        oldest = min(
            (c.get("proposed_ts") for c in cmts if c.get("proposed_ts")), default=now
        )
        evidence.append(
            ContextEvidence(
                source_kind="qq_lifeline",
                source_id=chat_id,
                platform="qqbot",
                occurred_at=oldest,
                excerpt="未完承诺（jojo %d / chika %d）：" % (n_jojo, n_chika) + chr(10) + chr(10).join(lines_l3),
                status="current",
                confidence=0.95,
                sensitivity="normal",
                authorized=True,
                owner_user_id=(principal.canonical_user_id or "").strip() or None,
            )
        )
    if emos:
        lines_e = [
            "  - [%s] %s：%s（%s%s）" % (
                e["status"], e["kind"], e["trigger"], e["age"],
                "，" + e["residue"] if e["residue"] else "",
            )
            for e in emos
        ]
        evidence.append(
            ContextEvidence(
                source_kind="qq_lifeline",
                source_id=chat_id,
                platform="qqbot",
                occurred_at=now,
                excerpt="情绪余韵:" + chr(10) + chr(10).join(lines_e),
                status="current",
                confidence=0.9,
                sensitivity="normal",
                authorized=True,
                owner_user_id=(principal.canonical_user_id or "").strip() or None,
            )
        )
    # Plan 2026-09-19 §4-B: old assistant nudges become background-only
    # before reaching the prompt envelope (current-turn and user lines stay
    # actionable; the outbound guard remains the reply-side safety net).
    try:
        from tools.context_assembler.sources.qq_topic_layering import apply_to_evidence

        current_input = (getattr(request, "query", "") or "").strip()
        evidence = apply_to_evidence(evidence, current_input=current_input, now=now)
    except Exception:  # noqa: BLE001 - layering must never break the chat path
        evidence = background_only_evidence(evidence)
    return evidence, degraded


# ── L3 relationship ledger (commitments + emotion episodes) ────────────────
# Spec: .hermes/plans/qq-chika-l3-relationship/task_plan.md
# Tables created non-destructively by create_l3_tables.py (T1). Reads are
# fail-open: any sqlite error degrades to an empty list, never breaks L2.

L3_MAX_ITEMS = 5


def _age_short(occurred_at: float, now: float) -> str:
    age = max(0.0, now - (occurred_at or 0.0))
    if age < 3600:
        return "刚提出"
    if age < 86400:
        return f"{int(age // 3600)}小时前"
    return f"{int(age // 86400)}天前"


def read_l3_commitments(chat_id: str, db_path=None, now=None) -> List[Dict]:
    """Active commitments (proposed/confirmed/deferred) for one chat."""
    now = time.time() if now is None else now
    try:
        conn = _open_db(db_path)
        try:
            rows = conn.execute(
                "SELECT content, maker, status, proposed_at, due_at FROM relationship_commitments "
                "WHERE chat_id=? AND status IN ('proposed','confirmed','deferred') "
                "ORDER BY updated_at DESC LIMIT ?",
                (chat_id, L3_MAX_ITEMS),
            ).fetchall()
        finally:
            conn.close()
    except Exception:
        return []
    return [
        {
            "content": r["content"],
            "maker": r["maker"],
            "status": r["status"],
            "age": _age_short(r["proposed_at"] or now, now),
            "overdue": bool(r["due_at"] and r["due_at"] < now and r["status"] == "confirmed"),
            "proposed_ts": float(r["proposed_at"] or now),
        }
        for r in rows
    ]


def read_l3_emotions(chat_id: str, db_path=None, now=None) -> List[Dict]:
    """Unresolved emotion episodes (open/lingering) for one chat."""
    now = time.time() if now is None else now
    try:
        conn = _open_db(db_path)
        try:
            rows = conn.execute(
                "SELECT kind, trigger_summary, status, opened_at, residue_hint FROM relationship_emotions "
                "WHERE chat_id=? AND status IN ('open','lingering') "
                "ORDER BY opened_at DESC LIMIT ?",
                (chat_id, L3_MAX_ITEMS),
            ).fetchall()
        finally:
            conn.close()
    except Exception:
        return []
    return [
        {
            "kind": r["kind"],
            "trigger": r["trigger_summary"],
            "status": r["status"],
            "age": _age_short(r["opened_at"] or now, now),
            "residue": r["residue_hint"] or "",
        }
        for r in rows
    ]





# Statuses (strings — evidence.status is a plain str field on ContextEvidence).
CURRENT_TURN = "current"
BACKGROUND_ONLY = "background_only"

# Old assistant nudges older than this become background-only.
BACKGROUND_AFTER_SEC = 2 * 3600  # 2h: this turn's context stays actionable

_NUDGE = re.compile(
    r"(qq\.exe|chrome\.exe|已经?\s*\d+\s*小时|连续\s*\d+\s*小时|吱一声|"
    r"喝水|伸懒腰|眨眨眼|看看窗外|眼睛要抗议|不许报仇|不许报复|烤串|"
    r"午饭点|晚饭点|别空着肚子|我还没等到|你还没回我|记得休息|我在这里等你)",
    re.I,
)


def is_old_nudge_topic(text: str) -> bool:
    """True when the excerpt carries a banned re-introduction topic."""
    return bool(text and _NUDGE.search(text))


def background_only_evidence(evidence, now: float = None) -> List:
    """Return the evidence list with old assistant nudges downgraded.

    Downgrade rule (all must hold):
      1. the excerpt matches a nudge pattern;
      2. it is assistant-authored (excerpt starts with 千花, not 思远);
      3. age >= BACKGROUND_AFTER_SEC;
      4. its current status is one of the actionable passthroughs.
    User-authored lines are NEVER downgraded here: what the user said is
    his own context, and the outbound guard handles reply-side filtering.
    """
    now = time.time() if now is None else now
    out = []
    for ev in evidence:
        try:
            text = getattr(ev, "excerpt", "") or ""
            status = getattr(ev, "status", "") or ""
            occurred = float(getattr(ev, "occurred_at", 0) or 0)
        except Exception:
            out.append(ev)
            continue
        if (
            status in ("pending", "current", "completed", "uncertain")
            and is_old_nudge_topic(text)
            and not text.startswith("思远")
            and (now - occurred) >= BACKGROUND_AFTER_SEC
        ):
            try:
                ev.status = BACKGROUND_ONLY
            except Exception:
                pass  # frozen/immutable evidence: leave as-is, caller renders
        out.append(ev)
    return out


# ── Relationship-mode classification (the old qq_lifeline mode logic) ─────


def classify_mode(evidence: List[ContextEvidence]) -> str:
    """Choose the single interaction mode for proactive QQ messages.

    Mirrors the ordering in the original qq_lifeline.py: busy > quiet >
    dismissive > pending > affection > default.
    """
    if not evidence:
        return "表达自己"
    latest_user = next(
        (e.excerpt.split("：", 1)[1] if "：" in e.excerpt else e.excerpt
         for e in reversed(evidence) if e.excerpt.startswith("思远")),
        "",
    )
    if BUSY.search(latest_user):
        return "忙时静默：不要主动发消息，不要问忙完没，不要继续邀约。"
    if QUIET.search(latest_user):
        return "安静陪伴：短一点，不盘问，不强行发起新任务。"
    if AFFECTION.search(latest_user):
        return "靠近回应：先回应亲密关系本身，再决定是否带一个自己的念头。"
    if any(e.status == "pending" and e.excerpt.startswith("思远") for e in evidence):
        return "续话题：从未完/待续中只选一项自然接上，不重复整段历史。"
    if DISMISSIVE.search(latest_user):
        # Plan §3.2: a neutral short reply is not dissatisfaction. Do not
        # manufacture a repair obligation from "嗯/好/收到".
        return "中性短回应：正常接话即可，不追问、不附加解读。"
    return "表达自己：分享一个真实的小想法或小决定，不把消息写成问候打卡。"


def anti_template_check(evidence: List[ContextEvidence]) -> Tuple[int, int]:
    """Count fixed questions / fixed endings in recent assistant lines."""
    assistant_lines = [
        e.excerpt.split("：", 1)[1] if "：" in e.excerpt else e.excerpt
        for e in evidence
        if e.excerpt.startswith("千花")
    ]
    repeated_questions = sum(1 for t in assistant_lines if QUESTION.search(t))
    repeated_endings = sum(1 for t in assistant_lines if ENDING.search(t))
    return repeated_questions, repeated_endings