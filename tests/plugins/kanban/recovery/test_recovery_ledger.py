"""Phase 2 recovery ledger tests: append/dedup/cooldown."""

from __future__ import annotations

import time
import sys
import os

# Ensure the hermes-agent root is on sys.path so the plugin can be imported.
_HERMES_AGENT_ROOT = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "..")
)
if _HERMES_AGENT_ROOT not in sys.path:
    sys.path.insert(0, _HERMES_AGENT_ROOT)

from plugins.kanban.recovery import (
    record_recovery_event,
    is_cooling_down,
    read_ledger,
    clear_cooldown,
)


def _event_kwargs(**overrides):
    defaults = dict(
        task_id="t_test00",
        run_id=42,
        exit_class="tool_loop_halt",
        fingerprint="abc123def4567890",
        action_type="observe",
    )
    defaults.update(overrides)
    return defaults


def test_record_creates_ledger_entry(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    result = record_recovery_event(**_event_kwargs())
    assert result["recorded"] is True
    assert result["reason"] == "recorded"
    assert len(result["entry_key"]) == 16

    ledger_entries = read_ledger(hermes_home=str(tmp_path))
    assert len(ledger_entries) == 1
    assert ledger_entries[0]["task_id"] == "t_test00"
    assert ledger_entries[0]["run_id"] == 42
    assert ledger_entries[0]["exit_class"] == "tool_loop_halt"
    assert ledger_entries[0]["fingerprint"] == "abc123def4567890"
    assert ledger_entries[0]["action_type"] == "observe"


def test_dedup_drops_duplicate_within_cooldown(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    r1 = record_recovery_event(**_event_kwargs(), cooldown_seconds=600)
    assert r1["recorded"] is True

    r2 = record_recovery_event(**_event_kwargs(), cooldown_seconds=600)
    assert r2["recorded"] is False
    assert "cooldown_active" in r2["reason"]

    entries = read_ledger(hermes_home=str(tmp_path))
    assert len(entries) == 1  # Only one entry written


def test_different_fingerprint_not_deduped(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    r1 = record_recovery_event(
        **_event_kwargs(fingerprint="aaa111"),
        cooldown_seconds=600,
    )
    assert r1["recorded"] is True

    r2 = record_recovery_event(
        **_event_kwargs(fingerprint="bbb222"),
        cooldown_seconds=600,
    )
    assert r2["recorded"] is True

    entries = read_ledger(hermes_home=str(tmp_path))
    assert len(entries) == 2


def test_different_action_type_not_deduped(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    r1 = record_recovery_event(
        **_event_kwargs(action_type="observe"),
        cooldown_seconds=600,
    )
    r2 = record_recovery_event(
        **_event_kwargs(action_type="retry"),
        cooldown_seconds=600,
    )
    assert r1["recorded"] is True
    assert r2["recorded"] is True
    entries = read_ledger(hermes_home=str(tmp_path))
    assert len(entries) == 2


def test_cooldown_expires_allows_rerecord(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    record_recovery_event(**_event_kwargs(), cooldown_seconds=0)
    # cooldown=0 means next call is not deduped
    r = record_recovery_event(**_event_kwargs(), cooldown_seconds=0)
    assert r["recorded"] is True


def test_is_cooling_down_true_when_recent(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    record_recovery_event(**_event_kwargs(), cooldown_seconds=600)
    assert is_cooling_down(**_event_kwargs(), cooldown_seconds=600) is True


def test_is_cooling_down_false_when_no_entry(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    assert is_cooling_down(**_event_kwargs(), cooldown_seconds=600) is False


def test_is_cooling_down_false_after_expiry(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    record_recovery_event(**_event_kwargs(), cooldown_seconds=0)
    assert is_cooling_down(**_event_kwargs(), cooldown_seconds=0) is False


def test_clear_cooldown_removes_entry(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    record_recovery_event(**_event_kwargs(), cooldown_seconds=600)
    assert is_cooling_down(**_event_kwargs(), cooldown_seconds=600) is True

    cleared = clear_cooldown(**_event_kwargs(), hermes_home=str(tmp_path))
    assert cleared is True
    assert is_cooling_down(**_event_kwargs(), cooldown_seconds=600) is False


def test_clear_cooldown_returns_false_when_not_found(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    cleared = clear_cooldown(**_event_kwargs(), hermes_home=str(tmp_path))
    assert cleared is False


def test_read_ledger_filter_by_task_id(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    record_recovery_event(**_event_kwargs(task_id="t_aaa"), cooldown_seconds=0)
    record_recovery_event(**_event_kwargs(task_id="t_bbb"), cooldown_seconds=0)
    record_recovery_event(**_event_kwargs(task_id="t_aaa"), cooldown_seconds=0)

    all_entries = read_ledger(hermes_home=str(tmp_path))
    assert len(all_entries) == 3

    filtered = read_ledger(hermes_home=str(tmp_path), task_id="t_aaa")
    assert len(filtered) == 2
    assert all(e["task_id"] == "t_aaa" for e in filtered)


def test_read_ledger_respects_limit(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    for i in range(5):
        record_recovery_event(
            **_event_kwargs(task_id=f"t_{i}"), cooldown_seconds=0,
        )
    entries = read_ledger(hermes_home=str(tmp_path), limit=2)
    assert len(entries) == 2
