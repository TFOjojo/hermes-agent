"""Bridge 受限执行策略（G2-T09）：不可变 ExecutionPolicy + 工具硬门。

方案 §4.2 / §5.4.1 的落地：
- 策略是**权限事实的唯一来源**：tool allowlist、参数形状、步数/调用预算、期限、
  网络用途、记忆隔离、派生执行开关，全部构造时定锤，运行期不可改。
- 两层门：allowlist 门（精确匹配，前缀/大小写变体不算）+ 策略门（v1 恒定禁派生
  执行与后台执行，即使 allowlist 被误配也拦得住）。
- 每次调用记审计；违规也记审计（不静默）。
- from_dispatch() 支持崩溃恢复：Broker 恢复时按同一 policy_ref + digest 重建，
  摘要不匹配即拒——模型不可自造 policy。
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any

# v1 恒定禁止的派生/后台执行工具（即使 allowlist 误含也拦）。
_DERIVATIVE_TOOLS = frozenset({
    "delegate", "delegate_task", "cronjob_manage", "cronjob_create",
    "computer_use", "skill_manage", "browser", "shell_exec", "terminal",
    "cli_exec", "slash_exec", "process_manage", "memory",
})

_VALID_NETWORK_PURPOSES = frozenset({"none", "public_search", "artifact_upload"})

_DIGEST_KEYS = (
    "policy_version", "principal", "tool_allowlist", "input_artifacts",
    "output_root", "network_purposes", "max_steps", "max_model_calls",
    "memory_injection", "allow_subagents", "allow_background",
)


class PolicyError(Exception):
    """策略构造/恢复失败（非法字段、摘要不匹配）。"""


class PolicyViolation(Exception):
    """运行期违反策略（工具不在名单、超预算、过期、派生执行）。"""


@dataclass(frozen=True)
class BridgeExecutionPolicy:
    principal: str
    task_id: str
    request_revision: int
    policy_version: int
    tool_allowlist: tuple[str, ...]
    input_artifacts: tuple[str, ...]
    output_root: str
    network_purposes: tuple[str, ...]
    max_steps: int
    max_model_calls: int
    expires_in: float
    memory_injection: bool = False
    allow_subagents: bool = False
    allow_background: bool = False
    # 绝对到期时间（epoch 秒）；构造时 = now + expires_in，运行期只读
    expires_at: float = field(default=0.0, init=False, repr=False)
    # 运行期状态（frozen 类里的可变记账：通过 object.__setattr__ 或持有容器）
    audit: list[dict] = field(default_factory=list, init=False, repr=False)
    violations: list[dict] = field(default_factory=list, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.request_revision <= 0:
            raise PolicyError("request_revision must be positive")
        if self.max_steps <= 0 or self.max_model_calls <= 0:
            raise PolicyError("budget must be positive")
        if self.expires_in <= 0:
            raise PolicyError("expires_in must be positive (no instant-expiry policies)")
        if not self.principal or not self.task_id:
            raise PolicyError("principal/task_id required")
        if not self.output_root:
            raise PolicyError("output_root required")
        for purpose in self.network_purposes:
            if purpose not in _VALID_NETWORK_PURPOSES:
                raise PolicyError(f"unknown network purpose: {purpose}")
        object.__setattr__(self, "expires_at", time.time() + self.expires_in)

    # ── 身份摘要 ──────────────────────────────────────────────

    def digest(self) -> str:
        payload = {
            "policy_version": self.policy_version,
            "principal": self.principal,
            "task_id": self.task_id,
            "request_revision": self.request_revision,
            "tool_allowlist": list(self.tool_allowlist),
            "input_artifacts": list(self.input_artifacts),
            "output_root": self.output_root,
            "network_purposes": list(self.network_purposes),
            "max_steps": self.max_steps,
            "max_model_calls": self.max_model_calls,
            "memory_injection": self.memory_injection,
            "allow_subagents": self.allow_subagents,
            "allow_background": self.allow_background,
        }
        raw = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(raw.encode()).hexdigest()

    @classmethod
    def from_dispatch(
        cls, *, dispatch_id: str, task_id: str, revision: int,
        policy_ref: dict[str, Any], known_digest: str,
    ) -> "BridgeExecutionPolicy":
        """从 Broker 的 dispatch 快照恢复策略；digest 对不上即拒。

        策略绑 (task_id, request_revision)：换任务或换版本都恢复不出同一摘要——
        Broker 不可拿任务 A 的策略去跑任务 B。
        """
        digest = policy_ref.get("digest")
        if digest != known_digest:
            raise PolicyError(
                f"policy digest mismatch: dispatch={dispatch_id} task={task_id}")
        try:
            policy = cls(
                principal=policy_ref["principal"],
                task_id=task_id,
                request_revision=revision,
                policy_version=int(policy_ref["policy_version"]),
                tool_allowlist=tuple(policy_ref["tool_allowlist"]),
                input_artifacts=tuple(policy_ref["input_artifacts"]),
                output_root=policy_ref["output_root"],
                network_purposes=tuple(policy_ref["network_purposes"]),
                max_steps=int(policy_ref["max_steps"]),
                max_model_calls=int(policy_ref["max_model_calls"]),
                expires_in=float(policy_ref.get("expires_in", 3600)),
                memory_injection=bool(policy_ref["memory_injection"]),
                allow_subagents=bool(policy_ref["allow_subagents"]),
                allow_background=bool(policy_ref["allow_background"]),
            )
        except KeyError as exc:
            raise PolicyError(f"policy_ref missing field: {exc}") from exc
        if policy.digest() != known_digest:
            raise PolicyError("reconstructed policy digest drift")
        return policy

    def to_ref(self) -> dict[str, Any]:
        """供 Broker 持久化/对账的策略正文。"""
        return {
            "policy_version": self.policy_version,
            "principal": self.principal,
            "tool_allowlist": list(self.tool_allowlist),
            "input_artifacts": list(self.input_artifacts),
            "output_root": self.output_root,
            "network_purposes": list(self.network_purposes),
            "max_steps": self.max_steps,
            "max_model_calls": self.max_model_calls,
            "expires_in": self.expires_in,
            "memory_injection": self.memory_injection,
            "allow_subagents": self.allow_subagents,
            "allow_background": self.allow_background,
            "digest": self.digest(),
        }

    # ── 运行期门 ──────────────────────────────────────────────

    def is_tool_allowed(self, name: str) -> bool:
        return name in set(self.tool_allowlist)

    def check_tool_call(self, name: str, args: Any) -> None:
        """调真实工具前的唯一入口。任何拒绝都记审计。"""
        self._check_fresh()
        if not isinstance(name, str) or not name:
            self._violate(name or "<missing>", "tool name must be non-empty string")
        if name in _DERIVATIVE_TOOLS:
            self._violate(name, "v1 禁用派生/后台/管理执行路径")
        if not self.is_tool_allowed(name):
            self._violate(name, "tool not in allowlist")
        if not isinstance(args, dict):
            self._violate(name, "tool args must be a dict")
        self.audit.append({"event": "tool_call", "tool": name,
                           "at": time.time(), "task": self.task_id})

    def consume_step(self) -> None:
        self._check_fresh()
        used = sum(1 for e in self.audit if e.get("event") == "step")
        if used >= self.max_steps:
            self._violate("<steps>", f"step budget exhausted (max {self.max_steps})")
        self.audit.append({"event": "step", "at": time.time(), "task": self.task_id})

    def consume_model_call(self) -> None:
        self._check_fresh()
        used = sum(1 for e in self.audit if e.get("event") == "model_call")
        if used >= self.max_model_calls:
            self._violate("<model>", f"model-call budget exhausted (max {self.max_model_calls})")
        self.audit.append({"event": "model_call", "at": time.time(), "task": self.task_id})

    def _check_fresh(self) -> None:
        if self.expires_at <= time.time():
            self._violate("<policy>", "policy window expired (absolute)")
        # 期限由构造方以启动时间计算；步数检查见 consume_step。

    def _violate(self, what: str, why: str) -> None:
        self.violations.append({"tool": what, "reason": why, "at": time.time()})
        raise PolicyViolation(f"{why}: {what}")
