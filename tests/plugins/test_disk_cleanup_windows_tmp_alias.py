"""Windows has no POSIX ``/tmp`` — a drive-root ``\\tmp`` is an ordinary user tree.

``Path("/tmp")`` on Windows resolves to ``<current drive>:\\tmp``. The bundled
disk-cleanup plugin grants a ``/tmp/hermes-*`` exemption (POSIX scratch, #98854)
and deletes ``test_*`` files there with ``age >= 0`` — so on Windows every
``D:\\tmp\\hermes-<anything>`` working tree silently lost its ``test_*.py`` files
the moment they were tracked. Reproduced 2026-10-22 through 10-23 on this host
(six DELETED records in ``disk-cleanup/cleanup.log`` for
``D:\\tmp\\hermes-repair-20261022-172834``).

These tests pin the two sides of the contract: the POSIX exemption must keep
working where ``/tmp`` really is POSIX, and must not treat a Windows drive-root
``\\tmp`` as disposable scratch.
"""

import importlib
import os
import sys
from pathlib import Path

import pytest


def _load_lib():
    """Load the bundled plugin's library module fresh (same pattern as the main suite)."""
    import hermes_constants  # noqa: F401  (plugin imports it at module scope)

    spec = importlib.util.spec_from_file_location(
        "disk_cleanup_windows_tmp_alias_test",
        Path(__file__).resolve().parents[2] / "plugins" / "disk-cleanup" / "disk_cleanup.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def _isolate_env(tmp_path, monkeypatch):
    """Keep HERMES_HOME out of the real home so nothing here can touch production."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    importlib.invalidate_caches()
    yield tmp_path


def test_drive_root_tmp_is_not_disposable_posix_tmp(_isolate_env):
    """``<drive>:\\tmp\\hermes-x`` must not inherit the POSIX ``/tmp/hermes-*`` exemption."""
    dg = _load_lib()
    candidate = Path("/tmp/hermes-not-posix-scratch/file.txt")
    if os.name == "nt":
        assert dg.is_safe_path(candidate) is False
        assert dg.guess_category(candidate) is None
    else:
        assert dg.is_safe_path(candidate) is True


@pytest.mark.skipif(os.name == "nt", reason="POSIX /tmp contract — runs on Linux/macOS CI")
def test_posix_tmp_hermes_exemption_still_works(_isolate_env, tmp_path):
    """The POSIX exemption must survive the Windows guard.

    Deliberately NOT exercised on Windows by patching ``os.name``: ``pathlib`` picks
    ``WindowsPath``/``PosixPath`` from that same flag, so the patch makes pytest itself
    raise ``NotImplementedError: cannot instantiate 'PosixPath'`` (INTERNALERROR). The
    non-narrowing contract for this host is covered by
    ``test_hermes_home_is_still_safe_on_every_platform``.
    """
    dg = _load_lib()
    candidate = Path("/tmp") / "hermes-disk-cleanup-contract" / "test_scratch.py"
    assert dg.is_safe_path(candidate) is True
    assert dg.guess_category(candidate) == "test"


def test_hermes_home_is_still_safe_on_every_platform(_isolate_env, tmp_path):
    """The fix must not narrow the primary (HERMES_HOME) branch."""
    dg = _load_lib()
    inside = Path(os.environ["HERMES_HOME"]) / "scratch" / "test_report.py"
    assert dg.is_safe_path(inside) is True
    assert dg.guess_category(inside) == "test"
