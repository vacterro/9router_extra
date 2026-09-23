"""
Tests for core/classification.py
"""
import pytest
from core.classification import (
    HealthState,
    Confidence,
    CostStatus,
    classify_probe_result,
    is_provider_or_model_known_free,
)
from core.probe import is_reasoning_model

def test_classify_success_free_provider():
    res = classify_probe_result(
        status_code=200,
        latency_ms=850.0,
        raw_body='{"choices":[{"message":{"content":"OK"}}]}',
        parsed_json={"choices": [{"message": {"content": "OK"}}]},
        provider_prefix="ag",
        model_id="gemini-3.8-flash-high",
    )
    assert res.state == HealthState.FREE_USE
    assert res.confidence == Confidence.LIVE
    assert res.cost_status == CostStatus.FREE
    assert res.status_code == 200

def test_classify_success_paid_provider():
    res = classify_probe_result(
        status_code=200,
        latency_ms=1200.0,
        raw_body='{"choices":[{"message":{"content":"Hello"}}]}',
        parsed_json={"choices": [{"message": {"content": "Hello"}}]},
        provider_prefix="openai",
        model_id="gpt-4o",
    )
    assert res.state == HealthState.PAID
    assert res.cost_status == CostStatus.PAID
    assert res.confidence == Confidence.LIVE

def test_classify_success_reasoning_soft_pass():
    # Reasoning model returns length limit but thinking was present
    res = classify_probe_result(
        status_code=200,
        latency_ms=2500.0,
        raw_body='{"choices":[{"finish_reason":"length","message":{"content":"","reasoning":"Thinking..."}}]}',
        parsed_json={"choices": [{"finish_reason": "length", "message": {"content": "", "reasoning": "Thinking..."}}]},
        provider_prefix="tb",
        model_id="claude-opus-5-thinking",
    )
    assert res.state in (HealthState.FREE_USE, HealthState.PAID, HealthState.USE_UNKNOWN)
    assert "soft-pass" in res.note

def test_classify_balance_exhausted_402():
    res = classify_probe_result(
        status_code=402,
        latency_ms=300.0,
        raw_body='{"error":{"message":"Insufficient balance"}}',
        parsed_json={"error": {"message": "Insufficient balance"}},
        provider_prefix="seekai",
        model_id="deepseek-v4",
    )
    assert res.state == HealthState.BALANCE
    assert res.confidence == Confidence.CONFIG_ERROR
    assert res.state != HealthState.DEAD

def test_classify_balance_via_keyword_in_400():
    res = classify_probe_result(
        status_code=400,
        latency_ms=310.0,
        raw_body='{"error":{"message":"You have exceeded your current quota, please check your plan and billing details."}}',
        parsed_json={"error": {"message": "You have exceeded your current quota, please check your plan and billing details."}},
        provider_prefix="tabi",
        model_id="claude-3-5-sonnet",
    )
    assert res.state == HealthState.BALANCE
    assert res.state != HealthState.DEAD

def test_classify_rate_limit_429():
    res = classify_probe_result(
        status_code=429,
        latency_ms=180.0,
        raw_body='{"error":{"message":"Rate limit exceeded: 5 requests per second allowed."}}',
        parsed_json={"error": {"message": "Rate limit exceeded"}},
        provider_prefix="gorouter",
        model_id="model-x",
    )
    assert res.state == HealthState.RATE_LIMIT
    assert res.confidence == Confidence.LIKELY_TEMPORARY
    assert res.state != HealthState.DEAD

def test_classify_auth_failure_401():
    res = classify_probe_result(
        status_code=401,
        latency_ms=150.0,
        raw_body='{"error":{"message":"Invalid API key provided."}}',
        parsed_json={"error": {"message": "Invalid API key provided."}},
        provider_prefix="custom",
        model_id="model-y",
    )
    assert res.state == HealthState.AUTH
    assert res.confidence == Confidence.CONFIG_ERROR
    assert res.state != HealthState.DEAD

def test_classify_timeout():
    res = classify_probe_result(
        status_code=408,
        latency_ms=15000.0,
        raw_body="",
        is_timeout=True,
    )
    assert res.state == HealthState.TIMEOUT
    assert res.confidence == Confidence.LIKELY_TEMPORARY
    assert res.state != HealthState.DEAD

def test_classify_model_missing_streak_progression():
    # 1st failure: MODEL_MISSING, NOT DEAD
    res1 = classify_probe_result(
        status_code=404,
        latency_ms=200.0,
        raw_body='{"error":{"message":"Requested entity was not found."}}',
        previous_streak=0,
    )
    assert res1.state == HealthState.MODEL_MISSING

    # 2nd failure: still MODEL_MISSING
    res2 = classify_probe_result(
        status_code=404,
        latency_ms=200.0,
        raw_body='{"error":{"message":"Requested entity was not found."}}',
        previous_streak=1,
    )
    assert res2.state == HealthState.MODEL_MISSING

    # 3rd failure (streak >= 3): DEAD with strong evidence
    res3 = classify_probe_result(
        status_code=404,
        latency_ms=200.0,
        raw_body='{"error":{"message":"Requested entity was not found."}}',
        previous_streak=2,
    )
    assert res3.state == HealthState.DEAD
    assert res3.confidence == Confidence.MODEL_DEAD

def test_classify_cost_override():
    res = classify_probe_result(
        status_code=200,
        latency_ms=500.0,
        raw_body='{"choices":[{"message":{"content":"Hi"}}]}',
        parsed_json={"choices": [{"message": {"content": "Hi"}}]},
        provider_prefix="openai",
        model_id="gpt-4o",
        cost_override="FREE",
    )
    assert res.state == HealthState.FREE_USE
    assert res.cost_status == CostStatus.FREE

def test_reasoning_model_detection():
    """Test reasoning model pattern matching for extended timeout support."""
    # SWE-1.6 Slow should be detected as reasoning model
    assert is_reasoning_model("cog/swe-1.6-slow")
    assert is_reasoning_model("cog/SWE-1.6-SLOW")
    
    # Generic reasoning patterns
    assert is_reasoning_model("claude-opus-5-thinking")
    assert is_reasoning_model("gpt-4o-reasoning")
    assert is_reasoning_model("nvidia/nemotron-3-nano-omni-30b-a3b-reasoning")
    
    # Non-reasoning models should not match
    assert not is_reasoning_model("ag/gemini-3.8-flash-high")
    assert not is_reasoning_model("wb/hy3")
    assert not is_reasoning_model("deepseek-v4-flash")
