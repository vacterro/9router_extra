"""
Tests for Correctness + Control-Plane Repair Wave (Acceptance Gates A-J)
"""
import pytest
import asyncio
from unittest.mock import MagicMock, patch

from core.classification import (
    AvailabilityState,
    CostState,
    Confidence,
    EvidenceCounters,
    classify_probe_result,
    get_ui_badge,
)
from core.history import HealthCache, ModelHealthRecord
from core.router_client import RouterClient
from core.combo_manager import StableCombo, compute_combo_diff, PresetManager
from core.discovery import DiscoveredModel
from core.probe import ScannerWorker, ScanMode, ProviderCircuitBreaker

# Gate A: RATE_LIMIT -> TEMP_ERROR -> 404 does NOT become DEAD
def test_gate_a_transient_failures_do_not_become_dead():
    counters = EvidenceCounters()

    # 1. RATE_LIMIT (429)
    res1 = classify_probe_result(
        status_code=429,
        latency_ms=150.0,
        raw_body='{"error":{"message":"Rate limit exceeded"}}',
        parsed_json={"error": {"message": "Rate limit exceeded"}},
        previous_counters=counters,
    )
    assert res1.availability == AvailabilityState.RATE_LIMIT
    assert res1.availability != AvailabilityState.DEAD
    assert res1.counters.consecutive_rate_limit == 1
    assert res1.counters.consecutive_model_missing == 0

    # 2. TEMP_ERROR (502 Bad Gateway)
    res2 = classify_probe_result(
        status_code=502,
        latency_ms=300.0,
        raw_body="Bad Gateway",
        previous_counters=res1.counters,
    )
    assert res2.availability == AvailabilityState.TEMP_ERROR
    assert res2.availability != AvailabilityState.DEAD
    assert res2.counters.consecutive_model_missing == 0

    # 3. Naked 404 (Not Found without explicit semantic model_not_found)
    res3 = classify_probe_result(
        status_code=404,
        latency_ms=250.0,
        raw_body="404 Not Found",
        previous_counters=res2.counters,
    )
    assert res3.availability == AvailabilityState.ROUTE_ERROR
    assert res3.availability != AvailabilityState.DEAD
    assert res3.counters.consecutive_model_missing == 0

# Gate B: Repeated semantic model_not_found (>=3) becomes DEAD
def test_gate_b_repeated_semantic_model_missing_becomes_dead():
    counters = EvidenceCounters()
    missing_body = '{"error":{"code":"model_not_found","message":"The requested model was not found."}}'
    parsed = {"error": {"code": "model_not_found", "message": "The requested model was not found."}}

    # Streak 1
    res1 = classify_probe_result(
        status_code=404, latency_ms=100.0, raw_body=missing_body, parsed_json=parsed,
        previous_counters=counters
    )
    assert res1.availability == AvailabilityState.MODEL_MISSING
    assert res1.counters.consecutive_model_missing == 1

    # Streak 2
    res2 = classify_probe_result(
        status_code=404, latency_ms=100.0, raw_body=missing_body, parsed_json=parsed,
        previous_counters=res1.counters
    )
    assert res2.availability == AvailabilityState.MODEL_MISSING
    assert res2.counters.consecutive_model_missing == 2

    # Streak 3 -> DEAD
    res3 = classify_probe_result(
        status_code=404, latency_ms=100.0, raw_body=missing_body, parsed_json=parsed,
        previous_counters=res2.counters
    )
    assert res3.availability == AvailabilityState.DEAD
    assert res3.confidence == Confidence.MODEL_DEAD
    assert res3.counters.consecutive_model_missing == 3

# Gate C: Naked 404 is ROUTE_ERROR, not MODEL_MISSING
def test_gate_c_naked_404_is_route_error():
    res = classify_probe_result(
        status_code=404,
        latency_ms=200.0,
        raw_body="Cannot POST /v1/chat/completions",
    )
    assert res.availability == AvailabilityState.ROUTE_ERROR
    # CORE-003: ROUTE_ERROR and MODEL_MISSING are DISTINCT values now, so the
    # naked-404 vs semantic-model-missing distinction is proven by identity.
    assert res.availability != AvailabilityState.MODEL_MISSING
    assert res.availability != AvailabilityState.ENDPOINT_OR_MODEL_INVALID
    assert res.availability != AvailabilityState.MODEL_INVALID
    assert res.counters.consecutive_route_error == 1
    assert res.counters.consecutive_model_missing == 0

# Gate D: Unknown successful provider remains UNKNOWN cost / USE/?
def test_gate_d_unknown_successful_provider_remains_use_unknown():
    res = classify_probe_result(
        status_code=200,
        latency_ms=650.0,
        raw_body='{"choices":[{"message":{"content":"OK"}}]}',
        parsed_json={"choices": [{"message": {"content": "OK"}}]},
        provider_prefix="unregistered-provider",
        model_id="custom-llm-1",
    )
    assert res.availability == AvailabilityState.LIVE
    assert res.cost == CostState.UNKNOWN
    assert res.state == "USE/?"

