"""
FREE Fallback cost-aware provider scan controller tests
(FREE-FALLBACK-001, milestones 5 and 10).

Every guard is proven with counters: a refused or skipped provider must produce
ZERO adapter calls (and therefore zero network/inference activity).
"""
import threading
import time

from core.free_evidence import (
    CostRisk,
    FreeEvidence,
    ProviderHealth,
    RoutingCapability,
    ScanMode,
    ScanPolicy,
)
from core.free_provider_adapters import (
    AdapterCapabilities,
    FreeProviderAdapter,
    LiveCanaryResult,
    MetadataScanResult,
)
from core.free_provider_registry import FreeProviderRegistry
from core.free_scan_controller import (
    ACTION_METADATA,
    SKIP_BACKOFF,
    SKIP_DISABLED,
    SKIP_NO_LIVE_CANARY,
    SKIP_NOT_STALE,
    SKIP_REFUSED_COST_RISK,
    SKIP_SINGLE_FLIGHT,
    SKIP_TRUSTED_ALIVE,
    STATUS_FAILED,
    STATUS_SCANNED,
    STATUS_SKIPPED,
    FreeScanController,
)


def _row(provider_id="prov-a", model_id="model-free",
         evidence=FreeEvidence.STRICT_FREE, health=ProviderHealth.UNKNOWN,
         routing=RoutingCapability.DIRECT_ROUTABLE, cost=CostRisk.FREE_QUOTA_PROBE,
         scanner=True) -> dict:
    return {
        "canonical_id": f"{provider_id}/{model_id}",
        "upstream_model_id": model_id,
        "provider_id": provider_id,
        "free_evidence": FreeEvidence(evidence).value,
        "provider_health": ProviderHealth(health).value,
        "routing": RoutingCapability(routing).value,
        "cost_risk": CostRisk(cost).value,
        "scanner_managed": scanner,
    }


class _FakeAdapter(FreeProviderAdapter):
    # (blocking_health is a constructor kwarg of the double, see __init__)

    """Deterministic adapter double with full call accounting."""

    def __init__(self, provider_id, *, metadata=True, live=False, can_bill=False,
                 quota=True, client_bound=False, models=None, metadata_error=None,
                 metadata_ok=True, retry_after=0.0, live_ok=True,
                 live_state="BRIDGE_OK", gate=None, delay=0.0, live_delay=0.0,
                 canary_target="", blocking_health=""):
        super().__init__(provider_id, provider_id.upper())
        self.adapter_id = "fake"
        self._metadata = metadata
        self._live = live
        self._can_bill = can_bill
        self._quota = quota
        self._client_bound = client_bound
        self.metadata_rows = list(models or [])
        self._metadata_error = metadata_error
        self._metadata_ok = metadata_ok
        self._retry_after = retry_after
        self._live_ok = live_ok
        self._live_state = live_state
        self.gate = gate
        self.delay = delay
        self.live_delay = live_delay
        self.canary_target = canary_target
        self.blocking_health = blocking_health
        self.live_targets = []
        self.metadata_calls = 0
        self.live_calls = 0
        self.metadata_inference_calls = 0
        self.inference_calls = 0
        self.concurrent = 0
        self.max_concurrent = 0
        self._counter_lock = threading.Lock()

    def capabilities(self):
        return AdapterCapabilities(
            adapter_id=self.adapter_id,
            provider_id=self.provider_id,
            display_name=self.display_name,
            supports_metadata=self._metadata,
            supports_strict_zero_cost_evidence=True,
            supports_live_canary=self._live,
            live_canary_can_consume_quota=self._quota,
            live_canary_can_bill=self._can_bill,
            client_bound=self._client_bound,
            transport="fake transport",
            metadata_source="fake metadata source",
        )

    def metadata_scan(self):
        with self._counter_lock:
            self.metadata_calls += 1
            self.concurrent += 1
            self.max_concurrent = max(self.max_concurrent, self.concurrent)
        try:
            if self.gate is not None:
                self.gate.wait(5.0)
            if self.delay:
                time.sleep(self.delay)
            if self._metadata_error:
                return MetadataScanResult(
                    ok=False, error_class=self._metadata_error,
                    error_summary="injected metadata failure",
                    retry_after_sec=self._retry_after, source="fake", network_calls=1,
                )
            return MetadataScanResult(
                ok=self._metadata_ok,
                models=[dict(row) for row in self.metadata_rows],
                source="fake", network_calls=1,
                inference_calls=self.metadata_inference_calls,
            )
        finally:
            with self._counter_lock:
                self.concurrent -= 1

    def preferred_canary_target(self, rows):
        if self.canary_target:
            return self.canary_target
        return super().preferred_canary_target(rows)

    def live_canary(self, model_id=""):
        self.live_calls += 1
        self.live_targets.append(model_id)
        self.inference_calls += 1
        if self.live_delay:
            time.sleep(self.live_delay)
        return LiveCanaryResult(
            ok=self._live_ok, state=self._live_state,
            error_class="" if self._live_ok else "injected",
            error_summary="" if self._live_ok else "injected live failure",
            model_id=model_id, network_calls=1, inference_calls=1,
            monetary_charge_possible=self._can_bill,
            blocking_health=self.blocking_health,
            healthy=self._live_ok,
        )


