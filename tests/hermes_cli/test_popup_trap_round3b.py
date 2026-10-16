"""Round-3b: the main CLI workspace git probe must stay hidden on Windows."""
import subprocess

from hermes_cli import main


def test_workspace_git_probe_hides_console(monkeypatch):
    calls = {}

    def fake_run(argv, **kwargs):
        calls.update(kwargs)
        return subprocess.CompletedProcess(argv, 0, "C:\\repo\n", "")

    monkeypatch.setattr(main.subprocess, "run", fake_run)
    monkeypatch.setattr(main.os.path, "abspath", lambda value: value)
    monkeypatch.setattr(main.os, "getcwd", lambda: "C:\\repo")
    main._resolve_workspace_key()
    assert calls.get("creationflags") == getattr(
        subprocess, "CREATE_NO_WINDOW", 0x08000000
    ), calls
