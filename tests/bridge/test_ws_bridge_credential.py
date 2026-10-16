"""H 侧 bridge 凭据通道：WS 盖章（A5.3 红测先行 · 绿）。

链路（方案 §5.6）：
  1) HERMES_BRIDGE_CREDENTIAL 装载进 BRIDGE_CREDENTIALS（含旧式 scope 名映射）
  2) /api/ws 连接升级时 query token 命中注册表 → ContextVar 盖章并放行
  3) unknown token → token_mismatch 且不盖章
  4) 方法级 scope 校验独立生效（双保险）
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


class FakeWs:
    """最小 WS 替身：query_params + client + 可盖章属性。"""

    def __init__(self, params: dict, host: str = "127.0.0.1"):
        self.query_params = params
        self.client = type("C", (), {"host": host})()
        self.url = type("U", (), {"path": "/api/ws"})()
        # gated 分支读 sec-websocket-protocol（ticket 子协议探测）
        self.headers = {}


@pytest.fixture
def bridge_env(monkeypatch):
    """注入一对 bridge 凭据（token:scope:principal,;分隔——同 _load_bridge_credentials）。

    注意：principal 不能含 ":"——装载协议是冒号分隔，含冒号会被静默丢弃（实锤）。
    """
    monkeypatch.setenv(
        "HERMES_BRIDGE_CREDENTIAL",
        "tok-req-0001:requester:cortico-mybot;tok-op-0002:operator:cortico-mybot",
    )
    for mod in ("tui_gateway.methods_bridge", "hermes_cli.web_server_chat"):
        sys.modules.pop(mod, None)
    import importlib
    # import_module 保证重建的模块既在 sys.modules 里又是返回值——
    # from tui_gateway import X 可能拿包的旧绑定属性。
    # 混跑隔离：高速缓存的事后诸葛不如事前清明——本 fixture 只保证
    # 「单跑本文件」的语义；跨文件混跑时若前序 fixture 把 installs/<hash>/venv
    # 或 .hermes-runtime 副本楔进 sys.path（本仓真实存在的解释器错配环境：
    # 3.11 venv 的 cp311 pydantic_core 二进制），import fastapi 会
    # ModuleNotFoundError。那种污染由混跑的对方 own，本 fixture 不替它擦屁股
    # （摘 sys.path 会连带摘掉解释器 stdlib——2026-09-27 实测失败），只如实报。
    mb = importlib.import_module("tui_gateway.methods_bridge")
    importlib.import_module("hermes_cli.web_server_chat")
    # gated 判定读 app.state.auth_required —— 跨用例会漂。fixture 显式钉住，
    # 否则前用例留下的 state 会让后用例走 legacy 分支（token_mismatch）。
    from hermes_cli.web_server import app as _app
    _app.state.auth_required = True
    mb.BRIDGE_CREDENTIALS = mb.BridgeCredentialStore()
    mb._load_bridge_credentials()
    return mb


def _reason(mb, ws):
    """每次断言前清章：认证函数 import 的是模块当前对象，章可能来自上一用例。"""
    mb._bridge_credential_var.set("")
    from hermes_cli.web_server_chat import _ws_auth_reason
    return _ws_auth_reason(ws)


class TestWsBridgeCredentialStamp:
    def test_gated_accepts_registered_bridge_token_and_stamps(self, bridge_env):
        """gated 模式下已注册 bridge token → 放行 + 盖章（credential 回 'bridge'）。"""
        mb = bridge_env
        reason, cred = _reason(mb, FakeWs({"bridge_token": "tok-req-0001"}))
        assert reason is None, f"注册过的 bridge token 必须放行，实际 {reason}"
        assert cred == "bridge"
        assert mb._bridge_credential_var.get() == "tok-req-0001"

    def test_unknown_bridge_token_rejected_without_stamp(self, bridge_env):
        """未知 token → token_mismatch + 不盖章（fail-closed，无静默成功）。"""
        mb = bridge_env
        reason, cred = _reason(mb, FakeWs({"bridge_token": "tok-bogus-999"}))
        assert reason == "token_mismatch"
        assert mb._bridge_credential_var.get() == ""

    def test_no_bridge_token_treated_as_ordinary_client(self, bridge_env):
        """非 bridge 连接（无 token）→ var 干净，不误盖。"""
        mb = bridge_env
        _reason(mb, FakeWs({}))
        assert mb._bridge_credential_var.get() == ""

    def test_scope_gate_still_enforced_at_method_level(self, bridge_env):
        """盖章只是进门；方法级 scope 校验不松——execute 干不了 admin 的活。

        双保险：连接层盖章 / 方法层 resolve_with_scope 各自独立，
        没有「过了门就全放行」的缺口。
        """
        mb = bridge_env
        reason, _ = _reason(mb, FakeWs({"bridge_token": "tok-req-0001"}))
        assert reason is None
        token = mb._bridge_credential_var.get()
        assert token == "tok-req-0001"
        ctx = mb.BRIDGE_CREDENTIALS.resolve(token)
        assert ctx["scope"] == "bridge.execute"
        try:
            mb.BRIDGE_CREDENTIALS.resolve_with_scope(token, "bridge.admin")
        except mb.BridgeAuthError:
            pass
        else:
            raise AssertionError("execute scope 竟然能过 admin 门")
        assert (
            mb.BRIDGE_CREDENTIALS.resolve_with_scope(
                token, "bridge.execute"
            )["principal"]
            == "cortico-mybot"
        )
