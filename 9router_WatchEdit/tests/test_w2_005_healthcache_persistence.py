"""
W2-005 explicit HealthCache persistence — focused deterministic suite.

Covers:
1. SAVE CONTRACT: HealthCache.save() raises a dedicated
   HealthCachePersistenceError on ANY failed persistence stage (mkdir, temp
   write, fsync, atomic replace) instead of returning normally; the pre-existing
   valid cache stays intact and temporary artifacts are cleaned where safely
   possible; a cleanup failure is classified without hiding the primary cause.
2. LOAD CONTRACT: absent / loaded / malformed JSON / truncated JSON /
   structurally invalid record / invalid value / I/O failure are
   distinguishable; corruption is surfaced, the original bytes remain
   recoverable byte-for-byte, and it is never silently converted into a clean
   empty cache.
3. SCANNER TERMINAL SEMANTICS: a scan whose probes succeed but whose FINAL
   persistence fails is never reported as plain COMPLETED; the redacted reason
   is surfaced once, ownership releases, and the next scan starts.
4. MID-SCAN POLICY: a periodic persistence failure is non-fatal but is
   recorded as pending, so terminal COMPLETED requires a later verified save.
5. ROUND TRIP: restored persistence writes a fresh instance readable from disk
   with records, counters, overrides and timestamps identical.

Offline only: httpx.MockTransport, tmp_path caches, no live network.
"""
import json
from dataclasses import asdict
from pathlib import Path

import httpx
import pytest
from PySide6.QtWidgets import QApplication

import core.history as history_module
import ui.main_window as main_window_module
from core.classification import (
    AvailabilityState,
    Confidence,
    CostState,
    EvidenceCounters,
    EvidenceRecord,
)
from core.discovery import DiscoveredModel
from core.history import (
    CacheLoadState,
    HealthCache,
    HealthCachePersistenceError,
    ModelHealthRecord,
)
from core.probe import ScanMode, ScannerWorker
from core.router_client import RouterClient
from ui.main_window import MainWindow
from ui.theme import apply_theme

SECRET = "sk-abcdefghijklmnop1234"


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    apply_theme(app)
    return app


def _models(n, prefix="p", conn="c-1"):
    return [
        DiscoveredModel(
            canonical_id=f"{prefix}/m{i}",
            provider_name=prefix,
            provider_prefix=prefix,
            connection_id=conn,
            model_id=f"m{i}",
            display_name=f"m{i}",
        )
        for i in range(n)
    ]


def _evidence(availability=AvailabilityState.LIVE, cost=CostState.FREE, counters=None):
    return EvidenceRecord(
        availability=availability,
        cost=cost,
        confidence=Confidence.LIVE,
        status_code=200,
        latency_ms=123.0,
        error_code="OK",
        reason="Success",
        raw_error="",
        counters=counters,
    )


def _seeded_cache(cache_file: Path) -> HealthCache:
    """A cache with one persisted record (a genuinely valid on-disk file)."""
    cache = HealthCache(cache_file=cache_file)
    cache.record_evidence("p/m1", "Provider", "m1", _evidence(), auto_save=False)
    cache.save()
    assert cache_file.exists()
    return cache


def _ok_worker(tmp_path, name="w2-005", models_n=2):
    async def handler(request):
        return httpx.Response(200, json={"ok": True})

    client = RouterClient(base_url="http://127.0.0.1:99999")
    cache = HealthCache(cache_file=tmp_path / f"{name}.json")
    worker = ScannerWorker(
        client, cache,
        global_concurrency=4, per_provider_concurrency=4,
        transport=httpx.MockTransport(handler),
    )
    return worker, cache


# ===================================================================
# 1. SAVE FAILURE MATRIX
# ===================================================================
def test_save_success_is_explicit_none(tmp_path):
    cache_file = tmp_path / "cache.json"
    cache = HealthCache(cache_file=cache_file)
    cache.record_evidence("p/m1", "Provider", "m1", _evidence(), auto_save=False)
    assert cache.save() is None
    assert cache_file.exists()
    assert not cache._temp_path().exists()


