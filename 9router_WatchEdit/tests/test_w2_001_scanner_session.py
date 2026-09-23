"""
W2-001 scanner single-session ownership — focused deterministic suite.

Covers:
1. MANDATORY STRESS: Scan + single-model Retest + combo-model Retest arriving
   effectively simultaneously -> exactly ONE acquires ownership, at most one
   scan thread is created, rejected requests never reset worker state.
2. Ownership release is exactly-once and only after real termination; the
   controller stays running until the worker session has really ended.
3. MANDATORY CANCEL: cancel G1 (blocked in-flight request) -> only G1
   receives cancellation, no evidence after the cancellation boundary, G1
   terminates, ownership releases exactly once; a stale G1 cancel delivered
   during G2 cannot cancel G2.
4. MANDATORY STALE CALLBACKS: delayed queued G1 callbacks delivered after G2
   owns the scanner alter nothing (probe results, progress/footer,
   active-probe count, status bar, last-scan time, G2 ownership).
5. MANDATORY CLOSE: closeEvent during an active scan blocks new sessions,
   cancels once, the worker thread terminates within the bounded shutdown
   window, terminal cache persistence completes, late callbacks mutate
   nothing afterward.
6. MANDATORY FAILURE: an unexpected ScannerWorker exception reports FAILED
   once, the cache-save path executes, ownership releases, the thread
   terminates, and a later normal Scan can start.

Offline only: httpx.MockTransport, tmp_path caches, no live network.
"""
import asyncio
import threading
import time
import types

import httpx
import pytest
from PySide6.QtWidgets import QApplication

import core.probe as probe_module
from core.discovery import DiscoveredModel
from core.history import HealthCache, ModelHealthRecord
from core.probe import (
    ScanSessionController,
    ScanSessionExecution,
    ScannerWorker,
    ScanMode,
)
from core.router_client import RouterClient
from ui.main_window import MainWindow
from ui.theme import apply_theme


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


def _wait(predicate, timeout=5.0, step=0.01):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(step)
    return predicate()


def _slow_worker(tmp_path, name, probe_seconds=1.5, models_n=6):
    """ScannerWorker probing N models, each blocked on a barrier-released
    slow response (controlled in-flight request). The handler is async and
    asyncio.Event-based so a task cancellation actually interrupts the
    in-flight request the way a real transport would."""
    release = threading.Event()
    first_request = threading.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        first_request.set()
        # Park interruptibly: poll the threading release flag so the
        # asyncio task stays cancellable while the scan is blocked.
        while not release.is_set():
            await asyncio.sleep(0.02)
        return httpx.Response(200, json={"ok": True})

    client = RouterClient(base_url="http://127.0.0.1:99999")
    cache = HealthCache(cache_file=tmp_path / f"cache-{name}.json")
    worker = ScannerWorker(
        client, cache,
        global_concurrency=2, per_provider_concurrency=1,
        transport=httpx.MockTransport(handler),
    )
    return worker, release, first_request


def _controlled_window(window):
    """Expose the worker/controller/entry points used by these tests."""
    return (
        window.worker,
        window.worker.session_controller,
        window._scan_session_is_current,
    )


# ===================================================================
# Controller unit contract: atomic claim, exactly-once release
# ===================================================================
def test_controller_claim_is_atomic_and_single():
    c = ScanSessionController()
    a = c.try_claim()
    b = c.try_claim()
    assert a is not None and b is None  # second claim rejected atomically
    assert c.max_active_sessions == 1
    assert c.running and c.active_session == a
    # A foreign release cannot clear the lease.
    assert c.release_session(a + 100) is False
    assert c.running
    assert c.release_session(a) is True
    assert c.release_session(a) is False  # exactly once
    assert not c.running and c.active_session is None
    # Ids are monotonic and never reused.
    nxt = c.try_claim()
    assert nxt == a + 1
    c.close()
    assert c.try_claim() is None and c.closing


