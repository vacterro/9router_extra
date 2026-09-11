"""
9router_WatchEdit - Runtime Correctness Wave Unit Tests
100% Offline tests using httpx.MockTransport.
"""
import asyncio
import time
import pytest
import httpx

from core.classification import (
    AvailabilityState,
    CostState,
    Confidence,
    EvidenceCounters,
    classify_probe_result,
)
from core.history import HealthCache, ModelHealthRecord
from core.router_client import RouterClient
from core.discovery import DiscoveredModel
from core.probe import (
    ScannerWorker,
    ScanMode,
    ProviderCircuitBreaker,
)
from core.combo_manager import StableCombo


# -------------------------------------------------------------
# 1. P0-1: ASYNC EXCEPTION HANDLING (No AttributeError)
# -------------------------------------------------------------
def test_async_timeout_exception_handling(tmp_path):
    async def timeout_handler(request: httpx.Request):
        raise httpx.ReadTimeout("Simulated read timeout", request=request)

    transport = httpx.MockTransport(timeout_handler)
    client = RouterClient(base_url="http://127.0.0.1:99999")
    cache = HealthCache(cache_file=tmp_path / "cache.json")
    worker = ScannerWorker(client, cache, transport=transport)

    model = DiscoveredModel(
        canonical_id="testprov/testmodel",
        provider_name="testprov",
        provider_prefix="testprov",
        connection_id="conn-1",
        model_id="testmodel",
        display_name="testmodel",
    )

    # Must complete cleanly without raising AttributeError or crashing
    worker.run_scan([model], mode=ScanMode.QUICK)

    rec = cache.get("testprov/testmodel")
    assert rec is not None
    assert rec.state == AvailabilityState.TIMEOUT.value
    assert "timeout" in rec.reason.lower()


def test_async_connect_error_handling(tmp_path):
    async def connect_error_handler(request: httpx.Request):
        raise httpx.ConnectError("Simulated connection refused", request=request)

    transport = httpx.MockTransport(connect_error_handler)
    client = RouterClient(base_url="http://127.0.0.1:99999")
    cache = HealthCache(cache_file=tmp_path / "cache.json")
    worker = ScannerWorker(client, cache, transport=transport)

    model = DiscoveredModel(
        canonical_id="testprov/testmodel",
        provider_name="testprov",
        provider_prefix="testprov",
        connection_id="conn-1",
        model_id="testmodel",
        display_name="testmodel",
    )

    worker.run_scan([model], mode=ScanMode.QUICK)

    rec = cache.get("testprov/testmodel")
    assert rec is not None
    assert rec.state == AvailabilityState.TEMP_ERROR.value
    assert "network" in rec.reason.lower() or "503" in rec.reason


# -------------------------------------------------------------
# 2. P0-2: REAL BOUNDED CONCURRENCY & TIMING TEST
# -------------------------------------------------------------
def test_bounded_concurrency_timing(tmp_path):
    """6 models, each 100ms. With concurrency 3, runs in ~200-350ms, NOT 600ms."""
    async def delayed_handler(request: httpx.Request):
        await asyncio.sleep(0.10)
        return httpx.Response(200, json={"ok": True, "model": "test", "usage": {"prompt_tokens": 10}})

    transport = httpx.MockTransport(delayed_handler)
    client = RouterClient(base_url="http://127.0.0.1:99999")
    cache = HealthCache(cache_file=tmp_path / "cache.json")
    worker = ScannerWorker(
        client,
        cache,
        global_concurrency=3,
        per_provider_concurrency=3,
        transport=transport,
    )

    models = [
        DiscoveredModel(
            canonical_id=f"prov{i%2}/model{i}",
            provider_name=f"prov{i%2}",
            provider_prefix=f"prov{i%2}",
            connection_id=f"conn-{i%2}",
            model_id=f"model{i}",
            display_name=f"model{i}",
        )
        for i in range(6)
    ]

    t0 = time.monotonic()
    worker.run_scan(models, mode=ScanMode.FULL)
    elapsed = time.monotonic() - t0

    # Sequential would be >= 0.60s. Bounded concurrency 3 should be < 0.45s.
    assert elapsed < 0.45, f"Expected bounded concurrency runtime < 0.45s, got {elapsed:.2f}s"
    for m in models:
        rec = cache.get(m.canonical_id)
        assert rec is not None
        assert rec.is_healthy()


