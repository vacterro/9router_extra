"""
Tests for core/combo_manager.py
"""
import pytest
from core.combo_manager import (
    StableCombo,
    compute_combo_diff,
    ComboDiff,
    PresetManager,
)
from core.history import HealthCache, ModelHealthRecord
from core.classification import HealthState, Confidence, CostStatus

def test_stable_combo_reorder_and_add():
    combo = StableCombo(
        combo_id="test-1",
        name="TEST_COMBO",
        models=["ag/gemini-3.8-flash-high", "wb/hy3", "gorouter/claude-opus-5-thinking"],
    )

    # Adding an already present model should fail (prevent duplicates)
    assert not combo.add_model("wb/hy3")
    assert len(combo.models) == 3

    # Add new model to specific index
    assert combo.add_model("cl/deepseek-v4", index=1)
    assert combo.models == ["ag/gemini-3.8-flash-high", "cl/deepseek-v4", "wb/hy3", "gorouter/claude-opus-5-thinking"]

    # Move up
    assert combo.move_up("wb/hy3")
    assert combo.models == ["ag/gemini-3.8-flash-high", "wb/hy3", "cl/deepseek-v4", "gorouter/claude-opus-5-thinking"]

    # Move down
    assert combo.move_down("ag/gemini-3.8-flash-high")
    assert combo.models == ["wb/hy3", "ag/gemini-3.8-flash-high", "cl/deepseek-v4", "gorouter/claude-opus-5-thinking"]

    # Remove
    assert combo.remove_model("cl/deepseek-v4")
    assert "cl/deepseek-v4" not in combo.models
    assert len(combo.models) == 3

def test_compute_combo_diff():
    original = ["model-a", "model-b", "model-c", "model-d"]
    modified = ["model-c", "model-a", "model-b", "model-e"]

    diff = compute_combo_diff(original, modified)

    assert "model-d" in diff.removed
    assert any(m == "model-e" for m, idx in diff.added)
    assert any(m == "model-c" for m, old_i, new_i in diff.moved)
    
    formatted = diff.format_text()
    assert "- model-d" in formatted
    assert "+ model-e" in formatted

def test_move_healthy_to_top(tmp_path):
    cache = HealthCache(cache_file=tmp_path / "cache.json")
    cache.records["dead-model"] = ModelHealthRecord(
        canonical_id="dead-model", provider="p", model_id="m", state=HealthState.DEAD.value,
        confidence=Confidence.MODEL_DEAD.value, cost_status=CostStatus.UNKNOWN.value
    )
    cache.records["healthy-model-1"] = ModelHealthRecord(
        canonical_id="healthy-model-1", provider="p", model_id="m", state=HealthState.FREE_USE.value,
        confidence=Confidence.LIVE.value, cost_status=CostStatus.FREE.value
    )
    cache.records["healthy-model-2"] = ModelHealthRecord(
        canonical_id="healthy-model-2", provider="p", model_id="m", state=HealthState.PAID.value,
        confidence=Confidence.LIVE.value, cost_status=CostStatus.PAID.value
    )

    combo = StableCombo(
        combo_id="test",
        name="COMBO",
        models=["dead-model", "healthy-model-1", "healthy-model-2"],
    )

    diff = combo.move_healthy_to_top(cache)
    assert combo.models == ["healthy-model-1", "healthy-model-2", "dead-model"]
    assert not diff.is_empty()

def test_remove_dead_models(tmp_path):
    cache = HealthCache(cache_file=tmp_path / "cache.json")
    cache.records["dead-model"] = ModelHealthRecord(
        canonical_id="dead-model", provider="p", model_id="m", state=HealthState.DEAD.value,
        confidence=Confidence.MODEL_DEAD.value, cost_status=CostStatus.UNKNOWN.value
    )

    combo = StableCombo(
        combo_id="test",
        name="COMBO",
        models=["dead-model", "live-model"],
    )

    diff = combo.remove_dead_models(cache)
    assert combo.models == ["live-model"]
    assert "dead-model" in diff.removed

def test_preset_manager(tmp_path):
    pm = PresetManager(presets_file=tmp_path / "presets.json")
    assert "SAIFREN_FREE" in pm.presets
    
    # Verify SWE-1.6 Slow is in SAIFREN_FREE preset
    assert "cog/swe-1.6-slow" in pm.presets["SAIFREN_FREE"].models
    assert pm.presets["SAIFREN_FREE"].models[0] == "cog/swe-1.6-slow"

    pm.save_preset("MY_TEST_PRESET", ["ag/gemini-3.8-flash-high", "wb/hy3"], "Test description")
    assert "MY_TEST_PRESET" in pm.presets
    assert pm.presets["MY_TEST_PRESET"].models == ["ag/gemini-3.8-flash-high", "wb/hy3"]

    # Re-load from disk
    pm2 = PresetManager(presets_file=tmp_path / "presets.json")
    assert "MY_TEST_PRESET" in pm2.presets
    assert "cog/swe-1.6-slow" in pm2.presets["SAIFREN_FREE"].models