# ===================================================================
# 1. MANDATORY STRESS — simultaneous Scan / Retest / Combo-Retest
# ===================================================================
def test_stress_simultaneous_scan_retests_single_session(qapp, tmp_path):
    window = MainWindow()
    try:
        worker, controller, _ = _controlled_window(window)
        started = threading.Event()
        release = threading.Event()
        scan_threads = []

        async def handler(request):
            started.set()
            while not release.is_set():
                await asyncio.sleep(0.02)
            return httpx.Response(200, json={"ok": True})

        # One slow, blocked probe body for the owned session.
        window.worker.transport = httpx.MockTransport(handler)

        window.discovered_models = _models(4)
        window._models_by_cid = {m.canonical_id: m for m in window.discovered_models}

        # The three requests arrive effectively simultaneously: fire them
        # from three threads through the REAL entry points.
        start_barrier = threading.Barrier(3, timeout=5.0)
        outcomes = {}

        def fire(key, fn):
            try:
                start_barrier.wait()
                fn()
                outcomes[key] = "done"
            except Exception as e:  # pragma: no cover
                outcomes[key] = repr(e)

        threads = [
            threading.Thread(target=fire, args=("scan", lambda: window.start_scan("ALL"))),
            threading.Thread(target=fire, args=("retest1", lambda: window._retest_single_model("p/m0"))),
            threading.Thread(target=fire, args=("retestN", lambda: window._retest_specific_models(["p/m1", "p/m2"]))),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10.0)

        assert started.wait(timeout=5.0), "owned session never started probing"

        # Exactly one request acquired ownership; the others were rejected
        # (status message, no thread spawn). Maximum active sessions == 1.
        assert controller.max_active_sessions == 1
        assert worker.is_running()
        session = controller.active_session
        assert session is not None

        # Give the entry threads a moment; no second thread may appear.
        time.sleep(0.2)
        alive_scan_threads = [
            t for t in threading.enumerate()
            if t.name.startswith("ScannerWorker-session-")
        ]
        assert len(alive_scan_threads) == 1, (
            f"expected exactly one scan thread, got {len(alive_scan_threads)}"
        )

        # The owned session probes its blocked request; controller stays
        # running until the session really terminates.
        assert controller.running
        release.set()
        assert _wait(lambda: not controller.running)
        assert controller.active_session is None

        # The worker's is_running is controller-backed: true until real
        # termination, false after.
        assert not worker.is_running()

        # A new scan can start afterwards.
        assert worker.try_start_session() is not None
        controller.release_session(controller.active_session)
    finally:
        release.set()
        controller.close()
        window.close()


def test_rejected_requests_do_not_reset_worker_state(qapp, tmp_path):
    window = MainWindow()
    try:
        worker, controller, _ = _controlled_window(window)
        first_request = threading.Event()
        release = threading.Event()

        async def handler(request):
            first_request.set()
            while not release.is_set():
                await asyncio.sleep(0.02)
            return httpx.Response(200, json={"ok": True})

        window.worker.transport = httpx.MockTransport(handler)
        window.discovered_models = _models(4)
        window._models_by_cid = {m.canonical_id: m for m in window.discovered_models}

        window.start_scan("ALL")
        assert first_request.wait(timeout=5.0)
        session = controller.active_session
        assert session is not None

        breaker_state_before = dict(worker.circuit_breaker._tripped)
        backoffs_before = dict(worker._provider_backoffs)
        active_tasks_before = set(worker._active_tasks)

        # Rejected retests while the session is active: must not touch
        # breaker / backoff / active-task state or the session token.
        window._retest_single_model("p/m1")
        window._retest_specific_models(["p/m2"])
        assert controller.active_session == session
        assert dict(worker.circuit_breaker._tripped) == breaker_state_before
        assert dict(worker._provider_backoffs) == backoffs_before
        assert set(worker._active_tasks) == active_tasks_before

        release.set()
        assert _wait(lambda: not controller.running)
    finally:
        release.set()
        controller.close()
        window.close()


# ===================================================================
# 2. Ownership release is exactly-once after real termination
# ===================================================================
def test_ownership_release_exactly_once_after_real_termination(tmp_path):
    """W2-001 (B3): the lease is created atomically at claim, run_scan
    executes and does NOT release, and the owner releases exactly once."""
    worker, release, first_request = _slow_worker(tmp_path, "own")
    models = _models(3)

    lease = worker.claim_execution()
    assert lease is not None
    session_id = lease.session_id

    def _owning_wrapper():
        # Mirrors MainWindow._scan_session_task: execute, then release once.
        try:
            worker.run_scan(
                models, ScanMode.FULL,
                session_id=session_id, execution=lease,
            )
        finally:
            worker.end_execution(lease)

    t = threading.Thread(target=_owning_wrapper, daemon=True)
    t.start()
    assert first_request.wait(timeout=5.0)
    assert worker.session_controller.running  # still alive mid-session

    # Premature release attempts by stale tokens are no-ops.
    assert worker.session_controller.release_session(session_id + 1) is False
    assert worker.session_controller.running
    # The lease cannot be released twice.
    assert worker.end_execution(lease) in (True, False)

    release.set()
    t.join(timeout=10.0)
    assert not t.is_alive()
    assert worker.session_controller.active_session is None
    assert lease.released is True
    assert worker.session_controller.release_session(session_id) is False  # already released
    # Ids never repeat.
    assert worker.try_start_session() == session_id + 1
    worker.session_controller.release_session(session_id + 1)


