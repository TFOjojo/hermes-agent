"""pm/client._request must spawn the worker hidden on Windows.

Seam: subprocess.Popen in pm/client.py::_request (every PM mutation funnels
through it). RED first: assert Popen receives
creationflags=CREATE_NO_WINDOW on win32 — otherwise a windowless parent
(desktop/serve pythonw) gives the console-subsystem worker its own VISIBLE
console (2026-10-12 popup-trap capture: pm-runtime python.exe with
visible=true under the desktop serve chain).
"""
import subprocess
import sys

import pytest

from pm import client


def test_request_hides_worker_console_on_windows(monkeypatch):
    calls = {}

    class FakeProc:
        stdin = None
        stdout = None

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_popen(cmd, **kwargs):
        calls.update(kwargs)
        return FakeProc()

    monkeypatch.setattr(client.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(sys, "platform", "win32")
    # Bypass worker-command resolution (it can refuse before spawning);
    # the spawn contract is what this test owns.
    monkeypatch.setattr(client, "_worker_command",
                        lambda *a, **k: [sys.executable, "-c", "pass"])
    monkeypatch.setattr(client, "runtime_environment", lambda: {})
    import pm.registry as reg

    monkeypatch.setattr(reg, "package_definitions", lambda names: [])
    # _request needs OPERATIONS[op]; prune_cache exists in worker_operations.
    import pm.worker_operations as wo

    op = next(iter(wo.OPERATIONS))
    with pytest.raises(Exception):
        # The fake proc has no stdin/stdout; _request asserts non-None.
        # The spawn contract (creationflags) is captured before that.
        client._request(op, {"cache": None, "name": "x", "names": ["x"]})

    assert calls.get("creationflags") == getattr(
        subprocess, "CREATE_NO_WINDOW", 0x08000000
    ), f"worker spawned without CREATE_NO_WINDOW: {calls!r}"
