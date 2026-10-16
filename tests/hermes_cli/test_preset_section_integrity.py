# -*- coding: utf-8 -*-
"""G2 单元测试：preset section 必须指向真实存在的 GGUF，且 staged 之外不得残留。

失败场景（今天生产 preset 就在发生的）：
  presets.ini 有一个 [Ternary-Bonsai-2-27B-mmproj-Q8_0] section，
  其 model = <models>/Ternary-Bonsai-2-27B-mmproj-Q8_0.gguf —— 该文件不存在。
  router 把它注册为可选模型，一点就 500。

预期新行为：
  - generate_presets() 只为 staged_in() 返回的文件写 section（现有契约，需断言）
  - 新增一致性校验：读回 preset 时，任何 section 的 model 路径不存在 => 视为失效，
    必须能被 detect_preset_drift() 报出来（G2 新函数）
  - _presets_stale() 之外的另一半：orphan section 检测
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli.local_runtime.presets import (
    detect_preset_drift,
    generate_presets,
    read_preset_decisions,
)


@pytest.fixture
def models_dir(tmp_path):
    d = tmp_path / "models"
    d.mkdir()
    return d


def _gguf(path: Path, name: str) -> Path:
    """A real, readable GGUF header stub so preset_for_model() can price it."""
    p = path / name
    p.write_bytes(b"GGUF" + b"\x00" * 200)
    return p


def test_generated_sections_all_point_at_real_files(models_dir, tmp_path):
    """Existing contract: every section the generator writes must reference a
    file that exists at generation time. Catches hand-added/drift sections."""
    _gguf(models_dir, "Real-Model-Q4_K_M.gguf")
    preset = tmp_path / "presets.ini"

    from hermes_cli.local_runtime.estimator import HardwareBudget
    budget = HardwareBudget(usable_vram_bytes=24 * 1024**3,
                            total_device_bytes=24 * 1024**3,
                            ram_available_bytes=32 * 1024**3)

    generate_presets(models_dir, budget, preset)

    text = preset.read_text(encoding="utf-8")
    decisions = read_preset_decisions(preset)
    # A generator that priced nothing (stub header unreadable) is fine — but if it
    # wrote a section, that section's model path MUST exist.
    for mid, entry in decisions.items():
        model = (entry.keys or {}).get("model")
        if model is None:
            continue
        assert Path(model).exists(), f"section {mid} references missing file: {model}"


def test_orphan_section_with_dead_path_is_detected(tmp_path):
    """THE PRODUCTION BUG: a section whose model file does not exist must be
    reported as drift, not silently served by the router as a broken model."""
    preset = tmp_path / "presets.ini"
    dead = tmp_path / "models" / "Ternary-Bonsai-2-27B-mmproj-Q8_0.gguf"
    preset.write_text(
        "[Real]\nmodel = %s\nctx-size = 8192\n\n"
        "[Ternary-Bonsai-2-27B-mmproj-Q8_0]\nmodel = %s\nctx-size = 8192\n"
        % (tmp_path / "exists.gguf", dead),
        encoding="utf-8")
    (tmp_path / "exists.gguf").write_bytes(b"GGUF")

    from hermes_cli.local_runtime.presets import detect_preset_drift
    drift = detect_preset_drift(preset, models_dir=models_dir)
    assert any("Ternary-Bonsai-2-27B-mmproj-Q8_0" in d for d in drift), drift


def test_staged_model_missing_from_preset_is_drift(models_dir, tmp_path):
    """The other half of staleness: a staged GGUF with no section autoloads
    with stock fit instead of a policy decision."""
    _gguf(models_dir, "Staged-No-Section.gguf")
    preset = tmp_path / "presets.ini"
    preset.write_text("[Other]\nmodel = %s\n" % (tmp_path / "x.gguf"), encoding="utf-8")
    (tmp_path / "x.gguf").write_bytes(b"GGUF")

    from hermes_cli.local_runtime.presets import detect_preset_drift
    drift = detect_preset_drift(preset, models_dir=models_dir)
    assert any("Staged-No-Section" in d for d in drift), drift


def test_generation_output_matches_staged_set_exactly(models_dir, tmp_path):
    """The production bug's real mechanism: a successful generation must leave the
    INI describing ONLY the staged set. A settled-by-failure INI can keep an orphan
    section alive forever, so a successful run must not silently retain one."""
    _gguf(models_dir, "Only-Staged.gguf")
    preset = tmp_path / "presets.ini"
    # Pre-seed with an orphan, as a failed-generation-fallback INI would carry.
    preset.write_text(
        "[Orphan]\nmodel = %s\n" % (tmp_path / "gone.gguf"), encoding="utf-8")
    (tmp_path / "gone.gguf").write_bytes(b"GGUF")
    (tmp_path / "gone.gguf").unlink()

    from hermes_cli.local_runtime.estimator import HardwareBudget
    budget = HardwareBudget(usable_vram_bytes=24 * 1024**3,
                            total_device_bytes=24 * 1024**3,
                            ram_available_bytes=32 * 1024**3)

    generate_presets(models_dir, budget, preset)

    leftover = detect_preset_drift(preset, models_dir=models_dir)
    assert not any("Orphan" in d for d in leftover), (
        f"a successful generation must not retain an orphan section: {leftover}")