def _seed_inventory(registry, provider_id, rows):
    """Pre-populate last-known-good inventory without a scan (test fixture)."""
    assert registry.record_metadata_result(provider_id, ok=True, models=list(rows)) is True


def _build(tmp_path, specs, *, max_workers=3, clock=None):
    """specs: list of (provider_id, seed_kwargs, adapter_kwargs)."""
    registry = FreeProviderRegistry(path=tmp_path / "providers.json", clock=clock)
    adapters = {}
    for provider_id, seed, adapter_kwargs in specs:
        adapter = _FakeAdapter(provider_id, **adapter_kwargs)
        adapters[provider_id] = adapter
        registry.ensure_provider(
            provider_id,
            provider_id.upper(),
            adapter="fake",
            metadata_cost_risk=seed.pop("metadata_cost_risk", CostRisk.ZERO_MONETARY_METADATA),
            live_probe_cost_risk=seed.pop("live_probe_cost_risk", CostRisk.FREE_QUOTA_PROBE),
            default_enabled=seed.pop("enabled", True),
            default_policy=seed.pop("policy", ScanPolicy.ALWAYS),
            default_mode=seed.pop("mode", ScanMode.METADATA_AND_LIVE_PROBE),
        )
        assert not seed, f"unused seed keys: {seed}"
        registry.describe_adapter(provider_id, adapter.capabilities().as_dict())
    controller = FreeScanController(registry, adapters, max_workers=max_workers, clock=clock)
    return registry, adapters, controller


# ------------------------------------------------------------ guard coverage
def test_disabled_provider_causes_zero_network_calls(tmp_path):
    registry, adapters, controller = _build(
        tmp_path, [("prov-a", {"enabled": False}, {"models": [_row()]})]
    )
    run = controller.scan_selected(["prov-a"])
    assert run.wait(5.0)
    outcome = run.outcomes["prov-a"]
    assert outcome.status == STATUS_SKIPPED
    assert outcome.reason == SKIP_DISABLED
    assert outcome.network_calls == 0 and outcome.inference_calls == 0
    assert adapters["prov-a"].metadata_calls == 0
    assert adapters["prov-a"].live_calls == 0


def test_selected_scan_never_touches_unchecked_providers(tmp_path):
    registry, adapters, controller = _build(tmp_path, [
        ("prov-a", {}, {"models": [_row("prov-a")]}),
        ("prov-b", {}, {"models": [_row("prov-b")]}),
    ])
    run = controller.metadata_scan_selected(["prov-a"])
    assert run.wait(5.0)
    assert adapters["prov-a"].metadata_calls == 1
    assert adapters["prov-b"].metadata_calls == 0
    assert adapters["prov-b"].live_calls == 0
    assert set(run.outcomes) == {"prov-a"}


def test_metadata_scan_selected_executes_zero_inference_calls(tmp_path):
    registry, adapters, controller = _build(
        tmp_path,
        [("prov-a", {"mode": ScanMode.METADATA_AND_LIVE_PROBE},
          {"models": [_row()], "live": True})],
    )
    run = controller.metadata_scan_selected(["prov-a"])
    assert run.wait(5.0)
    outcome = run.outcomes["prov-a"]
    assert outcome.status == STATUS_SCANNED
    assert outcome.action == ACTION_METADATA
    assert outcome.inference_calls == 0
    assert adapters["prov-a"].live_calls == 0
    assert adapters["prov-a"].metadata_calls == 1


