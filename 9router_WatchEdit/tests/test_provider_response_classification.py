"""Regression fixtures for provider-layer response classification."""

import json
from pathlib import Path

import pytest

from core.classification import (
    AuthState,
    AvailabilityState,
    CatalogState,
    Confidence,
    ReachabilityState,
    classify_probe_result,
    classify_provider_state,
)


FIXTURE = Path(__file__).parent / "fixtures" / "provider_response_classification.json"


def _classify(case):
    body = case.get("body", "")
    parsed = case.get("parsed")
    if parsed is None:
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError:
            parsed = None
    return classify_probe_result(
        status_code=case["status"],
        raw_body=body,
        parsed_json=parsed,
        response_headers=case.get("headers"),
        is_timeout=case.get("timeout", False),
    )


@pytest.mark.parametrize(
    ("case_name", "expected"),
    [
        ("live_completion", AvailabilityState.LIVE),
        ("auth_rejected", AvailabilityState.AUTH_REJECTED),
        ("access_forbidden", AvailabilityState.ACCESS_FORBIDDEN),
        ("waf_blocked", AvailabilityState.WAF_BLOCKED),
        ("endpoint_or_model_invalid", AvailabilityState.ENDPOINT_OR_MODEL_INVALID),
        ("model_gone", AvailabilityState.MODEL_GONE),
        ("router_degraded", AvailabilityState.ROUTER_DEGRADED),
        ("rate_limited", AvailabilityState.RATE_LIMITED),
        ("balance_required", AvailabilityState.BALANCE_REQUIRED),
        ("provider_error", AvailabilityState.PROVIDER_ERROR),
        ("dns_failure", AvailabilityState.DNS_FAILURE),
        ("connect_timeout", AvailabilityState.CONNECT_TIMEOUT),
        ("non_api_html", AvailabilityState.NON_API_HTML_RESPONSE),
    ],
)
def test_provider_response_fixture_classification(case_name, expected):
    cases = json.loads(FIXTURE.read_text(encoding="utf-8"))
    result = _classify(cases[case_name])
    assert result.availability == expected
    assert result.availability != AvailabilityState.DEAD


def test_waf_is_not_auth_rejected():
    cases = json.loads(FIXTURE.read_text(encoding="utf-8"))
    result = _classify(cases["waf_blocked"])
    assert result.error_code == "WAF_BLOCKED"
    assert result.confidence == Confidence.LIKELY_TEMPORARY


def test_nested_router_503_preserves_upstream_waf_classification():
    body = {
        "ok": False,
        "status": 503,
        "error": "HTTP 503: [provider/model] [403]: <!DOCTYPE html><!--[if lt IE 7]> Attention Required! | Cloudflare You have been blocked",
    }
    result = classify_probe_result(
        status_code=200,
        raw_body=json.dumps(body),
        parsed_json=body,
        response_headers={"content-type": "application/json"},
    )
    assert result.availability == AvailabilityState.WAF_BLOCKED
    assert result.status_code == 403


def test_catalog_failure_does_not_poison_successful_completion():
    cases = json.loads(FIXTURE.read_text(encoding="utf-8"))
    completion = _classify(cases["live_completion"])
    summary = classify_provider_state(
        completion,
        models_status_code=403,
        models_raw_body=cases["waf_blocked"]["body"],
    )
    assert summary.provider_state == AvailabilityState.LIVE
    assert summary.model_state == AvailabilityState.LIVE
    assert summary.discovery_state == "MODEL_DISCOVERY_UNAVAILABLE"


def test_catalog_success_exposes_invalid_model_without_killing_provider():
    cases = json.loads(FIXTURE.read_text(encoding="utf-8"))
    invalid_model = _classify(cases["endpoint_or_model_invalid"])
    summary = classify_provider_state(invalid_model, models_status_code=200)
    assert summary.provider_state == AvailabilityState.LIVE
    assert summary.model_state == AvailabilityState.MODEL_INVALID


def test_models_200_empty_is_not_models_api_pass():
    cases = json.loads(FIXTURE.read_text(encoding="utf-8"))
    completion = _classify(cases["live_completion"])
    summary = classify_provider_state(
        completion,
        models_status_code=cases["models_empty"]["status"],
        models_raw_body=cases["models_empty"]["body"],
    )

    assert summary.reachability == ReachabilityState.REACHABLE
    assert summary.auth == AuthState.AUTH_OK
    assert summary.catalog == CatalogState.EMPTY_MODEL_CATALOG
    assert summary.discovery_state == CatalogState.EMPTY_MODEL_CATALOG.value
    assert summary.completion_state == AvailabilityState.LIVE
    assert summary.usable is False
    assert summary.active_routing is False


def test_models_200_nonempty_catalog_can_be_routable_after_live_completion():
    cases = json.loads(FIXTURE.read_text(encoding="utf-8"))
    completion = _classify(cases["live_completion"])
    summary = classify_provider_state(
        completion,
        models_status_code=cases["models_available"]["status"],
        models_raw_body=cases["models_available"]["body"],
    )

    assert summary.reachability == ReachabilityState.REACHABLE
    assert summary.auth == AuthState.AUTH_OK
    assert summary.catalog == CatalogState.MODELS_AVAILABLE
    assert summary.completion_state == AvailabilityState.LIVE
    assert summary.usable is True
    assert summary.active_routing is True


def test_empty_catalog_plus_waf_keeps_dimensions_independent():
    cases = json.loads(FIXTURE.read_text(encoding="utf-8"))
    completion = _classify(cases["waf_blocked"])
    summary = classify_provider_state(
        completion,
        models_status_code=cases["models_empty"]["status"],
        models_raw_body=cases["models_empty"]["body"],
    )

    assert summary.reachability == ReachabilityState.REACHABLE
    assert summary.catalog == CatalogState.EMPTY_MODEL_CATALOG
    assert summary.completion_state == AvailabilityState.WAF_BLOCKED
    assert summary.usable is False
    assert summary.active_routing is False


def test_empty_catalog_plus_200_completion_is_edge_case_but_not_usable():
    cases = json.loads(FIXTURE.read_text(encoding="utf-8"))
    completion = _classify(cases["live_completion"])
    summary = classify_provider_state(
        completion,
        models_status_code=cases["models_empty"]["status"],
        models_raw_body=cases["models_empty"]["body"],
    )

    # A verified completion cannot override the authoritative empty catalog.
    assert summary.completion_state == AvailabilityState.LIVE
    assert summary.catalog == CatalogState.EMPTY_MODEL_CATALOG
    assert summary.usable is False
