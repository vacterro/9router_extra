"""
T-32 single-flight refresh & bounded fan-out — focused deterministic suite
(SRC-005:PERF-006).

Covers:
1. MANDATORY STRESS TEST: block provider discovery, trigger Refresh 20x:
   - maximum active logical refreshes = 1
   - executed logical passes <= 2 (current + at most one follow-up)
   - provider request concurrency <= configured bound (max_workers=4)
   - no thread-per-click growth
   - only newest eligible snapshot publishes
   - GUI thread remains responsive
2. Lock during active refresh: no later authenticated request starts; the
   pass cannot publish authoritative EMPTY evidence.
3. Close during active refresh: pending follow-up discarded, late workers
   cannot publish, shutdown stays bounded (no GUI hang).
4. Failure releases single-flight state: Refresh stays usable.
5. Follow-up uses the newest requested generation.
6. Pending follow-up disappears on Close.
7. Controller-level unit proofs of the coalescing contract.
"""
import threading
import time

import pytest
from PySide6.QtWidgets import QApplication

from core.classification import CatalogState
from core.discovery import DiscoveredModel, ModelDiscovery
from core.refresh_controller import (
    REFRESH_CLOSED,
    REFRESH_COALESCED,
    REFRESH_STARTED,
    DiscoverySnapshot,
    RefreshController,
)
from ui.main_window import MainWindow
from ui.theme import apply_theme

# Captured at import time before conftest stubs can replace it
_REAL_REFRESH_ALL_ASYNC = MainWindow.refresh_all_async

PROVIDER_BOUND = 4  # configured max_workers in discovery fan-out


class _ScriptedSecurity:
    """Deterministic SecurityManager double; supports live Lock transitions."""

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

    def lock(self):
        self._set_state("LOCKED")

    def _set_state(self, s):
        if s != self._state:
            self._state = s
            for cb in list(self._listeners):
                try:
                    cb(s)
                except Exception:
                    pass


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    apply_theme(app)
    return app


@pytest.fixture(autouse=True)
def _restore_real_refresh_all_async(monkeypatch):
    monkeypatch.setattr(MainWindow, "refresh_all_async", _REAL_REFRESH_ALL_ASYNC)


@pytest.fixture(autouse=True)
def _scripted_unlocked_security(monkeypatch):
    monkeypatch.setattr(
        "ui.main_window.get_default_security", lambda: _ScriptedSecurity("UNLOCKED")
    )


def _make_model(cid: str) -> DiscoveredModel:
    prefix = cid.split("/")[0] if "/" in cid else ""
    mid = cid.split("/")[1] if "/" in cid else cid
    return DiscoveredModel(
        canonical_id=cid,
        provider_name=prefix,
        provider_prefix=prefix,
        connection_id="conn1",
        model_id=mid,
        display_name=mid,
        configured=True,
    )


def _wait_for_condition(predicate, timeout: float = 5.0, step: float = 0.02) -> bool:
    deadline = time.time() + timeout
    app = QApplication.instance()
    while time.time() < deadline:
        if app:
            app.processEvents()
        if predicate():
            return True
        time.sleep(step)
    if app:
        app.processEvents()
    return predicate()


