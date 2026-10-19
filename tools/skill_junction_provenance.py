"""Keep ``.junction-provenance.json`` in step with in-place edits to linked skills.

Background: skills that live in the shared library are exposed through links in
``<HERMES_HOME>/skills``. ``.junction-provenance.json`` records each link's real target
plus the authored-content hash of that tree, and ``scan_skill_links.py`` verifies it;
the weekly evolution health gate reports ``provenance`` when the recording goes stale.
Any deliberate edit to the library makes the recording stale, so an unmaintained
snapshot turns a correct health gate into a recurring false alarm.

This module re-binds ONE entry right after a skill mutation, and only when the change
is an in-place content edit:

* the snapshot and the entry must already exist;
* the link's real target must still be the recorded one -- a re-pointed link, a
  removed link, a link that became a real directory, or a brand-new skill is a
  STRUCTURAL change and is deliberately left alone for a human (that is exactly the
  signal the provenance gate exists to catch);
* only ``tree_sha256`` / ``skill_md_sha256`` are rewritten, atomically.

Hashes must agree with ``skills/evolution-drift-detector/scripts/scan_skill_links.py``
(``tests/tools/test_skill_junction_provenance.py`` asserts the parity), because that is
what compares them.

Everything here is best-effort: ``maybe_rebind`` never raises, so a provenance write
can never break a skill edit.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Optional

SNAPSHOT_NAME = ".junction-provenance.json"
SCHEMA_VERSION = 1

# Mirrors scan_skill_links.GENERATED_SKILL_*: bytecode and tool caches are not authored
# content, so they must not move the hash.
GENERATED_SKILL_DIR_NAMES = frozenset({"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"})
GENERATED_SKILL_FILE_NAMES = frozenset({".coverage", ".DS_Store", "Thumbs.db"})
GENERATED_SKILL_SUFFIXES = frozenset({".pyc", ".pyo"})

# Actions whose outcome is a content edit. create/delete change the set of linked
# skills itself, which is structural: the gate should still be the one to speak.
CONTENT_ACTIONS = frozenset({"patch", "edit", "write_file", "remove_file"})

_WRITE_ATTEMPTS = 3


def _strip_long_path_prefix(value: str) -> str:
    for prefix in ("\\\\?\\", "\\??\\"):
        if value.startswith(prefix):
            return value[len(prefix):]
    return value


def canonical_path(value: Any) -> str:
    """Comparable form of a path: prefix stripped, symlinks resolved, case folded."""
    raw = _strip_long_path_prefix(str(value or "")).strip()
    if not raw:
        return ""
    try:
        return os.path.normcase(os.path.realpath(os.path.abspath(raw)))
    except OSError:
        return os.path.normcase(raw)


def is_generated_skill_artifact(path: Path, directory: Path) -> bool:
    try:
        relative = path.relative_to(directory)
    except ValueError:
        return False
    return (
        any(part in GENERATED_SKILL_DIR_NAMES for part in relative.parts)
        or path.name in GENERATED_SKILL_FILE_NAMES
        or path.suffix.lower() in GENERATED_SKILL_SUFFIXES
    )


def tree_sha256(directory: Path) -> str:
    """Authored files under *directory*, hashed with POSIX relative paths and NUL separators."""
    hasher = hashlib.sha256()
    if not directory.is_dir():
        return hasher.hexdigest()
    files = [p for p in directory.rglob("*")
             if p.is_file() and not is_generated_skill_artifact(p, directory)]
    for path in sorted(files, key=lambda item: item.relative_to(directory).as_posix()):
        relative = path.relative_to(directory).as_posix()
        hasher.update(relative.encode("utf-8"))
        hasher.update(b"\0")
        hasher.update(path.read_bytes())
        hasher.update(b"\0")
    return hasher.hexdigest()


def file_sha256(path: Path) -> Optional[str]:
    if not path.is_file():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _default_skills_root() -> Path:
    # Imported lazily: this module is loaded from the skill-mutation hook, which already
    # sits inside tools/, and a module-level import would cycle back into it.
    from tools.skill_manager_tool import _skills_dir
    return Path(_skills_dir())


def rebind_entry(
    skill_name: str,
    *,
    skills_root: Optional[Path] = None,
    snapshot_path: Optional[Path] = None,
) -> dict[str, Any]:
    """Re-bind one entry's content hashes to what is on disk now. Returns a status dict."""
    root = Path(skills_root) if skills_root else _default_skills_root()
    snapshot = Path(snapshot_path) if snapshot_path else root / SNAPSHOT_NAME
    result: dict[str, Any] = {"skill": skill_name, "snapshot": str(snapshot)}

    if not snapshot.is_file():
        return {**result, "status": "skipped", "reason": "no-snapshot"}
    try:
        payload = json.loads(snapshot.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {**result, "status": "skipped", "reason": f"snapshot-unreadable:{exc}"}
    if payload.get("schema_version") != SCHEMA_VERSION or not isinstance(payload.get("entries"), list):
        return {**result, "status": "skipped", "reason": "unsupported-schema"}

    live = root / skill_name
    if not live.exists():
        return {**result, "status": "skipped", "reason": "skill-missing"}

    for attempt in range(1, _WRITE_ATTEMPTS + 1):
        entry = next((e for e in payload["entries"]
                      if isinstance(e, dict) and e.get("path") == skill_name), None)
        if entry is None:
            # A linked skill the snapshot never recorded, or a de-linked one.
            return {**result, "status": "skipped", "reason": "no-entry"}

        recorded_target = entry.get("resolved_target")
        if canonical_path(live) != canonical_path(recorded_target):
            return {**result, "status": "skipped", "reason": "target-changed",
                    "recorded_target": recorded_target, "live_target": str(live.resolve())}

        new_tree = tree_sha256(live)
        new_md = file_sha256(live / "SKILL.md")
        if entry.get("tree_sha256") == new_tree and entry.get("skill_md_sha256") == new_md:
            return {**result, "status": "skipped", "reason": "up-to-date"}

        entry["tree_sha256"] = new_tree
        entry["skill_md_sha256"] = new_md
        payload["generated_at_utc"] = datetime.now(UTC).isoformat()

        tmp = snapshot.with_name(snapshot.name + f".tmp{os.getpid()}")
        try:
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
            os.replace(tmp, snapshot)
        except OSError as exc:
            with_tmp = tmp
            try:
                with_tmp.unlink()
            except OSError:
                pass
            return {**result, "status": "failed", "reason": f"write-failed:{exc}"}

        # Another writer may have replaced the file between our read and our write; if our
        # entry is no longer on disk, redo against the fresh copy.
        try:
            check = json.loads(snapshot.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {**result, "status": "rebound", "attempt": attempt}
        still = next((e for e in check.get("entries", [])
                      if isinstance(e, dict) and e.get("path") == skill_name), None)
        if still is not None and still.get("tree_sha256") == new_tree:
            return {**result, "status": "rebound", "attempt": attempt,
                    "tree_sha256": new_tree, "skill_md_sha256": new_md}
        payload = check

    return {**result, "status": "failed", "reason": "contended"}


def rebind_for_path(path: Any) -> dict[str, Any]:
    """Re-bind the snapshot entry for the linked skill that owns *path* (best-effort).

    ``skill_manage`` cannot reach linked skills (``Path.rglob`` does not descend into a
    link), so the edits that actually stale the snapshot arrive through ``write_file`` /
    ``patch`` on a path inside the shared library. This is the hook for those.
    """
    try:
        root, name = skills_root_for_path(path)
        if root is None or name is None:
            return {"status": "skipped", "reason": "not-a-skill-path", "path": str(path)}
        return rebind_entry(name, skills_root=root)
    except Exception as exc:
        return {"status": "failed", "reason": f"{type(exc).__name__}: {exc}", "path": str(path)}


def _skills_roots() -> list[Path]:
    roots: list[Path] = []
    try:
        from agent.skill_utils import get_all_skills_dirs
        roots.extend(Path(p) for p in get_all_skills_dirs())
    except Exception:
        pass
    try:
        roots.append(_default_skills_root())
    except Exception:
        pass
    seen: set[str] = set()
    unique: list[Path] = []
    for root in roots:
        key = canonical_path(root)
        if key and key not in seen and root.is_dir():
            seen.add(key)
            unique.append(root)
    return unique


def skills_root_for_path(path: Any) -> tuple[Optional[Path], Optional[str]]:
    """``(skills_root, skill_name)`` for a path inside a skill directory, else ``(None, None)``.

    The candidate is deliberately NOT resolved: a linked skill is reached through
    ``<root>/<name>/...``, and resolving that path would report the library location instead,
    which is not under the skills root at all (learned the hard way -- the first version used
    ``resolve()`` and answered "not-a-skill-path" for every real linked skill).
    """
    raw = Path(os.path.abspath(_strip_long_path_prefix(str(path))))
    for root in _skills_roots():
        root_abs = Path(os.path.abspath(_strip_long_path_prefix(str(root))))
        relative = None
        for base in (root_abs, Path(os.path.abspath(os.path.realpath(str(root_abs))))):
            try:
                relative = raw.relative_to(base)
                root_used = base
                break
            except ValueError:
                continue
        if relative is None:
            continue
        if len(relative.parts) < 2:
            # The skills root itself, or a loose file sitting directly in it.
            return None, None
        current = raw if raw.is_dir() else raw.parent
        while True:
            if (current / "SKILL.md").is_file():
                try:
                    return root, current.relative_to(root_used).as_posix()
                except ValueError:
                    return None, None
            parent = current.parent
            if os.path.normcase(str(parent)) == os.path.normcase(str(current)):
                return None, None
            if os.path.normcase(str(parent)) == os.path.normcase(str(root_used)):
                return None, None
            current = parent
    return None, None


def maybe_rebind(skill_name: str, *, action: Optional[str] = None,
                 skills_root: Optional[Path] = None) -> dict[str, Any]:
    """Hook entry point: rebind for content edits, never raise."""
    if action is not None and action not in CONTENT_ACTIONS:
        return {"skill": skill_name, "status": "skipped", "reason": f"structural-action:{action}"}
    try:
        return rebind_entry(skill_name, skills_root=skills_root)
    except Exception as exc:
        return {"skill": skill_name, "status": "failed", "reason": f"{type(exc).__name__}: {exc}"}
