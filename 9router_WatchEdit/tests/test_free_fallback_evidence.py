"""
FREE Fallback strict-free evidence + provider adapter tests
(FREE-FALLBACK-001, milestones 2, 3 and 10).

The point of these tests: "probably free", trial credit and unknown billing
must never become automatic strict FREE eligibility, and a discovery adapter
must never issue an inference call.
"""
from types import SimpleNamespace

import httpx

from core.free_evidence import (
    CostRisk,
    FreeEvidence,
    ProviderHealth,
    RoutingCapability,
    classify_free_evidence,
    tail_eligibility,
)
from core.free_provider_adapters import (
    ADAPTER_OPENCODE_LOCAL_FREE,
    ADAPTER_UNSUPPORTED,
    KiraAiAdapter,
    OpenCodeLocalFreeAdapter,
    OpenRouterAdapter,
    UnsupportedProviderAdapter,
    build_default_adapters,
    register_discovered_providers,
    resolve_adapter,
)
from core.free_provider_registry import ADAPTER_UNSUPPORTED_ID, FreeProviderRegistry


def _verdict(row):
    return classify_free_evidence(row)


# ------------------------------------------------------------------ evidence
def test_explicit_zero_pricing_is_strict_free():
    verdict = _verdict({
        "model_id": "vendor/cheap",
        "pricing": {"prompt": "0", "completion": "0", "request": "0"},
    })
    assert verdict.evidence == FreeEvidence.STRICT_FREE
    assert verdict.source == "explicit_zero_price"
    assert verdict.auto_tail_eligible is True


def test_official_free_route_suffix_is_strict_free():
    verdict = _verdict({"model_id": "vendor/model:free", "pricing": {"prompt": "0"}})
    assert verdict.evidence == FreeEvidence.STRICT_FREE
    assert verdict.source == "explicit_zero_price" or verdict.source == "explicit_free_route"


def test_catalogue_is_free_flag_is_strict_free():
    verdict = _verdict({"model_id": "vendor/model", "is_free": True})
    assert verdict.evidence == FreeEvidence.STRICT_FREE
    assert verdict.source == "provider_catalog_is_free"


def test_promotional_credit_and_trial_never_become_strict_free():
    for text in (
        "Free credits for new accounts",
        "Trial: first 100 requests free",
        "Signup bonus, limited-time promotional pricing",
        "Coupon applied, introductory offer",
    ):
        verdict = _verdict({"model_id": "vendor/model", "notes": text})
        assert verdict.evidence == FreeEvidence.CONDITIONAL_FREE, text
        assert verdict.strict is False
        assert verdict.auto_tail_eligible is False


def test_paid_metadata_wins_over_free_looking_names():
    assert _verdict({"model_id": "vendor/model-free", "is_free": False}).evidence == FreeEvidence.PAID
    assert _verdict({"model_id": "vendor/model-free",
                     "pricing": {"prompt": "0", "completion": "0.5"}}).evidence == FreeEvidence.PAID
    assert _verdict({"model_id": "vendor/model", "is_partner": True}).evidence == FreeEvidence.PAID


def test_ambiguous_metadata_stays_unknown_cost():
    verdict = _verdict({"model_id": "vendor/model", "notes": "great model"})
    assert verdict.evidence == FreeEvidence.UNKNOWN_COST
    assert verdict.auto_tail_eligible is False


def test_client_bound_free_id_is_not_a_generic_routable_free_model():
    verdict = _verdict({"model_id": "mimo-v2.5-free", "client_bound": True})
    assert verdict.evidence == FreeEvidence.CLIENT_BOUND_FREE
    assert verdict.source == "explicit_free_model_id_client_bound"
    assert verdict.auto_tail_eligible is False


def test_withdrawn_evidence_is_explicit():
    verdict = _verdict({"model_id": "vendor/model", "withdrawn": True})
    assert verdict.evidence == FreeEvidence.WITHDRAWN
    assert verdict.auto_tail_eligible is False