# Gate E: Cancellation terminates scanner immediately
# W2-001 update: cancellation is session-specific. A cancel request with no
# active session is a rejected no-op (never pre-cancels a future session);
# cancelling the active session terminates it promptly.
def test_gate_e_scanner_cancellation(tmp_path):
    client = RouterClient()
    cache = HealthCache(cache_file=tmp_path / "cache.json")
    worker = ScannerWorker(client, cache)

    models = [
        DiscoveredModel("p/m1", "p", "p", "c1", "m1", "m1"),
        DiscoveredModel("p/m2", "p", "p", "c1", "m2", "m2"),
        DiscoveredModel("p/m3", "p", "p", "c1", "m3", "m3"),
    ]

    # No active session: a bare cancel must NOT pre-cancel a future session.
    assert worker.cancel() is False
    assert worker.session_controller.session_counter == 0

    # Claim a session lease, cancel exactly it, then run: it terminates as
    # cancelled and the lease owner releases exactly once (W2-001 B3: run_scan
    # executes; the party that claimed the lease owns the release).
    lease = worker.claim_execution()
    assert lease is not None
    assert worker.cancel(lease.session_id) is True

    try:
        worker.run_scan(
            models, ScanMode.FULL,
            session_id=lease.session_id, execution=lease,
        )
    finally:
        worker.end_execution(lease)
    assert worker.is_running() is False
    assert worker.session_controller.active_session is None
    assert lease.released is True

# Gate F: Provider Circuit Breaker trips on AUTH/BALANCE
def test_gate_f_circuit_breaker():
    breaker = ProviderCircuitBreaker()
    assert breaker.is_tripped("conn-1") is False

    # Trip on AUTH
    breaker.record_result("conn-1", AvailabilityState.AUTH)
    assert breaker.is_tripped("conn-1") is True
    assert breaker.get_inherited_state("conn-1") == AvailabilityState.AUTH

    # Separate connection is healthy
    assert breaker.is_tripped("conn-2") is False

# Gate G: Two-stage timeouts
def test_gate_g_two_stage_timeout_configuration(tmp_path):
    client = RouterClient()
    cache = HealthCache(cache_file=tmp_path / "cache.json")
    worker = ScannerWorker(client, cache)
    assert worker.fast_timeout == 4.0
    assert worker.slow_timeout == 15.0

# Gate H: Fail-closed combo update: API failure does NOT touch SQLite
def test_gate_h_fail_closed_combo_update(tmp_path):
    client = RouterClient(db_path=tmp_path / "fake.sqlite")
    
    # Mock httpx PUT to fail
    with patch("httpx.put", side_effect=Exception("API connection refused")):
        res = client.update_combo("combo-1", "TestCombo", ["m1", "m2"])
        assert res is None
        # Assert fake sqlite file was NEVER created or touched
        assert not (tmp_path / "fake.sqlite").exists()

# Gate I: Optimistic Concurrency Conflict Detection
def test_gate_i_optimistic_concurrency_detection():
    combo = StableCombo(
        combo_id="c1",
        name="MainCombo",
        models=["m1", "m2"],
        baseline_models=["m1", "m2"],
        baseline_updated_at="2026-09-01T10:00:00Z",
    )
    assert combo.has_unsaved_changes() is False

    # Make local modification
    combo.add_model("m3")
    assert combo.has_unsaved_changes() is True
    assert combo.models == ["m1", "m2", "m3"]
    assert combo.baseline_models == ["m1", "m2"]

    # Revert local changes
    combo.revert_unsaved_changes()
    assert combo.models == ["m1", "m2"]
    assert combo.has_unsaved_changes() is False

# Gate J: Preset comparison and health annotation
def test_gate_j_preset_comparison_and_annotation(tmp_path):
    pm = PresetManager(presets_file=tmp_path / "presets.json")
    pm.save_preset("MY_PRESET", ["model-1", "model-2"])

    cache = HealthCache(cache_file=tmp_path / "cache.json")
    cache.record_evidence(
        "model-1", "p", "model-1",
        classify_probe_result(200, 500.0, '{"choices":[{"message":{"content":"hi"}}]}', parsed_json={"choices": [{"message": {"content": "hi"}}]}, provider_prefix="ag", model_id="model-1")
    )

    comparison = pm.compare_with_combo("MY_PRESET", ["model-1"], cache)
    assert len(comparison) == 2
    assert comparison[0]["model"] == "model-1"
    assert comparison[0]["in_combo"] is True
    assert comparison[0]["state"] == "FREE/USE"

    assert comparison[1]["model"] == "model-2"
    assert comparison[1]["in_combo"] is False
    assert comparison[1]["state"] == "UNKNOWN"
