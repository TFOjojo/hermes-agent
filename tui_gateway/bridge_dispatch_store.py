"""Bridge dispatch 持久回执与结果登记（G2-T11/T13）。

与 SessionDB 完全隔离：这是 broker 桥自己的账本，只服务 bridge.* 执行入口，
不读用户会话、不注入记忆。Hermes 侧的事实源通过 dispatch_id 与 Broker 对账，
不把两个进程的 SQLite 当成一个跨库原子事务（方案 §6.3）。

状态机：accepted → dispatching → running → verifying → succeeded
                                    └→ failed / outcome_unknown
       accepted → cancelled（定向取消）
       failed → accepted（retry，attempt+1）
"""
from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS bridge_dispatch (
    dispatch_id  TEXT PRIMARY KEY,
    task_id      TEXT NOT NULL,
    revision     INTEGER NOT NULL,
    principal    TEXT NOT NULL,
    policy_digest TEXT NOT NULL,
    nonce        TEXT NOT NULL,
    policy_ref   TEXT NOT NULL,
    state        TEXT NOT NULL,
    attempt      INTEGER NOT NULL DEFAULT 1,
    error_code   TEXT,
    result       TEXT,
    created_at   REAL NOT NULL,
    updated_at   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_dispatch_task ON bridge_dispatch(task_id, created_at);
CREATE INDEX IF NOT EXISTS idx_dispatch_state ON bridge_dispatch(state);
"""

_TRANSITIONS: set[tuple[str, str]] = {
    ("accepted", "dispatching"),
    ("accepted", "cancelled"),
    ("dispatching", "running"),
    ("dispatching", "cancelled"),
    ("dispatching", "accepted"),   # 回退重排队（执行端暂不可用），attempt 不变
    ("running", "verifying"),
    ("running", "failed"),
    ("running", "cancelled"),
    ("running", "outcome_unknown"),
    ("verifying", "succeeded"),
    ("verifying", "failed"),
    ("verifying", "outcome_unknown"),
    ("failed", "accepted"),        # retry
    ("outcome_unknown", "accepted"),  # 对账后可重试
}


class StoreError(Exception):
    """dispatch 账本操作失败（冲突、非法迁移、结果校验失败）。"""


@dataclass
class DispatchRecord:
    dispatch_id: str
    task_id: str
    revision: int
    principal: str
    policy_digest: str
    nonce: str
    policy_ref: dict
    state: str
    attempt: int = 1
    error_code: str | None = None
    result: dict | None = None
    created_at: float = 0.0
    updated_at: float = 0.0


@dataclass
class ResultRecord:
    dispatch_id: str
    summary: str
    confidence: str | None
    artifacts: list[dict]


class DispatchStore:
    """Broker bridge 侧持久回执账本（单文件 SQLite，单写者）。"""

    def __init__(self, path: str | Path) -> None:
        self._path = str(path)
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._conn:
            self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        self._conn.close()

    # ── 提交（先落 receipt 再入执行队列） ──────────────────────

    def record_dispatch(
        self, *, dispatch_id: str, task_id: str, revision: int, principal: str,
        policy_digest: str, nonce: str, policy_ref: dict[str, Any],
    ) -> DispatchRecord:
        existing = self.get(dispatch_id)
        if existing is not None:
            # 幂等：完全一致 → 返回原 receipt；任何一项不同 → 冲突（防换参数重放）
            self._assert_same(existing, task_id=task_id, revision=revision,
                             principal=principal, policy_digest=policy_digest,
                             nonce=nonce)
            return existing
        now = time.time()
        with self._conn:
            self._conn.execute(
                "INSERT INTO bridge_dispatch (dispatch_id, task_id, revision, principal,"
                " policy_digest, nonce, policy_ref, state, attempt, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?, 'accepted', 1, ?, ?)",
                (dispatch_id, task_id, revision, principal, policy_digest, nonce,
                 json.dumps(policy_ref, ensure_ascii=False), now, now))
        rec = self.get(dispatch_id)
        assert rec is not None
        return rec

    def _assert_same(self, rec: DispatchRecord, **fields: Any) -> None:
        for k, v in fields.items():
            if getattr(rec, k) != v:
                raise StoreError(
                    f"dispatch {rec.dispatch_id} 已被用于不同内容（{k}: {getattr(rec, k)} != {v}）；"
                    "重试必须复用同一 dispatch 与同一参数")

    # ── 查询 ──────────────────────────────────────────────────

    def get(self, dispatch_id: str) -> DispatchRecord | None:
        row = self._conn.execute(
            "SELECT * FROM bridge_dispatch WHERE dispatch_id=?",
            (dispatch_id,)).fetchone()
        return self._row_to_rec(row) if row else None

    def for_task(self, task_id: str) -> list[DispatchRecord]:
        rows = self._conn.execute(
            "SELECT * FROM bridge_dispatch WHERE task_id=? ORDER BY created_at DESC",
            (task_id,)).fetchall()
        return [self._row_to_rec(r) for r in rows]

    # ── 迁移 ──────────────────────────────────────────────────

    def transition(self, dispatch_id: str, to_state: str) -> DispatchRecord:
        with self._conn:
            row = self._conn.execute(
                "SELECT * FROM bridge_dispatch WHERE dispatch_id=?",
                (dispatch_id,)).fetchone()
            if row is None:
                raise StoreError(f"dispatch 不存在: {dispatch_id}")
            frm = row["state"]
            if (frm, to_state) not in _TRANSITIONS:
                raise StoreError(f"非法迁移 {frm} → {to_state}")
            self._conn.execute(
                "UPDATE bridge_dispatch SET state=?, updated_at=? WHERE dispatch_id=?",
                (to_state, time.time(), dispatch_id))
        rec = self.get(dispatch_id)
        assert rec is not None
        return rec

    def retry(self, dispatch_id: str) -> DispatchRecord:
        """failed/outcome_unknown → accepted，attempt+1。成功不可重试。"""
        rec = self.get(dispatch_id)
        if rec is None:
            raise StoreError(f"dispatch 不存在: {dispatch_id}")
        if rec.state not in ("failed", "outcome_unknown", "accepted"):
            raise StoreError(f"{rec.state} 不可重试（成功/在途/取消都不重放）")
        with self._conn:
            self._conn.execute(
                "UPDATE bridge_dispatch SET state='accepted', attempt=attempt+1,"
                " updated_at=?, error_code=NULL WHERE dispatch_id=?",
                (time.time(), dispatch_id))
        rec2 = self.get(dispatch_id)
        assert rec2 is not None
        return rec2

    def cancel(self, dispatch_id: str, *, reason: str) -> DispatchRecord:
        rec = self.transition(dispatch_id, "cancelled")
        with self._conn:
            self._conn.execute(
                "UPDATE bridge_dispatch SET error_code=?, updated_at=? WHERE dispatch_id=?",
                (reason, time.time(), dispatch_id))
        rec2 = self.get(dispatch_id)
        assert rec2 is not None
        return rec2

    def mark_outcome_unknown(self, dispatch_id: str, *, reason: str) -> DispatchRecord:
        self.transition(dispatch_id, "outcome_unknown")
        with self._conn:
            self._conn.execute(
                "UPDATE bridge_dispatch SET error_code=?, updated_at=? WHERE dispatch_id=?",
                (reason, time.time(), dispatch_id))
        rec = self.get(dispatch_id)
        assert rec is not None
        return rec

    # ── 结果登记（方案 §6.1：succeeded 必须经过验证） ──────────

    def register_result(
        self, dispatch_id: str, *, artifacts: list[dict], summary: str,
        confidence: str | None, force: bool = False,
    ) -> DispatchRecord:
        rec = self.get(dispatch_id)
        if rec is None:
            raise StoreError(f"dispatch 不存在: {dispatch_id}")
        if rec.state == "succeeded":
            raise StoreError("结果已登记过；重复登记拒绝（不覆盖成功事实）")
        # 校验产物：每个 artifact 必须 exists=True 且 sha256 齐全
        for art in artifacts:
            if not art.get("exists"):
                raise StoreError(
                    f"产物缺失或校验失败: {art.get('artifact_id', '?')}")
            if not isinstance(art.get("sha256"), str) or len(art["sha256"]) != 64:
                raise StoreError(
                    f"产物摘要缺失: {art.get('artifact_id', '?')}")
        if not artifacts and not force:
            # 模型自称「已完成」但没有可验证产物：不记为成功（方案 §6.1）
            self.mark_outcome_unknown(
                dispatch_id, reason="模型报告完成但无可验证产物")
            rec2 = self.get(dispatch_id)
            assert rec2 is not None
            return rec2
        if not summary.strip():
            raise StoreError("summary 不能为空")
        result = ResultRecord(
            dispatch_id=dispatch_id, summary=summary, confidence=confidence,
            artifacts=artifacts)
        # Only the verifier may seal success. A failed/unknown dispatch must go
        # through retry -> accepted -> running -> verifying first; accepting a
        # result directly from either state would let any stale/forged callback
        # upgrade a failure into success. ``force`` is intentionally limited to
        # the artifact-less outcome_unknown path above and never bypasses this
        # state gate.
        if rec.state != "verifying":
            raise StoreError(
                f"{rec.state} 状态不接受结果登记；必须先进入 verifying")
        with self._conn:
            self._conn.execute(
                "UPDATE bridge_dispatch SET state='succeeded', result=?, updated_at=?"
                " WHERE dispatch_id=?",
                (json.dumps({
                    "summary": summary, "confidence": confidence,
                    "artifacts": artifacts,
                }, ensure_ascii=False), time.time(), dispatch_id))
        rec2 = self.get(dispatch_id)
        assert rec2 is not None
        return rec2

    # ── 迁移辅助 ──────────────────────────────────────────────

    @staticmethod
    def _row_to_rec(row: sqlite3.Row) -> DispatchRecord:
        return DispatchRecord(
            dispatch_id=row["dispatch_id"], task_id=row["task_id"],
            revision=int(row["revision"]), principal=row["principal"],
            policy_digest=row["policy_digest"], nonce=row["nonce"],
            policy_ref=json.loads(row["policy_ref"]), state=row["state"],
            attempt=int(row["attempt"]), error_code=row["error_code"],
            result=json.loads(row["result"]) if row["result"] else None,
            created_at=float(row["created_at"]), updated_at=float(row["updated_at"]),
        )


def new_dispatch_id() -> str:
    return "disp_" + secrets.token_urlsafe(12).replace("-", "a").replace("_", "b")


def result_digest(summary: str, artifacts: list[dict]) -> str:
    raw = json.dumps({"summary": summary, "artifacts": artifacts},
                     sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()