# ------------------------------------------------------------ tail eligibility
def test_tail_eligibility_rules():
    def eligible(evidence, health=ProviderHealth.UNKNOWN, routing=RoutingCapability.DIRECT_ROUTABLE,
                 cost=CostRisk.FREE_QUOTA_PROBE, bridge=False):
        return tail_eligibility(
            evidence, provider_health=health, routing=routing, cost_risk=cost,
            client_bound_bridge_eligible=bridge,
        )[0]

    assert eligible(FreeEvidence.STRICT_FREE) is True
    # "better than zero": UNKNOWN health does not disqualify a proven-zero route.
    assert eligible(FreeEvidence.STRICT_FREE, health=ProviderHealth.UNKNOWN) is True
    assert eligible(FreeEvidence.STRICT_FREE, health=ProviderHealth.DEAD) is False
    assert eligible(FreeEvidence.STRICT_FREE, health=ProviderHealth.AUTH_FAILED) is False
    assert eligible(FreeEvidence.STRICT_FREE, routing=RoutingCapability.UNSUPPORTED) is False
    assert eligible(FreeEvidence.STRICT_FREE, cost=CostRisk.POSSIBLE_BILLING) is False
    assert eligible(FreeEvidence.PAID) is False
    assert eligible(FreeEvidence.CONDITIONAL_FREE) is False
    assert eligible(FreeEvidence.UNKNOWN_COST) is False
    assert eligible(FreeEvidence.WITHDRAWN) is False
    assert eligible(FreeEvidence.CLIENT_BOUND_FREE,
                    routing=RoutingCapability.LOCAL_BRIDGE_REQUIRED) is False
    assert eligible(FreeEvidence.CLIENT_BOUND_FREE,
                    routing=RoutingCapability.LOCAL_BRIDGE_REQUIRED, bridge=True) is True
    assert eligible(FreeEvidence.CLIENT_BOUND_FREE,
                    routing=RoutingCapability.UNSUPPORTED, bridge=True) is False


def test_cost_risk_vocabulary_blocks_by_default():
    from core.free_evidence import cost_risk_blocks_live_probe

    assert cost_risk_blocks_live_probe(CostRisk.ZERO_MONETARY_METADATA) is False
    assert cost_risk_blocks_live_probe(CostRisk.FREE_QUOTA_PROBE) is False
    assert cost_risk_blocks_live_probe(CostRisk.ACCOUNT_CONDITIONAL) is True
    assert cost_risk_blocks_live_probe(CostRisk.POSSIBLE_BILLING) is True
    assert cost_risk_blocks_live_probe(CostRisk.UNKNOWN) is True


# ------------------------------------------------------------------- adapters
def _openrouter_transport(payload, status=200, headers=None):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(status, json=payload, headers=headers or {})

    return httpx.MockTransport(handler), calls


def test_openrouter_metadata_never_calls_inference_and_classifies_pricing():
    payload = {"data": [
        {"id": "vendor/free-zero", "name": "Zero",
         "pricing": {"prompt": "0", "completion": "0", "request": "0"}},
        {"id": "vendor/paid", "name": "Paid",
         "pricing": {"prompt": "0.000001", "completion": "0.000002"}},
        {"id": "vendor/unknown", "name": "Unknown"},
    ]}
    transport, calls = _openrouter_transport(payload)
    adapter = OpenRouterAdapter(transport=transport)
    result = adapter.metadata_scan()

    assert result.ok is True
    assert result.inference_calls == 0
    assert calls["n"] == 1
    by_id = {row["canonical_id"]: row for row in result.models}
    assert by_id["openrouter/vendor/free-zero"]["free_evidence"] == FreeEvidence.STRICT_FREE.value
    assert by_id["openrouter/vendor/paid"]["free_evidence"] == FreeEvidence.PAID.value
    assert by_id["openrouter/vendor/unknown"]["free_evidence"] == FreeEvidence.UNKNOWN_COST.value
    caps = adapter.capabilities()
    assert caps.supports_metadata is True
    assert caps.supports_live_canary is False
    assert caps.live_canary_can_bill is True


def test_openrouter_rate_limit_honours_retry_after_without_inference():
    transport, calls = _openrouter_transport({}, status=429, headers={"Retry-After": "45"})
    result = OpenRouterAdapter(transport=transport).metadata_scan()
    assert result.ok is False
    assert result.error_class == "http_429"
    assert result.retry_after_sec == 45.0
    assert result.inference_calls == 0
    assert calls["n"] == 1


