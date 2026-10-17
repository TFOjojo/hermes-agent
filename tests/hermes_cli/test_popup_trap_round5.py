"""Popup-trap round 5: bare spawns in hermes_cli/source_releases.py.

Captured live 2026-10-17 16:16-16:22 under popup_trap.log with visible=true,
parent a windowless pythonw (Hermes-StableChannelWatch / desktop update check)
whose git children therefore had no console to inherit and allocated a fresh
visible one:

  git ls-remote --tags origin refs/tags/v0.22.2 refs/tags/v0.22.2^{}   x6
  git merge-base --is-ancestor <published> <head>                      x1

Every subprocess.run in that module omitted creationflags, while the module it
calls into for the same reads (hermes_cli/source_check._git_run) already passed
windows_hide_flags(). These tests pin the same hide at the remaining five seams
and add an AST invariant so a future edit cannot reintroduce a bare spawn.
"""
import ast
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

CNW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
SHA = "a" * 40
MODULE = REPO / "hermes_cli" / "source_releases.py"


def _hidden(kwargs) -> bool:
    flags = kwargs.get("creationflags", 0)
    return CNW != 0 and (flags & CNW) == CNW


class _Result:
    def __init__(self, stdout="", returncode=0):
        self.stdout = stdout
        self.stderr = ""
        self.returncode = returncode


def test_source_repository_spawn_hidden(monkeypatch):
    """git config --get remote.origin.url (line 30 seam)."""
    from hermes_cli import source_releases

    calls = []
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **k: (calls.append(k), _Result("", 1))[1],
    )
    source_releases.source_repository(["git"], cwd=REPO)
    assert calls, "expected the origin-url probe to run"
    assert all(_hidden(k) for k in calls), "source_repository git spawn must hide"


def test_head_containing_spawns_hidden(monkeypatch):
    """rev-parse / merge-base --is-ancestor (line 108 seam)."""
    from hermes_cli import source_releases

    calls = []

    def fake(*a, **k):
        calls.append(k)
        argv = a[0]
        if "merge-base" in argv:
            return _Result("", 0)
        if "--is-shallow-repository" in argv:
            return _Result("false", 0)
        return _Result(SHA, 0)

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(subprocess, "run", fake)
    head = source_releases._head_containing(["git"], REPO, SHA, "NousResearch/hermes-agent")
    assert head == SHA
    assert len(calls) >= 2, "expected rev-parse plus merge-base"
    assert all(_hidden(k) for k in calls), "_head_containing git spawns must hide"


def test_resolve_release_ls_remote_hidden(monkeypatch):
    """ls-remote --tags origin refs/tags/<tag> (line 345 seam) — the one that flashed."""
    from hermes_cli import source_releases

    calls = []
    tag = "v0.22.2"

    def fake(*a, **k):
        calls.append(k)
        argv = a[0]
        if "ls-remote" in argv:
            ref = f"refs/tags/{tag}"
            return _Result(f"{SHA}\t{ref}\n{SHA}\t{ref}^{{}}\n", 0)
        return _Result(SHA, 0)

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(subprocess, "run", fake)
    monkeypatch.setattr(source_releases, "_published_fallback", lambda *a, **k: {"tag_name": tag})
    monkeypatch.setattr(source_releases, "_published", lambda *a, **k: True)
    monkeypatch.setattr(source_releases, "_json", lambda *a, **k: {"sha": SHA})

    resolved_tag, sha = source_releases._resolve_release(
        "stable", ["git"], REPO, repository="example/fork")
    assert (resolved_tag, sha) == (tag, SHA)
    ls_calls = [k for k in calls]
    assert ls_calls, "expected the ls-remote probe to run"
    assert all(_hidden(k) for k in ls_calls), "ls-remote git spawn must hide"


def _bare_spawns(path: Path) -> list[int]:
    """Line numbers of subprocess spawns that pass neither creationflags nor **splat."""
    tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    bare = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr in
                {"run", "Popen", "check_output", "check_call", "call"}):
            continue
        value = func.value
        owner_ok = (isinstance(value, ast.Name) and value.id == "subprocess") or (
            isinstance(value, ast.Attribute) and value.attr == "subprocess")
        if not owner_ok:
            continue
        keywords = [k.arg for k in node.keywords]
        has_splat = any(k.arg is None for k in node.keywords)
        if "creationflags" not in keywords and not has_splat:
            bare.append(node.lineno)
    return bare


def test_no_bare_spawn_remains_in_source_releases():
    """Invariant: the module cannot spawn a console-subsystem child unhidden."""
    bare = _bare_spawns(MODULE)
    assert bare == [], (
        "hermes_cli/source_releases.py spawns without creationflags at "
        f"line(s) {bare} — a windowless parent (pythonw) makes each one a visible "
        "console flash; pass windows_hide_flags() from hermes_cli._subprocess_compat"
    )


def test_windows_hide_flags_is_the_canonical_helper():
    """The neighbour (source_check._git_run) already uses this helper; stay aligned."""
    from hermes_cli._subprocess_compat import windows_hide_flags

    assert windows_hide_flags() == CNW
