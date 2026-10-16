"""Popup-trap round 4: three bare spawns that flashed on 2026-10-16.

Seams (each captured with visible=true under hermes.exe kanban workers,
parent pythonw with no console to inherit):
- hermes_cli/version_info._git_version_info dirty probe
  (git --no-optional-locks status --porcelain -uno) — round 2 covered
  _run_git but missed this inline block in the same file.
- pm/environment.PythonEnvironment._run (uv venv/sync/check via subprocess.run).
- pm/environment._run_streaming (uv venv/sync via subprocess.Popen).
"""
import io
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest

REPO = Path(__file__).resolve().parents[2]
CNW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _hidden(kwargs):
    return (kwargs.get("creationflags", 0) & CNW) == CNW and CNW != 0


def test_version_info_dirty_probe_hidden(monkeypatch):
    sys.path.insert(0, str(REPO))
    from hermes_cli import version_info

    calls = []

    class R:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: (calls.append(k), R())[1]
    )
    version_info._git_version_info(REPO)
    assert calls, "expected git probes to run"
    assert all(_hidden(k) for k in calls), "all version_info git spawns must hide"


def test_pm_uv_run_hidden(monkeypatch):
    sys.path.insert(0, str(REPO))
    from pm.environment import PythonEnvironment

    calls = []

    class R:
        returncode = 0
        stdout = ""
        stderr = ""

    pe = object.__new__(PythonEnvironment)
    for key, value in {
        "uv": Path("uv"),
        "python": Path("python"),
        "destination": Path("D:/tmp/x"),
        "cache": Path("D:/tmp/c"),
        "env": {},
        "no_config": True,
        "offline": False,
        "output": None,
    }.items():
        object.__setattr__(pe, key, value)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: (calls.append(k), R())[1]
    )
    try:
        pe._run(["--version"], cwd=Path("D:/tmp"), timeout=10)
    except Exception:
        pass  # command is fake; only the spawn kwargs matter
    assert calls, "expected uv spawn to run"
    assert all(_hidden(k) for k in calls), "uv run must hide"


def test_pm_streaming_popen_hidden(monkeypatch):
    sys.path.insert(0, str(REPO))
    from pm.environment import _run_streaming

    calls = []

    class FakeStdout(io.TextIOWrapper):
        def __init__(self):
            super().__init__(io.BytesIO(b""), encoding="utf-8")

        def fileno(self):
            return 1  # _read_pipe is mocked; only isinstance/encoding/close matter

    class FakeProc:
        def __init__(self):
            self.stdout = FakeStdout()

        def wait(self, timeout=None):
            return 0

        def kill(self):
            pass

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda *a, **k: (calls.append(k), FakeProc())[1],
    )
    monkeypatch.setattr("pm.environment._read_pipe", lambda fd: b"")
    result = _run_streaming(
        ["uv", "--version"], cwd=Path("D:/tmp"), env={}, timeout=10,
        output=io.StringIO(),
    )
    assert result.returncode == 0
    assert calls, "expected streaming Popen to run"
    assert all(_hidden(k) for k in calls), "streaming Popen must hide"
