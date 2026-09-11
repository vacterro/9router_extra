"""
9router_WatchEdit - Final Core Repair Wave Regression Tests
Covers the remaining acceptance gates:
- P0-2/P0-3: redacted failure diagnostic + terminal status preserved through UI
- P0-4: cost override never delivers bool into ModelHealthRecord UI code
- P1-1: prompt thread-safe cancellation under asyncio debug mode
- P1-2: fair cross-connection scheduling (interleave proof, not just wall time)
- P1-4: external name/kind/model changes participate in conflict detection
- P1-5: per-connection live discovery outcome tracking (OK vs FAILED vs empty OK)
All tests are offline: httpx.MockTransport / lambdas only.
"""
import asyncio
import json
import threading
import time

import pytest
import httpx

from core.classification import AvailabilityState, CatalogState, EvidenceCounters, classify_probe_result
from core.combo_manager import StableCombo
from core.discovery import DiscoveredModel, ModelDiscovery
from core.history import HealthCache, ModelHealthRecord
from core.probe import ScanMode, ScannerWorker
from core.router_client import RouterClient

from PySide6.QtWidgets import QApplication


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


# -------------------------------------------------------------
# P0-1: single-owner counters through classifier -> HealthCache
# (authoritative counters; HealthCache must never rehydrate)
# -------------------------------------------------------------
def test_model_missing_temp_error_alternation_never_dead(tmp_path):
    cache = HealthCache(cache_file=tmp_path / "cache.json")
    cid = "prov/flaky"

    seq = [404, 500, 404, 500, 404]
    for status in seq:
        existing = cache.get(cid)
        ev = classify_probe_result(
            status_code=status,
            latency_ms=50.0,
            raw_body="Model not found" if status == 404 else "Internal Server Error",
            parsed_json={"error": {"code": "model_not_found", "message": "Model not found"}} if status == 404 else None,
            previous_counters=existing.counters if existing else EvidenceCounters(),
        )
        rec = cache.record_evidence(cid, "prov", "flaky", ev)

        if status == 404:
            # Every MODEL_MISSING begins a fresh streak of exactly 1
            assert rec.counters.consecutive_model_missing == 1
            assert rec.availability == AvailabilityState.MODEL_MISSING.value
            assert rec.availability != AvailabilityState.DEAD.value
        else:
            # TEMP_ERROR resets the incompatible counter: no rehydration of stale streaks
            assert rec.counters.consecutive_model_missing == 0
            assert rec.availability == AvailabilityState.TEMP_ERROR.value

    final = cache.get(cid)
    assert final.availability != AvailabilityState.DEAD.value
    assert final.counters.consecutive_model_missing == 1


# -------------------------------------------------------------
# P0-2 + P0-3: crash inside probe worker -> FAILED with redacted
# reason, carried through MainWindow terminal status handling
# -------------------------------------------------------------
def test_scan_failed_end_to_end_redacted_status(qapp, monkeypatch, tmp_path):
    from ui.main_window import MainWindow

    window = MainWindow()
    tmp_cache = HealthCache(cache_file=tmp_path / "cache.json")
    window.worker.cache = tmp_cache

    async def crash_probe(*args, **kwargs):
        raise RuntimeError("internal crash; key sk-abcdefghijklmnop1234 leaked")

    monkeypatch.setattr(window.worker, "_probe_single_model", crash_probe)

    model = DiscoveredModel(
        canonical_id="boom/m1",
        provider_name="boom",
        provider_prefix="boom",
        connection_id="c1",
        model_id="m1",
        display_name="m1",
    )

    # Same-thread run: Qt signals resolve as direct connections
    window.worker.run_scan([model], mode=ScanMode.QUICK)

    msg = window.status_bar.currentMessage()
    assert msg.startswith("Scan failed")
    # Redacted diagnostic, never the raw secret
    assert "[REDACTED]" in msg
    assert "sk-abcdefghijklmnop1234" not in msg
    # Terminal FAILED message survived (not overwritten by a generic completed text)
    assert "internal crash" in msg
    assert not window.worker.is_running()
    window.close()