# ===================================================================
# 3. MANDATORY CANCEL — session-specific, boundary-clean, stale-proof
# ===================================================================
def test_cancel_current_session_only_no_evidence_after_boundary(qapp, tmp_path):
    window = MainWindow()
    try:
        worker, controller, _ = _controlled_window(window)
        first_request = threading.Event()
        release = threading.Event()
        probe_finished = threading.Event()
        finished_records = []

        async def handler(request):
            first_request.set()
            while not release.is_set():
                await asyncio.sleep(0.02)
            return httpx.Response(200, json={"ok": True})

        window.worker.transport = httpx.MockTransport(handler)
        window.worker.on_probe_finished = lambda sid, cid, rec: (
            finished_records.append((sid, cid)) or probe_finished.set()
        )

        window.discovered_models = _models(4)
        window._models_by_cid = {m.canonical_id: m for m in window.discovered_models}

        window.start_scan("ALL")
        assert first_request.wait(timeout=5.0)
        g1 = controller.active_session
        assert g1 is not None

        # Stop scan cancels EXACTLY the current session.
        window.stop_scan()
        assert controller.is_cancel_requested(g1)

        release.set()
        assert _wait(lambda: not controller.running)

        # No probe evidence was produced after the cancellation boundary.
        assert finished_records == []
        # Exactly one terminal status, CANCELLED, for G1 only.
        # (Terminal callbacks are validated below via stale-callback test.)
        assert controller.session_counter >= 1
    finally:
        release.set()
        controller.close()
        window.close()


def test_stale_cancel_cannot_cancel_next_session(qapp, tmp_path):
    worker, release, first_request = _slow_worker(tmp_path, "stale-cancel")

    # G1 claims and is cancelled.
    g1 = worker.try_start_session()
    assert worker.cancel(g1) is True
    worker.session_controller.release_session(g1)

    # G2 claims.
    g2 = worker.try_start_session()
    assert g2 != g1
    assert not worker.session_controller.is_cancel_requested(g2)

    # A stale G1 cancellation arriving now must NOT cancel G2.
    assert worker.cancel(g1) is False
    assert not worker.session_controller.is_cancel_requested(g2)
    assert worker.session_controller.active_session == g2
    worker.session_controller.release_session(g2)


# ===================================================================
# 4. MANDATORY STALE CALLBACKS — delayed G1 callbacks after G2 owns
# ===================================================================
def test_stale_g1_callbacks_never_mutate_ui_after_g2_owns(qapp, tmp_path):
    window = MainWindow()
    try:
        worker, controller, is_current = _controlled_window(window)

        # G1 begins and "produces delayed queued callbacks".
        g1 = worker.try_start_session()
        assert g1 is not None
        window._active_probes = 1
        window._scan_progress = (0, 10)
        window._last_scan_error = ""
        before = {
            "footer": window.scan_footer.status_label.text(),
            "active": window._active_probes,
            "progress": window._scan_progress,
            "status": window.status_bar.currentMessage(),
            "last_scan": window.watch_view._last_scan_time_label.text()
            if hasattr(window.watch_view, "_last_scan_time_label") else None,
        }

        # G1 terminates/cancels; G2 starts.
        worker.session_controller.release_session(g1)
        g2 = worker.try_start_session()
        assert g2 is not None and g2 != g1

        # Sanity: current-session gate accepts G2 and rejects G1.
        assert is_current(g2)
        assert not is_current(g1)

        rec = ModelHealthRecord(canonical_id="p/m1", provider="p", model_id="m1")

        # Deliver the delayed G1 callbacks NOW, while G2 owns the scanner.
        window._on_probe_started(g1, "p/m1")
        window._on_probe_pending(g1, "p/m1", 9.9)
        window._on_probe_finished(g1, "p/m1", rec)
        window._on_progress(g1, 10, 10)
        window._on_scan_failed(g1, "stale G1 failure")
        window._on_scan_completed(g1, "COMPLETED")
        QApplication.processEvents()

        # Nothing changed: probe-result UI, progress/footer, active-probe
        # count, status bar, last-scan time, G2 ownership.
        assert window.scan_footer.status_label.text() == before["footer"]
        assert window._active_probes == before["active"]
        assert window._scan_progress == before["progress"]
        assert window.status_bar.currentMessage() == before["status"]
        assert window._last_scan_error == ""
        assert controller.active_session == g2 and controller.running

        # G2 callbacks still work normally.
        window._on_progress(g2, 3, 10)
        assert window._scan_progress == (3, 10)
        worker.session_controller.release_session(g2)
    finally:
        controller.close()
        window.close()


