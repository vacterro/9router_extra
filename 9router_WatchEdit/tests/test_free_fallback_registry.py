"""
FREE Fallback provider registry - deterministic persistence tests
(FREE-FALLBACK-001, milestone 1 + milestone 10 acceptance).

Covers: round-trip, atomic-save failure surfacing, corruption never accepted as
a valid empty registry, deterministic migration, secret-free storage and the
private-storage-only rule.
"""
import json
from pathlib import Path

import pytest

from core.free_evidence import (
    CostRisk,
    FreeEvidence,
    ProviderHealth,
    RoutingCapability,
    ScanMode,
    ScanPolicy,
)
from core.free_provider_registry import (
    LOAD_ABSENT,
    LOAD_LOADED,
    LOAD_MIGRATED,
    LOAD_SCHEMA_MISMATCH,
    LOAD_UNREADABLE,
    FreeProviderRegistry,
    STATUS_OK,
)


def _registry(tmp_path, clock=None) -> FreeProviderRegistry:
    return FreeProviderRegistry(path=tmp_path / "free_providers.json", clock=clock)


def _row(canonical_id: str = "prov-a/model-free", **overrides) -> dict:
    row = {
        "canonical_id": canonical_id,
        "upstream_model_id": canonical_id.split("/", 1)[1],
        "provider_id": canonical_id.split("/", 1)[0],
        "free_evidence": FreeEvidence.STRICT_FREE.value,
        "evidence_source": "explicit_zero_price",
        "provider_health": ProviderHealth.UNKNOWN.value,
        "routing": RoutingCapability.DIRECT_ROUTABLE.value,
        "cost_risk": CostRisk.FREE_QUOTA_PROBE.value,
        "scanner_managed": True,
    }
    row.update(overrides)
    return row


def test_private_storage_path_is_outside_the_source_tree(tmp_path):
    registry = _registry(tmp_path)
    repo_root = Path(__file__).resolve().parents[2]
    assert repo_root not in registry.path.parents
    assert registry.load_state == LOAD_ABSENT
    assert registry.state_valid is True


def test_round_trip_persists_policy_trust_note_and_inventory(tmp_path):
    clock = [1_000_000.0]
    registry = _registry(tmp_path, clock=lambda: clock[0])
    registry.ensure_provider(
        "prov-a", "Provider A",
        metadata_cost_risk=CostRisk.ZERO_MONETARY_METADATA,
        live_probe_cost_risk=CostRisk.FREE_QUOTA_PROBE,
        default_enabled=True,
        default_policy=ScanPolicy.STALE_ONLY,
        default_mode=ScanMode.METADATA_ONLY,
    )
    assert registry.set_policy("prov-a", ScanPolicy.ALWAYS) is True
    assert registry.set_mode("prov-a", ScanMode.METADATA_AND_LIVE_PROBE) is True
    assert registry.set_note("prov-a", "operator note: cheap lane") is True
    assert registry.mark_trusted_alive("prov-a", "7d") is True
    assert registry.record_metadata_result("prov-a", ok=True, models=[_row()]) is True
    registry.add_manual_model("prov-a", "prov-a/manual-free")
    registry.mark_synced(["prov-a/model-free"], "prov-a", "SAIFREN")

    reloaded = _registry(tmp_path, clock=lambda: clock[0])
    assert reloaded.load_state == LOAD_LOADED
    record = reloaded.get("prov-a")
    assert record is not None
    assert record.scan_policy == ScanPolicy.ALWAYS.value
    assert record.scan_mode == ScanMode.METADATA_AND_LIVE_PROBE.value
    assert record.operator_note == "operator note: cheap lane"
    assert record.trust_valid(clock[0]) is True
    assert record.trusted_alive_until == pytest.approx(clock[0] + 7 * 86400)
    assert [r["canonical_id"] for r in record.models] == ["prov-a/model-free",
                                                          "prov-a/manual-free"]
    assert reloaded.get("prov-a").last_status == STATUS_OK
    assert reloaded.synced_ids("SAIFREN") == ["prov-a/model-free"]

    row = [r for r in reloaded.provider_rows() if r["provider_id"] == "prov-a"][0]
    assert row["trusted_alive"] is True
    assert row["strict_free_count"] == 1
    assert row["models_discovered"] == 2
    assert row["has_strict_free"] is True
    # A manual row starts as UNKNOWN_COST: adding a row is not FREE evidence.
    manual = [r for r in reloaded.model_rows("prov-a")
              if r["canonical_id"] == "prov-a/manual-free"][0]
    assert manual["free_evidence"] == FreeEvidence.UNKNOWN_COST.value
    assert manual["saifren_eligible"] is False


