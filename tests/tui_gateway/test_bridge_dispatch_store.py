"""G2-T11/T13：dispatch 持久回执 + 结果登记（bridge 侧，与 SessionDB 隔离）。

红测先行：本文件先于 tui_gateway/bridge_dispatch_store.py 存在。

核心契约（方案 §5.4 / §5.4.1.6 / §6.3）：
- submit 先落 receipt（持久）再入执行队列；「已接单但响应丢失」不重复执行。
- receipt 绑定 dispatch_id + task + revision + policy_digest + nonce（一次性）。
- 重试只能复用同 dispatch，不得换参数用原 receipt。
- 恢复时按 task 查 receipt：无 receipt=不自动重放；receipt failed 才可重试。
- 结果登记：模型说「已完成」不算 succeeded；必须有结构化 result + 产物验证
  （artifact 存在 + 摘要一致），否则只能 failed / outcome_unknown。
- 控制面隔离：只存 bridge 自己的 dispatch 状态，不读用户会话库。
"""
from __future__ import annotations

import pytest

from tui_gateway.bridge_dispatch_store import (
    DispatchStore,
    DispatchRecord,
    ResultRecord,
    StoreError,
)


@pytest.fixture()
def store(tmp_path):
    s = DispatchStore(tmp_path / "bridge.sqlite")
    # 预置一条 receipt（最大化测试语义：msg from tests fixture）
    s.record_dispatch(
        dispatch_id="disp_001", task_id="req_test01", revision=1,
        principal="cortico:mybot", policy_digest="a" * 64, nonce="nonce-001",
        policy_ref={"tool_allowlist": ["artifact_read"]})
    yield s
    s.close()


def _submit(store, **over):
    kwargs = dict(
        dispatch_id="disp_001",
        task_id="req_test01",
        revision=1,
        principal="cortico:mybot",
        policy_digest="a" * 64,
        nonce="nonce-001",
        policy_ref={"tool_allowlist": ["artifact_read"]},
    )
    kwargs.update(over)
    return store.record_dispatch(**kwargs)


class TestDispatchReceipt:
    def test_record_and_get(self, store):
        rec = _submit(store)
        assert rec.dispatch_id == "disp_001"
        assert rec.state == "accepted"

    def test_same_dispatch_id_idempotent_same_content(self, store):
        r1 = _submit(store)
        r2 = _submit(store)  # 完全一致 → 幂等返回同一 receipt
        assert r2.state == r1.state

    def test_same_dispatch_id_different_task_conflicts(self, store):
        _submit(store)
        with pytest.raises(StoreError):
            _submit(store, task_id="req_other")

    def test_same_dispatch_id_different_policy_conflicts(self, store):
        _submit(store)
        with pytest.raises(StoreError):
            _submit(store, policy_digest="b" * 64)

    def test_same_dispatch_id_different_nonce_conflicts(self, store):
        _submit(store)
        with pytest.raises(StoreError):
            _submit(store, nonce="nonce-002")

    def test_unknown_dispatch_not_found(self, store):
        assert store.get("disp_nope") is None

    def test_persistence_across_reopen(self, tmp_path):
        s1 = DispatchStore(tmp_path / "b.sqlite")
        _submit(s1, dispatch_id="disp_p1")
        s1.close()
        s2 = DispatchStore(tmp_path / "b.sqlite")
        try:
            assert s2.get("disp_p1") is not None
            assert s2.get("disp_p1").state == "accepted"
        finally:
            s2.close()

    def test_nonpersist_receipt_state_machine(self, store):
        for st in ("dispatching", "running", "verifying"):
            store.transition("disp_001", st)
            assert store.get("disp_001").state == st
        with pytest.raises(StoreError):
            store.transition("disp_001", "accepted")  # 已受理不可回退


class TestRetrySemantics:
    def test_retry_reuses_dispatch_not_params(self, store):
        _submit(store)
        # 同一 dispatch 重试：状态可从 failed 重来
        store.transition("disp_001", "dispatching")
        store.transition("disp_001", "running")
        store.transition("disp_001", "failed")
        store.retry("disp_001")  # 重新排队
        assert store.get("disp_001").state == "accepted"
        assert store.get("disp_001").attempt == 2

    def test_succeeded_dispatch_not_retryable(self, store):
        for st in ("dispatching", "running", "verifying", "succeeded"):
            store.transition("disp_001", st)
        with pytest.raises(StoreError):
            store.retry("disp_001")  # 成功不可重试——不重复执行

    def test_cancel_dispatch_terminal(self, store):
        store.transition("disp_001", "dispatching")
        store.cancel("disp_001", reason="task cancelled")
        rec = store.get("disp_001")
        assert rec.state == "cancelled"
        assert rec.error_code == "task cancelled"


