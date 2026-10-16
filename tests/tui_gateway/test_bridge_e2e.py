"""G2 跨模块 e2e：dispatch 受理 → 策略执行 → 结果登记 → 定向取消。

验收口径（方案 §10 G2）：
- 权限逃逸测试全部通过：
  * bridge 只能调 bridge.* 方法
  * 工具层：delegate/shell_exec 即使误进 allowlist 也被策略层拦
  * 客户端伪造 principal 无效
  * policy digest 不匹配拒绝恢复
  * 跨任务盗用策略拒绝
- 真实只读调用路径可执行：
  * record_dispatch → policy.check_tool_call(artifact_read) → register_result
  * succeeded 必须有可验证产物；零产物 → outcome_unknown
- 定向取消：只停目标 dispatch，其他任务状态不变
"""
from __future__ import annotations

from dataclasses import dataclass

import pytest

from agent.bridge_execution_policy import BridgeExecutionPolicy, PolicyViolation
from tui_gateway.bridge_dispatch_store import DispatchStore, StoreError
from tui_gateway.methods_bridge import (
    BridgeAuthError,
    BridgeCredentialStore,
    is_bridge_method,
    method_task_cancel,
    method_task_submit,
    resolve_bridge_principal,
)


@pytest.fixture()
def world():
    creds = BridgeCredentialStore()
    token = creds.issue(scope="bridge.execute", principal="broker:local")
    store = DispatchStore.__new__(DispatchStore)  # 用 tmp 文件由 fixture 建
    import tempfile, pathlib
    tmp = pathlib.Path(tempfile.mkdtemp()) / "e2e.sqlite"
    store = DispatchStore(tmp)
    policy = BridgeExecutionPolicy(
        principal="cortico:mybot", task_id="req_e2e01", request_revision=1,
        policy_version=1, tool_allowlist=("artifact_read",),
        input_artifacts=("art_1",), output_root="artifacts/req_e2e01/out",
        network_purposes=("none",), max_steps=10, max_model_calls=20,
        expires_in=600)
    return {"creds": creds, "token": token, "store": store, "policy": policy}


class TestFullLifecycle:
    def test_submit_execute_result(self, world):
        store, policy = world["store"], world["policy"]
        ref = policy.to_ref()
        # Broker 提交 dispatch
        r = method_task_submit(
            _cred(world), {
                "dispatch_id": "disp_e2e1", "task_id": "req_e2e01", "revision": 1,
                "policy_digest": ref["digest"], "nonce": "n1", "policy_ref": ref,
            }, store=store)
        assert r["ok"] and r["state"] == "accepted"

        # 执行路径：从 dispatch 恢复策略 → 门检查 → 记账
        rec = store.get("disp_e2e1")
        p2 = BridgeExecutionPolicy.from_dispatch(
            dispatch_id="disp_e2e1", task_id=rec.task_id, revision=rec.revision,
            policy_ref=rec.policy_ref, known_digest=rec.policy_digest)
        assert p2.digest() == ref["digest"]
        p2.check_tool_call("artifact_read", {"artifact_id": "art_1"})
        p2.consume_step()

        # 登记结果：必须带可验证产物
        store.transition("disp_e2e1", "dispatching")
        store.transition("disp_e2e1", "running")
        store.transition("disp_e2e1", "verifying")
        store.register_result(
            "disp_e2e1",
            artifacts=[{"artifact_id": "art_out1", "sha256": "d" * 64, "exists": True}],
            summary="发现 ASR 后端冷启动竞态", confidence="high")
        assert store.get("disp_e2e1").state == "succeeded"

    def test_model_claim_without_artifact_is_unknown(self, world):
        store, policy = world["store"], world["policy"]
        ref = policy.to_ref()
        method_task_submit(
            _cred(world), {
                "dispatch_id": "disp_e2e2", "task_id": "req_e2e01", "revision": 1,
                "policy_digest": ref["digest"], "nonce": "n2", "policy_ref": ref,
            }, store=store)
        store.transition("disp_e2e2", "dispatching")
        store.transition("disp_e2e2", "running")
        store.transition("disp_e2e2", "verifying")
        store.register_result("disp_e2e2", artifacts=[], summary="已完成", confidence=None)
        assert store.get("disp_e2e2").state == "outcome_unknown"