def test_ui_terminal_status_messages(qapp, tmp_path):
    from ui.main_window import MainWindow

    window = MainWindow()

    window._on_scan_completed("COMPLETED")
    assert window.status_bar.currentMessage().startswith("Scan completed")

    window._on_scan_completed("CANCELLED")
    assert window.status_bar.currentMessage().startswith("Scan cancelled")

    # Real run_scan order: on_scan_failed(reason) fires BEFORE on_scan_completed("FAILED")
    window._on_scan_failed("redacted diagnostic reason")
    window._on_scan_completed("FAILED")
    final_msg = window.status_bar.currentMessage()
    assert final_msg.startswith("Scan failed")
    assert "redacted diagnostic reason" in final_msg
    window.close()


# -------------------------------------------------------------
# P0-4: cost override UI path delivers ModelHealthRecord, never bool
# -------------------------------------------------------------
def test_cost_override_ui_receives_record_never_bool(qapp, monkeypatch, tmp_path):
    from ui.main_window import MainWindow

    window = MainWindow()
    tmp_cache = HealthCache(cache_file=tmp_path / "cache.json")
    cid = "ov/m1"
    tmp_cache.records[cid] = ModelHealthRecord(
        canonical_id=cid, provider="ov", model_id="m1",
        availability="LIVE", cost="PAID",
    )
    window.cache = tmp_cache
    window.inspector._current_canonical_id = cid

    captured = []
    monkeypatch.setattr(window.watch_view, "update_probe_result", lambda c, rec: captured.append(rec))
    monkeypatch.setattr(window.inspector, "set_model", lambda c, rec: captured.append(rec))

    # FREE
    window._on_cost_override_changed(cid, "FREE")
    assert len(captured) == 2
    assert all(isinstance(rec, ModelHealthRecord) for rec in captured)
    assert captured[0].cost_override == "FREE"
    assert captured[0].is_free() is True

    # PAID
    captured.clear()
    window._on_cost_override_changed(cid, "PAID")
    assert all(isinstance(rec, ModelHealthRecord) for rec in captured)
    assert captured[0].cost_override == "PAID"

    # Clear override
    captured.clear()
    window._on_cost_override_changed(cid, None)
    assert all(isinstance(rec, ModelHealthRecord) for rec in captured)
    assert captured[0].cost_override is None
    window.close()


# -------------------------------------------------------------
# P1-1: prompt cancellation under asyncio debug mode (stress)
# -------------------------------------------------------------
def test_cancellation_stress_asyncio_debug_mode(monkeypatch, tmp_path):
    monkeypatch.setenv("PYTHONASYNCIODEBUG", "1")
    debug_observed = []

    async def slow_handler(request: httpx.Request):
        if not debug_observed:
            debug_observed.append(asyncio.get_running_loop().get_debug())
        await asyncio.sleep(0.30)
        return httpx.Response(200, json={"ok": True})

    transport = httpx.MockTransport(slow_handler)
    client = RouterClient(base_url="http://127.0.0.1:99999")
    cache = HealthCache(cache_file=tmp_path / "cache.json")
    worker = ScannerWorker(client, cache, global_concurrency=4, per_provider_concurrency=1, transport=transport)

    models = []
    for i in range(10):
        models.append(DiscoveredModel(
            canonical_id=f"connA/m{i}", provider_name="connA", provider_prefix="connA",
            connection_id="conn-A", model_id=f"m{i}", display_name=f"m{i}",
        ))
    for i in range(10):
        models.append(DiscoveredModel(
            canonical_id=f"connB/m{i}", provider_name="connB", provider_prefix="connB",
            connection_id="conn-B", model_id=f"m{i}", display_name=f"m{i}",
        ))

    statuses = []
    worker.on_scan_completed = lambda status="COMPLETED": statuses.append(status)

    def cancel_after_first_probe():
        deadline = time.monotonic() + 3.0
        while not first_probe_seen.is_set() and time.monotonic() < deadline:
            time.sleep(0.005)
        worker.cancel()

    first_probe_seen = threading.Event()
    worker.on_probe_started = lambda cid: first_probe_seen.set()
    canceller = threading.Thread(target=cancel_after_first_probe)
    canceller.start()

    t0 = time.monotonic()
    worker.run_scan(models, mode=ScanMode.FULL)
    elapsed = time.monotonic() - t0
    canceller.join()

    # The stress test really ran with asyncio debug mode enabled
    assert debug_observed == [True]
    # Prompt, thread-safe cancellation: no hang, terminal CANCELLED
    assert elapsed < 3.0, f"Cancellation not prompt under debug mode: {elapsed:.2f}s"
    assert statuses == ["CANCELLED"]
    assert not worker.is_running()


