"""
Tests for core/history.py
"""
import pytest
from core.history import HealthCache, ModelHealthRecord
from core.classification import HealthState, Confidence, CostStatus, EvidenceRecord

def test_health_cache_streaks_and_persistence(tmp_path):
    cache_path = tmp_path / "test_cache.json"
    cache = HealthCache(cache_file=cache_path)

    cid = "ag/gemini-3.8-flash-high"
    
    # 1. First successful test
    ev1 = EvidenceRecord(
        state=HealthState.FREE_USE,
        confidence=Confidence.LIVE,
        cost_status=CostStatus.FREE,
        status_code=200,
        latency_ms=850.0,
        error_code="OK",
        reason="Success",
        raw_error="",
    )
    rec1 = cache.record_evidence(cid, "Antigravity", "gemini-3.8-flash-high", ev1)
    assert rec1.success_streak == 1
    assert rec1.failure_streak == 0
    assert rec1.last_success_at is not None

    # 2. Second successful test
    rec2 = cache.record_evidence(cid, "Antigravity", "gemini-3.8-flash-high", ev1)
    assert rec2.success_streak == 2
    assert rec2.failure_streak == 0

    # 3. Failure test
    ev_fail = EvidenceRecord(
        state=HealthState.RATE_LIMIT,
        confidence=Confidence.LIKELY_TEMPORARY,
        cost_status=CostStatus.UNKNOWN,
        status_code=429,
        latency_ms=200.0,
        error_code="rate_limit",
        reason="Rate limit exceeded",
        raw_error="429 Too Many Requests",
    )
    rec3 = cache.record_evidence(cid, "Antigravity", "gemini-3.8-flash-high", ev_fail)
    assert rec3.success_streak == 0
    assert rec3.failure_streak == 1

    # Reload from disk
    cache2 = HealthCache(cache_file=cache_path)
    loaded_rec = cache2.get(cid)
    assert loaded_rec is not None
    assert loaded_rec.failure_streak == 1
    assert loaded_rec.state == HealthState.RATE_LIMIT.value

def test_cost_override(tmp_path):
    cache = HealthCache(cache_file=tmp_path / "cache.json")
    cid = "custom/model-1"

    ev = EvidenceRecord(
        state=HealthState.PAID,
        confidence=Confidence.LIVE,
        cost_status=CostStatus.PAID,
        status_code=200,
        latency_ms=1000.0,
        error_code="OK",
        reason="Success",
        raw_error="",
    )
    cache.record_evidence(cid, "Custom", "model-1", ev)

    # Set manual override to FREE
    cache.set_cost_override(cid, "FREE")
    rec = cache.get(cid)
    assert rec.cost_override == "FREE"
    assert rec.state == HealthState.FREE_USE.value
    assert rec.cost_status == CostStatus.FREE.value