def test_metadata_only_mode_scan_selected_keeps_inference_at_zero(tmp_path):
    registry, adapters, controller = _build(
        tmp_path,
        [("prov-a", {"mode": ScanMode.METADATA_ONLY}, {"models": [_row()], "live": True})],
    )
    run = controller.scan_selected(["prov-a"])
    assert run.wait(5.0)
    assert run.outcomes["prov-a"].status == STATUS_SCANNED
    assert adapters["prov-a"].live_calls == 0
    assert run.outcomes["prov-a"].inference_calls == 0


def test_adapter_that_claims_metadata_inference_is_rejected(tmp_path):
    registry, adapters, controller = _build(
        tmp_path, [("prov-a", {}, {"models": [_row()]})]
    )
    adapters["prov-a"].metadata_inference_calls = 2
    run = controller.metadata_scan_selected(["prov-a"])
    assert run.wait(5.0)
    outcome = run.outcomes["prov-a"]
    assert outcome.status == STATUS_FAILED
    assert outcome.error_class == "metadata_inference_violation"
    assert outcome.inference_calls == 0
    # The inventory was not applied from a violating result.
    assert registry.get("prov-a").models == []


def test_possible_billing_live_probe_is_refused_by_default(tmp_path):
    registry, adapters, controller = _build(tmp_path, [
        ("prov-a", {"live_probe_cost_risk": CostRisk.POSSIBLE_BILLING},
         {"models": [_row()], "live": True, "can_bill": True}),
    ])
    run = controller.validate_selected(["prov-a"])
    assert run.wait(5.0)
    outcome = run.outcomes["prov-a"]
    assert outcome.status == STATUS_SKIPPED
    assert outcome.reason == SKIP_REFUSED_COST_RISK
    assert adapters["prov-a"].live_calls == 0
    assert adapters["prov-a"].inference_calls == 0


def test_unknown_cost_live_probe_is_refused_and_consent_unlocks_it(tmp_path):
    registry, adapters, controller = _build(tmp_path, [
        ("prov-a", {"live_probe_cost_risk": CostRisk.UNKNOWN},
         {"models": [_row()], "live": True, "can_bill": True}),
    ])
    run = controller.validate_selected(["prov-a"])
    assert run.wait(5.0)
    assert run.outcomes["prov-a"].reason == SKIP_REFUSED_COST_RISK
    assert adapters["prov-a"].live_calls == 0

    _seed_inventory(registry, "prov-a", [_row()])
    registry.set_live_probe_consent("prov-a", True)
    run = controller.validate_selected(["prov-a"])
    assert run.wait(5.0)
    assert run.outcomes["prov-a"].status == STATUS_SCANNED
    assert adapters["prov-a"].live_calls == 1


def test_scan_selected_on_billing_risk_degrades_to_metadata_only(tmp_path):
    registry, adapters, controller = _build(tmp_path, [
        ("prov-a", {"live_probe_cost_risk": CostRisk.POSSIBLE_BILLING},
         {"models": [_row()], "live": True, "can_bill": True}),
    ])
    run = controller.scan_selected(["prov-a"])
    assert run.wait(5.0)
    outcome = run.outcomes["prov-a"]
    assert outcome.status == STATUS_SCANNED
    assert outcome.reason == SKIP_REFUSED_COST_RISK
    assert outcome.inference_calls == 0
    assert adapters["prov-a"].metadata_calls == 1
    assert adapters["prov-a"].live_calls == 0
    # Evidence from the permitted metadata pass is still recorded.
    assert [row["canonical_id"] for row in registry.get("prov-a").models] == [
        "prov-a/model-free"
    ]


def test_scan_selected_with_safe_live_cost_runs_metadata_then_canary(tmp_path):
    registry, adapters, controller = _build(tmp_path, [
        ("prov-a", {}, {"models": [_row()], "live": True}),
    ])
    run = controller.scan_selected(["prov-a"])
    assert run.wait(5.0)
    outcome = run.outcomes["prov-a"]
    assert outcome.status == STATUS_SCANNED
    assert adapters["prov-a"].metadata_calls == 1
    assert adapters["prov-a"].live_calls == 1
    assert outcome.inference_calls == 1
    assert registry.get("prov-a").last_live_probe_success_at