def test_atomic_save_failure_is_surfaced_not_swallowed(tmp_path, monkeypatch):
    registry = _registry(tmp_path)
    registry.ensure_provider("prov-a")
    assert registry.set_note("prov-a", "before") is True

    def _boom(self, target):
        raise OSError("injected replace failure")

    monkeypatch.setattr(Path, "replace", _boom)
    # The write path must report failure instead of pretending it saved.
    assert registry.set_note("prov-a", "after") is False
    assert "OSError" in registry.last_save_error
    stored = (tmp_path / "free_providers.json").read_text(encoding="utf-8")
    assert "before" in stored and "after" not in stored
    assert not (tmp_path / "free_providers.tmp.json").exists()

    monkeypatch.undo()
    assert registry.save() is True
    assert registry.last_save_error == ""
    assert "before" in (tmp_path / "free_providers.json").read_text(encoding="utf-8")
    assert "after" not in (tmp_path / "free_providers.json").read_text(encoding="utf-8")


def test_corrupt_registry_is_not_accepted_as_valid_empty_state(tmp_path):
    path = tmp_path / "free_providers.json"
    path.write_text("{ this is not json", encoding="utf-8")
    registry = FreeProviderRegistry(path=path)

    assert registry.load_state == LOAD_UNREADABLE
    assert registry.load_error
    assert registry.state_valid is False
    assert registry.providers == {}
    # Refusing to save is the point: the recoverable bytes are not overwritten
    # by an empty "valid" registry the operator never had.
    assert registry.save() is False
    assert "refusing to overwrite" in registry.last_save_error
    assert path.read_text(encoding="utf-8") == "{ this is not json"

    registry.ensure_provider("prov-a", default_enabled=True)
    assert registry.save(force=True) is True
    reloaded = FreeProviderRegistry(path=path)
    assert reloaded.load_state == LOAD_LOADED
    assert "prov-a" in reloaded.providers


def test_newer_schema_version_is_a_mismatch_not_an_upgrade(tmp_path):
    path = tmp_path / "free_providers.json"
    path.write_text(json.dumps({
        "schema_version": 999,
        "providers": {"prov-a": {"provider_id": "prov-a"}},
    }), encoding="utf-8")
    registry = FreeProviderRegistry(path=path)
    assert registry.load_state == LOAD_SCHEMA_MISMATCH
    assert registry.state_valid is False
    assert registry.providers == {}


def test_v1_file_migrates_deterministically(tmp_path):
    path = tmp_path / "free_providers.json"
    path.write_text(json.dumps({
        "schema_version": 1,
        "providers": {
            "prov-a": {
                "provider_id": "prov-a",
                "display_name": "Provider A",
                "enabled": True,
                "scan_policy": "ALWAYS",
                "scan_mode": "METADATA_ONLY",
                "metadata_cost_risk": "ZERO_MONETARY_METADATA",
                "live_probe_cost_risk": "UNKNOWN",
            }
        },
    }), encoding="utf-8")
    registry = FreeProviderRegistry(path=path)
    assert registry.load_state == LOAD_MIGRATED
    assert registry.migrated_from == 1
    record = registry.get("prov-a")
    assert record.enabled is True
    assert record.scan_policy == "ALWAYS"
    # Newer fields take deterministic defaults; nothing operator-visible invented.
    assert record.allow_billing_probe is False
    assert record.models == []
    assert record.next_scan_due == ""
    assert registry.save() is True
    assert FreeProviderRegistry(path=path).load_state == LOAD_LOADED