class TestPrivilegeEscape:
    def test_wrong_credential_shape_rejected(self, world):
        from tui_gateway.methods_bridge import method_task_get, method_task_submit
        # 先建一条真实 dispatch，再拿错形状的 cred 调 get，必须被拒
        ref = world["policy"].to_ref()
        method_task_submit(
            _cred(world), {
                "dispatch_id": "disp_shape", "task_id": "req_e2e01", "revision": 1,
                "policy_digest": ref["digest"], "nonce": "nshape", "policy_ref": ref,
            }, store=world["store"])
        with pytest.raises(AttributeError):
            # dict 而不是 BridgeCredential：方法体读 cred.principal 时炸
            method_task_get(
                {"principal": "x", "scope": "bridge.execute", "id": "y"},
                {"dispatch_id": "disp_shape"}, store=world["store"])

    def test_unknown_dispatch_returns_not_found(self, world):
        from tui_gateway.methods_bridge import method_task_get
        r = method_task_get(
            _cred(world), {"dispatch_id": "disp_nothing"}, store=world["store"])
        assert r["ok"] is False
        assert r["error"]["code"] == "not_found"

    def test_client_forged_principal_ignored(self, world):
        # params.principal 无效——身份来自 credential
        p = resolve_bridge_principal(
            world["creds"], credential=world["token"],
            params={"principal": "cortico:attacker"})
        assert p == "broker:local"

    def test_dangerous_methods_unreachable(self):
        for m in ("shell.exec", "prompt.submit", "session.resume", "delegate"):
            assert not is_bridge_method(m)

    def test_derivative_tool_blocked_even_if_allowlisted(self, world):
        p = BridgeExecutionPolicy(
            principal="cortico:mybot", task_id="req_e2e01", request_revision=1,
            policy_version=1,
            tool_allowlist=("artifact_read", "delegate", "shell_exec"),
            input_artifacts=(), output_root="o", network_purposes=("none",),
            max_steps=5, max_model_calls=5, expires_in=60)
        assert p.is_tool_allowed("delegate")  # allowlist 层不拦（说明测试有效）
        with pytest.raises(PolicyViolation):
            p.check_tool_call("delegate", {})

    def test_policy_digest_mismatch_blocks_restore(self, world):
        store, policy = world["store"], world["policy"]
        ref = policy.to_ref()
        method_task_submit(
            _cred(world), {
                "dispatch_id": "disp_e2e3", "task_id": "req_e2e01", "revision": 1,
                "policy_digest": ref["digest"], "nonce": "n3", "policy_ref": ref,
            }, store=store)
        rec = store.get("disp_e2e3")
        with pytest.raises(Exception):
            BridgeExecutionPolicy.from_dispatch(
                dispatch_id="disp_e2e3", task_id=rec.task_id, revision=rec.revision,
                policy_ref=rec.policy_ref, known_digest="f" * 64)

    def test_cross_task_policy_theft_blocked(self, world):
        policy = world["policy"]
        with pytest.raises(Exception):
            BridgeExecutionPolicy.from_dispatch(
                dispatch_id="disp_x", task_id="req_VICTIM", revision=1,
                policy_ref=policy.to_ref(), known_digest=policy.digest())


class TestTargetedCancel:
    def test_cancel_only_target_dispatch(self, world):
        store = world["store"]
        ref = world["policy"].to_ref()
        # 两个 dispatch
        for did, nonce in (("disp_c1", "nc1"), ("disp_c2", "nc2")):
            method_task_submit(
                _cred(world), {
                    "dispatch_id": did, "task_id": "req_e2e01", "revision": 1,
                    "policy_digest": ref["digest"], "nonce": nonce,
                    "policy_ref": ref,
                }, store=store)
        store.transition("disp_c1", "dispatching")
        store.transition("disp_c2", "dispatching")
        # 只取消 c1
        r = method_task_cancel(
            _cred(world), {"dispatch_id": "disp_c1", "reason": "operator stop"},
            store=store)
        assert r["ok"] and r["cancelled"]
        assert store.get("disp_c1").state == "cancelled"
        # c2 毫发无损
        assert store.get("disp_c2").state == "dispatching"

    def test_succeeded_dispatch_refuses_cancel(self, world):
        store = world["store"]
        ref = world["policy"].to_ref()
        method_task_submit(
            _cred(world), {
                "dispatch_id": "disp_c3", "task_id": "req_e2e01", "revision": 1,
                "policy_digest": ref["digest"], "nonce": "nc3", "policy_ref": ref,
            }, store=store)
        for st in ("dispatching", "running", "verifying", "succeeded"):
            store.transition("disp_c3", st)
        # succeeded 不可取消（合法迁移里没有 succeeded→cancelled）
        with pytest.raises(StoreError):
            store.transition("disp_c3", "cancelled")


class TestIdempotentReplay:
    def test_same_dispatch_replay_returns_same_receipt(self, world):
        store, policy = world["store"], world["policy"]
        ref = policy.to_ref()
        params = {
            "dispatch_id": "disp_idem", "task_id": "req_e2e01", "revision": 1,
            "policy_digest": ref["digest"], "nonce": "nidem", "policy_ref": ref,
        }
        r1 = method_task_submit(_cred(world), params, store=store)
        r2 = method_task_submit(_cred(world), params, store=store)
        assert r1 == r2

    def test_same_dispatch_different_params_conflict(self, world):
        store, policy = world["store"], world["policy"]
        ref = policy.to_ref()
        base = {
            "dispatch_id": "disp_idem2", "task_id": "req_e2e01", "revision": 1,
            "policy_digest": ref["digest"], "nonce": "nidem2", "policy_ref": ref,
        }
        method_task_submit(_cred(world), dict(base), store=store)
        # 同 dispatch 换 task：冲突
        r = method_task_submit(
            _cred(world), dict(base, task_id="req_other"), store=store)
        assert r["ok"] is False


def _cred(world):
    """从 fixture world 构造已认证 credential 视图（方法体只读 principal/scope/id）。"""
    return _FakeCred(
        id="bcred_test", scope="bridge.execute",
        principal=resolve_bridge_principal(
            world["creds"], credential=world["token"], params={}))


@dataclass(frozen=True)
class _FakeCred:
    """测试用 credential 视图：与方法体期望的 BridgeCredential 同形。"""
    id: str
    scope: str
    principal: str
