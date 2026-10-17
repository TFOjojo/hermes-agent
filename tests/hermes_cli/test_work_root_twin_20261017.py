"""Twin-resolution regression tests for the two root guards.

2026-10-17 incident: `D:\\work` held THREE names that render as
"Hermes workspace" — one hidden junction plus two real dirs differing only in
where the ZWNJ (U+200C) sits:

    "Hermes \\u200cworkspace"   <- canonical (ZWNJ after the SPACE)
    "Hermes\\u200c workspace"   <- stray twin  (ZWNJ after "Hermes")

Both guards used a last-one-wins iterdir scan, so the stray silently won and
17 days of `盘根-*` / `根目录-*` archives (37.8 GB) landed in it. These tests
pin the selection to exact code points and require twins to be reported.
"""
import os
import sys
import unicodedata
from pathlib import Path

import pytest

SCRIPTS = Path(r"D:\Program Files (x86)\AI\hermes\scripts")
CANON = "Hermes \u200cworkspace"
STRAY = "Hermes\u200c workspace"


def _load(name):
    sys.path.insert(0, str(SCRIPTS))
    return __import__(name)


def test_work_guard_selects_canonical_by_codepoint():
    guard = _load("work_root_guard")
    chosen = guard.find_hermes_ws()
    assert chosen.name == guard.CANONICAL_WS_NAME, (
        f"work_root_guard picked {chosen.name.encode('unicode_escape')!r}"
    )
    assert chosen.name == CANON


def test_drive_guard_selects_canonical_by_codepoint():
    guard = _load("drive_root_guard")
    chosen = guard.find_hermes_ws()
    assert chosen.name == guard.CANONICAL_WS_NAME, (
        f"drive_root_guard picked {chosen.name.encode('unicode_escape')!r}"
    )
    assert chosen.name == CANON


def test_canonical_constant_is_a_real_zwnj():
    """The constant must hold an actual U+200C, never a literal backslash-u."""
    for mod in ("work_root_guard", "drive_root_guard"):
        guard = _load(mod)
        assert "\u200c" in guard.CANONICAL_WS_NAME, f"{mod}: constant lost the ZWNJ"
        assert "\\u200c" not in guard.CANONICAL_WS_NAME, (
            f"{mod}: constant holds a literal escape, not the character "
            "(this is the 2026-09-22 'moves land nowhere' bug)"
        )


def test_work_guard_reports_twins():
    guard = _load("work_root_guard")
    assert hasattr(guard, "find_twins")
    twins = [t.name for t in guard.find_twins()]
    for name in twins:
        assert name != CANON
        assert "".join(c for c in name if unicodedata.category(c) != "Cf") == "Hermes workspace"


def test_guards_never_target_a_junction_for_archiving():
    """Archiving through the hidden junction is legal but must resolve to real."""
    for mod in ("work_root_guard", "drive_root_guard"):
        guard = _load(mod)
        target = guard.find_hermes_ws()
        assert os.path.isdir(target)
        # The canonical dir is the real one, not the hidden plain-name junction.
        assert not guard.is_reparse(target)
