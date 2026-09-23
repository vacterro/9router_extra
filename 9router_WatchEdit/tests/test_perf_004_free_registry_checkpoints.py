import math

from core.free_evidence import CostRisk, ScanMode, ScanPolicy
from core.free_provider_registry import FreeProviderRegistry
from core.free_scan_controller import FreeScanController

from tests.test_free_fallback_controller import _FakeAdapter, _row


def test_metadata_and_live_stages_share_one_provider_checkpoint(tmp_path, monkeypatch):
    registry = FreeProviderRegistry(path=tmp_path / "providers.json")
    adapter = _FakeAdapter("prov-a", live=True, models=[_row()])
    registry.ensure_provider(
        "prov-a", default_enabled=True, default_policy=ScanPolicy.ALWAYS,
        default_mode=ScanMode.METADATA_AND_LIVE_PROBE,
        metadata_cost_risk=CostRisk.ZERO_MONETARY_METADATA,
        live_probe_cost_risk=CostRisk.FREE_QUOTA_PROBE,
    )
    registry.describe_adapter("prov-a", adapter.capabilities().as_dict())
    controller = FreeScanController(registry, {"prov-a": adapter})
    count = [0]
    real_save = registry.save

    def count_save(*, force=False):
        count[0] += 1
        return real_save(force=force)

    monkeypatch.setattr(registry, "save", count_save)
    run = controller.scan_selected(["prov-a"])

    assert run.wait(5.0)
    assert run.durable is True
    assert count[0] == 1
    loaded = FreeProviderRegistry(path=registry.path)
    assert loaded.get("prov-a").last_metadata_success_at
    assert loaded.get("prov-a").last_live_probe_success_at


def test_hundred_provider_scan_uses_bounded_full_snapshot_checkpoints(tmp_path, monkeypatch):
    registry = FreeProviderRegistry(path=tmp_path / "providers.json")
    adapters = {}
    for index in range(100):
        provider_id = f"provider-{index:03}"
        adapter = _FakeAdapter(
            provider_id,
            models=[_row(provider_id, f"model-{n:02}") for n in range(20)],
        )
        adapters[provider_id] = adapter
        registry.ensure_provider(
            provider_id, default_enabled=True, default_policy=ScanPolicy.ALWAYS,
            default_mode=ScanMode.METADATA_ONLY,
        )
        registry.describe_adapter(provider_id, adapter.capabilities().as_dict())
    controller = FreeScanController(registry, adapters, max_workers=8)
    saves = [0]
    secret_scans = [0]
    real_save = registry.save
    real_scan = registry._secret_findings

    def count_save(*, force=False):
        saves[0] += 1
        return real_save(force=force)

    def count_secret_scan(path):
        secret_scans[0] += 1
        return real_scan(path)

    monkeypatch.setattr(registry, "save", count_save)
    monkeypatch.setattr(registry, "_secret_findings", count_secret_scan)
    run = controller.metadata_scan_selected(list(adapters))

    assert run.wait(20.0)
    expected = math.ceil(100 / 16)
    assert saves[0] == expected
    assert secret_scans[0] == expected
    assert run.durable is True
    assert len(run.outcomes) == 100
    assert all(outcome.status == "SCANNED" for outcome in run.outcomes.values())


def test_failed_terminal_checkpoint_is_reported_as_nondurable(tmp_path, monkeypatch):
    registry = FreeProviderRegistry(path=tmp_path / "providers.json")
    adapter = _FakeAdapter("prov-a", models=[_row()])
    registry.ensure_provider(
        "prov-a", default_enabled=True, default_policy=ScanPolicy.ALWAYS,
        default_mode=ScanMode.METADATA_ONLY,
    )
    registry.describe_adapter("prov-a", adapter.capabilities().as_dict())
    controller = FreeScanController(registry, {"prov-a": adapter})

    def fail_save(*, force=False):
        registry.last_save_error = "injected storage failure"
        return False

    monkeypatch.setattr(registry, "save", fail_save)
    run = controller.metadata_scan_selected(["prov-a"])

    assert run.wait(5.0)
    assert run.durable is False
    assert "injected storage failure" in run.persistence_error
    assert run.outcomes["prov-a"].status == "FAILED"
    assert run.outcomes["prov-a"].persistence_error
    assert registry.scan_evidence_dirty is True
