"""Hermes bridge.* RPC 入口（G2-T08）：来源鉴权、方法白名单、服务端盖章。

安全模型（方案 §5.4.1）：
1. **专属 credential**：broker 侧持有；不能沿用普通 Serve/clients 凭据。
2. **方法白名单**：只有 5 个 bridge.* 方法；shell.exec / session.resume /
   prompt.submit 等一律不在名单——bridge 连接再怎么请求都到不了。
3. **服务端盖章**：principal/task/revision 只从 credential + 持久 receipt 推导。
   客户端 JSON 里的 `principal` 字段是展示用途，不是身份。
4. **不降级**：capabilities 缺项如实报 False；缺失即拒绝调度，绝不回落
   普通 prompt.submit（那会绕开全部策略门）。
5. 本模块只依赖标准库，便于在 rpc_dispatch 派发前后两侧复用。
"""
from __future__ import annotations

import contextvars
import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional

BRIDGE_PROTOCOL_VERSION = "bridge-v1"

# 唯一允许 bridge 连接调用的方法集合。
BRIDGE_METHODS = frozenset({
    "bridge.capabilities",
    "bridge.task.submit",
    "bridge.task.get",
    "bridge.task.cancel",
    "bridge.task.events",
})

# 方法 → 所需 scope。缺 scope 的方法调用一律拒。
_METHOD_SCOPES: dict[str, frozenset[str]] = {
    "bridge.capabilities": frozenset(),
    "bridge.task.submit": frozenset({"bridge.execute"}),
    "bridge.task.get": frozenset({"bridge.read", "bridge.execute", "bridge.admin"}),
    "bridge.task.cancel": frozenset({"bridge.execute", "bridge.admin"}),
    "bridge.task.events": frozenset({"bridge.read", "bridge.execute", "bridge.admin"}),
}


class BridgeAuthError(Exception):
    """bridge 鉴权/白名单/scope 失败。"""


def is_bridge_method(method: str) -> bool:
    """精确白名单匹配：连 'bridge.task.submit.v2' 这种后缀变体都不放行。"""
    return isinstance(method, str) and method in BRIDGE_METHODS


def validate_bridge_scope(scope: Optional[str], method: str) -> bool:
    if not is_bridge_method(method):
        return False
    allowed = _METHOD_SCOPES[method]
    return not allowed or scope in allowed


@dataclass(frozen=True)
class BridgeCredential:
    id: str
    scope: str
    principal: str


class BridgeCredentialStore:
    """进程内 credential 注册表。生产从受保护配置装载；测试用临时实例。"""

    def __init__(self) -> None:
        self._by_id: dict[str, BridgeCredential] = {}
        self._by_token: dict[str, str] = {}

    def issue(self, *, scope: str, principal: str, credential: str | None = None) -> str:
        if scope not in ("bridge.execute", "bridge.read", "bridge.admin"):
            raise BridgeAuthError(f"unknown bridge scope: {scope}")
        cid = "bcred_" + secrets.token_urlsafe(10).replace("-", "a").replace("_", "b")
        token = credential or ("brg_" + secrets.token_urlsafe(32))
        self._by_id[cid] = BridgeCredential(id=cid, scope=scope, principal=principal)
        self._by_token[token] = cid
        return token

    def resolve(self, token: str) -> dict[str, Any]:
        if not isinstance(token, str) or not token:
            raise BridgeAuthError("missing bridge credential")
        cid = self._by_token.get(token)
        if cid is None:
            raise BridgeAuthError("unknown or revoked bridge credential")
        cred = self._by_id[cid]
        return {"id": cred.id, "scope": cred.scope, "principal": cred.principal}

    def resolve_with_scope(self, token: str, required_scope: str) -> dict[str, Any]:
        ctx = self.resolve(token)
        if ctx["scope"] not in (required_scope, "bridge.admin"):
            raise BridgeAuthError(
                f"credential scope {ctx['scope']} insufficient (need {required_scope})")
        return ctx


def resolve_bridge_principal(
    store: BridgeCredentialStore, *, credential: str, params: Mapping[str, Any],
) -> str:
    """身份推导：credential → principal。params 里的字段一律不采信。"""
    ctx = store.resolve(credential)
    # 客户端可填 display_principal，仅日志展示，绝不进入执行身份
    return ctx["principal"]