class TestResultRegistration:
    def _to_running(self, store):
        store.record_dispatch(
            dispatch_id="disp_001", task_id="req_test01", revision=1,
            principal="cortico:mybot", policy_digest="a" * 64, nonce="nonce-001",
            policy_ref={})
        for st in ("dispatching", "running", "verifying"):
            store.transition("disp_001", st)

    def test_result_requires_artifacts_and_summary(self, store):
        self._to_running(store)
        # 空摘要 + 零产物：不可能算成功。行为是转 outcome_unknown 而不是假装 succeeded
        rec = store.register_result(
            dispatch_id="disp_001", artifacts=[], summary="", confidence=None)
        assert rec.state == "outcome_unknown"
        assert rec.error_code == "模型报告完成但无可验证产物"

    def test_model_says_done_without_artifact_is_not_succeeded(self, store):
        self._to_running(store)
        # 有摘要但零产物：G2-T13 明确——模型自称完成不算 succeeded
        rec = store.register_result(
            dispatch_id="disp_001", artifacts=[], summary="分析完成",
            confidence=None, force=False)
        assert rec.state != "succeeded"

    def test_result_with_artifacts_succeeds(self, store):
        self._to_running(store)
        rec = store.register_result(
            dispatch_id="disp_001",
            artifacts=[{"artifact_id": "art_1", "sha256": "c" * 64, "exists": True}],
            summary="诊断完成：ASR 后端未起", confidence="high")
        assert rec.state == "succeeded"
        assert store.get("disp_001").result is not None

    def test_artifact_digest_mismatch_rejected(self, store):
        self._to_running(store)
        with pytest.raises(StoreError):
            store.register_result(
                dispatch_id="disp_001",
                artifacts=[{"artifact_id": "art_1", "sha256": "c" * 64, "exists": False}],
                summary="x", confidence=None)

    def test_result_persisted(self, tmp_path):
        s1 = DispatchStore(tmp_path / "r.sqlite")
        _submit(s1)
        for st in ("dispatching", "running", "verifying"):
            s1.transition("disp_001", st)
        s1.register_result(
            dispatch_id="disp_001",
            artifacts=[{"artifact_id": "art_1", "sha256": "c" * 64, "exists": True}],
            summary="done", confidence="high")
        s1.close()
        s2 = DispatchStore(tmp_path / "r.sqlite")
        try:
            rec = s2.get("disp_001")
            assert rec.state == "succeeded"
            assert rec.result["summary"] == "done"
        finally:
            s2.close()


class TestOutcomeUnknown:
    def test_cannot_determine_outcome_marks_unknown(self, store):
        store.record_dispatch(
            dispatch_id="disp_001", task_id="req_test01", revision=1,
            principal="cortico:mybot", policy_digest="a" * 64, nonce="nonce-001",
            policy_ref={})
        for st in ("dispatching", "running"):
            store.transition("disp_001", st)
        store.mark_outcome_unknown("disp_001", reason="回执丢失，无法确认")
        rec = store.get("disp_001")
        assert rec.state == "outcome_unknown"
        assert rec.error_code == "回执丢失，无法确认"


class TestTaskIsolation:
    def test_lookup_by_task_returns_latest(self, store):
        _submit(store, dispatch_id="d1", task_id="t1")
        store.transition("d1", "dispatching")
        store.transition("d1", "running")
        store.transition("d1", "verifying")
        store.transition("d1", "succeeded")
        _submit(store, dispatch_id="d2", task_id="t1")
        latest = store.for_task("t1")
        # d2 是最新 dispatch；d1 在下历史里
        assert latest[0].dispatch_id == "d2"
        assert any(r.dispatch_id == "d1" for r in latest)

    def test_result_cannot_be_registered_twice(self, store):
        store.record_dispatch(
            dispatch_id="disp_001", task_id="req_test01", revision=1,
            principal="cortico:mybot", policy_digest="a" * 64, nonce="nonce-001",
            policy_ref={})
        for st in ("dispatching", "running", "verifying", "succeeded"):
            store.transition("disp_001", st)
        with pytest.raises(StoreError):
            store.register_result(
                dispatch_id="disp_001",
                artifacts=[{"artifact_id": "art_1", "sha256": "c" * 64, "exists": True}],
                summary="again", confidence=None)
