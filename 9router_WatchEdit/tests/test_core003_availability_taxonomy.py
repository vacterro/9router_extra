"""
CORE-003 regressions - one coherent availability taxonomy.

Defect (audit/3.md CORE-003): ENDPOINT_OR_MODEL_INVALID, ROUTE_ERROR and
MODEL_MISSING were the SAME Enum object, so EvidenceRecord(ROUTE_ERROR) took
the MODEL_MISSING counter branch (consecutive_model_missing=1,
consecutive_route_error=0) and equality-based tests could not prove the
semantic-vs-naked-404 distinction the classifier already implements. UI
consumers (STATE_COLORS, Attention filter) remained partly on legacy strings.

Contract under test: distinct identity/value for ROUTE_ERROR and
MODEL_MISSING; each EvidenceRecord seeds exactly its own counter; the
classifier's semantic 404 and naked 404 produce distinct values AND counters;
legacy persisted broad values load without guessing a subtype.
"""
import pytest

from core.classification import (
    AvailabilityState,
    HealthState,
    EvidenceRecord,
    classify_probe_result,
)
from ui.theme import STATE_COLORS


def test_route_error_and_model_missing_are_distinct_identity():
    assert AvailabilityState.MODEL_MISSING is not AvailabilityState.ROUTE_ERROR
    assert AvailabilityState.MODEL_MISSING is not AvailabilityState.ENDPOINT_OR_MODEL_INVALID
    assert AvailabilityState.ROUTE_ERROR is not AvailabilityState.ENDPOINT_OR_MODEL_INVALID
    assert AvailabilityState.MODEL_MISSING.value != AvailabilityState.ROUTE_ERROR.value
    assert AvailabilityState.MODEL_MISSING.value == "MODEL_MISSING"
    assert AvailabilityState.ROUTE_ERROR.value == "ROUTE_ERROR"


def test_healthstate_taxonomy_matches_availability_state():
    assert HealthState.MODEL_MISSING is not HealthState.ROUTE_ERROR
    assert HealthState.MODEL_MISSING.value == "MODEL_MISSING"
    assert HealthState.ROUTE_ERROR.value == "ROUTE_ERROR"


def test_evidence_record_route_error_increments_only_route_counter():
    rec = EvidenceRecord(availability=AvailabilityState.ROUTE_ERROR)
    assert rec.counters.consecutive_route_error == 1
    assert rec.counters.consecutive_model_missing == 0


def test_evidence_record_model_missing_increments_only_model_counter():
    rec = EvidenceRecord(availability=AvailabilityState.MODEL_MISSING)
    assert rec.counters.consecutive_model_missing == 1
    assert rec.counters.consecutive_route_error == 0


def _semantic_404():
    return classify_probe_result(
        status_code=404,
        latency_ms=100.0,
        raw_body='{"error":{"code":"model_not_found","message":"The requested model was not found."}}',
        parsed_json={"error": {"code": "model_not_found", "message": "The requested model was not found."}},
    )


def _naked_404():
    return classify_probe_result(
        status_code=404,
        latency_ms=100.0,
        raw_body="Cannot POST /v1/chat/completions",
    )


def test_semantic_404_yields_model_missing_and_model_counter():
    res = _semantic_404()
    assert res.availability == AvailabilityState.MODEL_MISSING
    assert res.availability != AvailabilityState.ROUTE_ERROR
    assert res.counters.consecutive_model_missing == 1
    assert res.counters.consecutive_route_error == 0


def test_naked_404_yields_route_error_and_route_counter():
    res = _naked_404()
    assert res.availability == AvailabilityState.ROUTE_ERROR
    assert res.availability != AvailabilityState.MODEL_MISSING
    assert res.counters.consecutive_route_error == 1
    assert res.counters.consecutive_model_missing == 0


def test_semantic_and_naked_404_are_distinct_values_and_counters():
    semantic = _semantic_404()
    naked = _naked_404()
    assert semantic.availability != naked.availability
    assert semantic.counters.consecutive_model_missing == 1
    assert semantic.counters.consecutive_route_error == 0
    assert naked.counters.consecutive_route_error == 1
    assert naked.counters.consecutive_model_missing == 0


def test_legacy_broad_string_loads_without_guessing_subtype():
    """Old persisted broad value must stay broad, never become a fabricated subtype."""
    rec = EvidenceRecord(availability="ENDPOINT_OR_MODEL_INVALID")
    assert rec.availability == AvailabilityState.ENDPOINT_OR_MODEL_INVALID
    assert rec.availability != AvailabilityState.MODEL_MISSING
    assert rec.availability != AvailabilityState.ROUTE_ERROR
    # No counter inferred for the undistinguished broad state.
    assert rec.counters.consecutive_model_missing == 0
    assert rec.counters.consecutive_route_error == 0


@pytest.mark.parametrize(
    "canonical",
    [
        "BALANCE_REQUIRED", "AUTH_REJECTED", "RATE_LIMITED", "CONNECT_TIMEOUT",
        "PROVIDER_ERROR", "ENDPOINT_OR_MODEL_INVALID", "MODEL_INVALID",
        "MODEL_MISSING", "ROUTE_ERROR", "DEAD", "UNKNOWN",
    ],
)
def test_every_canonical_non_live_state_has_presentation(canonical):
    assert canonical in STATE_COLORS, f"canonical state {canonical!r} has no STATE_COLORS entry"