def test_save_reports_parent_mkdir_failure(tmp_path):
    blocker = tmp_path / "blocked"
    blocker.write_text("not a directory", encoding="utf-8")
    cache = HealthCache(cache_file=blocker / "sub" / "cache.json")
    cache.record_evidence("p/m1", "Provider", "m1", _evidence(), auto_save=False)

    with pytest.raises(HealthCachePersistenceError) as excinfo:
        cache.save()
    assert excinfo.value.stage == "mkdir"
    assert excinfo.value.cause is not None
    # The blocking file was not clobbered and no success state was reported.
    assert blocker.read_text(encoding="utf-8") == "not a directory"


def test_save_reports_temp_write_failure_and_leaves_original_intact(tmp_path, monkeypatch):
    cache_file = tmp_path / "cache.json"
    cache = _seeded_cache(cache_file)
    valid_bytes = cache_file.read_bytes()

    def boom(temp_file, payload):
        raise OSError("injected write failure")

    monkeypatch.setattr(history_module, "_write_temp_file", boom)
    with pytest.raises(HealthCachePersistenceError) as excinfo:
        cache.save()
    assert excinfo.value.stage == "write"
    assert "injected write failure" in str(excinfo.value)
    # Original valid cache untouched; no temporary artifact left behind.
    assert cache_file.read_bytes() == valid_bytes
    assert not cache._temp_path().exists()


def test_save_reports_fsync_failure_and_cleans_temp(tmp_path, monkeypatch):
    cache_file = tmp_path / "cache.json"
    cache = _seeded_cache(cache_file)
    valid_bytes = cache_file.read_bytes()

    def boom(fd):
        raise OSError("injected fsync failure")

    monkeypatch.setattr(history_module.os, "fsync", boom)
    with pytest.raises(HealthCachePersistenceError) as excinfo:
        cache.save()
    assert excinfo.value.stage == "fsync"
    assert cache_file.read_bytes() == valid_bytes
    assert not cache._temp_path().exists()


def test_save_reports_atomic_replace_failure_and_cleans_temp(tmp_path, monkeypatch):
    cache_file = tmp_path / "cache.json"
    cache = _seeded_cache(cache_file)
    valid_bytes = cache_file.read_bytes()

    def boom(src, dst):
        raise OSError("injected replace failure")

    monkeypatch.setattr(history_module.os, "replace", boom)
    with pytest.raises(HealthCachePersistenceError) as excinfo:
        cache.save()
    assert excinfo.value.stage == "replace"
    assert "injected replace failure" in str(excinfo.value)
    # No partial/authoritative state: the original file is unchanged.
    assert cache_file.read_bytes() == valid_bytes
    assert not cache._temp_path().exists()


def test_save_cleanup_failure_is_classified_without_hiding_primary(tmp_path):
    cache_file = tmp_path / "cache.json"
    cache = _seeded_cache(cache_file)
    valid_bytes = cache_file.read_bytes()

    # A directory squatting on the temp path makes the write fail AND makes
    # temporary-file cleanup impossible: both are reported, the primary cause
    # is the raised one, and the authoritative cache stays intact.
    temp_path = cache._temp_path()
    temp_path.mkdir()
    try:
        with pytest.raises(HealthCachePersistenceError) as excinfo:
            cache.save()
        error = excinfo.value
        assert error.stage == "write"
        assert error.cleanup_error is not None
        assert "cleanup also failed" in str(error)
        assert cache_file.read_bytes() == valid_bytes
    finally:
        temp_path.rmdir()


