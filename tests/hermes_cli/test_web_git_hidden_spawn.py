"""web_git._run must spawn git hidden on Windows (no visible Terminal flash).

Seam: hermes_cli.web_git._run (every backend git op funnels through it).
RED first: assert subprocess.run receives creationflags=CREATE_NO_WINDOW on win32.
"""
import subprocess
import sys

import pytest

from hermes_cli import web_git


def test_run_hides_console_on_windows(monkeypatch):
    calls = {}

    def fake_run(argv, **kwargs):
        calls.update(kwargs)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(web_git.subprocess, "run", fake_run)
    monkeypatch.setattr(sys, "platform", "win32")
    web_git._run(["git", "rev-parse", "HEAD"], "C:\\", 5, {})

    assert calls.get("creationflags") == getattr(
        subprocess, "CREATE_NO_WINDOW", 0x08000000
    ), f"git spawned without CREATE_NO_WINDOW: {calls!r}"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only spawn contract")
def test_run_live_git_hidden_and_working(tmp_path):
    code, out, _ = web_git._git(str(tmp_path), ["--version"])
    assert code == 0 and "git version" in out
