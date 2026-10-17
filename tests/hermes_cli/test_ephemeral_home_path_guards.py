"""Guards against home paths that leak into the User PATH — and against a home
path that the OS resolves against whatever drive happens to be current.

Live incident (2026-10-17, this machine): the User PATH held the entry

    \\d\\Program Files (x86)\\AI\\hermes\\bin

with the drive letter missing. Windows resolves a rooted entry that has no
drive and no UNC host against the *current* directory of the current drive, so
that entry named ``D:\\d\\Program Files (x86)\\AI\\hermes\\bin``. Hermes then
took ``D:\\d\\Program Files (x86)\\AI\\hermes`` as its home, ``mkdir -p``'d a
whole home there, ran ``boot_bootstrap`` home maintenance against it (1.36 GB /
35,876 files), and ``_register_windows_user_path`` wrote the same malformed
shape back into the User PATH. ``drive_root_guard`` archived ``D:\\d`` fifteen
minutes later; the cycle had already repeated seven times (09-22, 10-01, 10-04,
10-06, 10-08, 10-09, 10-17).

Three holes, one test each:

  G1  ``_expose_windows_user_bin`` unconditionally mkdir'd + registered whatever
      home it was handed, so every pytest/temp home permanently polluted the
      User PATH (65 of 84 entries on this machine).
  G2  ``_merge_user_path`` compared entries with ``rstrip("\\\\/").casefold()``
      only, so a drive-less variant never matched the real entry and was added
      alongside it instead of being recognised or dropped.
  G3  Nothing rejected a root-relative home, so the malformed entry became a
      home in the first place.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from hermes_cli import _launchers

windows_only = pytest.mark.skipif(os.name != "nt", reason="Windows PATH semantics")


# --------------------------------------------------------------------------- G2


def test_merge_user_path_drops_rootless_entry_and_adds_the_real_one():
    """A drive-less entry is unsafe and must not survive the merge."""
    existing = r"\d\Program Files (x86)\AI\hermes\bin;C:\keep\bin"
    entry = r"D:\Program Files (x86)\AI\hermes\bin"

    merged = _launchers._merge_user_path(existing, entry)

    assert merged is not None
    parts = merged.split(";")
    assert r"\d\Program Files (x86)\AI\hermes\bin" not in parts
    assert entry in parts
    assert r"C:\keep\bin" in parts


def test_merge_user_path_returns_none_when_value_is_already_correct():
    entry = r"D:\Program Files (x86)\AI\hermes\bin"
    existing = f"{entry};C:\\keep\\bin"

    assert _launchers._merge_user_path(existing, entry) is None


def test_merge_user_path_repairs_without_duplicating_a_present_entry():
    """Entry already present, garbage also present -> keep one copy, drop garbage."""
    entry = r"D:\Program Files (x86)\AI\hermes\bin"
    existing = f"{entry};\\d\\Program Files (x86)\\AI\\hermes\\bin;C:\\keep\\bin"

    merged = _launchers._merge_user_path(existing, entry)

    assert merged is not None
    parts = merged.split(";")
    assert parts.count(entry) == 1
    assert r"\d\Program Files (x86)\AI\hermes\bin" not in parts


def test_merge_user_path_keeps_unc_and_drive_qualified_entries():
    existing = r"\\server\share\bin;D:\tools\bin"
    entry = r"D:\Program Files (x86)\AI\hermes\bin"

    merged = _launchers._merge_user_path(existing, entry)

    assert merged is not None
    parts = merged.split(";")
    assert r"\\server\share\bin" in parts
    assert r"D:\tools\bin" in parts


# --------------------------------------------------------------------------- G1


@windows_only
def test_expose_windows_user_bin_skips_an_ephemeral_home(tmp_path, monkeypatch):
    """A throwaway home must not be mkdir'd and must never reach the User PATH."""
    # Drop the pytest marker so the path shape itself has to carry the verdict.
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    ephemeral_home = tmp_path / "pytest-of-someone" / "pytest-3" / "hermes_test"
    ephemeral_home.mkdir(parents=True)
    monkeypatch.setattr("hermes_constants.get_default_hermes_root",
                        lambda **kw: ephemeral_home)

    registered: list[Path] = []
    monkeypatch.setattr(_launchers, "_register_windows_user_path",
                        lambda entry: registered.append(entry) or "added")

    result = _launchers._expose_windows_user_bin(tmp_path / "checkout", create=True)

    assert result.get("skipped") == "ephemeral-home", result
    assert registered == []
    assert not (ephemeral_home / "bin").exists()


