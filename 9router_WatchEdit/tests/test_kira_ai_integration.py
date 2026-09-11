"""Focused offline contract tests for the Kira AI OpenAI-compatible profile."""
from unittest.mock import MagicMock

from core.classification import AvailabilityState, CostState, classify_probe_result
from core.discovery import DiscoveredModel, ModelDiscovery
from core.provider_profiles import (
    KIRA_AI_PRESET,
    compose_openai_endpoint,
    model_cost_hint,
    upstream_model_id,
)
from core.redaction import redact_text
from core.router_client import RouterClient


def test_kira_preset_contract():
    assert KIRA_AI_PRESET.id == "kira-ai"
    assert KIRA_AI_PRESET.display_name == "Kira AI"
    assert KIRA_AI_PRESET.base_url == "https://kiraai.vn/api/v1"
    assert KIRA_AI_PRESET.protocol == "openai"
    assert KIRA_AI_PRESET.auth_header == "Authorization"
    assert KIRA_AI_PRESET.auth_scheme == "bearer"
    assert KIRA_AI_PRESET.default_endpoint == "chat/completions"


def test_kira_endpoint_composition_is_exact_and_idempotent():
    expected = "https://kiraai.vn/api/v1/chat/completions"
    assert compose_openai_endpoint(KIRA_AI_PRESET.base_url) == expected
    assert compose_openai_endpoint("https://kiraai.vn/api/v1/") == expected
    assert compose_openai_endpoint(expected) == expected
    assert compose_openai_endpoint(KIRA_AI_PRESET.base_url, "/v1/chat/completions") == expected
    assert compose_openai_endpoint(KIRA_AI_PRESET.base_url, "/api/v1/chat/completions") == expected
    assert "/api/v1/v1/" not in expected
    assert "kiraai.vn/api/chat" not in expected


def test_kira_namespace_is_stripped_only_for_upstream_model_field():
    assert upstream_model_id("kira-ai/kira-auto") == "kira-auto"
    assert upstream_model_id("kira-auto") == "kira-auto"
    assert upstream_model_id("kira-ai/foo/bar") == "foo/bar"


def test_router_client_probe_uses_exact_kira_route_and_bearer(monkeypatch):
    captured = {}

    class FakeResponse:
        status_code = 200
        text = '{"choices":[{"message":{"content":"OK"}}]}'
        content = text.encode()

        def json(self):
            return {"choices": [{"message": {"content": "OK"}}]}

    class FakeClient:
        def __init__(self, **kwargs):
            captured["client_kwargs"] = kwargs

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, url, headers, json):
            captured.update(url=url, headers=headers, json=json)
            return FakeResponse()

    monkeypatch.setattr("core.router_client.httpx.Client", FakeClient)
    client = RouterClient(base_url="http://127.0.0.1:20128")
    client._get_headers = lambda include_bearer=False: {
        "Content-Type": "application/json",
        "Authorization": "Bearer test-key",
    }
    result = client.probe_chat_completion("kira-ai/kira-auto", provider_id="kira-ai")

    assert result["status"] == 200
    assert captured["url"] == "http://127.0.0.1:20128/v1/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer test-key"
    assert captured["json"]["model"] == "kira-auto"
    assert "test-key" not in redact_text(str(result))


def test_discovery_maps_kira_catalog_rows_and_metadata():
    client = MagicMock(spec=RouterClient)
    client.get_providers.return_value = [{
        "id": "kira-connection",
        "provider": "kira-ai",
        "name": "Kira AI",
        "isActive": True,
        "providerSpecificData": {},
    }]
    client.get_provider_nodes.return_value = []
    client.get_kv_scoped.return_value = []
    client.get_catalog_models.return_value = [
        {"provider": "kira-ai", "model": "kira-mini-1.0", "name": "Kira Mini 1.0", "is_free": True},
        {"provider": "kira-ai", "model": "kira-3.5-flash", "name": "Kira 3.5 Flash", "is_free": False, "price_input_vnd": 9000},
    ]
    client.get_combos.return_value = []

    models = ModelDiscovery(client).discover_all()
    by_id = {model.canonical_id: model for model in models}

    assert by_id["kira-ai/kira-mini-1.0"].model_id == "kira-mini-1.0"
    assert by_id["kira-ai/kira-mini-1.0"].cost_hint == CostState.FREE.value
    assert by_id["kira-ai/kira-3.5-flash"].cost_hint == CostState.PAID.value


