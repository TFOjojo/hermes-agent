"""Tests for tools.skill_junction_provenance: the linked-skill snapshot auto-rebind.

The seam under test is narrow but load-bearing: a stale provenance snapshot turns the
weekly evolution health gate into a permanent false alarm, so the rebind must happen for
in-place content edits and must NOT happen for structural changes (re-pointed link,
removed link, brand-new skill) -- those are exactly what the gate exists to report.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from tools import skill_junction_provenance as sjp
from tools import skill_manager_tool as smt


def _make_link(link: Path, target: Path) -> bool:
    """Directory link (symlink, else a Windows junction). False when the OS refuses."""
    try:
        os.symlink(target, link, target_is_directory=True)
        return True
    except (OSError, NotImplementedError):
        pass
    if os.name == "nt":
        result = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                                capture_output=True, text=True, check=False)
        return result.returncode == 0 and link.exists()
    return False


@pytest.fixture()
def linked_skill(tmp_path):
    target = tmp_path / "library" / "demo-skill"
    (target / "references").mkdir(parents=True)
    (target / "SKILL.md").write_text("---\nname: demo-skill\n---\nbody v1\n", encoding="utf-8")
    (target / "references" / "note.md").write_text("v1\n", encoding="utf-8")

    root = tmp_path / "skills"
    root.mkdir()
    link = root / "demo-skill"
    if not _make_link(link, target):
        pytest.skip("cannot create a directory link here")

    snapshot = root / sjp.SNAPSHOT_NAME
    snapshot.write_text(json.dumps({
        "schema_version": 1,
        "generated_at_utc": "2026-01-01T00:00:00+00:00",
        "root": str(root),
        "entries": [{
            "path": "demo-skill",
            "resolved_target": str(target),
            "tree_sha256": "0" * 64,
            "skill_md_sha256": "0" * 64,
        }],
    }), encoding="utf-8")
    return root, snapshot, target, link


def _entry(snapshot: Path, name: str = "demo-skill") -> dict:
    payload = json.loads(snapshot.read_text(encoding="utf-8"))
    return next(e for e in payload["entries"] if e["path"] == name)


def test_rebinds_after_in_place_edit(linked_skill):
    root, snapshot, target, link = linked_skill
    (target / "references" / "note.md").write_text("v2 changed\n", encoding="utf-8")

    result = sjp.rebind_entry("demo-skill", skills_root=root)

    assert result["status"] == "rebound", result
    entry = _entry(snapshot)
    assert entry["tree_sha256"] == sjp.tree_sha256(link)
    assert entry["skill_md_sha256"] == sjp.file_sha256(link / "SKILL.md")
    assert entry["tree_sha256"] != "0" * 64
    # generated_at_utc moves with the rebind so the file records when it was re-bound.
    assert json.loads(snapshot.read_text(encoding="utf-8"))["generated_at_utc"] != "2026-01-01T00:00:00+00:00"


def test_second_call_is_a_no_op(linked_skill):
    root, _snapshot, target, _link = linked_skill
    (target / "references" / "note.md").write_text("v2\n", encoding="utf-8")
    assert sjp.rebind_entry("demo-skill", skills_root=root)["status"] == "rebound"

    again = sjp.rebind_entry("demo-skill", skills_root=root)

    assert again["status"] == "skipped"
    assert again["reason"] == "up-to-date"


def test_repointed_target_is_left_for_a_human(linked_skill):
    root, snapshot, _target, _link = linked_skill
    payload = json.loads(snapshot.read_text(encoding="utf-8"))
    payload["entries"][0]["resolved_target"] = str(root.parent / "somewhere-else")
    before = json.dumps(payload, sort_keys=True)
    snapshot.write_text(json.dumps(payload), encoding="utf-8")

    result = sjp.rebind_entry("demo-skill", skills_root=root)

    assert result["status"] == "skipped"
    assert result["reason"] == "target-changed"
    # Untouched: a structural change must stay visible to the provenance gate.
    assert json.dumps(json.loads(snapshot.read_text(encoding="utf-8")), sort_keys=True) == before


def test_real_directory_is_not_a_linked_skill(tmp_path):
    root = tmp_path / "skills"
    (root / "demo-skill").mkdir(parents=True)
    (root / "demo-skill" / "SKILL.md").write_text("---\nname: demo-skill\n---\n", encoding="utf-8")
    (root / sjp.SNAPSHOT_NAME).write_text(json.dumps({
        "schema_version": 1,
        "entries": [{"path": "demo-skill",
                     "resolved_target": str(tmp_path / "library" / "demo-skill"),
                     "tree_sha256": "0" * 64, "skill_md_sha256": "0" * 64}],
    }), encoding="utf-8")

    result = sjp.rebind_entry("demo-skill", skills_root=root)

    assert result["status"] == "skipped"
    assert result["reason"] == "target-changed"


def test_no_snapshot_is_a_no_op(tmp_path):
    root = tmp_path / "skills"
    (root / "demo-skill").mkdir(parents=True)
    assert sjp.rebind_entry("demo-skill", skills_root=root)["reason"] == "no-snapshot"


def test_no_entry_is_a_no_op(linked_skill):
    root, _snapshot, _target, _link = linked_skill
    assert sjp.rebind_entry("another-skill", skills_root=root)["reason"] == "skill-missing"

    unrecorded = root / "unrecorded"
    if not _make_link(unrecorded, _target):
        pytest.skip("cannot create a directory link here")
    assert sjp.rebind_entry("unrecorded", skills_root=root)["reason"] == "no-entry"


def test_structural_actions_never_rebind(linked_skill):
    root, _snapshot, _target, _link = linked_skill
    for action in ("create", "delete"):
        result = sjp.maybe_rebind("demo-skill", action=action, skills_root=root)
        assert result["status"] == "skipped"
        assert result["reason"] == f"structural-action:{action}"


def test_maybe_rebind_swallows_failures(monkeypatch, linked_skill):
    root, _snapshot, _target, _link = linked_skill

    def boom(*_a, **_k):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(sjp, "rebind_entry", boom)

    result = sjp.maybe_rebind("demo-skill", action="patch", skills_root=root)

    assert result["status"] == "failed"
    assert "disk on fire" in result["reason"]


def test_tree_hash_matches_known_answer(tmp_path):
    """Pin the hash so a refactor cannot silently stop matching the scanner's recording.

    Byte-for-byte equality with ``scan_skill_links.py`` is asserted by the live verifier
    (``integration/verifiers/skill_junction_provenance.py``), because tests may not read the
    real Hermes home; this KAT catches a change on our side without touching it.
    """
    tree = tmp_path / "skill"
    (tree / "references").mkdir(parents=True)
    (tree / "SKILL.md").write_text("---\nname: parity\n---\nbody\n", encoding="utf-8")
    (tree / "references" / "a.md").write_text("alpha\n", encoding="utf-8")
    (tree / "__pycache__").mkdir()
    (tree / "__pycache__" / "junk.cpython-311.pyc").write_bytes(b"\x00\x01")
    (tree / ".coverage").write_text("noise", encoding="utf-8")

    assert sjp.tree_sha256(tree) == (
        "cd3dd76cb032ead22644624a348267f8d116fd2fa78bcff583a7460b7bff4f83")
    assert sjp.file_sha256(tree / "SKILL.md") == (
        "877e52699735b6b6b07c69ecfba51390752c5f8a2d37d2a0c401b43385d278f1")


def test_generated_artifacts_do_not_move_the_hash(tmp_path):
    """Bytecode and caches are not authored content: regenerating them must not re-bind."""
    tree = tmp_path / "skill"
    tree.mkdir()
    (tree / "SKILL.md").write_text("---\nname: gen\n---\n", encoding="utf-8")
    baseline = sjp.tree_sha256(tree)

    (tree / "__pycache__").mkdir()
    (tree / "__pycache__" / "x.cpython-311.pyc").write_bytes(b"\x00")
    (tree / ".coverage").write_text("noise", encoding="utf-8")

    assert sjp.tree_sha256(tree) == baseline


def test_skills_root_for_path_resolves_nested_skills(tmp_path, monkeypatch):
    """A direct write inside a skill dir must resolve to that skill (nested/category aware)."""
    root = tmp_path / "skills"
    skill = root / "devops" / "demo"
    (skill / "references").mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: demo\n---\n", encoding="utf-8")
    monkeypatch.setattr(sjp, "_skills_roots", lambda: [root])

    assert sjp.skills_root_for_path(skill / "references" / "a.md") == (root, "devops/demo")
    assert sjp.skills_root_for_path(skill) == (root, "devops/demo")
    # A loose file at the root, and anything outside, is not a skill path.
    assert sjp.skills_root_for_path(root / "loose.md") == (None, None)
    assert sjp.skills_root_for_path(tmp_path / "elsewhere.md") == (None, None)


def test_rebind_for_path_handles_a_direct_edit(linked_skill, monkeypatch):
    """The write_file/patch hook: a direct file edit inside a linked skill re-binds it."""
    root, snapshot, _target, link = linked_skill
    monkeypatch.setattr(sjp, "_skills_roots", lambda: [root])

    touched = link / "references" / "note.md"
    touched.write_text("direct edit\n", encoding="utf-8")

    result = sjp.rebind_for_path(touched)

    assert result["status"] == "rebound", result
    assert _entry(snapshot)["tree_sha256"] == sjp.tree_sha256(link)


def test_rebind_for_path_outside_a_skill_is_a_no_op(tmp_path, monkeypatch):
    monkeypatch.setattr(sjp, "_skills_roots", lambda: [tmp_path / "skills"])
    (tmp_path / "skills").mkdir()

    result = sjp.rebind_for_path(tmp_path / "somewhere.txt")

    assert result["status"] == "skipped"
    assert result["reason"] == "not-a-skill-path"


def test_record_success_calls_the_rebind_hook(monkeypatch):
    """The seam: every successful content mutation funnels through _record_success."""
    calls: list[tuple] = []
    monkeypatch.setattr(sjp, "maybe_rebind",
                        lambda name, action=None, **kw: calls.append((name, action)))
    monkeypatch.setattr(smt, "_find_skill", lambda name: None)
    monkeypatch.setattr("tools.skill_ledger.record_mutation", lambda *a, **k: None, raising=False)
    monkeypatch.setattr("agent.prompt_builder.clear_skills_system_prompt_cache",
                        lambda **k: None, raising=False)
    monkeypatch.setattr("tools.skill_usage.bump_patch", lambda *a, **k: None, raising=False)
    monkeypatch.setattr("tools.skill_usage.forget", lambda *a, **k: None, raising=False)
    monkeypatch.setattr("tools.skill_usage.record_created", lambda *a, **k: None, raising=False)
    monkeypatch.setattr("tools.skill_provenance.is_background_review", lambda: False, raising=False)

    smt._record_success("patch", "demo-skill", {}, file_path=None, absorbed_into=None,
                        task_id=None, session_id=None, ledger_before=None)

    assert ("demo-skill", "patch") in calls