# ===================================================================
# 1. MANDATORY STRESS TEST — 20 clicks during blocked discovery
# ===================================================================
def test_stress_refresh_20_clicks_single_flight(qapp):
    window = MainWindow()
    try:
        pass_started = threading.Event()
        release_pass = threading.Event()
        executed = {"passes": 0}
        pass_lock = threading.Lock()

        def blocked_discover(*args, **kwargs):
            with pass_lock:
                executed["passes"] += 1
            if not pass_started.is_set():
                pass_started.set()
                release_pass.wait(timeout=10.0)
            return [_make_model("prov/m1")]

        window.discovery.build_snapshot = blocked_discover
        window.client.get_combos = lambda: []

        # Click 1: starts the only active pass. G1 is BLOCKED and must never
        # publish once generation 20 has been requested (supersession).
        gen1 = window.refresh_all_async()
        assert gen1 == 1
        assert pass_started.wait(timeout=5.0)
        assert window._refresh_controller.running
        assert window._refresh_controller.active_generation == 1

        # Clicks 2..20 while pass 1 is blocked: every one coalesces and the
        # reserved follow-up advances to the newest requested generation.
        coalesced = 0
        for expected_gen in range(2, 21):
            gen = window.refresh_all_async()
            assert gen == expected_gen  # newest reserved follow-up generation
            coalesced += 1
            assert window._refresh_controller.active_generation == 1
        assert coalesced == 19
        assert window._refresh_controller.pending_follow_up == 20

        # Maximum active logical refreshes is structurally 1.
        assert window._refresh_controller.max_active_logical_refreshes <= 1
        assert window._refresh_diagnostics["threads_spawned"] == 1

        # GUI thread stays responsive while the pass is blocked.
        heartbeat = {"ok": False}
        QTimer_guard = threading.Timer(0.05, lambda: None)
        QTimer_guard.start()
        QTimer_guard.join()
        t0 = time.perf_counter()
        qapp.processEvents()
        heartbeat["ok"] = True
        elapsed = time.perf_counter() - t0
        assert heartbeat["ok"] and elapsed < 0.5

        # Release pass 1: it publishes, then at most ONE follow-up runs.
        release_pass.set()

        assert _wait_for_condition(
            lambda: window._refresh_controller.last_published_generation >= 1
            and not window._refresh_controller.running
        )

        # Executed logical passes <= 2 (current + at most one follow-up).
        assert executed["passes"] <= 2
        # No thread-per-click growth: at most 2 threads for 20 clicks.
        assert window._refresh_diagnostics["threads_spawned"] <= 2
        # Provider request concurrency stayed within the configured bound.
        assert (
            window._refresh_diagnostics["max_provider_requests_in_flight"]
            <= PROVIDER_BOUND
        )
        # STRESS PUBLICATION CONTRACT (strengthened): G1 must not publish
        # after generation 20 was requested — the first publication the
        # controller accepts is the follow-up (generation 20), never G1.
        assert window._refresh_controller.last_published_generation == 20
        assert window._refresh_controller.latest_requested_generation == 20
        assert window._refresh_controller.max_active_logical_refreshes <= 1
        # Controller ends idle: no phantom active generation remains.
        assert not window._refresh_controller.running
        assert window._refresh_controller.active_generation is None
        assert window._refresh_controller.pending_follow_up is None
        # Future Refresh remains usable.
        gen_after = window.refresh_all_async()
        assert gen_after == 21
        assert _wait_for_condition(
            lambda: window._refresh_controller.last_published_generation == 21
            and not window._refresh_controller.running
        )
    finally:
        release_pass.set()
        window.close()


# ===================================================================
# 2. LOCK DURING ACTIVE REFRESH
# ===================================================================
def test_lock_during_active_refresh_no_authenticated_request_no_empty(qapp, tmp_path):
    window = MainWindow()
    try:
        from core.security import LiveAccessLockedError

        scripted = _ScriptedSecurity("UNLOCKED")
        # Re-route the window's security to the scripted double.
        window.security = scripted
        requests_after_lock = {"n": 0}
        gate = threading.Event()
        lock_fired = threading.Event()

        client = window.discovery.client
        client.get_providers = lambda: [
            {"id": "conn-1", "name": "P1", "provider": "prov1", "isActive": True,
             "providerSpecificData": {"prefix": "p1"}},
        ]
        client.get_provider_nodes = lambda: []
        client.get_kv_scoped = lambda: [("customModels", "p1", '["m1"]')]
        client.get_catalog_models = lambda: []
        client.get_combos = lambda: []

        def live(cid):
            # Lock fires at the request boundary of the in-flight pass.
            scripted.lock()
            lock_fired.set()
            if scripted.is_locked():
                raise LiveAccessLockedError("locked at request boundary")
            requests_after_lock["n"] += 1
            return ("OK", [])

        client.get_connection_live_models_detailed = live

        gen = window.refresh_all_async()
        assert gen == 1
        assert lock_fired.wait(timeout=5.0)

        assert _wait_for_condition(
            lambda: window._refresh_controller.last_published_generation >= 1
        )

        # The pass published LOCKED evidence, never authoritative EMPTY.
        assert window.discovery.live_outcomes.get("conn-1") == "LOCKED"
        assert window.discovery.catalog_states.get("conn-1") in (
            CatalogState.DISCOVERY_UNAVAILABLE,
        )
        assert "conn-1" not in window.discovery.routing_excluded_connections
        # No later authenticated request started after the lock.
        assert requests_after_lock["n"] == 0
    finally:
        window.close()