# -------------------------------------------------------------
# 3. P0-2: PROMPT STOP CANCELLATION
# -------------------------------------------------------------
def test_stop_cancellation(tmp_path):
    """Cancelling active worker stops immediately without finishing all tasks."""
    probes_started = 0

    async def slow_handler(request: httpx.Request):
        nonlocal probes_started
        probes_started += 1
        await asyncio.sleep(0.15)
        return httpx.Response(200, json={"ok": True})

    transport = httpx.MockTransport(slow_handler)
    client = RouterClient(base_url="http://127.0.0.1:99999")
    cache = HealthCache(cache_file=tmp_path / "cache.json")
    worker = ScannerWorker(
        client,
        cache,
        global_concurrency=2,
        per_provider_concurrency=2,
        transport=transport,
    )

    models = [
        DiscoveredModel(
            canonical_id=f"prov/model{i}",
            provider_name="prov",
            provider_prefix="prov",
            connection_id="conn-1",
            model_id=f"model{i}",
            display_name=f"model{i}",
        )
        for i in range(10)
    ]

    def cancel_after_brief_delay():
        time.sleep(0.05)
        worker.cancel()

    import threading
    t = threading.Thread(target=cancel_after_brief_delay)
    t.start()

    worker.run_scan(models, mode=ScanMode.FULL)
    t.join()

    assert worker.is_cancelled()
    # Far fewer than 10 models should have been probed
    assert probes_started < 10


# -------------------------------------------------------------
# 4. P0-4: STRICT HTTP 200 SUCCESS CONTRACT
# -------------------------------------------------------------
def test_strict_200_success_contract():
    # 1. Valid ok: true with free provider -> LIVE, FREE
    res1 = classify_probe_result(
        status_code=200,
        latency_ms=100.0,
        raw_body='{"ok": true, "cost": "free"}',
        parsed_json={"ok": True, "cost": "free"},
        provider_prefix="antigravity",
    )
    assert res1.availability == AvailabilityState.LIVE
    assert res1.cost == CostState.FREE

    # 2. Valid choices with non-empty content -> LIVE
    res2 = classify_probe_result(
        status_code=200,
        latency_ms=100.0,
        raw_body='{"choices":[{"message":{"content":"pong"}}]}',
        parsed_json={"choices": [{"message": {"content": "pong"}}]},
    )
    assert res2.availability == AvailabilityState.LIVE

    # 3. Empty JSON {} without ok:true or content -> UNKNOWN / TEST_FAILED
    res3 = classify_probe_result(
        status_code=200,
        latency_ms=100.0,
        raw_body='{}',
        parsed_json={},
    )
    assert res3.availability == AvailabilityState.UNKNOWN
    assert res3.error_code == "TEST_FAILED"

    # 4. Empty choices [] -> UNKNOWN / TEST_FAILED
    res4 = classify_probe_result(
        status_code=200,
        latency_ms=100.0,
        raw_body='{"choices":[]}',
        parsed_json={"choices": []},
    )
    assert res4.availability == AvailabilityState.UNKNOWN
    assert res4.error_code == "TEST_FAILED"

    # 5. ok: false in body -> UNKNOWN / TEST_FAILED
    res5 = classify_probe_result(
        status_code=200,
        latency_ms=100.0,
        raw_body='{"ok": false, "error": "unknown failure"}',
        parsed_json={"ok": False, "error": "unknown failure"},
    )
    assert res5.availability == AvailabilityState.UNKNOWN
    assert res5.error_code == "TEST_FAILED"