def test_openrouter_malformed_payload_is_surfaced():
    transport, _ = _openrouter_transport({"unexpected": "shape"})
    result = OpenRouterAdapter(transport=transport).metadata_scan()
    assert result.ok is False
    assert result.error_class == "malformed_payload"


def test_kira_adapter_uses_explicit_catalogue_metadata_only():
    catalog = [
        {"provider": "kira-ai", "model": "kira-free", "is_free": True},
        {"provider": "kira-ai", "model": "kira-paid", "is_free": False},
        {"provider": "kira-ai", "model": "kira-mystery"},
        {"provider": "other", "model": "not-ours", "is_free": True},
    ]
    adapter = KiraAiAdapter(catalog_fetch=lambda: catalog)
    result = adapter.metadata_scan()
    assert result.ok is True
    assert result.inference_calls == 0
    by_id = {row["upstream_model_id"]: row for row in result.models}
    assert set(by_id) == {"kira-free", "kira-paid", "kira-mystery"}
    assert by_id["kira-free"]["free_evidence"] == FreeEvidence.STRICT_FREE.value
    assert by_id["kira-paid"]["free_evidence"] == FreeEvidence.PAID.value
    assert by_id["kira-mystery"]["free_evidence"] == FreeEvidence.UNKNOWN_COST.value
    caps = adapter.capabilities()
    assert caps.supports_live_canary is False
    assert caps.live_canary_can_bill is True


def test_kira_adapter_without_wiring_reports_unavailable_instead_of_guessing():
    adapter = KiraAiAdapter(catalog_fetch=None)
    result = adapter.metadata_scan()
    assert result.ok is False
    assert result.error_class == "metadata_source_unavailable"
    assert result.authoritative is False
    assert adapter.capabilities().supports_metadata is False


class _FakeOpenCodeCatalog:
    def __init__(self, models=None, status="API_OK", error_class=""):
        self.models = models or []
        self._status = status
        self._error_class = error_class
        self.refresh_calls = 0

    def refresh(self, force=False):
        self.refresh_calls += 1
        return {"fresh": True, "status": self._status, "changed": False,
                "diff": None, "fetch": True, "error_class": self._error_class}


class _FakeBridge:
    def __init__(self, state="BRIDGE_OK"):
        self.state = state
        self.calls = []

    def canary(self, model_id):
        self.calls.append(model_id)
        return SimpleNamespace(state=self.state, error_class="", detail="ok",
                               model_id=model_id)


def test_opencode_adapter_marks_free_models_client_bound_and_reuses_the_bridge():
    catalog = _FakeOpenCodeCatalog([
        SimpleNamespace(model_id="mimo-v2.5-free", free_candidate=True),
        SimpleNamespace(model_id="big-model", free_candidate=False),
    ])
    bridge = _FakeBridge()
    adapter = OpenCodeLocalFreeAdapter(catalog=catalog, bridge=bridge)

    result = adapter.metadata_scan()
    assert result.ok is True
    assert result.inference_calls == 0
    assert catalog.refresh_calls == 1
    by_id = {row["model_id"]: row for row in result.models}
    assert by_id["mimo-v2.5-free"]["free_evidence"] == FreeEvidence.CLIENT_BOUND_FREE.value
    assert by_id["mimo-v2.5-free"]["canonical_id"] == "ocf/mimo-v2.5-free"
    assert by_id["mimo-v2.5-free"]["routing"] == RoutingCapability.LOCAL_BRIDGE_REQUIRED.value
    assert by_id["big-model"]["free_evidence"] == FreeEvidence.UNKNOWN_COST.value

    caps = adapter.capabilities()
    assert caps.client_bound is True
    assert caps.supports_live_canary is True
    assert caps.live_canary_can_bill is False

    canary = adapter.live_canary("mimo-v2.5-free")
    assert canary.ok is True
    assert canary.inference_calls == 1
    assert bridge.calls == ["mimo-v2.5-free"]


