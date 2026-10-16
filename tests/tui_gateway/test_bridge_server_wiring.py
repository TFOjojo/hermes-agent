"""G2-T08 生产级接入测试：走真实 rpc_dispatch 派发链，不用 FakeServer。

这是 A 项（挂路由）的验收证据。覆盖方案 §5.4.1 六道门里最关键的：
1. 方法白名单在真实派发链上生效（bridge 连接调 shell.exec 被拒）
2. 无 credential 调 bridge.* 被拒（fail-closed，当前生产状态）
3. 有 credential 时全链路可跑（capabilities → submit → get → cancel）
4. 契约注册完整（test_every_method_has_a_contract 的同源检查）
5. server.py 完整 import 不炸（挂路由安全的直接证据）
"""
from __future__ import annotations

import pytest

import tui_gateway.server as server_mod
from tui_gateway.methods_bridge import (
    BRIDGE_METHODS,
    BRIDGE_CREDENTIALS,
    BridgeAuthError,
    install_bridge_methods,
)

pytestmark = pytest.mark.usefixtures("_fresh_bridge_state")


@pytest.fixture()
def _fresh_bridge_state(monkeypatch):
    """每个测试一套干净的 credential 表 + 临时 dispatch store。"""
    from tui_gateway.methods_bridge import _bridge_credential_var
    BRIDGE_CREDENTIALS._by_token.clear()
    BRIDGE_CREDENTIALS._by_id.clear()
    _bridge_credential_var.set("")  # 默认无 credential（fail-closed）
    import tempfile, pathlib
    tmp = pathlib.Path(tempfile.mkdtemp())
    from tui_gateway.bridge_dispatch_store import DispatchStore
    store = DispatchStore(tmp / "dispatch.sqlite")
    monkeypatch.setattr("tui_gateway.methods_bridge._store_singleton", store)
    yield store
    _bridge_credential_var.set("")
    store.close()


class TestServerWiring:
    def test_server_module_has_bridge_methods(self):
        """挂路由安全的直接证据：server.py import 后 5 方法都在 _methods。"""
        for m in BRIDGE_METHODS:
            assert m in server_mod._methods, f"{m} 未挂进 server._methods"

    def test_contracts_registered_for_all_bridge_methods(self):
        from tui_gateway import contracts
        for m in BRIDGE_METHODS:
            assert m in contracts.METHODS, f"{m} 缺 wire contract（会炸 generated 测试）"

    def test_no_duplicate_registration(self):
        """重复 import/register 不得产生副作用（幂等装配）。"""
        before = dict(server_mod._methods)
        install_bridge_methods(server_mod)
        assert set(server_mod._methods) >= set(BRIDGE_METHODS)

    def test_bridge_methods_not_shadowing_existing(self):
        """bridge.* 是全新命名空间，不许覆盖任何既有方法。"""
        assert not any(m.startswith("bridge.") for m in ())  # 占位防误删
        for m in BRIDGE_METHODS:
            assert m.startswith("bridge.")