# -------------------------------------------------------------
# 5. P0-5: INTERRUPTED STREAK NEVER BECOMES DEAD
# -------------------------------------------------------------
def test_interrupted_streak_never_dead():
    """
    Sequence: MODEL_MISSING -> 429 -> MODEL_MISSING -> 500 -> MODEL_MISSING
    Must NOT become DEAD because intervening 429 and 500 reset consecutive_model_missing.
    """
    counters = EvidenceCounters()
    missing_body = '{"error":{"code":"model_not_found","message":"Not found"}}'
    missing_json = {"error": {"code": "model_not_found", "message": "Not found"}}

    # 1. MODEL_MISSING (streak = 1)
    res1 = classify_probe_result(
        status_code=404, latency_ms=100.0, raw_body=missing_body, parsed_json=missing_json,
        previous_counters=counters,
    )
    assert res1.availability == AvailabilityState.MODEL_MISSING
    assert res1.counters.consecutive_model_missing == 1

    # 2. 429 RATE_LIMIT (resets consecutive_model_missing)
    res2 = classify_probe_result(
        status_code=429, latency_ms=100.0, raw_body='{"error":"rate"}', parsed_json={"error": "rate"},
        previous_counters=res1.counters,
    )
    assert res2.availability == AvailabilityState.RATE_LIMIT
    assert res2.counters.consecutive_model_missing == 0
    assert res2.counters.consecutive_rate_limit == 1

    # 3. MODEL_MISSING (streak = 1 again, NOT 2)
    res3 = classify_probe_result(
        status_code=404, latency_ms=100.0, raw_body=missing_body, parsed_json=missing_json,
        previous_counters=res2.counters,
    )
    assert res3.availability == AvailabilityState.MODEL_MISSING
    assert res3.counters.consecutive_model_missing == 1

    # 4. 500 TEMP_ERROR (resets consecutive_model_missing)
    res4 = classify_probe_result(
        status_code=500, latency_ms=100.0, raw_body="Server Error",
        previous_counters=res3.counters,
    )
    assert res4.availability == AvailabilityState.TEMP_ERROR
    assert res4.counters.consecutive_model_missing == 0

    # 5. MODEL_MISSING (streak = 1 again, NOT 3)
    res5 = classify_probe_result(
        status_code=404, latency_ms=100.0, raw_body=missing_body, parsed_json=missing_json,
        previous_counters=res4.counters,
    )
    assert res5.availability == AvailabilityState.MODEL_MISSING
    assert res5.availability != AvailabilityState.DEAD
    assert res5.counters.consecutive_model_missing == 1


# -------------------------------------------------------------
# 6. P0-3 & P1-8: COMBO RENAME & BASELINE
# -------------------------------------------------------------
def test_combo_rename_contract(monkeypatch):
    last_put_payload = None

    def combo_api_handler(request: httpx.Request):
        nonlocal last_put_payload
        if request.method == "PUT" and "/api/combos/c-123" in request.url.path:
            import json
            last_put_payload = json.loads(request.content)
            return httpx.Response(200, json={"id": "c-123", **last_put_payload})
        return httpx.Response(404)

    transport = httpx.MockTransport(combo_api_handler)
    real_client_cls = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kw: real_client_cls(transport=transport))

    client = RouterClient(base_url="http://127.0.0.1:99999")
    res = client.rename_combo(
        combo_id="c-123",
        new_name="NewName",
        current_models=["prov/m1", "prov/m2"],
        kind="llm",
    )
    assert res is not None
    assert last_put_payload is not None
    assert last_put_payload["name"] == "NewName"
    assert last_put_payload["models"] == ["prov/m1", "prov/m2"]
    assert last_put_payload["kind"] == "llm"


def test_stable_combo_full_baseline():
    combo = StableCombo(
        combo_id="c-1",
        name="OrigName",
        models=["prov/m1", "prov/m2"],
        kind="llm",
        updated_at="2026-01-01T00:00:00Z",
    )
    assert not combo.has_unsaved_changes()

    # Rename changes baseline
    combo.name = "ModifiedName"
    assert combo.has_unsaved_changes()

    combo.mark_clean("2026-01-01T01:00:00Z")
    assert not combo.has_unsaved_changes()
    assert combo.baseline_name == "ModifiedName"
    assert combo.baseline_updated_at == "2026-01-01T01:00:00Z"

    # Models change
    combo.models.append("prov/m3")
    assert combo.has_unsaved_changes()
    combo.revert_unsaved_changes()
    assert not combo.has_unsaved_changes()
    assert len(combo.models) == 2