def test_credential_shaped_material_is_refused_by_the_write_path(tmp_path):
    registry = _registry(tmp_path)
    registry.ensure_provider("prov-a", default_enabled=True)
    registry.set_note("prov-a", "before")
    # Assembled at runtime so this test file carries no credential-shaped literal.
    note_with_key_shape = "sk-" + "live" + "-9f3k2m8q7z1p4x6v"
    registry.get("prov-a").operator_note = note_with_key_shape

    assert registry.save() is False
    assert "credential-shaped" in registry.last_save_error

    stored = (tmp_path / "free_providers.json").read_text(encoding="utf-8")
    assert "sk-" not in stored
    registry.get("prov-a").operator_note = "clean note"
    assert registry.save() is True


def test_missing_private_storage_never_falls_back_to_the_source_tree(tmp_path):
    registry = _registry(tmp_path)
    registry.ensure_provider("prov-a")
    registry.storage_available = False
    assert registry.save() is False
    assert "private storage unavailable" in registry.last_save_error
    assert not (tmp_path / "free_providers.json").exists()


def test_trust_expiry_and_live_probe_suppression(tmp_path):
    clock = [10_000.0]
    registry = _registry(tmp_path, clock=lambda: clock[0])
    registry.ensure_provider("prov-a", default_enabled=True)

    assert registry.mark_trusted_alive("prov-a", "1h") is True
    assert registry.trust_valid("prov-a") is True
    assert registry.live_probe_needed("prov-a") is False

    clock[0] += 3601
    assert registry.trust_valid("prov-a") is False
    assert registry.live_probe_needed("prov-a") is True
    assert "expired" in registry.get("prov-a").trust_text(clock[0])

    assert registry.mark_trusted_alive("prov-a", "until_cleared") is True
    assert registry.trust_valid("prov-a") is True
    clock[0] += 400 * 86400
    assert registry.trust_valid("prov-a") is True
    assert registry.clear_trusted_alive("prov-a") is True
    assert registry.trust_valid("prov-a") is False


def test_authoritative_metadata_success_prunes_only_scanner_owned_synced_rows(tmp_path):
    registry = _registry(tmp_path)
    registry.ensure_provider("prov-a", default_enabled=True)
    registry.record_metadata_result("prov-a", ok=True, models=[
        _row("prov-a/keep"), _row("prov-a/gone"),
    ])
    registry.add_manual_model("prov-a", "prov-a/manual")
    registry.mark_synced(["prov-a/gone", "prov-a/keep"], "prov-a", "SAIFREN")

    # Source outage: NOTHING is pruned and the inventory survives intact.
    registry.record_metadata_result(
        "prov-a", ok=False, error_class="timeout", error_summary="outage"
    )
    assert registry.pending_prune_ids("SAIFREN") == []
    assert sorted(r["canonical_id"] for r in registry.model_rows("prov-a")) == [
        "prov-a/gone", "prov-a/keep", "prov-a/manual",
    ]
    assert registry.get("prov-a").last_status == "OUTAGE"

    # Successful authoritative refresh that dropped a synced FREE model.
    registry.record_metadata_result("prov-a", ok=True, models=[_row("prov-a/keep")])
    pending = [row["canonical_id"] for row in registry.pending_prune_ids("SAIFREN")]
    assert pending == ["prov-a/gone"]
    ids = sorted(r["canonical_id"] for r in registry.model_rows("prov-a"))
    assert ids == ["prov-a/keep", "prov-a/manual"]  # manual row survived

    assert registry.confirm_pruned(["prov-a/gone"]) is True
    assert registry.pending_prune_ids("SAIFREN") == []
    assert registry.synced_ids("SAIFREN") == ["prov-a/keep"]


def test_disabled_unscanned_provider_reports_disabled_status(tmp_path):
    registry = _registry(tmp_path)
    registry.ensure_provider("prov-a")
    assert registry.provider_rows()[0]["last_status"] == "DISABLED"
    registry.set_enabled("prov-a", True)
    assert registry.provider_rows()[0]["last_status"] == "UNSCANNED"
    assert registry.is_due("prov-a") is False  # MANUAL policy: explicit actions only