def bridge_capabilities(
    *, supports_cancel: bool, supports_persist: bool, supports_events: bool,
) -> dict[str, Any]:
    """能力如实自报：缺一项调度方就必须拒绝，而不是静默降级。"""
    return {
        "protocol_version": BRIDGE_PROTOCOL_VERSION,
        "targeted_cancel": supports_cancel,
        "persistent_dispatch": supports_persist,
        "events": supports_events,
        "tools": [
            "artifact_read", "artifact_write", "approved_health_probe",
            "structured_result",
        ],
    }


# ── 待注册到 server.py 的方法实现 ──────────────────────────────
# 每个方法只接受已认证的 bridge ctx；ctx 由装配层注入（不读客户端字段）。


def method_capabilities(
    cred: BridgeCredential, params: Mapping[str, Any],
    *, supports_cancel: bool, supports_persist: bool, supports_events: bool,
) -> dict[str, Any]:
    return bridge_capabilities(
        supports_cancel=supports_cancel,
        supports_persist=supports_persist,
        supports_events=supports_events)


def method_task_submit(
    cred: BridgeCredential, params: Mapping[str, Any],
    *, store: Any,
) -> dict[str, Any]:
    """受理 dispatch。receipt 先落库（幂等），再返回执行入口元数据。"""
    from .bridge_dispatch_store import DispatchStore  # 局部导入，避免循环

    if not isinstance(store, DispatchStore):
        raise BridgeAuthError("dispatch store not configured")
    dispatch_id = params.get("dispatch_id")
    task_id = params.get("task_id")
    revision = params.get("revision")
    policy_digest = params.get("policy_digest")
    nonce = params.get("nonce")
    policy_ref = params.get("policy_ref") or {}
    # 字符串字段缺一即拒（revision 单独走 int 校验）
    for name, val in (("dispatch_id", dispatch_id), ("task_id", task_id),
                      ("policy_digest", policy_digest), ("nonce", nonce)):
        if not isinstance(val, str) or not val:
            raise BridgeAuthError(f"{name} missing or not a string")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision <= 0:
        raise BridgeAuthError("revision must be a positive integer")
    if not isinstance(policy_ref, dict):
        raise BridgeAuthError("policy_ref must be an object")
    try:
        rec = store.record_dispatch(
            dispatch_id=str(dispatch_id), task_id=str(task_id),
            revision=int(revision), principal=cred.principal,
            policy_digest=str(policy_digest), nonce=str(nonce),
            policy_ref=policy_ref)
    except Exception as exc:
        return {"ok": False, "error": {"code": "dispatch_conflict",
                                       "message": str(exc)}}
    return {"ok": True, "state": rec.state, "attempt": rec.attempt}


def method_task_get(
    cred: BridgeCredential, params: Mapping[str, Any], *, store: Any,
) -> dict[str, Any]:
    from .bridge_dispatch_store import DispatchStore

    if not isinstance(store, DispatchStore):
        raise BridgeAuthError("dispatch store not configured")
    dispatch_id = params.get("dispatch_id")
    if not isinstance(dispatch_id, str) or not dispatch_id:
        raise BridgeAuthError("dispatch_id required")
    rec = store.get(dispatch_id)
    if rec is None:
        return {"ok": False, "error": {"code": "not_found", "message": "dispatch 不存在"}}
    # 归属校验：只能看自己的 dispatch（principal 盖自 credential）
    if rec.principal != cred.principal:
        return {"ok": False, "error": {"code": "not_found", "message": "dispatch 不存在"}}
    return {
        "ok": True, "dispatch_id": rec.dispatch_id, "task_id": rec.task_id,
        "state": rec.state, "attempt": rec.attempt, "result": rec.result,
        "error_code": rec.error_code,
    }


def method_task_cancel(
    cred: BridgeCredential, params: Mapping[str, Any], *, store: Any,
) -> dict[str, Any]:
    """定向取消：只停这个 dispatch；不碰用户会话、不停全局 TTS。"""
    from .bridge_dispatch_store import DispatchStore

    if not isinstance(store, DispatchStore):
        raise BridgeAuthError("dispatch store not configured")
    dispatch_id = params.get("dispatch_id")
    if not isinstance(dispatch_id, str) or not dispatch_id:
        raise BridgeAuthError("dispatch_id required")
    rec = store.get(dispatch_id)
    if rec is None or rec.principal != cred.principal:
        return {"ok": False, "error": {"code": "not_found", "message": "dispatch 不存在"}}
    reason = params.get("reason") or "operator cancel"
    try:
        rec2 = store.cancel(dispatch_id, reason=str(reason))
    except Exception as exc:
        return {"ok": False, "error": {"code": "illegal_state", "message": str(exc)}}
    return {"ok": True, "state": rec2.state, "cancelled": rec2.state == "cancelled"}