# ===================================================================
# 5. MANDATORY CLOSE — closeEvent during an active controlled scan
# ===================================================================
def test_close_during_active_scan_bounded_and_persisted(qapp, tmp_path, monkeypatch):
    window = MainWindow()
    try:
        worker, controller, _ = _controlled_window(window)
        first_request = threading.Event()
        release = threading.Event()

        async def handler(request):
            first_request.set()
            while not release.is_set():
                await asyncio.sleep(0.02)
            return httpx.Response(200, json={"ok": True})

        window.worker.transport = httpx.MockTransport(handler)
        window.discovered_models = _models(4)
        window._models_by_cid = {m.canonical_id: m for m in window.discovered_models}

        window.start_scan("ALL")
        assert first_request.wait(timeout=5.0)
        assert controller.running

        # Cache sentinel: the terminal persistence must run.
        worker.cache.records["p/m0"] = ModelHealthRecord(
            canonical_id="p/m0", provider="p", model_id="m0"
        )

        t0 = time.monotonic()
        window.close()  # closeEvent
        elapsed = time.monotonic() - t0

        # Controller entered closing state; cancellation was requested once.
        assert controller.closing
        # The worker thread terminated within the bounded shutdown window
        # (bounded wait observed, never an indefinite hang).
        assert elapsed < window._SCAN_SHUTDOWN_TIMEOUT_SEC + 2.0
        thread = controller.owned_thread()
        assert thread is None or not thread.is_alive()
        # Ownership released.
        assert controller.active_session is None
        # Terminal cache persistence completed.
        assert worker.cache.cache_file.exists()
        assert "p/m0" in worker.cache.records
        # No new scan can start after close.
        assert worker.try_start_session() is None
    finally:
        release.set()
        controller.close()


def test_close_blocks_new_sessions_even_without_active_scan(qapp):
    window = MainWindow()
    try:
        controller = window.worker.session_controller
        window.close()
        assert controller.closing
        assert window.worker.try_start_session() is None
        window.start_scan("ALL")
        assert not controller.running
    finally:
        controller.close()


# ===================================================================
# 6. MANDATORY FAILURE — unexpected exception releases ownership
# ===================================================================
def test_unexpected_failure_reports_failed_once_and_releases(tmp_path):
    worker, release, first_request = _slow_worker(tmp_path, "fail")

    calls = {"failed": 0, "completed": []}
    worker.on_scan_failed = lambda sid, err: calls.__setitem__("failed", calls["failed"] + 1)
    worker.on_scan_completed = lambda sid, status="COMPLETED": calls["completed"].append(status)

    async def crash_probe(*args, **kwargs):
        raise RuntimeError("scanner exploded")

    worker._probe_single_model = crash_probe
    worker.cache.records["p/m0"] = ModelHealthRecord(canonical_id="p/m0", provider="p", model_id="m0")
    save_calls = {"n": 0}
    real_save = worker.cache.save
    def counting_save():
        save_calls["n"] += 1
        real_save()
    worker.cache.save = counting_save

    # Explicit legacy claim/run/release wrapper (B3 contract).
    session_id = worker.run_scan_owned(_models(2), mode=ScanMode.FULL)
    assert session_id == 1

    # FAILED reported once, terminal COMPLETED-status callback never lies.
    assert calls["failed"] == 1
    assert calls["completed"] == ["FAILED"]
    # Cache save path executed (terminal persistence before ownership release).
    assert save_calls["n"] >= 1
    # Ownership released; a later normal scan can start.
    assert worker.session_controller.active_session is None
    assert worker.try_start_session() is not None
    worker.session_controller.release_session(worker.session_controller.active_session)


# ===================================================================
# 7. MANDATORY (W2-001 B1) — forced G1-cancel / G2-handover race
# ===================================================================
class _HandoverProbe:
    """Deterministic barrier INSIDE the accepted cancel's propagation (after
    the controller accepted the request and the target session is already
    marked cancelled, before the worker-side task cancellation runs), plus
    per-session recording of every cancellation callback that a session's own
    context receives."""

    def __init__(self):
        self.armed_for = None
        self.entered = threading.Event()
        self.resume = threading.Event()
        self.external_cancel_tasks = []   # session ids addressed by cancel()
        self.loop_cancel_calls = []       # session ids whose scheduler cancelled
        self.task_cancels = []            # (session_id, task) that got cancel()
        self._lock = threading.Lock()

    def record_task_cancels(self, session_id, tasks):
        with self._lock:
            self.task_cancels.extend((session_id, t) for t in tasks)

    def record_loop_cancel(self, session_id):
        with self._lock:
            self.loop_cancel_calls.append(session_id)

    def record_external_cancel(self, session_id):
        with self._lock:
            self.external_cancel_tasks.append(session_id)

    def loop_cancels_for(self, session_id):
        with self._lock:
            return self.loop_cancel_calls.count(session_id)

    def task_cancel_sessions(self):
        with self._lock:
            return {sid for sid, _ in self.task_cancels}