# -------------------------------------------------------------
# P1-2: fair scheduling - conn B starts during conn A's first
# in-flight probe (interleave proof, provider-sorted targets)
# -------------------------------------------------------------
def test_fair_scheduling_interleave_proof(tmp_path):
    starts = {"connA": [], "connB": []}

    async def probe_handler(request: httpx.Request):
        payload = json.loads(request.content)
        model = payload.get("model", "")
        conn = "connA" if model.startswith("connA/") else "connB"
        starts[conn].append(time.monotonic())
        await asyncio.sleep(0.10)
        return httpx.Response(200, json={"ok": True, "usage": {"prompt_tokens": 1}})

    transport = httpx.MockTransport(probe_handler)
    client = RouterClient(base_url="http://127.0.0.1:99999")
    cache = HealthCache(cache_file=tmp_path / "cache.json")
    worker = ScannerWorker(client, cache, global_concurrency=4, per_provider_concurrency=1, transport=transport)

    # Provider-sorted order: all conn-A first (head-of-line hazard), then conn-B
    models = []
    for i in range(6):
        models.append(DiscoveredModel(
            canonical_id=f"connA/m{i}", provider_name="connA", provider_prefix="connA",
            connection_id="conn-A", model_id=f"m{i}", display_name=f"m{i}",
        ))
    for i in range(6):
        models.append(DiscoveredModel(
            canonical_id=f"connB/m{i}", provider_name="connB", provider_prefix="connB",
            connection_id="conn-B", model_id=f"m{i}", display_name=f"m{i}",
        ))

    worker.run_scan(models, mode=ScanMode.FULL)

    assert len(starts["connA"]) == 6
    assert len(starts["connB"]) == 6
    # conn-B must start while conn-A's FIRST probe is still in flight:
    # available global capacity is used by the other connection instead of
    # waiting behind one provider's queue.
    assert min(starts["connB"]) < min(starts["connA"]) + 0.10


# -------------------------------------------------------------
# P1-5: per-connection live discovery outcome tracking
# -------------------------------------------------------------
def test_live_discovery_outcome_tracking(tmp_path):
    client = RouterClient(base_url="http://127.0.0.1:99999")
    discovery = ModelDiscovery(client)

    client.get_providers = lambda: [
        {"id": "conn-ok", "name": "OK Provider", "provider": "okprov", "isActive": True, "providerSpecificData": {"prefix": "okp"}},
        {"id": "conn-empty", "name": "Empty Provider", "provider": "emptyprov", "isActive": True, "providerSpecificData": {"prefix": "emp"}},
        {"id": "conn-failed", "name": "Failed Provider", "provider": "failprov", "isActive": True, "providerSpecificData": {"prefix": "fap"}},
        {"id": "conn-timeout", "name": "Timeout Provider", "provider": "timeoutprov", "isActive": True, "providerSpecificData": {"prefix": "tmp"}},
        {"id": "conn-nosupport", "name": "NoSupport Provider", "provider": "nosupportprov", "isActive": True, "providerSpecificData": {"prefix": "nsp"}},
    ]
    client.get_provider_nodes = lambda: []
    client.get_kv_scoped = lambda: [
        ("customModels", "okp", '["m1", "m2"]'),
        ("customModels", "emp", '["only-model"]'),
        ("customModels", "fap", '["dead-listing"]'),
        ("customModels", "tmp", '["slow-listing"]'),
        ("customModels", "nsp", '["static-listing"]'),
    ]
    client.get_catalog_models = lambda: []
    client.get_combos = lambda: []

    def mock_live_detailed(cid):
        return {
            "conn-ok": ("OK", [{"id": "okp/m1"}, {"id": "okp/m2"}]),
            "conn-empty": ("OK", []),  # successful discovery, zero advertised models
            "conn-failed": ("FAILED", []),
            "conn-timeout": ("TIMEOUT", []),
            "conn-nosupport": ("NOT_SUPPORTED", []),
        }[cid]

    client.get_connection_live_models_detailed = mock_live_detailed

    models = discovery.discover_all(include_combo_models=True, query_live=True)
    by_cid = {m.canonical_id: m for m in models}

    # Per-connection outcomes are tracked as distinct facts
    assert discovery.live_outcomes == {
        "conn-ok": "OK",
        "conn-empty": "OK",
        "conn-failed": "FAILED",
        "conn-timeout": "TIMEOUT",
        "conn-nosupport": "NOT_SUPPORTED",
    }

    # Successful discovery: advertised models -> True
    assert by_cid["okp/m1"].advertised_live is True
    assert by_cid["okp/m2"].advertised_live is True

    # Successful EMPTY discovery: confirmed absent -> False (negative evidence allowed)
    assert by_cid["emp/only-model"].advertised_live is False
    assert discovery.catalog_states["conn-empty"] == CatalogState.EMPTY_MODEL_CATALOG
    assert discovery.catalog_model_counts["conn-empty"] == 0
    assert "conn-empty" in discovery.routing_excluded_connections
    assert discovery.catalog_status_text("conn-empty") == "Models API: EMPTY (0 models)"
    assert by_cid["emp/only-model"].routing_eligible is False
    assert by_cid["emp/only-model"].routing_exclusion_reason == CatalogState.EMPTY_MODEL_CATALOG.value
    assert by_cid["emp/only-model"] not in discovery.active_routing_models(models)

    # Failed discovery must remain UNKNOWN, never negative evidence
    assert by_cid["fap/dead-listing"].advertised_live is None
    assert by_cid["tmp/slow-listing"].advertised_live is None
    assert by_cid["nsp/static-listing"].advertised_live is None

    # The provider row is retained and becomes eligible again after a later
    # successful non-empty re-probe.
    def recovered_live_detailed(cid):
        if cid == "conn-empty":
            return ("OK", [{"id": "emp/only-model"}])
        return mock_live_detailed(cid)

    client.get_connection_live_models_detailed = recovered_live_detailed
    recovered = discovery.discover_all(include_combo_models=True, query_live=True)
    recovered_by_cid = {m.canonical_id: m for m in recovered}
    assert "conn-empty" not in discovery.routing_excluded_connections
    assert discovery.catalog_states["conn-empty"] == CatalogState.MODELS_AVAILABLE
    assert recovered_by_cid["emp/only-model"].routing_eligible is True

    # A fresh discover_all resets the previous outcome map
    client.get_providers = lambda: []
    client.get_kv_scoped = lambda: []
    discovery.discover_all(query_live=True)
    assert discovery.live_outcomes == {}