def test_opencode_adapter_prefers_the_bridges_known_good_canary_model():
    from core.opencode_bridge import CANARY_MODEL

    rows = [
        {"upstream_model_id": "unlucky-free", "free_evidence": "CLIENT_BOUND_FREE"},
        {"upstream_model_id": CANARY_MODEL, "free_evidence": "CLIENT_BOUND_FREE"},
    ]
    adapter = OpenCodeLocalFreeAdapter(catalog=_FakeOpenCodeCatalog(), bridge=_FakeBridge())
    assert adapter.preferred_canary_target(rows) == CANARY_MODEL
    # Without the known-good model there is still a bounded fallback target.
    assert adapter.preferred_canary_target(rows[:1]) == "unlucky-free"
    assert adapter.preferred_canary_target([{"free_evidence": "PAID"}]) == ""


def test_opencode_adapter_surfaces_catalog_failure_without_looking_free():
    catalog = _FakeOpenCodeCatalog(status="API_FAILED", error_class="timeout")
    adapter = OpenCodeLocalFreeAdapter(catalog=catalog, bridge=_FakeBridge())
    result = adapter.metadata_scan()
    assert result.ok is False
    assert result.error_class == "timeout"
    assert result.models == []


def test_unsupported_adapter_makes_no_calls_and_stays_visible():
    adapter = UnsupportedProviderAdapter("gemini-cli", "Gemini CLI")
    caps = adapter.capabilities()
    assert adapter.adapter_id == ADAPTER_UNSUPPORTED
    assert caps.supports_metadata is False
    assert caps.supports_live_canary is False
    assert caps.live_canary_can_bill is False
    metadata = adapter.metadata_scan()
    assert metadata.ok is False
    assert metadata.authoritative is False
    assert metadata.network_calls == 0 and metadata.inference_calls == 0
    canary = adapter.live_canary("x")
    assert canary.ok is False and canary.inference_calls == 0


def test_unwired_known_adapter_is_reported_not_silently_free():
    adapters = build_default_adapters()  # nothing wired
    adapter = adapters["kira-ai"]
    assert adapter.capabilities().supports_metadata is False
    assert "NO FREE ADAPTER" not in adapter.capabilities().note  # adapter exists
    assert adapter.provider_id == "kira-ai"


def test_default_adapters_cover_the_three_real_provider_surfaces():
    adapters = build_default_adapters()
    assert set(adapters) == {"opencode", "kira-ai", "openrouter"}
    assert adapters["opencode"].adapter_id == ADAPTER_OPENCODE_LOCAL_FREE


# ------------------------------------------------------- registry integration
def test_discovered_providers_are_visible_but_never_auto_probed(tmp_path):
    registry = FreeProviderRegistry(path=tmp_path / "providers.json")
    adapters = build_default_adapters()
    added = register_discovered_providers(
        registry, adapters, ["gemini-cli", "cloudflare-ai", "gemini-cli"]
    )
    assert added == ["gemini-cli", "cloudflare-ai"]
    rows = {row["provider_id"]: row for row in registry.provider_rows()}
    for provider_id in ("gemini-cli", "cloudflare-ai"):
        row = rows[provider_id]
        assert row["enabled"] is False
        assert row["scan_policy"] == "NEVER"
        assert row["scan_mode"] == "DISABLED"
        assert row["metadata_cost_risk"] == CostRisk.UNKNOWN.value
        assert row["live_probe_cost_risk"] == CostRisk.UNKNOWN.value
        assert "NO FREE ADAPTER" in row["badges"]
        assert row["cost_safe"] is False
    assert resolve_adapter("gemini-cli", adapters).adapter_id == ADAPTER_UNSUPPORTED
    assert registry.get("gemini-cli").adapter == ADAPTER_UNSUPPORTED_ID


def test_registry_never_reinterprets_existing_operator_state(tmp_path):
    registry = FreeProviderRegistry(path=tmp_path / "providers.json")
    adapters = build_default_adapters()
    registry.ensure_provider("gemini-cli", default_enabled=True)
    registry.set_policy("gemini-cli", "MANUAL")
    register_discovered_providers(registry, adapters, ["gemini-cli"])
    record = registry.get("gemini-cli")
    assert record.enabled is True
    assert record.scan_policy == "MANUAL"