# -------------------------------------------------------------
# 7. P1-2: CIRCUIT BREAKER CONNECTION SCOPING
# -------------------------------------------------------------
def test_circuit_breaker_connection_scoping():
    cb = ProviderCircuitBreaker()

    # Trip connection 1
    cb.trip("conn-1", AvailabilityState.AUTH, "Invalid token")
    assert cb.is_tripped("conn-1")
    assert not cb.is_tripped("conn-2")
    assert cb.get_inherited_state("conn-1") == AvailabilityState.AUTH
    assert cb.get_inherited_state("conn-2") is None


# -------------------------------------------------------------
# 8. P1-1: DISCOVERED MODEL PROVENANCE FLAGS
# -------------------------------------------------------------
def test_discovered_model_provenance_flags():
    m = DiscoveredModel(
        canonical_id="p/m",
        provider_name="p",
        provider_prefix="p",
        connection_id="c1",
        model_id="m",
        display_name="m",
        configured=True,
        catalog=False,
        combo=True,
        advertised_live=True,
    )
    assert m.configured is True
    assert m.catalog is False
    assert m.combo is True
    assert m.advertised_live is True


# -------------------------------------------------------------
# 9. P0-1: SINGLE OWNER EVIDENCE COUNTERS (MODEL_MISSING vs TEMP_ERROR)
# -------------------------------------------------------------
def test_alternating_evidence_sequence_single_owner(tmp_path):
    """
    Test real classifier + HealthCache sequence:
    MODEL_MISSING -> TEMP_ERROR -> MODEL_MISSING -> TEMP_ERROR -> MODEL_MISSING
    Must reset consecutive_model_missing to 1 each time and NEVER transition to DEAD.
    """
    cache = HealthCache(cache_file=tmp_path / "cache.json")
    cid = "testprov/flaky-model"

    # 1. First 404 (MODEL_MISSING)
    rec_pre = cache.get(cid)
    counters = rec_pre.counters if rec_pre else EvidenceCounters()
    ev1 = classify_probe_result(
        status_code=404,
        body="Model not found",
        latency_ms=50.0,
        model=cid,
        prev_counters=counters,
    )
    assert ev1.availability == AvailabilityState.MODEL_MISSING
    assert ev1.counters.consecutive_model_missing == 1
    rec1 = cache.record_evidence(cid, "testprov", "flaky-model", ev1)
    assert rec1.counters.consecutive_model_missing == 1
    assert rec1.state == AvailabilityState.MODEL_MISSING.value

    # 2. 500 (TEMP_ERROR)
    rec_pre = cache.get(cid)
    ev2 = classify_probe_result(
        status_code=500,
        body="Internal Server Error",
        latency_ms=50.0,
        model=cid,
        prev_counters=rec_pre.counters,
    )
    assert ev2.availability == AvailabilityState.TEMP_ERROR
    assert ev2.counters.consecutive_model_missing == 0  # Incompatible counter reset!
    rec2 = cache.record_evidence(cid, "testprov", "flaky-model", ev2)
    assert rec2.counters.consecutive_model_missing == 0  # Must NOT be rehydrated!
    assert rec2.state == AvailabilityState.TEMP_ERROR.value

    # 3. Second 404 (MODEL_MISSING)
    rec_pre = cache.get(cid)
    ev3 = classify_probe_result(
        status_code=404,
        body="Model not found",
        latency_ms=50.0,
        model=cid,
        prev_counters=rec_pre.counters,
    )
    assert ev3.availability == AvailabilityState.MODEL_MISSING
    assert ev3.counters.consecutive_model_missing == 1  # Reset to 1, NOT 2!
    rec3 = cache.record_evidence(cid, "testprov", "flaky-model", ev3)
    assert rec3.counters.consecutive_model_missing == 1
    assert rec3.state == AvailabilityState.MODEL_MISSING.value

    # 4. Another 500 (TEMP_ERROR)
    rec_pre = cache.get(cid)
    ev4 = classify_probe_result(
        status_code=500,
        body="Internal Server Error",
        latency_ms=50.0,
        model=cid,
        prev_counters=rec_pre.counters,
    )
    assert ev4.counters.consecutive_model_missing == 0
    rec4 = cache.record_evidence(cid, "testprov", "flaky-model", ev4)
    assert rec4.counters.consecutive_model_missing == 0

    # 5. Third 404 (MODEL_MISSING)
    rec_pre = cache.get(cid)
    ev5 = classify_probe_result(
        status_code=404,
        body="Model not found",
        latency_ms=50.0,
        model=cid,
        prev_counters=rec_pre.counters,
    )
    assert ev5.counters.consecutive_model_missing == 1  # Still 1!
    rec5 = cache.record_evidence(cid, "testprov", "flaky-model", ev5)
    assert rec5.counters.consecutive_model_missing == 1
    assert rec5.state == AvailabilityState.MODEL_MISSING.value
    assert rec5.state != AvailabilityState.DEAD.value