def test_save_never_reports_success_when_persistence_failed(tmp_path, monkeypatch):
    """Cross-check: a failed save can never look like a successful one."""
    cache_file = tmp_path / "cache.json"
    cache = _seeded_cache(cache_file)
    original = cache_file.read_bytes()

    calls = {"save": 0}

    def counting_boom(temp_file, payload):
        calls["save"] += 1
        raise OSError("nope")

    monkeypatch.setattr(history_module, "_write_temp_file", counting_boom)
    cache.records["p/m2"] = ModelHealthRecord(
        canonical_id="p/m2", provider="p", model_id="m2"
    )
    with pytest.raises(HealthCachePersistenceError):
        cache.save()
    assert calls["save"] == 1
    # The added record is NOT on disk: the file is exactly the old valid state.
    assert cache_file.read_bytes() == original
    assert "p/m2" not in json.loads(cache_file.read_text(encoding="utf-8"))


# ===================================================================
# 2. LOAD CORRUPTION MATRIX
# ===================================================================
def test_load_absent_and_loaded(tmp_path):
    absent = HealthCache(cache_file=tmp_path / "missing.json")
    assert absent.load_state == CacheLoadState.ABSENT
    assert absent.load_error == "" and absent.quarantine_path is None
    assert absent.records == {}
    assert not absent.corruption_detected

    cache_file = tmp_path / "cache.json"
    seeded = _seeded_cache(cache_file)
    fresh = HealthCache(cache_file=cache_file)
    assert fresh.load_state == CacheLoadState.LOADED
    assert not fresh.corruption_detected
    assert set(fresh.records) == set(seeded.records)


@pytest.mark.parametrize(
    "payload,expected_state",
    [
        (b'{"p/m1": {"latency_ms": 12.5, "tags": [', CacheLoadState.MALFORMED_JSON),
        (b'{"p/m1": {"provider": "p"', CacheLoadState.MALFORMED_JSON),
        (b'not json at all', CacheLoadState.MALFORMED_JSON),
        (b'["not", "an", "object"]', CacheLoadState.INVALID_RECORD),
        (b'{"p/m1": "not-a-record"}', CacheLoadState.INVALID_RECORD),
        (b'{"p/m1": {"latency_ms": "not-a-number"}}', CacheLoadState.INVALID_RECORD),
        (b'{"p/m1": {"status_code": "abc"}}', CacheLoadState.INVALID_RECORD),
        (b'{"p/m1": {"counters": "not-an-object"}}', CacheLoadState.INVALID_RECORD),
    ],
)
def test_load_corruption_is_surfaced_and_bytes_recoverable(
    tmp_path, payload, expected_state
):
    cache_file = tmp_path / "cache.json"
    cache_file.write_bytes(payload)

    cache = HealthCache(cache_file=cache_file)

    # Corruption is surfaced explicitly and never merged into state.
    assert cache.load_state == expected_state
    assert cache.corruption_detected
    assert cache.load_error
    assert cache.records == {}

    # Original bytes remain recoverable byte-for-byte at a deterministic name.
    quarantine = Path(cache.quarantine_path)
    assert quarantine == cache_file.with_name(cache_file.name + ".corrupt")
    assert quarantine.read_bytes() == payload
    # The corrupt source was moved out of the authoritative path, never
    # overwritten or silently re-read as an empty cache.
    assert not cache_file.exists()
    again = HealthCache(cache_file=cache_file)
    assert again.load_state == CacheLoadState.ABSENT


def test_load_io_failure_is_not_a_clean_empty_cache(tmp_path):
    cache_file = tmp_path / "cache.json"
    cache_file.mkdir()  # exists() is True, read_bytes() fails
    try:
        cache = HealthCache(cache_file=cache_file)
        assert cache.load_state == CacheLoadState.IO_ERROR
        assert cache.load_error
        assert cache.records == {}
        assert cache.quarantine_path is None
        assert cache_file.exists()  # untouched: an I/O failure is not corruption
    finally:
        cache_file.rmdir()