def test_canary_uses_the_adapter_preferred_target(tmp_path):
    registry, adapters, controller = _build(tmp_path, [
        ("prov-a", {}, {
            "models": [_row(model_id="first-free"), _row(model_id="verified-free")],
            "live": True, "canary_target": "verified-free",
        }),
    ])
    run = controller.scan_selected(["prov-a"])
    assert run.wait(5.0)
    assert adapters["prov-a"].live_targets == ["verified-free"]


def test_canary_falls_back_to_the_first_free_row(tmp_path):
    registry, adapters, controller = _build(tmp_path, [
        ("prov-a", {}, {"models": [_row(model_id="first-free")], "live": True}),
    ])
    run = controller.scan_selected(["prov-a"])
    assert run.wait(5.0)
    assert adapters["prov-a"].live_targets == ["first-free"]


def test_live_probe_reports_no_target_when_inventory_has_no_free_row(tmp_path):
    registry, adapters, controller = _build(tmp_path, [
        ("prov-a", {}, {"models": [_row(evidence=FreeEvidence.PAID)], "live": True}),
    ])
    run = controller.validate_selected(["prov-a"])
    assert run.wait(5.0)
    assert run.outcomes["prov-a"].status == STATUS_SKIPPED
    assert run.outcomes["prov-a"].reason == "no_free_model_to_canary"
    assert adapters["prov-a"].live_calls == 0


def test_successful_canary_clears_an_older_failure_verdict(tmp_path):
    registry, adapters, controller = _build(tmp_path, [
        ("prov-a", {}, {"models": [_row()], "live": True}),
    ])
    _seed_inventory(registry, "prov-a", [_row()])
    registry.record_metadata_result("prov-a", ok=False, error_class="timeout",
                                    error_summary="outage")
    assert registry.get("prov-a").last_status == "OUTAGE"

    run = controller.validate_selected(["prov-a"])
    assert run.wait(5.0)
    record = registry.get("prov-a")
    assert record.last_status == "OK"
    assert record.last_error_class == ""
    assert record.last_live_probe_success_at
    assert record.models[0]["provider_health"] == "HEALTHY"
    assert registry.eligible_tail_ids() == ["prov-a/model-free"]


def test_blocking_canary_failure_marks_the_lane_and_excludes_the_tail(tmp_path):
    registry, adapters, controller = _build(tmp_path, [
        ("prov-a", {}, {
            "models": [_row()], "live": True, "live_ok": False,
            "live_state": "BRIDGE_UPSTREAM_REJECTED",
            "blocking_health": ProviderHealth.AUTH_FAILED.value,
        }),
    ])
    _seed_inventory(registry, "prov-a", [_row()])
    assert registry.eligible_tail_ids() == ["prov-a/model-free"]

    run = controller.validate_selected(["prov-a"])
    assert run.wait(5.0)
    record = registry.get("prov-a")
    assert record.last_status == "AUTH_FAILED"
    assert record.models[0]["provider_health"] == "AUTH_FAILED"
    assert registry.eligible_tail_ids() == []
    # The FREE evidence itself is untouched by a health verdict.
    assert record.models[0]["free_evidence"] == "STRICT_FREE"


def test_transient_canary_failure_does_not_exclude_the_lane(tmp_path):
    registry, adapters, controller = _build(tmp_path, [
        ("prov-a", {}, {
            "models": [_row()], "live": True, "live_ok": False,
            "live_state": "BRIDGE_QUOTA",
        }),
    ])
    _seed_inventory(registry, "prov-a", [_row()])
    run = controller.validate_selected(["prov-a"])
    assert run.wait(5.0)
    record = registry.get("prov-a")
    assert record.last_status == "BRIDGE_QUOTA"
    assert record.models[0]["provider_health"] == "UNKNOWN"
    # Still better-than-zero eligible: quota is transient, not dead.
    assert registry.eligible_tail_ids() == ["prov-a/model-free"]


def test_validate_selected_refuses_a_provider_without_a_canary(tmp_path):
    registry, adapters, controller = _build(tmp_path, [
        ("prov-a", {}, {"models": [_row()], "live": False, "can_bill": True}),
    ])
    run = controller.validate_selected(["prov-a"])
    assert run.wait(5.0)
    assert run.outcomes["prov-a"].reason == SKIP_NO_LIVE_CANARY
    assert adapters["prov-a"].live_calls == 0