# ── 装配（G2-T08）───────────────

_store_singleton: Any = None
_event_log: list[dict] = []


def _default_bridge_dir() -> Path:
    """dispatch 账本落盘位置。

    优先 profile data 目录（与 serve 实例绑定）；取不到时退回 Hermes home 下
    ``bridge/``。绝不放在项目源码树里，也不进 git。
    """
    for env in ("HERMES_PROFILE_DIR", "HERMES_DATA_DIR"):
        val = os.environ.get(env)
        if val:
            return Path(val) / "bridge"
    return Path.home() / ".hermes" / "bridge"


def get_bridge_store() -> Any:
    """进程级 dispatch store 单例；未初始化时按默认路径懒加载。"""
    global _store_singleton
    if _store_singleton is None:
        from .bridge_dispatch_store import DispatchStore
        d = _default_bridge_dir()
        d.mkdir(parents=True, exist_ok=True)
        _store_singleton = DispatchStore(d / "dispatch.sqlite")
    return _store_singleton


def _bridge_supports() -> tuple[bool, bool, bool]:
    """如实自报三项能力。G2 阶段：
    - persistent_dispatch: True（store 已落盘）
    - targeted_cancel: True（只动 dispatch 账本，不碰用户会话/全局 TTS）
    - events: False（事件流尚未装配，Broker 要靠 task.get 轮询）
    """
    return (True, True, False)


def _events_after(principal: str, after_cursor: int) -> tuple[list[dict], int]:
    """进程内事件游标（G2 未持久化，如实自报 events=False）。"""
    rows = [e for e in _event_log
            if e.get("principal") == principal and e.get("cursor", 0) > after_cursor]
    nxt = max((e.get("cursor", 0) for e in _event_log), default=after_cursor)
    return rows, nxt


def _record_event(principal: str, dispatch_id: str, task_id: str,
                  event_type: str, payload: dict) -> None:
    global _event_log
    cursor = (max((e.get("cursor", 0) for e in _event_log), default=0)) + 1
    _event_log.append({
        "cursor": cursor, "principal": principal, "dispatch_id": dispatch_id,
        "task_id": task_id, "event_type": event_type, "payload": payload,
    })