def test_load_invalid_record_keeps_earlier_quarantine(tmp_path):
    cache_file = tmp_path / "cache.json"
    first = b'{"p/m1": bad'
    cache_file.write_bytes(first)
    cache = HealthCache(cache_file=cache_file)
    assert Path(cache.quarantine_path).read_bytes() == first

    second = b'{"p/m1": "still bad"}'
    cache_file.write_bytes(second)
    cache = HealthCache(cache_file=cache_file)
    assert cache.load_state == CacheLoadState.INVALID_RECORD
    assert Path(cache.quarantine_path).read_bytes() == second
    # The earlier sample was not destroyed.
    assert cache_file.with_name(cache_file.name + ".corrupt").read_bytes() == first


# ===================================================================
# 3. SCANNER TERMINAL PERSISTENCE SEMANTICS
# ===================================================================
def test_scanner_terminal_persistence_failure_is_never_completed(tmp_path, monkeypatch):
    worker, cache = _ok_worker(tmp_path, "terminal")

    def failing_save():
        raise HealthCachePersistenceError(
            "replace", cache.cache_file, OSError(f"disk offline token={SECRET}")
        )

    monkeypatch.setattr(cache, "save", failing_save)
    statuses, failures = [], []
    worker.on_scan_completed = lambda sid, status="COMPLETED": statuses.append((sid, status))
    worker.on_scan_failed = lambda sid, err: failures.append((sid, err))

    models = _models(2)
    session_id = worker.run_scan_owned(models, mode=ScanMode.FULL)

    assert session_id == 1
    # Terminal state is NOT plain COMPLETED and is delivered exactly once.
    assert statuses == [(session_id, "COMPLETED_PERSISTENCE_FAILED")]
    assert len(failures) == 1
    reason = failures[0][1]
    assert "NOT persisted" in reason
    assert "disk offline" in reason
    assert SECRET not in reason and "[REDACTED]" in reason

    # The probes themselves succeeded (in-memory evidence exists).
    assert all(cache.get(m.canonical_id) is not None for m in models)
    # Execution ownership released and a later scan can start.
    assert worker.session_controller.active_session is None
    next_id = worker.try_start_session()
    assert next_id is not None and next_id == session_id + 1
    worker.session_controller.release_session(next_id)


def test_scanner_recovers_when_persistence_is_restored(tmp_path, monkeypatch):
    worker, cache = _ok_worker(tmp_path, "restored")

    def failing_save():
        raise HealthCachePersistenceError("write", cache.cache_file, OSError("gone"))

    monkeypatch.setattr(cache, "save", failing_save)
    statuses = []
    worker.on_scan_completed = lambda sid, status="COMPLETED": statuses.append(status)
    worker.run_scan_owned(_models(2), mode=ScanMode.FULL)
    assert statuses == ["COMPLETED_PERSISTENCE_FAILED"]
    assert not cache.cache_file.exists()  # nothing was durably written

    # Restore persistence: a later scan must be startable, honest and durable.
    monkeypatch.undo()
    monkeypatch.setenv("WATCHEDIT_LIVE_ACCESS", "1")
    statuses.clear()
    worker.on_scan_completed = lambda sid, status="COMPLETED": statuses.append(status)
    session_id = worker.run_scan_owned(_models(2), mode=ScanMode.FULL)
    assert statuses == ["COMPLETED"]
    assert cache.cache_file.exists()
    reloaded = HealthCache(cache_file=cache.cache_file)
    assert reloaded.load_state == CacheLoadState.LOADED
    assert set(reloaded.records) == {"p/m0", "p/m1"}
    assert worker.session_controller.active_session is None
    assert worker.session_controller.release_session(session_id) is False


