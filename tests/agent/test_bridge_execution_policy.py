"""Bridge 受限执行策略（G2-T09）：不可变 ExecutionPolicy + 工具门。

红测先行：本文件先于 agent/bridge_execution_policy.py 存在。
核心规则（方案 §4.2 / §5.4.1）：
- 策略从 agent 构造时就绑定，不是 agent 建完再补字段。
- 精确 tool-name allowlist；不在名单里的工具一律拒。
- 参数级校验：对允许的工具逐次校验参数（这里先做形状校验）。
- 派生路径（并行/重试/子代理/后台/动态加载）不得绕过门：v1 子代理与任意后台执行全禁。
- 记忆隔离：策略默认不注入用户长期记忆与其他会话检索。
- 缺策略/摘要不匹配/版本不兼容 → 阻塞，不退回默认全工具模式。
"""
from __future__ import annotations

import time

import pytest

from agent.bridge_execution_policy import (
    BridgeExecutionPolicy,
    PolicyError,
    PolicyViolation,
)


def _policy(**over):
    kwargs = dict(
        principal="cortico:mybot",
        task_id="req_test01",
        request_revision=1,
        policy_version=1,
        tool_allowlist=("artifact_read", "artifact_write", "approved_health_probe"),
        input_artifacts=("art_1",),
        output_root="artifacts/req_test01/out",
        network_purposes=("none",),
        max_steps=20,
        max_model_calls=50,
        expires_in=3600,
        memory_injection=False,
    )
    kwargs.update(over)
    return BridgeExecutionPolicy(**kwargs)


class TestConstruction:
    def test_defaults_deny_everything_unknown(self):
        p = _policy()
        assert not p.is_tool_allowed("terminal")
        assert not p.is_tool_allowed("shell_exec")

    def test_allowlist_exact_match_only(self):
        p = _policy()
        assert p.is_tool_allowed("artifact_read")
        assert not p.is_tool_allowed("artifact_read_v2")  # 前缀不算
        assert not p.is_tool_allowed("ARTIFACT_READ")

    def test_expires_in_zero_rejected_at_construction(self):
        # expires_in<=0 表示即刻过期：构造时拒（不留"看起来有效其实已过期"的策略）
        with pytest.raises(PolicyError):
            _policy(expires_in=0)

    def test_revision_must_be_positive(self):
        with pytest.raises(PolicyError):
            _policy(request_revision=0)

    def test_network_purposes_validated(self):
        with pytest.raises(PolicyError):
            _policy(network_purposes=("ssrf-target",))


class TestGuard:
    def test_allowed_tool_passes(self):
        p = _policy()
        p.check_tool_call("artifact_read", {"artifact_id": "art_1"})

    def test_disallowed_tool_raises(self):
        p = _policy()
        with pytest.raises(PolicyViolation) as exc:
            p.check_tool_call("browser_navigate", {"url": "https://x"})
        assert "browser_navigate" in str(exc.value)

    def test_dangerous_tools_always_denied_regardless_of_allowlist(self):
        # 即使有人把危险的加进 allowlist，也只是 allowlist 校验；策略自身再禁一层
        p = _policy(tool_allowlist=("artifact_read", "delegate", "cronjob_manage"))
        assert p.is_tool_allowed("delegate") is True  # allowlist 层没拦
        with pytest.raises(PolicyViolation):
            p.check_tool_call("delegate", {"tasks": []})  # 策略层禁派生执行

    def test_args_shape_validated(self):
        p = _policy()
        with pytest.raises(PolicyViolation):
            p.check_tool_call("artifact_read", "not-a-dict")

    def test_step_budget_enforced(self):
        p = _policy(max_steps=2)
        p.consume_step()
        p.consume_step()
        with pytest.raises(PolicyViolation):
            p.consume_step()

    def test_expiry_enforced(self):
        p = _policy(expires_in=1)  # 1 秒有效窗口
        # 手工把绝对到期时间拨到过去，模拟"过期后来调用"
        object.__setattr__(p, "expires_at", time.time() - 1)
        with pytest.raises(PolicyViolation):
            p.check_tool_call("artifact_read", {"artifact_id": "art_1"})

    def test_memory_injection_defaults_off(self):
        p = _policy()
        assert p.memory_injection is False

    def test_subagents_disabled_in_v1(self):
        p = _policy()
        assert p.allow_subagents is False

    def test_background_execution_disabled_in_v1(self):
        p = _policy()
        assert p.allow_background is False


class TestPolicyIntegrity:
    def test_digest_stable_for_same_content(self):
        a, b = _policy(), _policy()
        assert a.digest() == b.digest()

    def test_digest_changes_with_task(self):
        a = _policy()
        b = _policy(task_id="req_other")
        assert a.digest() != b.digest()

    def test_from_dispatch_rejects_unknown_policy_digest(self):
        # Broker 不可自造 policy：摘要对不上直接拒
        with pytest.raises(PolicyError):
            BridgeExecutionPolicy.from_dispatch(
                dispatch_id="d1", task_id="req_test01", revision=1,
                policy_ref={"policy_version": 1, "digest": "deadbeef"},
                known_digest="cafebabe",
            )

    def test_from_dispatch_restores_fields(self):
        base = _policy()
        ref = base.to_ref()
        p = BridgeExecutionPolicy.from_dispatch(
            dispatch_id="d1", task_id="req_test01", revision=1,
            policy_ref=ref, known_digest=base.digest())
        assert p.principal == "cortico:mybot"
        assert p.is_tool_allowed("artifact_read")
        assert p.digest() == ref["digest"]
        assert p.digest() == base.digest()

    def test_from_dispatch_rejects_revision_mismatch(self):
        # 同一策略快照不能换 revision 恢复（digest 里含 revision）
        base = _policy()
        ref = base.to_ref()
        with pytest.raises(PolicyError):
            BridgeExecutionPolicy.from_dispatch(
                dispatch_id="d1", task_id="req_test01", revision=99,
                policy_ref=ref, known_digest=base.digest())

    def test_from_dispatch_rejects_task_swap(self):
        # 拿 A 任务的策略恢复 B 任务：digest 校验必拒（防跨任务盗用）
        base = _policy()
        ref = base.to_ref()
        with pytest.raises(PolicyError):
            BridgeExecutionPolicy.from_dispatch(
                dispatch_id="d1", task_id="req_other", revision=1,
                policy_ref=ref, known_digest=base.digest())


class TestAuditTrail:
    def test_violation_recorded(self):
        p = _policy()
        with pytest.raises(PolicyViolation):
            p.check_tool_call("browser_navigate", {})
        assert len(p.violations) == 1
        assert p.violations[0]["tool"] == "browser_navigate"

    def test_tool_call_audit_recorded(self):
        p = _policy()
        p.check_tool_call("artifact_read", {"artifact_id": "art_1"})
        assert any(e.get("tool") == "artifact_read" for e in p.audit)