# ------------------------------------------------------------------- trust
def test_trusted_alive_suppresses_live_probe_but_allows_metadata_refresh(tmp_path):
    clock = [100_000.0]
    registry, adapters, controller = _build(
        tmp_path,
        [("prov-a", {"policy": ScanPolicy.ALWAYS}, {"models": [_row()], "live": True})],
        clock=lambda: clock[0],
    )
    assert registry.mark_trusted_alive("prov-a", "7d") is True

    run = controller.validate_selected(["prov-a"])
    assert run.wait(5.0)
    assert run.outcomes["prov-a"].reason == SKIP_TRUSTED_ALIVE
    assert adapters["prov-a"].live_calls == 0

    run = controller.scan_selected(["prov-a"])
    assert run.wait(5.0)
    assert run.outcomes["prov-a"].reason == SKIP_TRUSTED_ALIVE
    assert adapters["prov-a"].metadata_calls == 1
    assert adapters["prov-a"].live_calls == 0


def test_trusted_alive_never_creates_free_evidence_and_never_overrides_paid(tmp_path):
    clock = [100_000.0]
    registry, adapters, controller = _build(
        tmp_path, [("prov-a", {"policy": ScanPolicy.ALWAYS}, {"models": [_row()]})],
        clock=lambda: clock[0],
    )
    registry.mark_trusted_alive("prov-a", "until_cleared")
    # A trusted provider with no inventory and no metadata pass is NOT free.
    assert registry.get("prov-a").models == []
    assert registry.get("prov-a").trust_valid(clock[0]) is True
    assert registry.eligible_tail_ids() == []

    # Trust does not overwrite an authoritative PAID classification.
    registry.record_metadata_result("prov-a", ok=True, models=[
        _row(evidence=FreeEvidence.PAID, cost=CostRisk.POSSIBLE_BILLING)
    ])
    assert registry.get("prov-a").trust_valid(clock[0]) is True
    assert registry.get("prov-a").models[0]["free_evidence"] == FreeEvidence.PAID.value
    assert registry.eligible_tail_ids() == []


def test_trust_expiry_re_enables_live_validation(tmp_path):
    clock = [200_000.0]
    registry, adapters, controller = _build(
        tmp_path,
        [("prov-a", {"policy": ScanPolicy.ALWAYS}, {"models": [_row()], "live": True})],
        clock=lambda: clock[0],
    )
    _seed_inventory(registry, "prov-a", [_row()])
    registry.mark_trusted_alive("prov-a", "1h")
    run = controller.validate_selected(["prov-a"])
    assert run.wait(5.0)
    assert run.outcomes["prov-a"].reason == SKIP_TRUSTED_ALIVE
    assert adapters["prov-a"].live_calls == 0

    clock[0] += 3601
    run = controller.validate_selected(["prov-a"])
    assert run.wait(5.0)
    assert run.outcomes["prov-a"].status == STATUS_SCANNED
    assert adapters["prov-a"].live_calls == 1


def test_stale_only_policy_skips_a_fresh_provider(tmp_path):
    clock = [300_000.0]
    registry, adapters, controller = _build(
        tmp_path,
        [("prov-a", {"policy": ScanPolicy.STALE_ONLY}, {"models": [_row()]})],
        clock=lambda: clock[0],
    )
    run = controller.scan_selected(["prov-a"])
    assert run.wait(5.0)
    assert run.outcomes["prov-a"].status == STATUS_SCANNED
    assert adapters["prov-a"].metadata_calls == 1

    run = controller.scan_selected(["prov-a"])
    assert run.wait(5.0)
    assert run.outcomes["prov-a"].reason == SKIP_NOT_STALE
    assert adapters["prov-a"].metadata_calls == 1

    clock[0] += 6 * 3600 + 1
    run = controller.scan_selected(["prov-a"])
    assert run.wait(5.0)
    assert adapters["prov-a"].metadata_calls == 2