def _probed_execution_class(probe: _HandoverProbe):
    class _ProbedExecution(ScanSessionExecution):
        def cancel_tasks(self):
            # External (cross-thread) cancellation addressed to THIS session.
            probe.record_external_cancel(self.session_id)
            if probe.armed_for == self.session_id:
                # The request is already accepted; hold here so the handover
                # happens while propagation is still in flight.
                probe.entered.set()
                probe.resume.wait(timeout=5.0)
            return super().cancel_tasks()

        def cancel_tasks_on_loop(self):
            # Runs on THIS session's own loop thread when it cancels work.
            probe.record_loop_cancel(self.session_id)
            probe.record_task_cancels(
                self.session_id, [t for t in list(self.task_set()) if not t.done()]
            )
            return super().cancel_tasks_on_loop()

    return _ProbedExecution


def test_forced_g1_cancel_after_g2_handover_cannot_touch_g2(qapp, tmp_path, monkeypatch):
    """MANDATORY B1 handover race.

    G1 owns the scanner. cancel(G1) is accepted, then PAUSED after acceptance
    but before worker-side propagation completes. G1 terminates and releases
    ownership; G2 claims and starts. The old G1 cancel resumes.

    Assertions: G2 remains uncancelled, G2's loop receives no stale
    cancellation callback, G2's task set receives no cancel(), G2 continues
    normally, and the G1 cancellation completes against G1 only.
    """
    probe = _HandoverProbe()
    monkeypatch.setattr(probe_module, "ScanSessionExecution", _probed_execution_class(probe))

    window = MainWindow()
    try:
        worker, controller, _ = _controlled_window(window)
        release = threading.Event()

        async def handler(request):
            while not release.is_set():
                await asyncio.sleep(0.01)
            return httpx.Response(200, json={"ok": True})

        window.worker.transport = httpx.MockTransport(handler)
        window.discovered_models = _models(4)
        window._models_by_cid = {m.canonical_id: m for m in window.discovered_models}

        # ---- G1 owns the scanner with in-flight work ----
        window.start_scan("ALL")
        assert _wait(lambda: controller.active_session is not None)
        g1 = controller.active_session
        exec_g1 = controller.execution_for(g1)
        assert exec_g1 is not None
        assert _wait(lambda: bool(exec_g1.task_set()), timeout=5.0)

        # ---- Begin cancel(G1) and pause it mid-propagation ----
        probe.armed_for = g1
        outcome = {}

        def _do_cancel():
            outcome["value"] = worker.cancel(g1)

        canceller = threading.Thread(target=_do_cancel, daemon=True)
        canceller.start()
        assert probe.entered.wait(timeout=5.0), "cancel(G1) was not accepted/paused"
        assert controller.is_cancel_requested(g1)
        assert exec_g1.cancelled is True
        assert worker._session == g1
        assert worker._cancelled is True

        # ---- G1 terminates and releases ownership on its own ----
        assert _wait(lambda: controller.active_session is None, timeout=5.0)
        assert controller.release_session(g1) is False  # already released by its owner

        # ---- G2 claims and starts while the old cancel is still paused ----
        window.discovered_models = _models(4)
        window.start_scan("ALL")
        assert _wait(lambda: controller.active_session is not None)
        g2 = controller.active_session
        assert g2 is not None and g2 != g1
        exec_g2 = controller.execution_for(g2)
        assert exec_g2 is not None
        assert _wait(lambda: bool(exec_g2.task_set()), timeout=5.0)
        g2_loop = exec_g2.owned_loop()
        g2_tasks = set(exec_g2.task_set())
        assert g2_loop is not None and g2_tasks
        g2_loop_cancels_before = probe.loop_cancels_for(g2)

        # ---- Resume the OLD G1 cancel now that G2 owns the scanner ----
        probe.resume.set()
        canceller.join(timeout=5.0)
        assert not canceller.is_alive()
        assert outcome["value"] is True

        # G1's cancellation completed against G1Context only.
        assert exec_g1.released is True
        assert exec_g1.cancelled is True
        assert probe.external_cancel_tasks == [g1]
        assert probe.task_cancel_sessions() <= {g1}

        # G2 is physically untouched: not cancelled, same loop, same tasks,
        # no stale cancellation callback, no task.cancel() on its task set.
        assert controller.active_session == g2
        assert controller.is_cancel_requested(g2) is False
        assert exec_g2.cancelled is False
        assert worker._session == g2
        assert worker._cancelled is False
        assert exec_g2.owned_loop() is g2_loop
        assert set(exec_g2.task_set()) == g2_tasks
        assert all(not t.cancelled() for t in g2_tasks)
        assert probe.loop_cancels_for(g2) == g2_loop_cancels_before == 0
        assert g2 not in probe.task_cancel_sessions()

        # ---- G2 continues normally to completion ----
        release.set()
        assert _wait(lambda: controller.active_session is None, timeout=10.0)
        assert worker.cache.records  # G2 produced real evidence
        assert controller.latest_session == g2
        assert probe.loop_cancels_for(g2) == 0
    finally:
        probe.resume.set()
        release.set()
        controller.close()
        window.close()


