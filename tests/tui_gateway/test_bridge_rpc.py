"""G2-T08：Hermes bridge.* RPC 入口（来源鉴权 + 方法白名单 + 服务端盖章）。

红测先行：本文件先于 tui_gateway/methods_bridge.py 存在。

核心契约（方案 §5.4 / §5.4.1）：
1. **方法白名单**：bridge 连接只能调 bridge.* 五个方法；其他方法一律拒。
   覆盖同步派发与线程池派发两条路径。
2. **响应帧归属门**：带 server request 语义的帧不能被 bridge 代答人类审批。
3. **来源服务端盖章**：principal/task/revision/policy 从认证与持久 receipt 推导，
   客户端 JSON 字段不能改写身份（不伪造 _turn_author）。
4. **身份不是 loopback**：错 token / 错 scope 一律拒。
5. **capabilities 诚实**：缺能力返回「不支持」而不是降级到普通 prompt.submit。
"""
from __future__ import annotations

import pytest

from tui_gateway.methods_bridge import (
    BRIDGE_METHODS,
    BridgeAuthError,
    BridgeCredentialStore,
    is_bridge_method,
    resolve_bridge_principal,
    validate_bridge_scope,
)


@pytest.fixture()
def creds():
    s = BridgeCredentialStore()
    s.issue(scope="bridge.execute", principal="broker:local",
            credential="broker-secret-1")
    return s


class TestMethodWhitelist:
    def test_bridge_methods_exact(self):
        assert is_bridge_method("bridge.capabilities")
        assert is_bridge_method("bridge.task.submit")
        assert is_bridge_method("bridge.task.get")
        assert is_bridge_method("bridge.task.cancel")
        assert is_bridge_method("bridge.task.events")
        assert not is_bridge_method("bridge.task.submit.v2")  # 后缀不算

    @pytest.mark.parametrize("dangerous", [
        "shell.exec", "cli.exec", "slash.exec",
        "prompt.submit", "session.create", "session.resume",
        "session.interrupt", "config.set", "skill_manage",
        "browser.manage", "vault.add", "cronjob_manage",
        "memory.write", "connectors.connect", "delegate",
    ])
    def test_dangerous_methods_rejected(self, dangerous):
        assert not is_bridge_method(dangerous)

    def test_bridge_methods_count(self):
        assert set(BRIDGE_METHODS) == {
            "bridge.capabilities", "bridge.task.submit", "bridge.task.get",
            "bridge.task.cancel", "bridge.task.events"}


class TestCredentialResolution:
    def test_valid_credential_resolves(self, creds):
        ctx = creds.resolve("broker-secret-1")
        assert ctx["principal"] == "broker:local"
        assert ctx["scope"] == "bridge.execute"

    def test_unknown_credential_rejected(self, creds):
        with pytest.raises(BridgeAuthError):
            creds.resolve("nope")

    def test_empty_credential_rejected(self, creds):
        with pytest.raises(BridgeAuthError):
            creds.resolve("")

    def test_wrong_scope_rejected(self, creds):
        with pytest.raises(BridgeAuthError):
            creds.resolve_with_scope("broker-secret-1", "bridge.admin")


class TestPrincipalStamping:
    def test_principal_comes_from_credential_not_params(self, creds):
        # 客户端传 principal='cortico:attacker' 无效——服务端盖认证主体
        p = resolve_bridge_principal(
            creds, credential="broker-secret-1",
            params={"principal": "cortico:attacker", "task_id": "t1"})
        assert p == "broker:local"

    def test_missing_credential_rejected(self, creds):
        with pytest.raises(BridgeAuthError):
            resolve_bridge_principal(creds, credential="", params={})


class TestScopeValidation:
    def test_execute_scope_required_for_submit(self, creds):
        assert validate_bridge_scope("bridge.execute", "bridge.task.submit") is True
        assert validate_bridge_scope("bridge.admin", "bridge.task.submit") is False

    def test_read_scope_allows_get(self):
        assert validate_bridge_scope("bridge.read", "bridge.task.get") is True
        assert validate_bridge_scope("bridge.read", "bridge.task.submit") is False

    def test_no_scope_allows_nothing(self):
        assert validate_bridge_scope(None, "bridge.task.get") is False


class TestCapabilitiesHonesty:
    def test_missing_capability_advertised_as_unsupported(self):
        from tui_gateway.methods_bridge import bridge_capabilities
        caps = bridge_capabilities(
            supports_cancel=False, supports_persist=False, supports_events=False)
        # 缺能力必须如实报 False，让调度方拒绝而不是降级到 prompt.submit
        assert caps["targeted_cancel"] is False
        assert caps["persistent_dispatch"] is False
        assert caps["events"] is False

    def test_full_capabilities(self):
        from tui_gateway.methods_bridge import bridge_capabilities
        caps = bridge_capabilities(
            supports_cancel=True, supports_persist=True, supports_events=True)
        assert caps["targeted_cancel"] is True
        assert caps["protocol_version"] == "bridge-v1"