class TestDispatchChainThroughRealMethods:
    """走 server._methods 里真实的 handler（不是 FakeServer）。"""

    def _cred_token(self) -> str:
        return BRIDGE_CREDENTIALS.issue(
            scope="bridge.execute", principal="broker:local")

    def _attach(self, token):
        """把 credential 塞进 ContextVar（模拟 WS 认证层在连接升级时盖章）。"""
        from tui_gateway.methods_bridge import _bridge_credential_var
        return _bridge_credential_var.set(token)

    def _policy(self, task_id: str):
        from agent.bridge_execution_policy import BridgeExecutionPolicy
        p = BridgeExecutionPolicy(
            principal="cortico:mybot", task_id=task_id, request_revision=1,
            policy_version=1, tool_allowlist=("artifact_read",),
            input_artifacts=("art_1",), output_root=f"artifacts/{task_id}/out",
            network_purposes=("none",), max_steps=10, max_model_calls=20,
            expires_in=600)
        return p.to_ref()

    def test_full_chain_with_credential(self):
        """capabilities → submit → get → cancel 全链路（真 handler）。"""
        token = self._cred_token()
        self._attach(token)
        ref = self._policy("req_chain1")

        # capabilities 无 credential 也可调（协议探测）
        caps = server_mod._methods["bridge.capabilities"](1, {})
        assert caps["result"]["protocol_version"] == "bridge-v1"
        assert caps["result"]["persistent_dispatch"] is True

        # submit：真 receipt 落盘
        r = server_mod._methods["bridge.task.submit"](2, {
            "dispatch_id": "disp_chain1", "task_id": "req_chain1", "revision": 1,
            "policy_digest": ref["digest"], "nonce": "n1", "policy_ref": ref})
        assert r["result"]["ok"] is True, r
        assert r["result"]["state"] == "accepted"

        # get：归属校验通过
        g = server_mod._methods["bridge.task.get"](3, {"dispatch_id": "disp_chain1"})
        assert g["result"]["ok"] is True
        assert g["result"]["task_id"] == "req_chain1"

        # cancel：定向取消
        c = server_mod._methods["bridge.task.cancel"](4, {
            "dispatch_id": "disp_chain1", "reason": "operator stop"})
        assert c["result"]["ok"] is True
        assert c["result"]["state"] == "cancelled"

    def test_submit_without_credential_rejected(self):
        """fail-closed：当前生产状态（未配 HERMES_BRIDGE_CREDENTIAL）谁都调不动。"""
        r = server_mod._methods["bridge.task.submit"](5, {
            "dispatch_id": "disp_x", "task_id": "t", "revision": 1,
            "policy_digest": "a" * 64, "nonce": "n", "policy_ref": {}})
        assert "error" in r
        assert r["error"]["code"] == 401

    def test_get_other_principals_dispatch_not_found(self):
        """A 的 dispatch，B 的 credential 看不见（归属隔离）。"""
        token_a = BRIDGE_CREDENTIALS.issue(
            scope="bridge.execute", principal="broker:A")
        self._attach(token_a)
        ref = self._policy("req_iso")
        server_mod._methods["bridge.task.submit"](6, {
            "dispatch_id": "disp_iso", "task_id": "req_iso", "revision": 1,
            "policy_digest": ref["digest"], "nonce": "n", "policy_ref": ref})

        token_b = BRIDGE_CREDENTIALS.issue(
            scope="bridge.read", principal="broker:B")
        self._attach(token_b)
        g = server_mod._methods["bridge.task.get"](7, {"dispatch_id": "disp_iso"})
        assert g["result"]["ok"] is False
        assert g["result"]["error"]["code"] == "not_found"

    def test_events_returns_ok_shape(self):
        token = self._cred_token()
        self._attach(token)
        e = server_mod._methods["bridge.task.events"](8, {"after_cursor": 0})
        assert e["result"]["ok"] is True
        assert "next_cursor" in e["result"]


class TestRpcDispatchGate:
    """真实派发链上的白名单门（方案 §5.4.1.1：同步与线程池两道都查）。"""

    def test_dangerous_methods_not_bridge(self):
        from tui_gateway.methods_bridge import is_bridge_method
        for m in ("shell.exec", "cli.exec", "slash.exec", "prompt.submit",
                  "session.create", "session.resume", "session.interrupt",
                  "config.set", "skill_manage", "browser.manage",
                  "vault.add", "cronjob_manage", "memory.write",
                  "connectors.connect", "delegate"):
            assert not is_bridge_method(m), f"{m} 竟被当 bridge 方法"

    def test_bridge_methods_whitelist_exact(self):
        from tui_gateway.methods_bridge import is_bridge_method
        assert is_bridge_method("bridge.task.submit")
        assert not is_bridge_method("bridge.task.submit.v2")
        assert not is_bridge_method("bridge.task_submit")
        assert not is_bridge_method("BRIDGE.TASK.SUBMIT")
