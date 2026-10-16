"""Guard: kanban regression tests must open the DB through the live API.

Regression guard for a real drift: the kanban-db module split moved the
connection helper out of ``hermes_cli.kanban_db`` into
``hermes_cli.kanban_db_connect``. One governance test kept calling the removed
``kb.connect()`` and went red for an ``AttributeError``, which reads as a
broken occupancy gate while the governance code itself is intact.

Fails when a kanban test references ``connect`` on the ``kanban_db`` namespace
(after the split it no longer exists there) instead of the supported
``kanban_db_connect`` entry points.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parents[1] / "hermes_cli"
SPLIT_MARKER = "split into connect/workspace/dispatch/notify modules"


def _kanban_db_source() -> str:
    from hermes_cli import kanban_db
    return Path(kanban_db.__file__).read_text(encoding="utf-8")


def _connect_lives_on_kanban_db() -> bool:
    """True only on pre-split trees, where kb.connect was the real entry."""
    from hermes_cli import kanban_db
    return hasattr(kanban_db, "connect")


def test_split_modules_are_imported_by_kanban_db():
    """The connect entry must be re-exported/maintained by kanban_db_connect."""
    from hermes_cli import kanban_db_connect
    assert callable(getattr(kanban_db_connect, "connect", None))
    assert callable(getattr(kanban_db_connect, "connect_closing", None))


def test_occupancy_governance_api_is_intact():
    """The governance surface the failing tests exercise must still exist."""
    from hermes_cli import kanban_db as kb
    for name in (
        "claim_task",
        "claim_review_task",
        "complete_task",
        "create_task",
        "get_task",
        "list_events",
        "request_review",
        "write_txn",
        "_normalised_occupancy_path",
    ):
        assert callable(getattr(kb, name, None)), f"kanban_db.{name} missing"


@pytest.mark.skipif(
    _connect_lives_on_kanban_db(),
    reason="pre-split tree: kb.connect is the supported entry, no drift",
)
@pytest.mark.parametrize("test_file", sorted(
    p for p in TESTS_DIR.glob("test_kanban_*.py") if p.is_file()
))
def test_no_test_uses_the_removed_kb_connect(test_file: Path):
    text = test_file.read_text(encoding="utf-8")
    offenders = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        # Only executable code counts. A docstring or comment that MENTIONS
        # ``kb.connect()`` (this very file does) is documentation, not a call,
        # and the guard must not flag its own prose.
        if stripped.startswith("#") or _in_docstring(text, lineno):
            continue
        if re.search(r"\bkb\.connect\s*\(", stripped):
            offenders.append(f"{test_file.name}:{lineno}: {stripped}")
    assert not offenders, (
        "these call the removed ``kb.connect``; the kanban-db split moved it to "
        "``kanban_db_connect.connect`` / ``connect_closing`` (see "
        f"{SPLIT_MARKER}), so use that module instead:\n  "
        + "\n  ".join(offenders)
    )


def _in_docstring(text: str, lineno: int) -> bool:
    """True when line *lineno* falls inside a triple-quoted docstring."""
    import ast
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return False
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            first = getattr(node, "lineno", None)
            last = getattr(node, "end_lineno", None)
            if first is not None and last is not None and first <= lineno <= last:
                return True
    return False