# -------------------------------------------------------------
# 10. P0-2 & P0-3: UNEXPECTED TASK FAILURE & TERMINAL STATUS
# -------------------------------------------------------------
def test_scanner_unexpected_failure_handling(monkeypatch, tmp_path):
    """Internal task failure transitions status to FAILED, reports error, and does not hang."""
    client = RouterClient(base_url="http://127.0.0.1:99999")
    cache = HealthCache(cache_file=tmp_path / "cache.json")
    worker = ScannerWorker(client, cache)

    model = DiscoveredModel(
        canonical_id="failprov/failmodel",
        provider_name="failprov",
        provider_prefix="failprov",
        connection_id="c1",
        model_id="failmodel",
        display_name="failmodel",
    )

    # Monkeypatch _probe_single_model to raise unexpected RuntimeError
    async def crash_probe(*args, **kwargs):
        raise RuntimeError("Fatal internal assertion failure")

    monkeypatch.setattr(worker, "_probe_single_model", crash_probe)

    failed_messages = []
    completed_statuses = []
    worker.on_scan_failed = lambda err: failed_messages.append(err)
    worker.on_scan_completed = lambda status="COMPLETED": completed_statuses.append(status)

    worker.run_scan([model], mode=ScanMode.QUICK)

    assert len(failed_messages) == 1
    assert "Fatal internal assertion failure" in failed_messages[0]
    assert len(completed_statuses) == 1
    assert completed_statuses[0] == "FAILED"
    assert not worker.is_running()


# -------------------------------------------------------------
# 11. P0-4: HEALTH CACHE COST OVERRIDE RETURN VALUE
# -------------------------------------------------------------
def test_cost_override_return_record(tmp_path):
    cache = HealthCache(cache_file=tmp_path / "cache.json")
    cid = "p/m1"
    # Seed record
    cache.records[cid] = ModelHealthRecord(
        canonical_id=cid,
        provider="p",
        model_id="m1",
        availability="LIVE",
        cost="PAID",
        confidence="CONFIRMED",
        latency_ms=100.0,
        status_code=200,
        last_tested_at="",
        state="LIVE",
    )

    # Set to FREE
    rec = cache.set_cost_override(cid, "FREE")
    assert isinstance(rec, ModelHealthRecord)
    assert rec.cost_override == "FREE"
    assert rec.is_free() is True

    # Set to PAID
    rec = cache.set_cost_override(cid, "PAID")
    assert isinstance(rec, ModelHealthRecord)
    assert rec.cost_override == "PAID"
    assert rec.is_free() is False

    # Clear override
    rec = cache.set_cost_override(cid, None)
    assert isinstance(rec, ModelHealthRecord)
    assert rec.cost_override is None

    # Unknown model returns None
    assert cache.set_cost_override("nonexistent/model", "FREE") is None

    # Invalid cost returns None
    assert cache.set_cost_override(cid, "CHEAP") is None