def test_mid_scan_persistence_policy(tmp_path):
    """Documented + tested policy.

    * A periodic failure is NON-FATAL but pending: plain COMPLETED requires a
      later verified save (case A: later save succeeds -> COMPLETED).
    * If the final save fails too, terminal status is never COMPLETED (case B).
    * A failure of only the FINAL save is never COMPLETED either (case C).
    """
    # ---- case A: one periodic failure, then a verified terminal save ----
    worker_a, cache_a = _ok_worker(tmp_path, "policy-a")
    real_save_a = cache_a.save
    calls_a = {"n": 0}

    def flaky_once():
        calls_a["n"] += 1
        if calls_a["n"] == 1:
            raise HealthCachePersistenceError("write", cache_a.cache_file, OSError("transient"))
        real_save_a()

    cache_a.save = flaky_once
    statuses_a = []
    worker_a.on_scan_completed = lambda sid, status="COMPLETED": statuses_a.append(status)
    worker_a.run_scan_owned(_models(10), mode=ScanMode.FULL)
    assert calls_a["n"] >= 2  # periodic failure happened, then the terminal save
    assert statuses_a == ["COMPLETED"]
    assert cache_a.cache_file.exists()

    # ---- case B: periodic and terminal persistence both fail ----
    worker_b, cache_b = _ok_worker(tmp_path, "policy-b")

    def always_fail():
        raise HealthCachePersistenceError("write", cache_b.cache_file, OSError("still down"))

    cache_b.save = always_fail
    statuses_b = []
    failures_b = []
    worker_b.on_scan_completed = lambda sid, status="COMPLETED": statuses_b.append(status)
    worker_b.on_scan_failed = lambda sid, err: failures_b.append(err)
    worker_b.run_scan_owned(_models(10), mode=ScanMode.FULL)
    assert statuses_b == ["COMPLETED_PERSISTENCE_FAILED"]
    assert len(failures_b) == 1

    # ---- case C: only the FINAL save fails ----
    worker_c, cache_c = _ok_worker(tmp_path, "policy-c")
    real_save_c = cache_c.save
    calls_c = {"n": 0}

    def fail_last():
        calls_c["n"] += 1
        if calls_c["n"] == 2:
            raise HealthCachePersistenceError("replace", cache_c.cache_file, OSError("nope"))
        real_save_c()

    cache_c.save = fail_last
    statuses_c = []
    worker_c.on_scan_completed = lambda sid, status="COMPLETED": statuses_c.append(status)
    worker_c.run_scan_owned(_models(10), mode=ScanMode.FULL)
    assert calls_c["n"] == 2
    assert statuses_c == ["COMPLETED_PERSISTENCE_FAILED"]


def test_scanner_owns_release_after_persistence_failure(tmp_path, monkeypatch):
    worker, cache = _ok_worker(tmp_path, "own")

    def failing_save():
        raise HealthCachePersistenceError("mkdir", cache.cache_file, OSError("no"))

    monkeypatch.setattr(cache, "save", failing_save)
    session_id = worker.run_scan_owned(_models(1), mode=ScanMode.FULL)
    assert session_id == 1
    # Ownership cleanup still happened; no wedged scanner.
    assert worker.session_controller.active_session is None
    assert worker.session_controller.running is False
    assert worker.is_running() is False
    assert worker.try_start_session() is not None


# ===================================================================
# 4. UI SURFACING
# ===================================================================
def test_ui_surfaces_persistence_failure_distinctly(qapp, tmp_path):
    window = MainWindow()
    try:
        async def handler(request):
            return httpx.Response(200, json={"ok": True})

        window.worker.transport = httpx.MockTransport(handler)
        window.discovered_models = _models(2)
        window._models_by_cid = {m.canonical_id: m for m in window.discovered_models}

        def failing_save():
            raise HealthCachePersistenceError(
                "replace", window.cache.cache_file, OSError("disk offline")
            )

        window.cache.save = failing_save
        window.start_scan("ALL")
        session_id = window.worker.session_controller.active_session
        assert session_id is not None
        assert _wait(lambda: window.worker.session_controller.active_session is None)

        QApplication.processEvents()
        message = window.status_bar.currentMessage()
        assert "NOT saved" in message
        assert "disk offline" in message
        assert not message.startswith("Scan completed")
        # Terminal UI state still settled: footer idle, probes cleared.
        assert window.scan_footer.status_label.text() == "Ready / Idle"
        assert window._active_probes == 0
        assert window.watch_view.lbl_last_scan.text().startswith("Last scan: ")
    finally:
        window.worker.session_controller.close()
        window.close()


