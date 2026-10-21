"""Quality gates for goals: deterministic shell commands that must pass before a goal is done.

Owns the ``GoalGate`` record, its defaults, the workspace resolution a gate runs in, and the
bounded execution itself. Split out of ``goals.py`` so the goal state/CAS core stays in one
place; the facade re-exports the names other packages import.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

DEFAULT_GATE_TIMEOUT_SECONDS = 300
DEFAULT_GATE_MAX_RETRIES = 3
# Bounded tail of a failed gate's combined stdout/stderr fed back to the agent.
_GATE_OUTPUT_TAIL_CHARS = 3000


@dataclass
class GoalGate:
    """A deterministic shell command that must pass before a goal can be done.

    Gates run at turn boundary BEFORE the LLM judge; a failing gate short-circuits judging and its
    bounded output becomes the continuation prompt.
    """
    command: str
    timeout_seconds: int = DEFAULT_GATE_TIMEOUT_SECONDS
    max_retries: int = DEFAULT_GATE_MAX_RETRIES
    attempts: int = 0
    last_exit_code: Optional[int] = None
    last_output_tail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Optional[dict[str, Any]]) -> GoalGate:
        if not isinstance(data, dict):
            return cls(command="")
        return cls(
            command=str(data.get("command") or ""),
            timeout_seconds=int(data.get("timeout_seconds") or DEFAULT_GATE_TIMEOUT_SECONDS),
            max_retries=int(data.get("max_retries") or DEFAULT_GATE_MAX_RETRIES),
            attempts=int(data.get("attempts") or 0),
            last_exit_code=(int(data["last_exit_code"]) if data.get("last_exit_code") is not None else None),
            last_output_tail=str(data.get("last_output_tail") or ""),
        )


def _gate_workspace() -> tuple[Optional[str], Optional[str]]:
    """``(cwd, refusal)`` for this check's gates. A multi-session backend's process directory is not
    the session's project, so gates run in the scoped session workspace (#125369). A declared
    workspace that is not a directory on this host (deleted, remote, container) is a refusal: a
    relative gate run anywhere else would check a different project and could pass a failing goal,
    and no agent turn can fix it, so the caller pauses instead of retrying.
    No declared workspace keeps the classic resolution (TERMINAL_CWD, else the launch directory)."""
    from agent.runtime_cwd import resolve_agent_cwd, scoped_session_cwd

    declared = scoped_session_cwd()
    if declared:
        path = Path(declared).expanduser()
        if path.is_dir():
            return str(path), None
        return None, (f"the session workspace {declared} is not a directory on this host, "
                      "and running gates anywhere else would check a different project")
    try:
        return str(resolve_agent_cwd()), None
    except OSError:
        return None, None  # deleted launch directory: subprocess reports it per gate


def run_gate(gate: GoalGate, *, cwd: Optional[str] = None) -> tuple[bool, int, str]:
    """Run one gate through the shell. Returns ``(passed, exit_code, output_tail)``; a timeout kills
    the process and counts as exit code -1."""
    try:
        # utf-8/replace: operator-configured output is arbitrary bytes; strict codepage decoding of
        # one unmappable byte (emoji/CJK on a non-UTF-8 Windows console) kills the reader thread and
        # the tail the agent needs arrives empty.
        proc = subprocess.run(
            gate.command, shell=True, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=max(1, int(gate.timeout_seconds)), cwd=cwd or None,
            check=False,
        )
        combined = (proc.stdout or "") + (("\n" + proc.stderr) if proc.stderr else "")
        return proc.returncode == 0, proc.returncode, combined[-_GATE_OUTPUT_TAIL_CHARS:]
    except subprocess.TimeoutExpired as exc:
        out = "".join(c if isinstance(c, str) else c.decode("utf-8", "replace") for c in (exc.stdout, exc.stderr) if c)
        return False, -1, (out + f"\n[gate timed out after {gate.timeout_seconds}s]")[-_GATE_OUTPUT_TAIL_CHARS:]
    except Exception as exc:
        return False, -1, f"[gate could not run: {type(exc).__name__}: {exc}]"