# -------------------------------------------------------------
# P1-4: complete optimistic concurrency baseline
# -------------------------------------------------------------
def test_conflict_detection_full_baseline(tmp_path):
    combo = StableCombo(
        combo_id="c1",
        name="MainCombo",
        models=["p/m1", "p/m2", "p/m3"],
        kind="llm",
        updated_at="2026-09-01T10:00:00Z",
    )

    # No external change -> no conflict
    assert not combo.check_server_conflict("MainCombo", "llm", ["p/m1", "p/m2", "p/m3"], "2026-09-01T10:00:00Z")

    # Local model reorder only (unsaved) must NOT silently overwrite an external rename
    combo.reorder_model("p/m3", 0)  # local: [m3, m1, m2]
    assert combo.has_unsaved_changes()
    assert combo.check_server_conflict("RenamedExternally", "llm", ["p/m1", "p/m2", "p/m3"], "2026-09-01T10:00:00Z")

    # External kind change alone conflicts
    assert combo.check_server_conflict("MainCombo", "embedding", ["p/m1", "p/m2", "p/m3"], "2026-09-01T10:00:00Z")

    # External model reorder conflicts (ordered models participate)
    assert combo.check_server_conflict("MainCombo", "llm", ["p/m2", "p/m1", "p/m3"], "2026-09-01T10:00:00Z")

    # External updatedAt bump alone conflicts when both timestamps are known
    assert combo.check_server_conflict("MainCombo", "llm", ["p/m1", "p/m2", "p/m3"], "2026-09-02T10:00:00Z")

    # Field-level divergence description for the conflict dialog
    div = combo.describe_server_divergence("RenamedExternally", "embedding", ["p/m1", "p/m2", "p/m3"], "2026-09-01T10:00:00Z")
    assert any("name" in d for d in div)
    assert any("kind" in d for d in div)
    assert not any("models" in d for d in div)

    div_order = combo.describe_server_divergence("MainCombo", "llm", ["p/m2", "p/m1", "p/m3"], "")
    assert any("model order changed" in d for d in div_order)