def test_policy_never_and_mode_disabled_block_every_action(tmp_path):
    registry, adapters, controller = _build(tmp_path, [
        ("prov-never", {"policy": ScanPolicy.NEVER}, {"models": [_row("prov-never")]}),
        ("prov-off", {"mode": ScanMode.DISABLED}, {"models": [_row("prov-off")]}),
    ])
    for kind, call in (
        ("scan", lambda: controller.scan_selected(["prov-never", "prov-off"])),
        ("metadata", lambda: controller.metadata_scan_selected(["prov-never", "prov-off"])),
        ("validate", lambda: controller.validate_selected(["prov-never", "prov-off"])),
    ):
        run = call()
        assert run.wait(5.0), kind
        assert all(o.status == STATUS_SKIPPED for o in run.outcomes.values()), kind
    assert adapters["prov-never"].metadata_calls == 0
    assert adapters["prov-off"].metadata_calls == 0


# ------------------------------------------------------- scheduling / safety
def test_backoff_from_retry_after_is_honoured_without_hammering(tmp_path):
    clock = [400_000.0]
    registry, adapters, controller = _build(
        tmp_path,
        [("prov-a", {}, {"metadata_error": "http_429", "retry_after": 60.0})],
        clock=lambda: clock[0],
    )
    run = controller.metadata_scan_selected(["prov-a"])
    assert run.wait(5.0)
    assert run.outcomes["prov-a"].status == STATUS_FAILED
    assert run.outcomes["prov-a"].retry_after_sec == 60.0
    assert adapters["prov-a"].metadata_calls == 1

    run = controller.metadata_scan_selected(["prov-a"])
    assert run.wait(5.0)
    assert run.outcomes["prov-a"].reason == SKIP_BACKOFF
    assert adapters["prov-a"].metadata_calls == 1

    clock[0] += 61
    run = controller.metadata_scan_selected(["prov-a"])
    assert run.wait(5.0)
    assert adapters["prov-a"].metadata_calls == 2


def test_source_outage_preserves_last_known_good_inventory(tmp_path):
    registry, adapters, controller = _build(
        tmp_path, [("prov-a", {}, {"models": [_row(), _row("prov-a", "second-free")]})]
    )
    run = controller.metadata_scan_selected(["prov-a"])
    assert run.wait(5.0)
    good = sorted(row["canonical_id"] for row in registry.get("prov-a").models)
    assert good == ["prov-a/model-free", "prov-a/second-free"]
    registry.mark_synced(good, "prov-a", "SAIFREN")

    adapters["prov-a"]._metadata_error = "timeout"
    run = controller.metadata_scan_selected(["prov-a"])
    assert run.wait(5.0)
    assert run.outcomes["prov-a"].status == STATUS_FAILED
    assert sorted(row["canonical_id"] for row in registry.get("prov-a").models) == good
    assert registry.pending_prune_ids("SAIFREN") == []
    assert registry.eligible_tail_ids() == good


def test_positive_dead_state_excludes_a_route_with_old_free_evidence(tmp_path):
    registry, adapters, controller = _build(tmp_path, [("prov-a", {}, {"models": [_row()]})])
    run = controller.metadata_scan_selected(["prov-a"])
    assert run.wait(5.0)
    assert registry.eligible_tail_ids() == ["prov-a/model-free"]

    registry.record_health_state("prov-a", ProviderHealth.AUTH_FAILED)
    assert registry.eligible_tail_ids() == []
    registry.record_health_state("prov-a", ProviderHealth.DEAD)
    assert registry.eligible_tail_ids() == []
    # AUTH/DEAD exclusion does not erase the evidence row itself.
    assert registry.get("prov-a").models[0]["free_evidence"] == "STRICT_FREE"


def test_unknown_health_strict_zero_cost_route_stays_tail_eligible(tmp_path):
    registry, adapters, controller = _build(tmp_path, [
        ("prov-a", {}, {"models": [_row(health=ProviderHealth.UNKNOWN)]}),
    ])
    run = controller.metadata_scan_selected(["prov-a"])
    assert run.wait(5.0)
    assert registry.eligible_tail_ids() == ["prov-a/model-free"]


def test_ambiguous_and_trial_evidence_never_reaches_the_tail(tmp_path):
    registry, adapters, controller = _build(tmp_path, [
        ("prov-a", {}, {"models": [
            _row(model_id="trial-only", evidence=FreeEvidence.CONDITIONAL_FREE),
            _row(model_id="mystery", evidence=FreeEvidence.UNKNOWN_COST),
        ]}),
    ])
    run = controller.metadata_scan_selected(["prov-a"])
    assert run.wait(5.0)
    assert registry.eligible_tail_ids() == []


