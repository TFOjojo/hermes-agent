"""Popup-trap round 2: four more bare spawns must stay hidden on Windows.

Seams (each captured with visible=true on 2026-10-12):
  - scripts/branch_drift_sentinel.git + worktree check (scheduled task)
  - hermes_cli/version_info._run_git (describe/diff-index/show probes)
  - pm/runtime._validate + pm/runtime_stage (packaging probe)
  - hermes_cli/git_credentials.run_git_with_credential_fallback (plugin git)
"""
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]


def _capture(monkeypatch, module, run_attr="run"):
    calls = {}

    def fake_run(argv, **kwargs):
        calls.update(kwargs)
        return subprocess.CompletedProcess(argv, 0, "main\n", "")

    monkeypatch.setattr(module, run_attr, fake_run)
    monkeypatch.setattr(sys, "platform", "win32")
    return calls


def test_sentinel_git_hidden(monkeypatch):
    sys.path.insert(0, str(Path(r"D:\Program Files (x86)\AI\hermes\scripts")))
    import branch_drift_sentinel as s

    calls = _capture(monkeypatch, s.subprocess)
    s.git("branch", "--show-current")
    assert calls.get("creationflags") == getattr(
        subprocess, "CREATE_NO_WINDOW", 0x08000000), calls


def test_version_info_git_hidden(monkeypatch):
    from hermes_cli import version_info as vi

    calls = _capture(monkeypatch, vi.subprocess)
    vi._run_git(REPO, "describe", "--tags", "--always")
    assert calls.get("creationflags") == getattr(
        subprocess, "CREATE_NO_WINDOW", 0x08000000), calls


def test_pm_runtime_validate_hidden(monkeypatch):
    from pm import runtime as rt

    calls = _capture(monkeypatch, rt.subprocess)
    rt._validate(Path(sys.executable), {})
    assert calls.get("creationflags") == getattr(
        subprocess, "CREATE_NO_WINDOW", 0x08000000), calls


def test_plugin_git_hidden(monkeypatch):
    from hermes_cli import git_credentials as gc

    calls = _capture(monkeypatch, gc.subprocess)
    gc.run_git_with_credential_fallback(
        ["git", "--version"], "", env={},
        capture_output=True, text=True, timeout=15)
    assert calls.get("creationflags") == getattr(
        subprocess, "CREATE_NO_WINDOW", 0x08000000), calls