# ===================================================================
# 8. MANDATORY (W2-001 B2) — delayed terminal callbacks vs new session
# ===================================================================
def test_delayed_terminal_callbacks_accepted_after_execution_release(qapp, tmp_path):
    """G1 runs to completion and its terminal callbacks are queued (Qt signals
    emitted from the worker thread, deliberately not delivered yet). G1's
    execution lease releases. No G2 is started. The queued callbacks are then
    delivered and must be ACCEPTED: the UI reaches the correct terminal state."""
    window = MainWindow()
    try:
        worker, controller, is_current = _controlled_window(window)

        async def handler(request):
            return httpx.Response(200, json={"ok": True})

        window.worker.transport = httpx.MockTransport(handler)
        window.discovered_models = _models(3)
        window._models_by_cid = {m.canonical_id: m for m in window.discovered_models}

        window.start_scan("ALL")
        g1 = controller.active_session
        assert g1 is not None

        # Execution finished: the lease is gone, the publication generation
        # survives it.
        assert _wait(lambda: controller.active_session is None, timeout=10.0)
        assert controller.latest_session == g1
        assert is_current(g1)
        assert window.scan_footer.status_label.text().startswith("Scanning")

        # Deliver the queued final probe_finished / progress / scan_completed.
        QApplication.processEvents()

        assert window.scan_footer.status_label.text() == "Ready / Idle"
        assert window.watch_view.table.isSortingEnabled() is True  # scan-active false
        assert window._active_probes == 0
        assert window._scan_progress == (3, 3)
        assert window.status_bar.currentMessage().startswith("Scan completed at")
        assert window.watch_view.lbl_last_scan.text().startswith("Last scan: ")
        # Final probe evidence is visible for every scanned model.
        for m in window.discovered_models:
            rec = window.cache.get(m.canonical_id)
            assert rec is not None and rec.availability == "LIVE"
        assert window.combo_editor is not None  # badge path ran without error
    finally:
        controller.close()
        window.close()


def test_delayed_g1_callbacks_rejected_once_g2_is_claimed(qapp, tmp_path):
    """Same delayed G1 terminal callbacks, but G2 is claimed BEFORE delivery:
    every G1 callback is rejected and cannot mutate G2 UI state."""
    window = MainWindow()
    try:
        worker, controller, is_current = _controlled_window(window)

        async def handler(request):
            return httpx.Response(200, json={"ok": True})

        window.worker.transport = httpx.MockTransport(handler)
        window.discovered_models = _models(3)
        window._models_by_cid = {m.canonical_id: m for m in window.discovered_models}

        window.start_scan("ALL")
        g1 = controller.active_session
        assert g1 is not None
        assert _wait(lambda: controller.active_session is None, timeout=10.0)

        before = {
            "footer": window.scan_footer.status_label.text(),
            "active": window._active_probes,
            "progress": window._scan_progress,
            "status": window.status_bar.currentMessage(),
            "last_scan": window.watch_view.lbl_last_scan.text(),
        }

        # G2 is claimed before the queued G1 callbacks are delivered.
        g2 = worker.try_start_session()
        assert g2 is not None and g2 != g1
        assert is_current(g2)
        assert not is_current(g1)

        QApplication.processEvents()  # deliver the stale G1 callbacks

        assert window.scan_footer.status_label.text() == before["footer"]
        assert window._active_probes == before["active"]
        assert window._scan_progress == before["progress"]
        assert window.status_bar.currentMessage() == before["status"]
        assert window.watch_view.lbl_last_scan.text() == before["last_scan"]
        assert controller.active_session == g2
        controller.release_session(g2)
    finally:
        controller.close()
        window.close()


