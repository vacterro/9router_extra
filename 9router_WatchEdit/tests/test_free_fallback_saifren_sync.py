"""
Strict FREE -> SAIFREN bottom sync tests (FREE-FALLBACK-001, milestones 8, 10).

The sync is a pure plan + an explicit apply. These tests prove that reliable
routes never move, the append is tail-only and idempotent, manual entries are
never touched, and removal requires positive authoritative evidence.
"""
from core.free_evidence import (
    CostRisk,
    FreeEvidence,
    ProviderHealth,
    RoutingCapability,
)
from core.free_provider_registry import FreeProviderRegistry
from core.free_saifren_sync import commit_tail_sync, plan_tail_sync

RELIABLE = ["ag/opus-4", "wb/gpt-5", "cx/claude-4"]


def _row(provider_id="prov-a", model_id="model-free", **overrides) -> dict:
    row = {
        "canonical_id": f"{provider_id}/{model_id}",
        "upstream_model_id": model_id,
        "provider_id": provider_id,
        "free_evidence": FreeEvidence.STRICT_FREE.value,
        "provider_health": ProviderHealth.UNKNOWN.value,
        "routing": RoutingCapability.DIRECT_ROUTABLE.value,
        "cost_risk": CostRisk.FREE_QUOTA_PROBE.value,
        "scanner_managed": True,
    }
    row.update(overrides)
    return row


def _registry(tmp_path) -> FreeProviderRegistry:
    return FreeProviderRegistry(path=tmp_path / "providers.json")


def test_sync_appends_only_at_the_bottom_and_preserves_reliable_order(tmp_path):
    registry = _registry(tmp_path)
    registry.ensure_provider("prov-a", default_enabled=True)
    registry.record_metadata_result("prov-a", ok=True, models=[
        _row(model_id="z-free"), _row(model_id="a-free"),
    ])

    plan = plan_tail_sync(RELIABLE, registry)

    assert plan.models[:len(RELIABLE)] == RELIABLE
    assert plan.models[len(RELIABLE):] == ["prov-a/a-free", "prov-a/z-free"]
    assert plan.removed == []
    assert plan.unchanged is False
    # Reliable routes are untouched, byte for byte, in order.
    assert RELIABLE == ["ag/opus-4", "wb/gpt-5", "cx/claude-4"]


def test_sync_is_idempotent(tmp_path):
    registry = _registry(tmp_path)
    registry.ensure_provider("prov-a", default_enabled=True)
    registry.record_metadata_result("prov-a", ok=True, models=[_row()])

    first = plan_tail_sync(RELIABLE, registry)
    assert first.appended == ["prov-a/model-free"]
    second = plan_tail_sync(first.models, registry)
    assert second.unchanged is True
    assert second.appended == []
    assert second.models == first.models


def test_existing_tail_entries_are_not_reordered_above_reliable_routes(tmp_path):
    registry = _registry(tmp_path)
    registry.ensure_provider("prov-a", default_enabled=True)
    registry.record_metadata_result("prov-a", ok=True, models=[
        _row(model_id="b-free"), _row(model_id="a-free"),
    ])
    registry.mark_synced(["prov-a/a-free", "prov-a/b-free"], "prov-a", "SAIFREN")

    combo = RELIABLE + ["prov-a/a-free", "prov-a/b-free"]
    plan = plan_tail_sync(combo, registry)
    assert plan.unchanged is True
    assert plan.models == combo


def test_manual_entries_are_preserved_verbatim(tmp_path):
    registry = _registry(tmp_path)
    registry.ensure_provider("prov-a", default_enabled=True)
    registry.record_metadata_result("prov-a", ok=True, models=[_row()])

    combo = RELIABLE + ["prov-a/operator-pinned"]
    plan = plan_tail_sync(combo, registry)

    assert "prov-a/operator-pinned" in plan.models
    assert "prov-a/operator-pinned" in plan.kept_manual
    assert plan.removed == []
    # Manual entry keeps its position; FREE tail lands after it.
    assert plan.models.index("prov-a/operator-pinned") < plan.models.index("prov-a/model-free")


def test_removal_requires_positive_authoritative_evidence(tmp_path):
    registry = _registry(tmp_path)
    registry.ensure_provider("prov-a", default_enabled=True)
    registry.record_metadata_result("prov-a", ok=True, models=[
        _row(model_id="keep-free"), _row(model_id="gone-free"),
    ])
    registry.mark_synced(["prov-a/keep-free", "prov-a/gone-free"], "prov-a", "SAIFREN")
    combo = RELIABLE + ["prov-a/keep-free", "prov-a/gone-free"]

    # A source OUTAGE prunes nothing: last-known-good survives.
    registry.record_metadata_result("prov-a", ok=False, error_class="timeout",
                                    error_summary="outage")
    plan = plan_tail_sync(combo, registry)
    assert plan.removed == []
    assert plan.unchanged is True

    # A successful authoritative refresh that dropped the model does prune it.
    registry.record_metadata_result("prov-a", ok=True, models=[_row(model_id="keep-free")])
    plan = plan_tail_sync(combo, registry)
    assert plan.removed == ["prov-a/gone-free"]
    assert "prov-a/keep-free" in plan.models
    assert "prov-a/gone-free" not in plan.models
    assert plan.models[:len(RELIABLE)] == RELIABLE