def test_stale_only_policy_schedules_by_metadata_age(tmp_path):
    clock = [50_000.0]
    registry = _registry(tmp_path, clock=lambda: clock[0])
    registry.ensure_provider(
        "prov-a", default_enabled=True, default_policy=ScanPolicy.STALE_ONLY,
    )
    assert registry.is_due("prov-a") is True
    registry.record_metadata_result("prov-a", ok=True, models=[_row()])
    assert registry.is_due("prov-a") is False
    clock[0] += 6 * 3600 + 1
    assert registry.is_due("prov-a") is True


def test_no_forbidden_fields_and_no_provider_bound_status_is_lost(tmp_path):
    registry = _registry(tmp_path)
    registry.ensure_provider("prov-a", default_enabled=True)
    registry.set_live_probe_consent("prov-a", True)
    assert registry.get("prov-a").cost_safe_for_live() is True
    doc = registry.document()
    assert set(doc) == {"schema_version", "saved_at", "providers", "synced",
                        "prune_candidates", "pending_tail_sync"}
    assert registry.snapshot()["load_state"] == LOAD_ABSENT


def test_staged_tail_intent_recovers_after_backend_commit(tmp_path):
    registry = _registry(tmp_path)
    registry.ensure_provider("prov-a", default_enabled=True)
    registry.record_metadata_result("prov-a", ok=True, models=[_row()])
    old_models = ["ag/opus-4"]
    new_models = old_models + ["prov-a/model-free"]

    assert registry.stage_tail_sync_intent({
        "combo_name": "SAIFREN",
        "old_models": old_models,
        "new_models": new_models,
        "appended": ["prov-a/model-free"],
        "removed": [],
        "provider_ids": {},
    }) is True
    reloaded = _registry(tmp_path)
    assert reloaded.pending_tail_sync["new_models"] == new_models
    assert reloaded.synced_ids("SAIFREN") == []

    assert reloaded.reconcile_pending_tail_sync(new_models) == "COMPLETED"
    assert reloaded.pending_tail_sync is None
    assert reloaded.synced_ids("SAIFREN") == ["prov-a/model-free"]


def test_failed_tail_ledger_commit_restores_intent_for_recovery(tmp_path, monkeypatch):
    registry = _registry(tmp_path)
    registry.ensure_provider("prov-a", default_enabled=True)
    registry.record_metadata_result("prov-a", ok=True, models=[_row()])
    intent = {
        "combo_name": "SAIFREN",
        "old_models": ["ag/opus-4"],
        "new_models": ["ag/opus-4", "prov-a/model-free"],
        "appended": ["prov-a/model-free"],
        "removed": [],
        "provider_ids": {},
    }
    assert registry.stage_tail_sync_intent(intent) is True
    before = registry.snapshot()

    def fail_save():
        registry.last_save_error = "injected disk failure"
        return False

    monkeypatch.setattr(registry, "save", fail_save)
    assert registry.finalize_tail_sync_intent(intent["new_models"]) is False
    assert registry.pending_tail_sync is not None
    assert registry.synced_ids("SAIFREN") == []
    assert "injected disk failure" in registry.last_save_error
    assert registry.snapshot()["synced"] == before["synced"]


@pytest.mark.parametrize("corrupt", [
    lambda doc: doc["providers"]["prov-a"].update(scan_policy="SOMETIMES"),
    lambda doc: doc["providers"]["prov-a"].update(enabled=1),
    lambda doc: doc["providers"]["prov-a"].update(last_metadata_success_epoch=float("inf")),
    lambda doc: doc["providers"]["prov-a"].update(models=[_row(provider_health="MAYBE")]),
])
def test_invalid_persisted_registry_fields_fail_closed(tmp_path, corrupt):
    registry = _registry(tmp_path)
    registry.ensure_provider("prov-a", default_enabled=True)
    assert registry.save() is True
    raw = json.loads(registry.path.read_text(encoding="utf-8"))
    corrupt(raw)
    registry.path.write_text(json.dumps(raw), encoding="utf-8")
    original = registry.path.read_bytes()

    reloaded = _registry(tmp_path)
    assert reloaded.load_state == LOAD_UNREADABLE
    assert reloaded.state_valid is False
    assert reloaded.save() is False
    assert reloaded.path.read_bytes() == original