def test_discovery_unavailable_does_not_delete_manual_kira_model():
    client = MagicMock(spec=RouterClient)
    client.get_providers.return_value = [{
        "id": "kira-connection",
        "provider": "kira-ai",
        "name": "Kira AI",
        "isActive": True,
        "providerSpecificData": {},
    }]
    client.get_provider_nodes.return_value = []
    client.get_kv_scoped.return_value = [("customModels", "kira-ai", '["kira-auto"]')]
    client.get_catalog_models.return_value = []
    client.get_combos.return_value = []
    client.get_connection_live_models_detailed.return_value = ("NOT_SUPPORTED", [])

    discovery = ModelDiscovery(client)
    models = discovery.discover_all(query_live=True)

    assert any(m.canonical_id == "kira-ai/kira-auto" for m in models)
    assert discovery.catalog_states["kira-connection"].value == "DISCOVERY_UNAVAILABLE"
    assert all(m.routing_eligible for m in models)


def test_kira_cost_is_model_specific_not_provider_global():
    free = classify_probe_result(
        200,
        raw_body='{"choices":[{"message":{"content":"OK"}}]}',
        parsed_json={"choices": [{"message": {"content": "OK"}}]},
        provider_prefix="kira-ai",
        model_id="kira-mini-1.0",
        cost_hint="FREE",
    )
    paid = classify_probe_result(
        200,
        raw_body='{"choices":[{"message":{"content":"OK"}}]}',
        parsed_json={"choices": [{"message": {"content": "OK"}}]},
        provider_prefix="kira-ai",
        model_id="kira-3.5-flash",
        cost_hint="PAID",
    )
    unknown_manual_free_name = classify_probe_result(
        200,
        raw_body='{"choices":[{"message":{"content":"OK"}}]}',
        parsed_json={"choices": [{"message": {"content": "OK"}}]},
        provider_prefix="kira-ai",
        model_id="kira-auto",
    )

    assert free.cost == CostState.FREE
    assert paid.cost == CostState.PAID
    assert unknown_manual_free_name.cost == CostState.UNKNOWN
    assert unknown_manual_free_name.availability == AvailabilityState.LIVE


def test_kira_metadata_requires_explicit_free_or_paid_evidence():
    assert model_cost_hint({"is_free": True}) == "FREE"
    assert model_cost_hint({"is_free": False}) == "PAID"
    assert model_cost_hint({"is_partner": True}) == "PAID"
    assert model_cost_hint({"id": "kira-auto"}) is None
    assert model_cost_hint({"id": "kira-auto", "price_input_vnd": 0, "price_output_vnd": 0}) is None


def test_kira_auth_and_transient_errors_are_not_dead():
    auth = classify_probe_result(401, raw_body='{"error":{"message":"Invalid API key"}}', parsed_json={"error": {"message": "Invalid API key"}})
    rate = classify_probe_result(429, raw_body='{"error":{"message":"Too many requests"}}', parsed_json={"error": {"message": "Too many requests"}})
    transient = classify_probe_result(503, raw_body='{"error":{"message":"upstream unavailable"}}', parsed_json={"error": {"message": "upstream unavailable"}})
    missing = classify_probe_result(404, raw_body='{"error":{"message":"model not found"}}', parsed_json={"error": {"message": "model not found"}})

    assert auth.availability == AvailabilityState.AUTH_REJECTED
    assert rate.availability == AvailabilityState.RATE_LIMITED
    assert transient.availability == AvailabilityState.PROVIDER_ERROR
    assert missing.availability == AvailabilityState.MODEL_MISSING
    assert all(result.availability != AvailabilityState.DEAD for result in (auth, rate, transient, missing))