def test_a_non_authoritative_result_never_prunes(tmp_path):
    registry = _registry(tmp_path)
    registry.ensure_provider("prov-a", default_enabled=True)
    registry.record_metadata_result("prov-a", ok=True, models=[_row(model_id="gone-free")])
    registry.mark_synced(["prov-a/gone-free"], "prov-a", "SAIFREN")

    registry.record_metadata_result(
        "prov-a", ok=True, models=[], authoritative=False,
    )
    assert registry.pending_prune_ids("SAIFREN") == []
    plan = plan_tail_sync(RELIABLE + ["prov-a/gone-free"], registry)
    assert plan.removed == []


def test_paid_conditional_withdrawn_and_dead_routes_are_never_added(tmp_path):
    registry = _registry(tmp_path)
    registry.ensure_provider("prov-a", default_enabled=True)
    registry.record_metadata_result("prov-a", ok=True, models=[
        _row(model_id="free"),
        _row(model_id="paid", free_evidence=FreeEvidence.PAID.value,
             cost_risk=CostRisk.POSSIBLE_BILLING.value),
        _row(model_id="trial", free_evidence=FreeEvidence.CONDITIONAL_FREE.value),
        _row(model_id="unknown", free_evidence=FreeEvidence.UNKNOWN_COST.value),
        _row(model_id="withdrawn", free_evidence=FreeEvidence.WITHDRAWN.value),
        _row(model_id="auth-dead", provider_health=ProviderHealth.AUTH_FAILED.value),
        _row(model_id="dead", provider_health=ProviderHealth.DEAD.value),
    ])

    plan = plan_tail_sync(RELIABLE, registry)

    assert plan.models[len(RELIABLE):] == ["prov-a/free"]
    excluded = {row["canonical_id"] for row in plan.excluded}
    assert {"prov-a/paid", "prov-a/trial", "prov-a/unknown", "prov-a/withdrawn"} <= excluded


def test_unknown_health_zero_cost_route_follows_the_better_than_zero_policy(tmp_path):
    registry = _registry(tmp_path)
    registry.ensure_provider("prov-a", default_enabled=True)
    registry.record_metadata_result("prov-a", ok=True, models=[
        _row(model_id="unverified-free", provider_health=ProviderHealth.UNKNOWN.value),
    ])

    plan = plan_tail_sync(RELIABLE, registry)

    assert plan.models[-1] == "prov-a/unverified-free"


def test_client_bound_free_requires_the_bridge_contract(tmp_path):
    registry = _registry(tmp_path)
    registry.ensure_provider("opencode", default_enabled=True)
    registry.record_metadata_result("opencode", ok=True, models=[
        _row("opencode", "mimo-free",
             free_evidence=FreeEvidence.CLIENT_BOUND_FREE.value,
             routing=RoutingCapability.LOCAL_BRIDGE_REQUIRED.value),
    ])

    without_bridge = plan_tail_sync(RELIABLE, registry)
    assert without_bridge.models == RELIABLE
    assert without_bridge.unchanged is True

    with_bridge = plan_tail_sync(
        RELIABLE, registry, bridge_eligible=["opencode/mimo-free"]
    )
    assert with_bridge.models[-1] == "opencode/mimo-free"


def test_planning_is_pure_and_commit_records_the_ledger(tmp_path):
    registry = _registry(tmp_path)
    registry.ensure_provider("prov-a", default_enabled=True)
    registry.record_metadata_result("prov-a", ok=True, models=[_row()])
    before = registry.snapshot()

    plan = plan_tail_sync(RELIABLE, registry)
    assert registry.snapshot() == before
    assert registry.synced_ids("SAIFREN") == []

    assert commit_tail_sync(registry, plan) is True
    assert registry.synced_ids("SAIFREN") == ["prov-a/model-free"]
    # Re-planning after the commit is a no-op (idempotent end to end).
    again = plan_tail_sync(plan.models, registry)
    assert again.unchanged is True
    assert again.appended == []


def test_commit_removes_only_the_pruned_ledger_entries(tmp_path):
    registry = _registry(tmp_path)
    registry.ensure_provider("prov-a", default_enabled=True)
    registry.record_metadata_result("prov-a", ok=True, models=[
        _row(model_id="keep-free"), _row(model_id="gone-free"),
    ])
    registry.mark_synced(["prov-a/keep-free", "prov-a/gone-free"], "prov-a", "SAIFREN")
    registry.record_metadata_result("prov-a", ok=True, models=[_row(model_id="keep-free")])

    combo = RELIABLE + ["prov-a/keep-free", "prov-a/gone-free"]
    plan = plan_tail_sync(combo, registry)
    assert plan.removed == ["prov-a/gone-free"]
    assert commit_tail_sync(registry, plan) is True
    assert registry.synced_ids("SAIFREN") == ["prov-a/keep-free"]


def test_sync_never_duplicates_an_existing_tail_entry(tmp_path):
    registry = _registry(tmp_path)
    registry.ensure_provider("prov-a", default_enabled=True)
    registry.record_metadata_result("prov-a", ok=True, models=[_row()])
    registry.mark_synced(["prov-a/model-free"], "prov-a", "SAIFREN")

    combo = RELIABLE + ["prov-a/model-free", "prov-a/model-free"]
    plan = plan_tail_sync(combo, registry)
    # The scanner-owned duplicate collapses to exactly one tail occurrence.
    assert plan.models.count("prov-a/model-free") == 1
    assert plan.appended == []
    assert plan.models[:len(RELIABLE)] == RELIABLE