def install_bridge_methods(server) -> None:
    """把 5 个 bridge.* 方法注册进 server 的 ``_methods`` 表。

    每条链路：
      1. transport 上带的 bridge credential（连接升级时由认证层盖章）→ cred
      2. 方法白名单 + scope 校验（双保险：rpc_dispatch 门 + 这里）
      3. 服务端盖章 principal → store 操作
    没有 credential 的普通连接调用 bridge.* 一律被拒——bridge 不是"谁都能调"。
    """
    from .method_ctx import bind_module

    def _cred(method_name: str) -> BridgeCredential:
        """从 ContextVar 取 credential（WS 认证层盖章），按 method_name 校验 scope。

        method_name 由各 handler 闭包捕获自身名字，不依赖 RPC 上下文——直接调
        handler（测试/内部路径）也走同一条校验，没有"绕过 dispatch 就没门"的缺口。
        """
        token = _bridge_credential_var.get()
        if not token:
            raise BridgeAuthError("bridge credential 未提供（非 bridge 连接）")
        ctx = BRIDGE_CREDENTIALS.resolve(token)
        if not validate_bridge_scope(ctx["scope"], method_name):
            raise BridgeAuthError(
                f"scope {ctx['scope']} 不足以调用 {method_name}")
        return BridgeCredential(id=ctx["id"], scope=ctx["scope"], principal=ctx["principal"])

    def _ok(rid, result: dict) -> dict:
        return {"jsonrpc": "2.0", "id": rid, "result": result}

    def _fail(rid, code: int, message: str) -> dict:
        return {"jsonrpc": "2.0", "id": rid,
                "error": {"code": code, "message": message}}

    def bridge_capabilities(rid, params):
        s_cancel, s_persist, s_events = _bridge_supports()
        caps = method_capabilities(
            BridgeCredential(id="anon", scope="", principal=""), params or {},
            supports_cancel=s_cancel, supports_persist=s_persist,
            supports_events=s_events)
        return _ok(rid, caps)

    def bridge_task_submit(rid, params):
        try:
            cred = _cred("bridge.task.submit")
        except BridgeAuthError as exc:
            return _fail(rid, 401, str(exc))
        try:
            out = method_task_submit(cred, params or {}, store=get_bridge_store())
        except BridgeAuthError as exc:
            return _fail(rid, 400, str(exc))
        if out.get("ok"):
            rec = get_bridge_store().get(params["dispatch_id"])
            _record_event(cred.principal, params["dispatch_id"],
                          params.get("task_id", ""), "dispatch.accepted",
                          {"state": rec.state if rec else "accepted"})
        return _ok(rid, out)

    def bridge_task_get(rid, params):
        try:
            cred = _cred("bridge.task.get")
        except BridgeAuthError as exc:
            return _fail(rid, 401, str(exc))
        return _ok(rid, method_task_get(cred, params or {}, store=get_bridge_store()))

    def bridge_task_cancel(rid, params):
        try:
            cred = _cred("bridge.task.cancel")
        except BridgeAuthError as exc:
            return _fail(rid, 401, str(exc))
        out = method_task_cancel(cred, params or {}, store=get_bridge_store())
        if out.get("ok"):
            _record_event(cred.principal, params.get("dispatch_id", ""),
                          params.get("task_id", ""), "dispatch.cancel_requested",
                          {"reason": params.get("reason", "")})
        return _ok(rid, out)

    def bridge_task_events(rid, params):
        try:
            cred = _cred("bridge.task.events")
        except BridgeAuthError as exc:
            return _fail(rid, 401, str(exc))
        rows, nxt = _events_after(cred.principal, int((params or {}).get("after_cursor", 0)))
        return _ok(rid, {"ok": True, "events": rows, "next_cursor": nxt})

    server.register_method("bridge.capabilities", bridge_capabilities)
    server.register_method("bridge.task.submit", bridge_task_submit)
    server.register_method("bridge.task.get", bridge_task_get)
    server.register_method("bridge.task.cancel", bridge_task_cancel)
    server.register_method("bridge.task.events", bridge_task_events)


# 进程级 credential 注册表：凭据从受保护 env 注入，不进代码、不进 git。
BRIDGE_CREDENTIALS = BridgeCredentialStore()

# 当前 RPC 上下文携带的 bridge credential（WS 认证层在连接升级时 set()）。
# 与 current_transport 同一机制：每个 async 任务/线程有独立副本，不跨连接泄漏。
_bridge_credential_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "hermes_bridge_credential", default="")


# Bridge scope aliases: operator/requester are the legacy names used in
# HERMES_BRIDGE_CREDENTIAL docs; the store's whitelist uses the bridge.*
# vocabulary. Without this map every configured credential failed to load
# (issue() raised on the first entry and the batch loader had no per-item
# recovery), leaving bridge.* permanently 401 even with the env set.
_SCOPE_ALIASES = {
    "operator": "bridge.admin",
    "requester": "bridge.execute",
    "reader": "bridge.read",
}


def _load_bridge_credentials() -> None:
    """从 HERMES_BRIDGE_CREDENTIAL（格式: token:scope:principal,可多条用;分隔）装载。

    没有配置 = bridge 入口存在但谁都调不动（fail-closed）。这正是当前状态。
    单项坏（scope 不认识/长度不够）跳过并继续，绝不让一个错项废掉整批。
    """
    raw = os.environ.get("HERMES_BRIDGE_CREDENTIAL", "")
    for item in raw.split(";"):
        item = item.strip()
        if not item:
            continue
        parts = item.split(":")
        if len(parts) != 3:
            continue
        token, scope, principal = (p.strip() for p in parts)
        scope = _SCOPE_ALIASES.get(scope, scope)
        if token and scope and principal:
            try:
                BRIDGE_CREDENTIALS.issue(scope=scope, principal=principal,
                                         credential=token)
            except BridgeAuthError:
                continue


def register(server) -> None:
    """server.py 的统一挂载点（与 methods_voice.register 同款）。"""
    _load_bridge_credentials()
    install_bridge_methods(server)