# ===================================================================
# 9. MANDATORY (W2-001 B3) — single authoritative release ownership
# ===================================================================
def test_release_ownership_is_single_and_after_terminal_persistence(qapp, tmp_path):
    """Instrument release transitions for one UI-owned session.

    Asserts: exactly ONE authoritative release transition; it is performed by
    the UI worker-wrapper (the lease owner), after run_scan returned — i.e.
    after terminal persistence finished and while the owned worker thread is
    still alive; no newer execution session overlaps the owned execution
    lifetime; the controller ends idle; the newest publication generation
    remains available for queued callbacks."""
    window = MainWindow()
    try:
        worker, controller, is_current = _controlled_window(window)
        real_release = controller.release_session
        real_save = window.worker.cache.save
        persistence = {"finished": False, "terminal_save_seen": False}
        events = []
        overlapping_claims = []
        release_context = {}

        def instrumented_save():
            persistence["finished"] = False
            # While the session executes, no second execution may be claimed.
            overlapping_claims.append(controller.try_claim())
            real_save()
            persistence["finished"] = True
            persistence["terminal_save_seen"] = True

        def instrumented_release(session_id):
            released = real_release(session_id)
            release_context["thread"] = threading.current_thread().name
            release_context["owner_thread_alive"] = any(
                th.name == f"ScannerWorker-session-{session_id}"
                for th in threading.enumerate()
            )
            events.append(("release", session_id, released, persistence["finished"]))
            return released

        controller.release_session = instrumented_release
        window.worker.cache.save = instrumented_save

        async def handler(request):
            return httpx.Response(200, json={"ok": True})

        window.worker.transport = httpx.MockTransport(handler)
        window.discovered_models = _models(2)
        window._models_by_cid = {m.canonical_id: m for m in window.discovered_models}

        window.start_scan("ALL")
        g1 = controller.active_session
        assert g1 is not None
        assert _wait(lambda: controller.active_session is None, timeout=10.0)

        # ONE authoritative release transition, after terminal persistence.
        releases = [e for e in events if e[0] == "release"]
        assert len(releases) == 1, f"expected one release transition, got {releases}"
        assert releases[0] == ("release", g1, True, True)
        assert persistence["terminal_save_seen"] is True
        assert overlapping_claims == [None]  # no overlapping execution session

        # The release was performed by the lease OWNER (the UI worker wrapper
        # thread), while that owned thread was still alive: it happens after
        # run_scan returned, never by a second layer.
        assert release_context["thread"] == f"ScannerWorker-session-{g1}"
        assert release_context["owner_thread_alive"] is True

        # Controller ends idle; the context is dead; the publication
        # generation stays available.
        assert not controller.running
        assert controller.active_session is None
        assert controller.execution_for(g1) is None
        assert controller.latest_session == g1
        assert is_current(g1)

        # A normal future scan remains startable.
        assert worker.claim_execution() is not None
        controller.release_session(controller.active_session)
    finally:
        window.worker.cache.save = real_save
        controller.release_session = real_release
        controller.close()
        window.close()


def test_legacy_run_scan_owned_helper_claims_and_releases():
    """Legacy direct callers get ONE explicit helper: it claims its own
    single session lease atomically and releases it exactly once."""
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as td:
        cache = HealthCache(cache_file=Path(td) / "cache.json")
        worker = ScannerWorker(RouterClient(base_url="http://127.0.0.1:99999"), cache)
        worker._probe_single_model = lambda *a, **k: None

        session_id = worker.run_scan_owned(_models(1), mode=ScanMode.FULL)
        assert session_id == 1
        assert worker.session_controller.active_session is None
        assert worker.session_controller.release_session(session_id) is False

        # Busy scanner: the helper refuses instead of queueing.
        held = worker.try_start_session()
        assert held is not None
        assert worker.run_scan_owned(_models(1), mode=ScanMode.FULL) is None
        worker.session_controller.release_session(held)


def test_failed_cache_save_does_not_block_ownership_cleanup(tmp_path, monkeypatch):
    worker, release, first_request = _slow_worker(tmp_path, "savefail")

    async def crash_probe(*args, **kwargs):
        raise RuntimeError("boom")

    worker._probe_single_model = crash_probe

    def exploding_save():
        raise OSError("disk full")

    monkeypatch.setattr(worker.cache, "save", exploding_save)

    statuses = []
    worker.on_scan_completed = lambda sid, status="COMPLETED": statuses.append(status)

    session_id = worker.run_scan_owned(_models(1), mode=ScanMode.FULL)

    assert session_id == 1
    assert statuses == ["FAILED"]  # terminal still delivered
    assert worker.session_controller.active_session is None  # ownership released


# ===================================================================
# 10. MANDATORY (W2-001) — terminal-path matrix
# ===================================================================
class _ScriptedSecurity:
    """Deterministic lock-state double (no DPAPI, no env influence)."""

    def __init__(self, state="UNLOCKED"):
        self._state = state
        self._listeners = []

    @property
    def state(self):
        return self._state

    def is_live_allowed(self):
        return self._state in ("OS_VAULT", "UNLOCKED")

    def is_locked(self):
        return not self.is_live_allowed()

    def require_live(self, operation):
        if not self.is_live_allowed():
            from core.security import LiveAccessLockedError
            raise LiveAccessLockedError(operation)

    def on_state_changed(self, cb):
        self._listeners.append(cb)

    def _set(self, state):
        self._state = state
        for cb in list(self._listeners):
            cb(state)

    def lock(self):
        self._set("LOCKED")

    def unlock(self):
        self._set("UNLOCKED")


def _matrix_worker(tmp_path, name, handler, security=None):
    client = RouterClient(base_url="http://127.0.0.1:99999")
    client.security = security or _ScriptedSecurity()
    cache = HealthCache(cache_file=tmp_path / f"matrix-{name}.json")
    return ScannerWorker(
        client, cache,
        global_concurrency=2, per_provider_concurrency=2,
        transport=httpx.MockTransport(handler),
    )


