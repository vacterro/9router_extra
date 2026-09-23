"""
CORE-002 regressions - preset persistence must be fail-closed and truthful.

Defects pinned here (audit/3.md CORE-002):
- load() swallowed every parse/read error and destroyed malformed bytes by
  overwriting them with seeded defaults;
- save() swallowed every persistence failure and returned normally;
- save_preset()/delete_preset() reported success (returned the preset / True)
  even when nothing was persisted, leaving memory and disk divergent.

Contract under test: ABSENT != MALFORMED/UNREADABLE; malformed bytes are
preserved before replacement; a failed save raises PresetPersistenceError;
create/delete roll back in-memory state on failure.
"""
import json
from pathlib import Path

import pytest

from core.combo_manager import (
    DEFAULT_PRESETS,
    Preset,
    PresetLoadState,
    PresetManager,
    PresetPersistenceError,
)


def _pm(tmp_path: Path) -> PresetManager:
    return PresetManager(presets_file=tmp_path / "presets.json")


# ---------------------------------------------------------------------------
# load(): ABSENT vs MALFORMED
# ---------------------------------------------------------------------------
def test_absent_file_seeds_defaults_and_persists(tmp_path):
    pm = _pm(tmp_path)
    assert pm.load_state is PresetLoadState.ABSENT
    assert set(DEFAULT_PRESETS).issubset(pm.presets.keys())
    assert (tmp_path / "presets.json").exists()


def test_malformed_json_preserved_and_not_overwritten(tmp_path):
    presets_file = tmp_path / "presets.json"
    malformed = b"{broken"
    presets_file.write_bytes(malformed)

    pm = _pm(tmp_path)

    assert pm.load_state is PresetLoadState.MALFORMED_JSON
    assert pm.quarantine_path is not None
    # Original bytes survive verbatim at the quarantine path.
    assert Path(pm.quarantine_path).read_bytes() == malformed
    # Defaults are seeded in memory so the UI stays usable.
    assert set(DEFAULT_PRESETS).issubset(pm.presets.keys())


def test_invalid_record_structure_surfaces_and_preserves(tmp_path):
    presets_file = tmp_path / "presets.json"
    raw = json.dumps({"BROKEN": ["not", "an", "object"]}).encode("utf-8")
    presets_file.write_bytes(raw)

    pm = _pm(tmp_path)

    assert pm.load_state is PresetLoadState.INVALID_RECORD
    assert pm.quarantine_path is not None
    assert Path(pm.quarantine_path).read_bytes() == raw


def test_valid_file_loads_without_seeding(tmp_path):
    presets_file = tmp_path / "presets.json"
    presets_file.write_text(
        json.dumps({"ONLY": {"name": "ONLY", "models": ["m/1"], "description": "", "updated_at": "t"}}),
        encoding="utf-8",
    )
    pm = _pm(tmp_path)
    assert pm.load_state is PresetLoadState.LOADED
    assert list(pm.presets.keys()) == ["ONLY"]
    assert "SAIFREN_FREE" not in pm.presets


# ---------------------------------------------------------------------------
# save(): truthful failure
# ---------------------------------------------------------------------------
def test_unwritable_destination_raises_and_reports_stage(tmp_path, monkeypatch):
    pm = _pm(tmp_path)
    pm.presets["X"] = Preset(name="X", models=["m"], description="", updated_at="t")

    import core.combo_manager as cm

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(cm.os, "fsync", boom)
    with pytest.raises(PresetPersistenceError) as ex:
        pm.save()
    assert ex.value.stage in ("write", "fsync")


def test_temp_write_failure_leaves_no_false_success(tmp_path, monkeypatch):
    pm = _pm(tmp_path)

    import builtins

    real_open = builtins.open

    def failing_open(path, *a, **k):
        if str(path).endswith(".tmp"):
            raise OSError("no temp")
        return real_open(path, *a, **k)

    monkeypatch.setattr(builtins, "open", failing_open)
    with pytest.raises(PresetPersistenceError):
        pm.save()


# ---------------------------------------------------------------------------
# save_preset / delete_preset: no phantom success, rollback on failure
# ---------------------------------------------------------------------------
def test_save_preset_rolls_back_memory_on_persistence_failure(tmp_path, monkeypatch):
    pm = _pm(tmp_path)
    assert "CUSTOM" not in pm.presets

    monkeypatch.setattr(
        PresetManager, "save",
        lambda self: (_ for _ in ()).throw(PresetPersistenceError("write", self.presets_file)),
    )
    with pytest.raises(PresetPersistenceError):
        pm.save_preset("CUSTOM", ["x/model"])

    assert "CUSTOM" not in pm.presets, "failed save must not leave a phantom in-memory entry"


def test_save_preset_restores_previous_entry_on_failure(tmp_path, monkeypatch):
    pm = _pm(tmp_path)
    pm.save_preset("KEEP", ["old/model"])
    assert pm.presets["KEEP"].models == ["old/model"]

    monkeypatch.setattr(
        PresetManager, "save",
        lambda self: (_ for _ in ()).throw(PresetPersistenceError("replace", self.presets_file)),
    )
    with pytest.raises(PresetPersistenceError):
        pm.save_preset("KEEP", ["new/model"])

    assert pm.presets["KEEP"].models == ["old/model"], "previous entry must be restored"


def test_delete_preset_restores_entry_on_failure(tmp_path, monkeypatch):
    pm = _pm(tmp_path)
    pm.save_preset("DEL", ["m/1"])

    monkeypatch.setattr(
        PresetManager, "save",
        lambda self: (_ for _ in ()).throw(PresetPersistenceError("write", self.presets_file)),
    )
    with pytest.raises(PresetPersistenceError):
        pm.delete_preset("DEL")

    assert "DEL" in pm.presets, "failed delete must not silently discard the entry"


def test_delete_preset_unknown_returns_false(tmp_path):
    pm = _pm(tmp_path)
    assert pm.delete_preset("NOPE") is False


# ---------------------------------------------------------------------------
# round-trip
# ---------------------------------------------------------------------------
def test_save_restart_round_trip(tmp_path):
    pm = _pm(tmp_path)
    pm.save_preset("MY_TEST_PRESET", ["ag/gemini-3.8-flash-high", "wb/hy3"], "Test description")

    pm2 = PresetManager(presets_file=tmp_path / "presets.json")
    assert pm2.load_state is PresetLoadState.LOADED
    assert "MY_TEST_PRESET" in pm2.presets
    assert pm2.presets["MY_TEST_PRESET"].models == ["ag/gemini-3.8-flash-high", "wb/hy3"]