@windows_only
def test_expose_windows_user_bin_still_registers_a_permanent_home(tmp_path, monkeypatch):
    """The guard must not disable exposure for a real home."""
    permanent_home = tmp_path / "hermes"
    permanent_home.mkdir()
    monkeypatch.setattr("hermes_constants.get_default_hermes_root",
                        lambda **kw: permanent_home)
    # The test process always looks ephemeral; simulate a real install here so the
    # wiring below the guard is what gets exercised.
    monkeypatch.setattr(_launchers, "_is_ephemeral_home", lambda home: False)

    registered: list[Path] = []
    monkeypatch.setattr(_launchers, "_register_windows_user_path",
                        lambda entry: registered.append(entry) or "added")
    monkeypatch.setattr(_launchers, "ensure_install_launchers",
                        lambda root, directory: [str(directory / n)
                                                 for n in _launchers.WINDOWS_BIN_LAUNCHERS])

    result = _launchers._expose_windows_user_bin(tmp_path / "checkout", create=True)

    assert result.get("ok") is True, result
    assert registered == [permanent_home / "bin"], registered


@windows_only
def test_is_ephemeral_home_false_for_a_real_install_home(monkeypatch):
    """No false positives: the real install home is not ephemeral."""
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)

    assert _launchers._is_ephemeral_home(Path(r"D:\Program Files (x86)\AI\hermes")) is False
    assert _launchers._is_ephemeral_home(Path(r"C:\Users\JoJo\AppData\Local\hermes")) is False


@windows_only
def test_is_ephemeral_home_true_for_temp_and_pytest_homes(monkeypatch):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)

    assert _launchers._is_ephemeral_home(Path(r"D:\tmp\hermes-build\gate\tmp\pytest-of-JoJo\pytest-30\x")) is True
    assert _launchers._is_ephemeral_home(Path(r"C:\Users\JoJo\AppData\Local\Temp\w103b-home-abc\bin")) is True


# --------------------------------------------------------------------------- G3


@windows_only
def test_normalize_hermes_home_env_drops_a_rootless_home(monkeypatch):
    """A rooted path with no drive must not be kept as HERMES_HOME."""
    from hermes_cli import _startup_fast

    monkeypatch.setenv("HERMES_HOME", r"\d\Program Files (x86)\AI\hermes")
    _startup_fast.normalize_hermes_home_env()

    assert os.environ.get("HERMES_HOME", "") != r"\d\Program Files (x86)\AI\hermes"
    assert not os.environ.get("HERMES_HOME", "").startswith("\\")


@windows_only
def test_get_default_hermes_root_rejects_a_rootless_home(monkeypatch):
    """The root resolver must never answer with a path the OS resolves per drive."""
    import hermes_constants

    monkeypatch.setenv("HERMES_HOME", r"\d\Program Files (x86)\AI\hermes")
    hermes_constants._default_hermes_root_memo = None
    try:
        root = hermes_constants.get_default_hermes_root()
        assert not str(root).startswith("\\"), root
        assert str(root) == str(hermes_constants._get_platform_default_hermes_home())
    finally:
        hermes_constants._default_hermes_root_memo = None


def test_expand_hermes_home_keeps_a_normal_absolute_home(tmp_path, monkeypatch):
    """No false positives: a real absolute home passes through untouched."""
    import hermes_constants

    home = str(tmp_path / "hermes")
    assert str(hermes_constants._expand_hermes_home(home)) == home