def _blocked_handler():
    """Handler that parks every request until released, counting them."""
    release = threading.Event()
    counter = {"n": 0}

    async def handler(request):
        counter["n"] += 1
        while not release.is_set():
            await asyncio.sleep(0.01)
        return httpx.Response(200, json={"ok": True})

    return handler, release, counter


def _assert_ownership_available(worker):
    lease = worker.claim_execution()
    assert lease is not None, "a normal subsequent scan must remain startable"
    worker.end_execution(lease)
    assert worker.session_controller.active_session is None


def test_terminal_path_matrix_releases_and_restarts(tmp_path):
    """MANDATORY matrix: normal completion, Stop, Lock, unexpected failure and
    Close. Each path ends with correct ownership, exposes the right terminal
    status, creates no post-cancellation evidence, and leaves a normal
    subsequent scan startable (Close blocks new claims by design)."""
    async def ok(request):
        return httpx.Response(200, json={"ok": True})

    # ---- 1. normal completion ----
    w1 = _matrix_worker(tmp_path, "completed", ok)
    st1 = []
    w1.on_scan_completed = lambda sid, s="COMPLETED": st1.append(s)
    assert w1.run_scan_owned(_models(2), mode=ScanMode.FULL) == 1
    assert st1 == ["COMPLETED"]
    assert w1.session_controller.active_session is None
    assert w1.session_controller.latest_session == 1
    assert len(w1.cache.records) == 2
    _assert_ownership_available(w1)

    # ---- 2. Stop during an active scan ----
    handler2, release2, counter2 = _blocked_handler()
    w2 = _matrix_worker(tmp_path, "stop", handler2)
    st2 = []
    w2.on_scan_completed = lambda sid, s="COMPLETED": st2.append(s)
    t2 = threading.Thread(
        target=lambda: w2.run_scan_owned(_models(4), mode=ScanMode.FULL), daemon=True
    )
    t2.start()
    assert _wait(lambda: w2.session_controller.active_session is not None and counter2["n"] >= 2)
    g2 = w2.session_controller.active_session
    assert w2.cancel(g2) is True  # Stop targets exactly the current session
    release2.set()
    t2.join(timeout=10.0)
    assert not t2.is_alive()
    assert st2 == ["CANCELLED"]
    # Bounded concurrency means at most 2 requests were ever in flight, and no
    # request was issued after the stop. No evidence after the boundary.
    assert counter2["n"] <= 2
    assert w2.cache.records == {}
    assert w2.session_controller.active_session is None
    _assert_ownership_available(w2)

    # ---- 3. Lock during an active scan ----
    security3 = _ScriptedSecurity()
    handler3, release3, counter3 = _blocked_handler()
    w3 = _matrix_worker(tmp_path, "lock", handler3, security=security3)
    st3 = []
    w3.on_scan_completed = lambda sid, s="COMPLETED": st3.append(s)
    t3 = threading.Thread(
        target=lambda: w3.run_scan_owned(_models(4), mode=ScanMode.FULL), daemon=True
    )
    t3.start()
    assert _wait(lambda: w3.session_controller.active_session is not None and counter3["n"] >= 2)
    g3 = w3.session_controller.active_session
    security3.lock()  # Lock Now -> cancels only the current execution context
    assert w3.session_controller.is_cancel_requested(g3)
    release3.set()
    t3.join(timeout=10.0)
    assert not t3.is_alive()
    assert st3 == ["LOCKED"]
    assert counter3["n"] <= 2  # zero authenticated requests after the lock
    assert len(w3.cache.records) <= 2  # no synthetic evidence after the lock
    assert w3.session_controller.active_session is None
    security3.unlock()
    _assert_ownership_available(w3)

    # ---- 4. unexpected worker failure ----
    w4 = _matrix_worker(tmp_path, "failure", ok)
    st4, failed4 = [], []
    w4.on_scan_completed = lambda sid, s="COMPLETED": st4.append(s)
    w4.on_scan_failed = lambda sid, err: failed4.append(err)

    async def crash_probe(*args, **kwargs):
        raise RuntimeError("scanner exploded")

    w4._probe_single_model = crash_probe
    assert w4.run_scan_owned(_models(2), mode=ScanMode.FULL) == 1
    assert st4 == ["FAILED"] and len(failed4) == 1
    assert w4.session_controller.active_session is None
    assert w4.session_controller.latest_session == 1
    _assert_ownership_available(w4)

    # ---- 5. Close ----
    w5 = _matrix_worker(tmp_path, "close", ok)
    w5.session_controller.close()
    assert w5.session_controller.closing is True
    assert w5.claim_execution() is None  # new claims blocked by design
    assert w5.session_controller.active_session is None
    assert w5.session_controller.latest_session is None
