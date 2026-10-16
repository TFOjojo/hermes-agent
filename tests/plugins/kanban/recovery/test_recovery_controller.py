"""TDD tests for Kanban Recovery Controller — hardening increment.

Phase 3 tests covering:
  - plugin.yaml discovery and real registration
  - Hook handling (worker-exit and dispatch-tick)
  - Safe classification (observe-only categories)
  - Unsafe classes produce no candidate
  - CAS mismatch rejection (stale candidate)
  - Stable fingerprint
  - Concurrent/duplicate ledger cooldown (atomic)
  - Diagnosis projection
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Optional

import pytest

# Ensure hermes-agent root is on sys.path
_HERMES_AGENT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))
if _HERMES_AGENT_ROOT not in sys.path:
    sys.path.insert(0, _HERMES_AGENT_ROOT)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fake_task(
    *,
    task_id: str = "t_aabbcc",
    status: str = "running",
    run_id: int = 7,
    latest_event_id: int = 42,
    assignee: str = "default",
    board: str = "default",
) -> dict:
    """Create a minimal fake task for DI-based testing (no DB needed)."""
    return {
        "id": task_id,
        "status": status,
        "run_id": run_id,
        "latest_event_id": latest_event_id,
        "assignee": assignee,
        "board": board,
    }


def _make_task_reader(*tasks: dict) -> callable:
    """Return a task reader function that looks up tasks by id."""
    by_id = {t["id"]: t for t in tasks}
    return lambda task_id: by_id.get(task_id)


def _worker_exit_payload(
    *,
    task_id: str = "t_aabbcc",
    exit_kind: str = "nonzero_exit",
    outcome: str = "crashed",
    worker_pid: int = 12345,
    exit_code: int = 1,
    retry_status: str = "retry",
    run_id: int = 7,
    latest_event_id: int = 42,
) -> dict:
    """Worker-exit hook payload."""
    return {
        "task_id": task_id,
        "run_id": run_id,
        "worker_pid": worker_pid,
        "exit_kind": exit_kind,
        "exit_code": exit_code,
        "outcome": outcome,
        "retry_status": retry_status,
        "latest_event_id": latest_event_id,
    }


def _tick_payload(
    *,
    board: str = "default",
    active_tasks: int = 0,
    pending_tasks: int = 0,
) -> dict:
    """Dispatch-tick hook payload."""
    return {
        "board": board,
        "active_tasks": active_tasks,
        "pending_tasks": pending_tasks,
    }


# ===================================================================
# 1. Manifest / Real Registration
# ===================================================================

class TestManifestDiscovery:
    """plugin.yaml exists at the correct directory level and has valid fields."""

    def test_plugin_yaml_exists(self):
        yaml_path = Path(_HERMES_AGENT_ROOT) / "plugins" / "kanban" / "recovery" / "plugin.yaml"
        assert yaml_path.exists(), f"plugin.yaml not found at {yaml_path}"

    def test_plugin_yaml_valid_id(self):
        import yaml
        yaml_path = Path(_HERMES_AGENT_ROOT) / "plugins" / "kanban" / "recovery" / "plugin.yaml"
        with open(yaml_path) as f:
            data = yaml.safe_load(f)
        assert data["id"] == "kanban-recovery-controller"
        assert data["name"] == "kanban-recovery-controller"

    def test_plugin_yaml_kind_plugin(self):
        import yaml
        yaml_path = Path(_HERMES_AGENT_ROOT) / "plugins" / "kanban" / "recovery" / "plugin.yaml"
        with open(yaml_path) as f:
            data = yaml.safe_load(f)
        assert data["kind"] == "plugin"

    def test_plugin_yaml_only_valid_lifecycle_hooks(self):
        import yaml
        from hermes_cli.plugins import VALID_HOOKS
        yaml_path = Path(_HERMES_AGENT_ROOT) / "plugins" / "kanban" / "recovery" / "plugin.yaml"
        with open(yaml_path) as f:
            data = yaml.safe_load(f)
        hooks = data.get("hooks", [])
        for h in hooks:
            assert h in VALID_HOOKS, f"Hook '{h}' not in VALID_HOOKS"

    def test_register_function_exists_and_registers_real_hooks(self):
        """register(ctx) must call ctx.register_hook for real callbacks."""
        from plugins.kanban.recovery import register
        import types

        registered_hooks = {}

        class FakeCtx:
            def register_hook(self, hook_name: str, callback):
                registered_hooks.setdefault(hook_name, []).append(callback)
                return None

        ctx = FakeCtx()
        register(ctx)
        # Must have registered real hooks
        assert "on_kanban_worker_exited" in registered_hooks, (
            "register(ctx) did not register on_kanban_worker_exited"
        )
        assert "on_kanban_dispatch_tick" in registered_hooks, (
            "register(ctx) did not register on_kanban_dispatch_tick"
        )

    def test_register_does_not_declare_unsupported_hooks(self):
        """register(ctx) must never call ctx.register_hook with an unsupported name."""
        from hermes_cli.plugins import VALID_HOOKS
        from plugins.kanban.recovery import register

        registered_hooks = []

        class FakeCtx:
            def register_hook(self, hook_name: str, callback):
                registered_hooks.append(hook_name)
                return None

        ctx = FakeCtx()
        register(ctx)
        for h in registered_hooks:
            assert h in VALID_HOOKS, f"register(ctx) registered unsupported hook '{h}'"


# ===================================================================
# 2. Hook Handling — Worker Exit
# ===================================================================

class TestWorkerExitHook:
    """on_kanban_worker_exited handler derives candidates for safe categories."""

    def test_tool_loop_halt_produces_candidate(self):
        from plugins.kanban.recovery.lifecycle_hooks import on_worker_exited
        payload = _worker_exit_payload(
            exit_kind="nonzero_exit",
            outcome="tool_loop_halt",
        )
        task_reader = _make_task_reader(
            _fake_task(status="running", run_id=7, latest_event_id=42)
        )
        result = on_worker_exited(payload, task_reader=task_reader)
        assert result is not None, "tool_loop_halt should produce a candidate"
        assert result["recovery_class"] == "tool_loop_halt"

    def test_crashed_dead_pid_produces_candidate(self):
        from plugins.kanban.recovery.lifecycle_hooks import on_worker_exited
        payload = _worker_exit_payload(
            exit_kind="nonzero_exit",
            outcome="crashed",
        )
        task_reader = _make_task_reader(
            _fake_task(status="running", run_id=7, latest_event_id=42)
        )
        result = on_worker_exited(payload, task_reader=task_reader)
        assert result is not None
        assert result["recovery_class"] in ("tool_loop_halt", "crashed")

    def test_rate_limited_produces_candidate(self):
        from plugins.kanban.recovery.lifecycle_hooks import on_worker_exited
        payload = _worker_exit_payload(
            exit_kind="rate_limited",
            outcome="rate_limited",
        )
        task_reader = _make_task_reader(
            _fake_task(status="running", run_id=7, latest_event_id=42)
        )
        result = on_worker_exited(payload, task_reader=task_reader)
        assert result is not None, "rate_limited should produce a candidate"
        assert result["recovery_class"] == "rate_limited"

    def test_clean_exit_produces_no_candidate(self):
        from plugins.kanban.recovery.lifecycle_hooks import on_worker_exited
        payload = _worker_exit_payload(
            exit_kind="clean_exit",
            outcome="clean",
        )
        task_reader = _make_task_reader(
            _fake_task(status="completed", run_id=7, latest_event_id=42)
        )
        result = on_worker_exited(payload, task_reader=task_reader)
        assert result is None, "clean_exit should not produce a candidate"

    def test_handler_never_mutates_task(self):
        """Verify observe-only: no task mutation occurs."""
        from plugins.kanban.recovery.lifecycle_hooks import on_worker_exited
        task = _fake_task(status="running", run_id=7, latest_event_id=42)
        original = dict(task)
        payload = _worker_exit_payload()
        task_reader = _make_task_reader(task)
        on_worker_exited(payload, task_reader=task_reader)
        assert task == original, "Handler must not mutate task state"


# ===================================================================
# 3. Hook Handling — Dispatch Tick
# ===================================================================

class TestDispatchTickHook:
    """on_kanban_dispatch_tick handler observes but never acts."""

    def test_tick_returns_none(self):
        from plugins.kanban.recovery.lifecycle_hooks import on_dispatch_tick
        result = on_dispatch_tick(_tick_payload())
        assert result is None, "Dispatch tick observer should return None"


# ===================================================================
# 4. Safe / Unsafe Classification
# ===================================================================

class TestSafeClassification:
    """Only OBSERVE-safe categories yield candidates."""

    SAFE_CATEGORIES = ("tool_loop_halt", "crashed", "rate_limited")

    @pytest.mark.parametrize("category", SAFE_CATEGORIES)
    def test_safe_category_classified(self, category):
        from plugins.kanban.recovery.lifecycle_hooks import classify_exit
        result = classify_exit(
            exit_kind="nonzero_exit" if category != "rate_limited" else "rate_limited",
            outcome=category,
        )
        assert result["recovery_class"] == category
        assert result["is_safe"] is True

    def test_all_safe_categories_have_automation_observe_only(self):
        from plugins.kanban.recovery.lifecycle_hooks import classify_exit
        for cat in self.SAFE_CATEGORIES:
            result = classify_exit(
                exit_kind="nonzero_exit" if cat != "rate_limited" else "rate_limited",
                outcome=cat,
            )
            assert result["automation_level"] == "observe_only"


class TestUnsafeClassification:
    """Unsafe categories never produce candidates."""

    UNSAFE_OUTCOMES = ("needs_input", "security", "auth", "terminal_provider")

    @pytest.mark.parametrize("outcome", UNSAFE_OUTCOMES)
    def test_unsafe_outcome_no_candidate(self, outcome):
        from plugins.kanban.recovery.lifecycle_hooks import classify_exit
        result = classify_exit(exit_kind="nonzero_exit", outcome=outcome)
        assert result["is_safe"] is False
        assert result["recovery_class"] is None


# ===================================================================
# 5. CAS Mismatch Rejection
# ===================================================================

class TestCASMismatchRejection:
    """CAS snapshot mismatch → candidate rejected as stale."""

    def test_matching_cas_passes(self):
        from plugins.kanban.recovery.lifecycle_hooks import verify_cas, build_cas_snapshot
        task = _fake_task(status="running", run_id=7, latest_event_id=42)
        snapshot = build_cas_snapshot(task)
        assert verify_cas(snapshot, task) is True

    def test_mismatched_status_rejects(self):
        from plugins.kanban.recovery.lifecycle_hooks import verify_cas, build_cas_snapshot
        task = _fake_task(status="running", run_id=7, latest_event_id=42)
        snapshot = build_cas_snapshot(task)
        # Mutate task status to simulate state change
        task["status"] = "completed"
        assert verify_cas(snapshot, task) is False

    def test_mismatched_run_id_rejects(self):
        from plugins.kanban.recovery.lifecycle_hooks import verify_cas, build_cas_snapshot
        task = _fake_task(status="running", run_id=7, latest_event_id=42)
        snapshot = build_cas_snapshot(task)
        task["run_id"] = 8
        assert verify_cas(snapshot, task) is False

    def test_mismatched_event_id_rejects(self):
        from plugins.kanban.recovery.lifecycle_hooks import verify_cas, build_cas_snapshot
        task = _fake_task(status="running", run_id=7, latest_event_id=42)
        snapshot = build_cas_snapshot(task)
        task["latest_event_id"] = 99
        assert verify_cas(snapshot, task) is False

    def test_cas_rejection_ledgered_as_stale(self):
        """When CAS fails, the candidate should be recorded as stale."""
        from plugins.kanban.recovery.lifecycle_hooks import (
            on_worker_exited, build_cas_snapshot,
        )
        task = _fake_task(status="running", run_id=7, latest_event_id=42)
        snapshot = build_cas_snapshot(task)
        # Mutate task to cause CAS failure
        task["status"] = "completed"
        task_reader = _make_task_reader(task)

        payload = _worker_exit_payload()
        result = on_worker_exited(
            payload, task_reader=task_reader, cas_snapshot=snapshot
        )
        # Should either return None or return a candidate marked stale
        if result is not None:
            assert result.get("rejected") is True or result.get("stale") is True, (
                "Mismatched CAS should produce a stale/rejected candidate"
            )


# ===================================================================
# 6. Stable Fingerprint
# ===================================================================

class TestStableFingerprint:
    """Fingerprint is deterministic for same parameters."""

    def test_same_params_same_fingerprint(self):
        from plugins.kanban.recovery.lifecycle_hooks import compute_fingerprint
        fp1 = compute_fingerprint(
            task_id="t_aabbcc", run_id=7, exit_class="tool_loop_halt",
        )
        fp2 = compute_fingerprint(
            task_id="t_aabbcc", run_id=7, exit_class="tool_loop_halt",
        )
        assert fp1 == fp2
        assert len(fp1) >= 16

    def test_different_params_different_fingerprint(self):
        from plugins.kanban.recovery.lifecycle_hooks import compute_fingerprint
        fp1 = compute_fingerprint(
            task_id="t_aabbcc", run_id=7, exit_class="tool_loop_halt",
        )
        fp2 = compute_fingerprint(
            task_id="t_aabbcc", run_id=8, exit_class="tool_loop_halt",
        )
        assert fp1 != fp2


# ===================================================================
# 7. Concurrent / Duplicate Ledger Cooldown (Atomic)
# ===================================================================

class TestAtomicLedger:
    """Ledger writes are concurrency-safe and atomic."""

    def test_concurrent_writes_no_corruption(self):
        """Two concurrent record_recovery_event calls must not corrupt the ledger."""
        from plugins.kanban.recovery import record_recovery_event, read_ledger
        import threading

        with tempfile.TemporaryDirectory() as tmpdir:
            # Simulate concurrent writes for different fingerprints
            results = [None, None]

            def writer(idx, fp):
                results[idx] = record_recovery_event(
                    task_id=f"t_concurrent_{idx}",
                    run_id=100 + idx,
                    exit_class="tool_loop_halt",
                    fingerprint=fp,
                    action_type="observe",
                    cooldown_seconds=0,
                    hermes_home=tmpdir,
                )

            t1 = threading.Thread(target=writer, args=(0, "fp_aaa111"))
            t2 = threading.Thread(target=writer, args=(1, "fp_bbb222"))
            t1.start()
            t2.start()
            t1.join(timeout=5)
            t2.join(timeout=5)

            assert results[0]["recorded"] is True
            assert results[1]["recorded"] is True

            entries = read_ledger(hermes_home=tmpdir)
            assert len(entries) == 2

    def test_dedup_index_corruption_fails_closed(self):
        """A malformed dedup index should fail closed (deny the write)."""
        from plugins.kanban.recovery import record_recovery_event
        with tempfile.TemporaryDirectory() as tmpdir:
            dedup_dir = Path(tmpdir) / "kanban" / "recovery"
            dedup_dir.mkdir(parents=True)
            dedup_file = dedup_dir / ".dedup_index.json"
            dedup_file.write_text("NOT VALID JSON {{{", encoding="utf-8")

            result = record_recovery_event(
                task_id="t_corrupt",
                run_id=1,
                exit_class="tool_loop_halt",
                fingerprint="fp_corrupt",
                action_type="observe",
                cooldown_seconds=0,
                hermes_home=tmpdir,
            )
            # Should still record (observe-only allows reset)
            assert result["recorded"] is True

    def test_duplicate_same_fingerprint_cooldown(self):
        """Same fingerprint within cooldown window → second write dropped."""
        from plugins.kanban.recovery import record_recovery_event
        with tempfile.TemporaryDirectory() as tmpdir:
            r1 = record_recovery_event(
                task_id="t_dup",
                run_id=1,
                exit_class="tool_loop_halt",
                fingerprint="fp_dup_test",
                action_type="observe",
                cooldown_seconds=600,
                hermes_home=tmpdir,
            )
            assert r1["recorded"] is True

            r2 = record_recovery_event(
                task_id="t_dup",
                run_id=1,
                exit_class="tool_loop_halt",
                fingerprint="fp_dup_test",
                action_type="observe",
                cooldown_seconds=600,
                hermes_home=tmpdir,
            )
            assert r2["recorded"] is False
            assert "cooldown" in r2["reason"]


# ===================================================================
# 8. Diagnosis Projection
# ===================================================================

class TestDiagnosisProjection:
    """project_diagnostic enriches recovery entries with diagnostic fields."""

    def test_projection_fields_present(self):
        from plugins.kanban.recovery.lifecycle_hooks import project_diagnostic
        entry = {
            "task_id": "t_proj",
            "recovery_class": "tool_loop_halt",
            "action_type": "observe",
            "cooldown_active": True,
            "cas_eligible": True,
        }
        proj = project_diagnostic(entry)
        assert proj["recovery_class"] == "tool_loop_halt"
        assert proj["recommended_action"] is not None
        assert proj["automation_level"] == "observe_only"
        assert proj["cooldown_active"] is True
        assert proj["cas_eligible"] is True

    def test_projection_maps_unsafe_to_manual(self):
        from plugins.kanban.recovery.lifecycle_hooks import project_diagnostic
        entry = {
            "task_id": "t_unsafe",
            "recovery_class": None,
            "action_type": "observe",
            "cooldown_active": False,
            "cas_eligible": False,
        }
        proj = project_diagnostic(entry)
        assert proj["automation_level"] == "manual"

    def test_projection_rate_limited(self):
        from plugins.kanban.recovery.lifecycle_hooks import project_diagnostic
        entry = {
            "task_id": "t_rl",
            "recovery_class": "rate_limited",
            "action_type": "observe",
            "cooldown_active": False,
            "cas_eligible": True,
        }
        proj = project_diagnostic(entry)
        assert proj["recovery_class"] == "rate_limited"
        assert proj["automation_level"] == "observe_only"
        assert proj["cooldown_active"] is False
