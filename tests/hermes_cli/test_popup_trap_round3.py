"""Popup-trap round 3: the spawn sites captured visible=true on 2026-10-13.

Captures (PseudoConsoleWindow, visible=true, all under the pythonw serve tree):
  1. docker.exe version  <- run_capture / _run_ok (backend probes)
  2. git.exe rev-parse   <- boot_bootstrap.read_git_head
  3. grep.exe / cmd.exe  <- execute_code kernel cell children
     (kernel runner = pythonw -> bare console grandchildren allocate
      their own VISIBLE console)

Fixes under test:
  A. tools/environments/remote_common.run_capture  -> CREATE_NO_WINDOW
  B. hermes_cli/boot_bootstrap.read_git_head       -> CREATE_NO_WINDOW
  C. hermes_cli/doctor_tools._run_ok               -> CREATE_NO_WINDOW
  D. tools/code_execution_env._prefer_console_python: kernel child swaps
     pythonw.exe -> sibling python.exe so the kernel owns a HIDDEN console
     and every bare cell-grandchild inherits it (decisive A/B: GUI runner's
     bare children get visible consoles; console runner + CNW -> zero).
"""
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]


def _capture(monkeypatch, module):
    calls = {}

    def fake_run(argv, **kwargs):
        calls.update(kwargs)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(module, "subprocess", _SubprocessProxy(fake_run, module))
    return calls


class _SubprocessProxy:
    """Proxy that keeps subprocess constants but captures run()."""

    def __init__(self, fake_run, module):
        self._fake = fake_run
        self._real = module.subprocess if hasattr(module, "subprocess") else subprocess
        self.CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        self.DEVNULL = subprocess.DEVNULL
        self.STARTUPINFO = subprocess.STARTUPINFO
        self.STARTF_USESHOWWINDOW = subprocess.STARTF_USESHOWWINDOW
        self.SW_HIDE = subprocess.SW_HIDE
        self.TimeoutExpired = subprocess.TimeoutExpired
        self.SubprocessError = subprocess.SubprocessError
        self.CompletedProcess = subprocess.CompletedProcess

    def run(self, argv, **kwargs):
        return self._fake(argv, **kwargs)


CNW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)


def test_run_capture_hidden(monkeypatch):
    from tools.environments import remote_common as rc

    calls = _capture(monkeypatch, rc)
    rc.run_capture(["docker", "version"], timeout=5)
    assert calls.get("creationflags") == CNW, calls


def test_boot_bootstrap_git_hidden(monkeypatch):
    from hermes_cli import boot_bootstrap as bb

    calls = _capture(monkeypatch, bb)
    monkeypatch.setattr(bb, "_git_binary", lambda: "git")
    bb.read_git_head(REPO)
    assert calls.get("creationflags") == CNW, calls


def test_doctor_run_ok_hidden(monkeypatch):
    from hermes_cli import doctor_tools as dt

    calls = _capture(monkeypatch, dt)
    dt._run_ok(["docker", "version"], timeout=5)
    assert calls.get("creationflags") == CNW, calls


def test_kernel_child_prefers_console_python():
    """pythonw.exe child swaps to its python.exe sibling (hidden console)."""
    from tools.code_execution_env import _prefer_console_python

    pyw = Path(sys.executable).with_name("pythonw.exe")
    if not pyw.is_file():
        pytest.skip("no pythonw sibling next to the test interpreter")
    got = _prefer_console_python(str(pyw))
    assert got.lower().endswith("python.exe"), got
    assert not got.lower().endswith("pythonw.exe"), got