# -------------------------------------------------------------
# 12. P1-1: THREADSAFE CANCELLATION UNDER ASYNCIO DEBUG MODE
# -------------------------------------------------------------
def test_threadsafe_cancellation_debug_mode(tmp_path):
    """Ensure cancel() called from separate thread does not raise cross-thread loop error in debug mode."""
    import threading

    async def slow_handler(request: httpx.Request):
        await asyncio.sleep(1.0)
        return httpx.Response(200, json={"ok": True})

    transport = httpx.MockTransport(slow_handler)
    client = RouterClient(base_url="http://127.0.0.1:99999")
    cache = HealthCache(cache_file=tmp_path / "cache.json")
    worker = ScannerWorker(client, cache, transport=transport)

    models = [
        DiscoveredModel(
            canonical_id=f"p/m{i}",
            provider_name="p",
            provider_prefix="p",
            connection_id="c1",
            model_id=f"m{i}",
            display_name=f"m{i}",
        )
        for i in range(5)
    ]

    completed_statuses = []
    worker.on_scan_completed = lambda status="COMPLETED": completed_statuses.append(status)

    def cancel_after_delay():
        time.sleep(0.05)
        worker.cancel()

    t = threading.Thread(target=cancel_after_delay)
    t.start()

    # Run scan on main thread with loop debug enabled
    worker.run_scan(models, mode=ScanMode.FULL)
    t.join()

    assert not worker.is_running()
    assert "CANCELLED" in completed_statuses


# -------------------------------------------------------------
# 13. P1-2: FAIR ROUND-ROBIN SCHEDULING
# -------------------------------------------------------------
def test_fair_scheduling_round_robin(tmp_path):
    """
    12 models: 6 for conn-A, 6 for conn-B.
    Each probe takes 0.10s.
    With global_concurrency=4, per_provider_concurrency=1:
    conn-A and conn-B each run 1 task concurrently (total 2 concurrent).
    If scheduling was blocked by conn-A (head-of-line), conn-B wouldn't start until conn-A finished.
    With fair scheduling, both finish in ~0.60 - 0.75s, NOT >1.0s.
    """
    async def probe_handler(request: httpx.Request):
        await asyncio.sleep(0.10)
        return httpx.Response(200, json={"ok": True, "model": "test", "usage": {"prompt_tokens": 10}})

    transport = httpx.MockTransport(probe_handler)
    client = RouterClient(base_url="http://127.0.0.1:99999")
    cache = HealthCache(cache_file=tmp_path / "cache.json")
    worker = ScannerWorker(
        client,
        cache,
        global_concurrency=4,
        per_provider_concurrency=1,
        transport=transport,
    )

    models = []
    # 6 models for conn-A first, then 6 for conn-B
    for i in range(6):
        models.append(DiscoveredModel(
            canonical_id=f"connA/m{i}",
            provider_name="connA",
            provider_prefix="connA",
            connection_id="conn-A",
            model_id=f"m{i}",
            display_name=f"m{i}",
        ))
    for i in range(6):
        models.append(DiscoveredModel(
            canonical_id=f"connB/m{i}",
            provider_name="connB",
            provider_prefix="connB",
            connection_id="conn-B",
            model_id=f"m{i}",
            display_name=f"m{i}",
        ))

    start = time.perf_counter()
    worker.run_scan(models, mode=ScanMode.FULL)
    elapsed = time.perf_counter() - start

    assert elapsed < 0.95  # Strict check: 6 rounds * 0.10s ~ 0.60s. Head-of-line sequential would be 1.2s+