def test_ui_surfaces_cache_corruption_at_startup(qapp, tmp_path, monkeypatch):
    """Startup with a corrupt cache: the operator sees the explicit state and
    the recovery location instead of an apparently clean empty cache."""
    cache_file = tmp_path / "cache.json"
    payload = b'{"p/m1": {"latency_ms": '
    cache_file.write_bytes(payload)

    def _corrupt_cache(*args, **kwargs):
        return history_module.HealthCache(cache_file=cache_file)

    monkeypatch.setattr(main_window_module, "HealthCache", _corrupt_cache)
    window = MainWindow()
    try:
        message = window.status_bar.currentMessage()
        assert message.startswith("Health cache MALFORMED_JSON")
        assert "recoverable copy" in message
        assert window.cache.corruption_detected
        assert Path(window.cache.quarantine_path).read_bytes() == payload
        # Startup stays local: no live traffic, no scan, no probes.
        assert window.worker.is_running() is False
        assert window.scan_footer.status_label.text() == "Ready / Idle"
        assert window.discovered_models == []
    finally:
        window.worker.session_controller.close()
        window.close()


# ===================================================================
# 5. ROUND TRIP
# ===================================================================
def test_successful_persistence_and_restart_round_trip(tmp_path):
    cache_file = tmp_path / "cache.json"
    cache = HealthCache(cache_file=cache_file)

    counters_live = EvidenceCounters(consecutive_route_error=3)
    live = cache.record_evidence(
        "p/live", "Provider", "live", _evidence(counters=counters_live), auto_save=False
    )
    limiter = EvidenceCounters(consecutive_rate_limit=2, consecutive_timeout=1)
    limited = cache.record_evidence(
        "p/limited", "Provider", "limited",
        _evidence(
            availability=AvailabilityState.RATE_LIMIT,
            cost=CostState.UNKNOWN,
            counters=limiter,
        ),
        auto_save=False,
    )
    assert cache.set_cost_override("p/limited", "FREE") is not None
    assert cache.save() is None

    fresh = HealthCache(cache_file=cache_file)
    assert fresh.load_state == CacheLoadState.LOADED
    assert not fresh.corruption_detected
    assert fresh.load_error == ""
    assert set(fresh.records) == {"p/live", "p/limited"}

    for cid in ("p/live", "p/limited"):
        original = cache.records[cid]
        loaded = fresh.records[cid]
        assert asdict(loaded) == asdict(original), cid
        assert loaded.counters.to_dict() == original.counters.to_dict()
        assert loaded.last_tested_at == original.last_tested_at
        assert loaded.last_success_at == original.last_success_at
        assert loaded.success_streak == original.success_streak
        assert loaded.cost_override == original.cost_override
        assert loaded.state == original.state
        assert loaded.cost_status == original.cost_status
        assert loaded.availability == original.availability
        assert loaded.is_healthy() == original.is_healthy()

    # The two boundary records really differ (the round trip is not lossy in a
    # way that flattens them together).
    assert live.availability != limited.availability
    assert fresh.get("p/limited").cost_override == "FREE"
    assert fresh.get("p/limited").cost_status == "FREE"
    assert fresh.get("p/limited").is_free() is True
    assert fresh.get("p/limited").counters.consecutive_rate_limit == 2
    assert fresh.get("p/limited").counters.consecutive_timeout == 1


def _wait(predicate, timeout=10.0, step=0.01):
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(step)
    return predicate()