# ===================================================================
# 3. CLOSE DURING ACTIVE REFRESH
# ===================================================================
def test_close_during_active_refresh_discards_follow_up_and_blocks_publish(qapp):
    window = MainWindow()
    try:
        gate = threading.Event()
        executed = {"passes": 0}

        def blocked_discover(*args, **kwargs):
            executed["passes"] += 1
            gate.wait(timeout=5.0)
            return [_make_model("prov/m1")]

        window.discovery.build_snapshot = blocked_discover
        window.client.get_combos = lambda: []

        gen1 = window.refresh_all_async()
        assert gen1 == 1
        deadline = time.time() + 3
        while executed["passes"] < 1 and time.time() < deadline:
            time.sleep(0.01)

        # A click during the pass reserves a follow-up…
        gen2 = window.refresh_all_async()
        assert gen2 == 2
        assert window._refresh_controller.pending_follow_up == 2

        # …then the application closes: follow-up discarded, publication blocked.
        window.close()
        assert window._refresh_controller.closing
        assert window._refresh_controller.pending_follow_up is None

        # A late Refresh after close never starts anything.
        gen3 = window.refresh_all_async()
        assert gen3 == 0

        # The blocked pass finishes; its late publication must be REJECTED.
        gate.set()
        assert _wait_for_condition(lambda: not window._refresh_controller.running)
        time.sleep(0.15)
        for _ in range(10):
            qapp.processEvents()
        assert executed["passes"] == 1  # no follow-up thread was spawned
        assert window._refresh_controller.last_published_generation == 0
    finally:
        window.close()


# ===================================================================
# 4. FAILURE RELEASES SINGLE-FLIGHT STATE
# ===================================================================
def test_failure_releases_single_flight_state(qapp):
    window = MainWindow()
    try:
        attempts = {"n": 0}

        def failing_discover(*args, **kwargs):
            attempts["n"] += 1
            raise RuntimeError("provider exploded")

        window.discovery.build_snapshot = failing_discover
        window.client.get_combos = lambda: []

        gen1 = window.refresh_all_async()
        assert gen1 == 1
        assert _wait_for_condition(
            lambda: "provider exploded" in window.status_bar.currentMessage()
        )

        # The failure released the running state…
        assert not window._refresh_controller.running
        # …and Refresh remains usable (a follow-up pass was spawned by the
        # finishing pass; it will also fail, but the state machine recovers).
        assert attempts["n"] >= 1
        assert _wait_for_condition(lambda: not window._refresh_controller.running)
        gen2 = window.refresh_all_async()
        assert gen2 >= 1
        assert window._refresh_controller.running
        # Clean up the spawned pass.
        window._refresh_controller.close()
        window._refresh_controller.finish_pass(window._refresh_controller.active_generation or 0)
        assert not window._refresh_controller.running
    finally:
        window.close()


# ===================================================================
# 4b. MANDATORY RACE (Defect C): G1 superseded by requested G2 must never
#     publish — restored original T-9 semantics
# ===================================================================
def test_superseded_g1_never_publishes_after_g2_requested(qapp):
    """G1 starts and blocks. The user requests G2 (coalesced follow-up).
    G1 then completes. G1 is obsolete user intent the moment G2 was
    requested: it must never change visible models, visible combos,
    accepted compatibility discovery state, or status for G2 intent. Only
    G2 becomes authoritative."""
    window = MainWindow()
    try:
        g1_started = threading.Event()
        g1_proceed = threading.Event()
        g2_started = threading.Event()
        call_lock = threading.Lock()
        calls = {"discover": 0, "combos": 0}

        old_models = [_make_model("prov/g1-old")]
        old_combos = [{"id": "g1_old_combo", "name": "G1 Old", "models": ["prov/g1-old"]}]
        new_models = [_make_model("prov/g2-new")]
        new_combos = [{"id": "g2_new_combo", "name": "G2 New", "models": ["prov/g2-new"]}]

        def mock_discover_all(*args, **kwargs):
            with call_lock:
                calls["discover"] += 1
                n = calls["discover"]
            if n == 1:
                g1_started.set()
                g1_proceed.wait(timeout=5.0)
                return old_models
            g2_started.set()
            return new_models

        def mock_get_combos():
            with call_lock:
                calls["combos"] += 1
                n = calls["combos"]
            return old_combos if n == 1 else new_combos

        window.discovery.build_snapshot = mock_discover_all
        window.client.get_combos = mock_get_combos

        # 1. G1 starts and blocks.
        gen1 = window.refresh_all_async()
        assert gen1 == 1
        assert g1_started.wait(timeout=3.0)

        # 2. G2 is requested (coalesced). From THIS moment G1 is obsolete.
        gen2 = window.refresh_all_async()
        assert gen2 == 2
        assert window._refresh_controller.pending_follow_up == 2
        assert window._refresh_controller.latest_requested_generation == 2

        # 3. G1 completes.
        g1_proceed.set()

        # 4. G2 runs and completes; only G2 becomes authoritative.
        assert _wait_for_condition(
            lambda: [m.canonical_id for m in window.discovered_models] == ["prov/g2-new"]
            and "g2_new_combo" in window.combo_editor.combos
        )

        # G1 NEVER changed visible models.
        assert [m.canonical_id for m in window.discovered_models] == ["prov/g2-new"]
        # G1 NEVER changed visible combos.
        assert "g1_old_combo" not in window.combo_editor.combos
        assert "g2_new_combo" in window.combo_editor.combos
        # G1 NEVER changed accepted compatibility discovery state.
        assert [c["id"] for c in window.discovery.combos] == ["g2_new_combo"]
        # Only G2 was ever accepted.
        assert window._refresh_controller.last_published_generation == 2
        # Controller ends idle and usable.
        assert not window._refresh_controller.running
        assert window._refresh_controller.active_generation is None
        # Exactly two passes, exactly two combo captures.
        assert calls["discover"] == 2
        assert calls["combos"] == 2
    finally:
        window.close()