def test_successful_authoritative_refresh_prunes_only_scanner_owned_synced_rows(tmp_path):
    registry, adapters, controller = _build(tmp_path, [
        ("prov-a", {}, {"models": [_row(), _row("prov-a", "second-free")]}),
    ])
    run = controller.metadata_scan_selected(["prov-a"])
    assert run.wait(5.0)
    registry.mark_synced(["prov-a/model-free", "prov-a/second-free"], "prov-a", "SAIFREN")
    registry.add_manual_model("prov-a", "prov-a/manual-entry")

    adapters["prov-a"].metadata_rows = [_row()]
    run = controller.metadata_scan_selected(["prov-a"])
    assert run.wait(5.0)
    pending = [row["canonical_id"] for row in registry.pending_prune_ids("SAIFREN")]
    assert pending == ["prov-a/second-free"]
    assert "prov-a/manual-entry" in [r["canonical_id"] for r in registry.get("prov-a").models]


def test_per_provider_single_flight_prevents_duplicate_concurrent_runs(tmp_path):
    gate = threading.Event()
    registry, adapters, controller = _build(
        tmp_path, [("prov-a", {}, {"models": [_row()], "gate": gate})], max_workers=4
    )
    first = controller.metadata_scan_selected(["prov-a"])
    deadline = time.time() + 5
    while adapters["prov-a"].metadata_calls == 0 and time.time() < deadline:
        time.sleep(0.01)

    second = controller.metadata_scan_selected(["prov-a"])
    assert second.wait(5.0)
    assert second.outcomes["prov-a"].reason == SKIP_SINGLE_FLIGHT
    assert adapters["prov-a"].metadata_calls == 1

    gate.set()
    assert first.wait(5.0)
    assert first.outcomes["prov-a"].status == STATUS_SCANNED


def test_bounded_global_concurrency(tmp_path):
    registry, adapters, controller = _build(
        tmp_path,
        [(f"prov-{i}", {}, {"models": [_row(f"prov-{i}")], "delay": 0.05})
         for i in range(6)],
        max_workers=2,
    )
    run = controller.metadata_scan_selected(list(adapters))
    assert run.wait(10.0)
    peak = max(adapter.max_concurrent for adapter in adapters.values())
    assert peak <= 2
    assert peak >= 1
    assert all(o.status == STATUS_SCANNED for o in run.outcomes.values())


def test_cancellation_terminates_boundedly_and_publishes_no_stale_generation(tmp_path):
    gate = threading.Event()
    registry, adapters, controller = _build(
        tmp_path, [("prov-a", {}, {"models": [_row()], "gate": gate})]
    )
    run = controller.scan_selected(["prov-a"])
    deadline = time.time() + 5
    while adapters["prov-a"].metadata_calls == 0 and time.time() < deadline:
        time.sleep(0.01)

    assert controller.stop(run) is True
    gate.set()
    assert run.wait(5.0) is True
    assert run.cancelled is True
    # The cancelled pass published no evidence.
    assert registry.get("prov-a").models == []

    # Generation gate: an older result can never overwrite a newer one.
    from core.free_scan_controller import ProviderScanOutcome

    stale = ProviderScanOutcome(provider_id="prov-a", generation=1, status=STATUS_SCANNED)
    fresh = ProviderScanOutcome(provider_id="prov-a", generation=7, status=STATUS_SCANNED)
    assert controller.apply_outcome(fresh) is True
    assert controller.apply_outcome(stale) is False
    assert controller.last_generation("prov-a") == 7
    assert controller.stale_rejections[-1]["provider_id"] == "prov-a"


def test_second_stage_never_runs_after_a_failed_metadata_refresh(tmp_path):
    registry, adapters, controller = _build(tmp_path, [
        ("prov-a", {}, {"metadata_error": "timeout", "live": True, "models": [_row()]}),
    ])
    run = controller.scan_selected(["prov-a"])
    assert run.wait(5.0)
    assert run.outcomes["prov-a"].status == STATUS_FAILED
    assert adapters["prov-a"].live_calls == 0


def test_no_providers_selected_is_rejected_without_any_call(tmp_path):
    registry, adapters, controller = _build(tmp_path, [("prov-a", {}, {"models": [_row()]})])
    run = controller.scan_selected([])
    assert run.wait(1.0)
    assert run.rejected_reason == "no_providers_selected"
    assert adapters["prov-a"].metadata_calls == 0