# -------------------------------------------------------------
# 14. P1-3 & P1-5: DISCOVERY PROVENANCE & LIVE OUTCOME TRACKING
# -------------------------------------------------------------
def test_discovery_provenance_and_live_status_tracking():
    from core.discovery import ModelDiscovery

    client = RouterClient(base_url="http://127.0.0.1:99999")
    discovery = ModelDiscovery(client)

    # Mock get_providers and get_provider_nodes
    client.get_providers = lambda: [
        {"id": "conn-1", "name": "OpenAI Provider", "provider": "openai", "isActive": True, "providerSpecificData": {"prefix": "oai"}},
        {"id": "conn-2", "name": "Broken Provider", "provider": "broken", "isActive": True, "providerSpecificData": {"prefix": "brk"}},
    ]
    client.get_provider_nodes = lambda: []
    client.get_kv_scoped = lambda: [
        ("customModels", "oai", '["gpt-4o", "gpt-4o-mini", "omitted-model"]'),
        ("customModels", "brk", '["broken-model-1"]'),
    ]
    client.get_catalog_models = lambda: [
        {"provider": "openai", "model": "gpt-4o", "name": "GPT-4o Omnimodel"},
    ]
    client.get_combos = lambda: [
        {"id": "c1", "name": "Combo1", "models": ["oai/gpt-4o"]},
    ]

    # Mock detailed live responses: conn-1 -> OK, conn-2 -> FAILED
    def mock_live_detailed(cid):
        if cid == "conn-1":
            return ("OK", [{"id": "oai/gpt-4o"}, {"id": "oai/gpt-4o-mini"}])
        else:
            return ("FAILED", [])

    client.get_connection_live_models_detailed = mock_live_detailed

    models = discovery.discover_all(include_combo_models=True, query_live=True)
    by_cid = {m.canonical_id: m for m in models}

    # gpt-4o: configured=True, catalog=True, combo=True, advertised_live=True
    m_4o = by_cid["oai/gpt-4o"]
    assert m_4o.configured is True
    assert m_4o.catalog is True
    assert m_4o.combo is True
    assert m_4o.is_combo_member is True
    assert m_4o.advertised_live is True

    # gpt-4o-mini: configured=True, catalog=False, combo=False, advertised_live=True
    m_mini = by_cid["oai/gpt-4o-mini"]
    assert m_mini.configured is True
    assert m_mini.catalog is False
    assert m_mini.combo is False
    assert m_mini.advertised_live is True

    # omitted-model: configured=True, conn-1 was OK, but omitted from live list -> advertised_live=False
    m_omitted = by_cid["oai/omitted-model"]
    assert m_omitted.configured is True
    assert m_omitted.advertised_live is False

    # broken-model-1: conn-2 query FAILED -> advertised_live must remain None (not False!)
    m_broken = by_cid["brk/broken-model-1"]
    assert m_broken.configured is True
    assert m_broken.advertised_live is None


# -------------------------------------------------------------
# 15. P1-4: OPTIMISTIC CONCURRENCY CHECK IN STABLECOMBO
# -------------------------------------------------------------
def test_stable_combo_check_server_conflict():
    combo = StableCombo(
        combo_id="c-1",
        name="TestCombo",
        models=["p/m1", "p/m2"],
        kind="llm",
        updated_at="2026-01-01T00:00:00Z",
    )

    # Identical server state -> No conflict
    assert not combo.check_server_conflict("TestCombo", "llm", ["p/m1", "p/m2"], "2026-01-01T00:00:00Z")

    # Name changed externally -> Conflict
    assert combo.check_server_conflict("DifferentName", "llm", ["p/m1", "p/m2"], "2026-01-01T00:00:00Z")

    # Kind changed externally -> Conflict
    assert combo.check_server_conflict("TestCombo", "embedding", ["p/m1", "p/m2"], "2026-01-01T00:00:00Z")

    # Models changed externally -> Conflict
    assert combo.check_server_conflict("TestCombo", "llm", ["p/m1", "p/m3"], "2026-01-01T00:00:00Z")

    # Timestamp changed externally -> Conflict
    assert combo.check_server_conflict("TestCombo", "llm", ["p/m1", "p/m2"], "2026-01-02T00:00:00Z")