def test_superseded_g1_failure_cannot_overwrite_g2_status(qapp):
    """Same supersession rule applied to FAILURE publication: once G2 is
    requested, G1's late failure must not overwrite the status/intent for
    G2."""
    window = MainWindow()
    try:
        g1_started = threading.Event()
        g1_proceed = threading.Event()
        calls = {"n": 0}

        def mock_discover_all(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                g1_started.set()
                g1_proceed.wait(timeout=5.0)
                raise RuntimeError("G1 late failure")
            return [_make_model("prov/g2-ok")]

        window.discovery.build_snapshot = mock_discover_all
        window.client.get_combos = lambda: []

        gen1 = window.refresh_all_async()
        assert gen1 == 1
        assert g1_started.wait(timeout=3.0)

        gen2 = window.refresh_all_async()
        assert gen2 == 2

        # G1 fails after G2 was requested.
        g1_proceed.set()

        # G2 completes successfully.
        assert _wait_for_condition(
            lambda: [m.canonical_id for m in window.discovered_models] == ["prov/g2-ok"]
        )
        time.sleep(0.15)
        for _ in range(10):
            qapp.processEvents()

        # G1's superseded failure never became the status for G2 intent.
        assert "G1 late failure" not in window.status_bar.currentMessage()
        assert window._refresh_controller.last_published_generation == 2
        assert [m.canonical_id for m in window.discovered_models] == ["prov/g2-ok"]
    finally:
        window.close()


# ===================================================================
# 4c. MANDATORY (Defect F): synchronous owner cannot create a phantom
#     follow-up; coalesced follow-up executes exactly once
# ===================================================================
def test_sync_owner_follow_up_executes_exactly_once_no_phantom(qapp):
    """refresh_all() runs as the active owner in a controlled worker thread.
    While it is blocked, refresh_all_async() coalesces a follow-up. When the
    synchronous owner finishes, the coalesced follow-up must execute exactly
    once, the controller must reach running=False, no phantom active
    generation may remain, and future Refresh stays usable."""
    window = MainWindow()
    try:
        sync_started = threading.Event()
        sync_release = threading.Event()
        calls = {"n": 0}
        lock = threading.Lock()

        def blocked_discover(*args, **kwargs):
            with lock:
                calls["n"] += 1
                n = calls["n"]
            if n == 1:
                sync_started.set()
                sync_release.wait(timeout=5.0)
            return [_make_model(f"prov/sync-m{n}")]

        window.discovery.build_snapshot = blocked_discover
        window.client.get_combos = lambda: []

        # The synchronous refresh_all() runs in a controlled worker thread.
        sync_thread = threading.Thread(target=window.refresh_all, daemon=True)
        sync_thread.start()
        assert sync_started.wait(timeout=3.0)
        assert window._refresh_controller.running

        # A coalesced request arrives while the SYNC owner holds the pass.
        gen2 = window.refresh_all_async()
        assert gen2 == 2
        assert window._refresh_controller.pending_follow_up == 2

        # Release the synchronous owner.
        sync_release.set()
        sync_thread.join(timeout=5.0)
        assert not sync_thread.is_alive()

        # The coalesced follow-up executes EXACTLY once.
        assert _wait_for_condition(
            lambda: calls["n"] == 2
            and not window._refresh_controller.running
        )
        time.sleep(0.15)
        for _ in range(10):
            qapp.processEvents()
        assert calls["n"] == 2
        # No phantom active generation remains.
        assert not window._refresh_controller.running
        assert window._refresh_controller.active_generation is None
        assert window._refresh_controller.pending_follow_up is None
        assert window._refresh_controller.last_published_generation == 2
        # Future Refresh remains usable.
        gen3 = window.refresh_all_async()
        assert gen3 == 3
        assert _wait_for_condition(
            lambda: window._refresh_controller.last_published_generation == 3
            and not window._refresh_controller.running
        )
        assert calls["n"] == 3
    finally:
        window.close()


def test_sync_owner_authorization_refusal_leaves_no_phantom(qapp, monkeypatch):
    """A synchronous owner refused at the authorization boundary releases
    its reservation without spawning anything: running=False, no active
    generation, no pending follow-up."""
    window = MainWindow()
    try:
        scripted = _ScriptedSecurity("LOCKED")
        window.security = scripted
        calls = {"n": 0}

        def counting_discover(*args, **kwargs):
            calls["n"] += 1
            return [_make_model("prov/never")]

        window.discovery.build_snapshot = counting_discover
        window.client.get_combos = lambda: []

        window.refresh_all()
        assert calls["n"] == 0
        assert not window._refresh_controller.running
        assert window._refresh_controller.active_generation is None
        assert window._refresh_controller.pending_follow_up is None
    finally:
        window.close()


# ===================================================================
# 5. FOLLOW-UP USES THE NEWEST REQUESTED GENERATION
# ===================================================================
def test_follow_up_represents_newest_request(qapp):
    window = MainWindow()
    try:
        gate = threading.Event()

        def blocked_discover(*args, **kwargs):
            gate.wait(timeout=5.0)
            return [_make_model("prov/m1")]

        window.discovery.build_snapshot = blocked_discover
        window.client.get_combos = lambda: []

        gen1 = window.refresh_all_async()
        assert gen1 == 1
        # Rapid clicks: generations advance, but only the newest is reserved.
        g2 = window.refresh_all_async()
        g3 = window.refresh_all_async()
        g4 = window.refresh_all_async()
        assert (g2, g3, g4) == (2, 3, 4)
        assert window._refresh_controller.pending_follow_up == 4

        gate.set()
        assert _wait_for_condition(
            lambda: window._refresh_controller.last_published_generation >= 4
        )
    finally:
        window.close()


# ===================================================================
# 6. PENDING FOLLOW-UP DISAPPEARS ON CLOSE
# ===================================================================
def test_pending_follow_up_discarded_on_close():
    controller = RefreshController(lambda: 42)
    controller._running = True
    controller._active_generation = 1
    controller._pending_generation = 7
    controller.close()
    assert controller.pending_follow_up is None
    assert controller.closing
    # No follow-up can ever be handed out after close.
    assert controller.finish_pass(1) is None
    assert controller.begin_refresh() == (REFRESH_CLOSED, 0)


# ===================================================================
# 7. CONTROLLER-LEVEL UNIT PROOFS
# ===================================================================
def test_controller_coalescing_contract():
    counter = {"n": 0}

    def next_gen():
        counter["n"] += 1
        return counter["n"]

    c = RefreshController(next_gen)

    outcome, g1 = c.begin_refresh()
    assert (outcome, g1) == (REFRESH_STARTED, 1)
    assert c.running and c.active_generation == 1

    for expected in (2, 3, 4, 5):
        outcome, g = c.begin_refresh()
        assert outcome == REFRESH_COALESCED
        assert g == expected  # each click re-reserves the newest follow-up gen
    assert c.pending_follow_up == 5  # only ONE follow-up reserved: the newest

    # Finish without follow-up: running released.
    assert c.finish_pass(1, spawn_follow_up=False) is None
    assert not c.running

    # Next begin starts a fresh pass (generations 2..5 were reserved by the
    # coalesced clicks, so the fresh pass takes the next one).
    outcome, g = c.begin_refresh()
    assert (outcome, g) == (REFRESH_STARTED, 6)
    c.finish_pass(3)
    assert not c.running


def test_controller_stale_publication_rejection():
    c = RefreshController(lambda: 1)
    assert c.may_publish(1)
    c.record_published(2)
    assert not c.may_publish(1)   # stale success rejected
    assert not c.may_publish(2)   # duplicate rejected
    assert c.may_publish(3)       # newer accepted
    c.close()
    assert not c.may_publish(99)  # nothing publishes after close
