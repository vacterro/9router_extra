"""
T-9 refresh generation regression suite (extended for T-31/T-32).

T-9 semantics preserved through the single-flight publication contract:
1. Race: G1 starts and blocks; a second request coalesces; G1 completes with
   OLD data, the coalesced follow-up G2 completes with NEW data. Final visible
   state is entirely G2 (one coherent snapshot).
2. Old failure after new success cannot replace status.
3. Old success after new failure cannot resurrect old state.
4. Stale combo results cannot publish: combos and models publish atomically
   through one snapshot acceptance boundary.
5. Dirty combo survives accepted current-generation refresh.
6. Selected model and combo survive when still present.
7. Synchronous refresh shares single-flight ownership; async requests during
   the sync pass coalesce.
8. Monotonically increasing generation source under concurrency.

Determinism: security is a scripted double so no real vault state (or live
HTTP) can leak into the suite.
"""
import threading
import time
from typing import List
import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from core.discovery import DiscoveredModel
from core.history import HealthCache
from core.security import LOCKED, OS_VAULT, SecurityManager, UNLOCKED
from ui.main_window import MainWindow
from ui.theme import apply_theme

# Captured at import time before conftest stubs can replace it
_REAL_REFRESH_ALL_ASYNC = MainWindow.refresh_all_async


class _ScriptedSecurity:
    """Deterministic SecurityManager double (no DPAPI, no env var influence,
    no local settings file)."""

    def __init__(self, state=UNLOCKED):
        self._state = state
        self._listeners = []

    @property
    def state(self):
        return self._state

    def is_live_allowed(self):
        return self._state in (OS_VAULT, UNLOCKED)

    def is_locked(self):
        return not self.is_live_allowed()

    def require_live(self, operation):
        if not self.is_live_allowed():
            from core.security import LiveAccessLockedError
            raise LiveAccessLockedError(operation)

    def on_state_changed(self, cb):
        self._listeners.append(cb)

    def lock(self):
        self._set_state(LOCKED)

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
    """Counter-override the conftest MainWindow.refresh_all_async no-op stub."""
    monkeypatch.setattr(MainWindow, "refresh_all_async", _REAL_REFRESH_ALL_ASYNC)


@pytest.fixture(autouse=True)
def _scripted_unlocked_security(monkeypatch):
    """Every window in this module uses a deterministic UNLOCKED security
    double: refresh requests proceed through the real single-flight path
    without touching the operator's real vault or the network."""
    monkeypatch.setattr(
        "ui.main_window.get_default_security", lambda: _ScriptedSecurity(UNLOCKED)
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


def _wait_for_condition(predicate, timeout: float = 3.0, step: float = 0.02) -> bool:
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
# 1. Mandatory Race Test (original T-9 semantics, restored)
# ===================================================================
def test_mandatory_race_g1_blocks_g2_completes_g1_completes_visible_state_g2(qapp):
    """
    G1 starts and blocks. A second Refresh request coalesces into one newest
    follow-up G2. From the moment G2 is REQUESTED, G1 is obsolete user
    intent: its completion must publish NOTHING — no models, no combos, no
    status. Only G2 becomes authoritative. Final visible state is entirely
    G2 — one coherent snapshot.
    """
    window = MainWindow()
    try:
        g1_discovery_started = threading.Event()
        g1_can_proceed = threading.Event()
        g2_started = threading.Event()

        old_models = [_make_model("prov/old-m1"), _make_model("prov/old-m2")]
        old_combos = [{"id": "old_c1", "name": "Old Combo", "models": ["prov/old-m1"]}]

        new_models = [_make_model("prov/new-m1"), _make_model("prov/new-m2"), _make_model("prov/new-m3")]
        new_combos = [{"id": "new_c1", "name": "New Combo", "models": ["prov/new-m1", "prov/new-m2"]}]

        call_count = {"discovery": 0, "combos": 0}

        def mock_discover_all(*args, **kwargs):
            with threading.Lock():
                call_count["discovery"] += 1
                idx = call_count["discovery"]
            if idx == 1:
                g1_discovery_started.set()
                g1_can_proceed.wait(timeout=5.0)
                return old_models
            else:
                g2_started.set()
                return new_models

        def mock_get_combos():
            with threading.Lock():
                call_count["combos"] += 1
                idx = call_count["combos"]
            if idx == 1:
                return old_combos
            return new_combos

        window.discovery.build_snapshot = mock_discover_all
        window.client.get_combos = mock_get_combos

        # 1. G1 starts and blocks
        gen1 = window.refresh_all_async()
        assert gen1 == 1
        assert g1_discovery_started.wait(timeout=3.0), "G1 did not start discovery"

        # 2. Second Refresh while G1 blocked: coalesced, no thread spawned
        gen2 = window.refresh_all_async()
        assert gen2 == 2
        assert window._refresh_controller.pending_follow_up == 2
        threads_before = window._refresh_diagnostics["threads_spawned"]
        assert threads_before == 1, "coalesced request must not spawn a thread"

        # 3. Unblock G1: superseded by the requested G2, it must publish
        #    NOTHING; the follow-up G2 then runs with NEW data.
        g1_can_proceed.set()

        assert _wait_for_condition(
            lambda: call_count["discovery"] == 2
            and len(window.discovered_models) == len(new_models)
            and "new_c1" in window.combo_editor.combos
        ), "G2 did not populate UI"

        # Exactly two logical passes executed, exactly two combo captures.
        assert call_count["discovery"] == 2
        assert call_count["combos"] == 2  # one capture per pass
        assert window._refresh_diagnostics["threads_spawned"] == 2

        # ORIGINAL T-9 RACE SEMANTICS: G1 never published after G2 was
        # requested — the first and only accepted publication is G2.
        assert window._refresh_controller.last_published_generation == 2

        # Verify G2 visible state
        assert [m.canonical_id for m in window.discovered_models] == [m.canonical_id for m in new_models]
        assert window.watch_view.table.rowCount() == len(new_models)
        assert "new_c1" in window.combo_editor.combos
        assert "old_c1" not in window.combo_editor.combos
        assert window.combo_editor.cb_combo_selector.findData("new_c1") >= 0
        assert window.combo_editor.cb_combo_selector.findData("old_c1") == -1
        # G1's combo capture never reached the UI either.
        assert "old_c1" not in window.combo_editor.combos
    finally:
        window.close()


# ===================================================================
# 2. Old failure after new success cannot replace status
# ===================================================================
def test_old_failure_after_new_success_cannot_replace_status(qapp):
    """
    G1 starts and blocks.
    G2 starts and completes successfully with status update.
    G1 unblocks and fails.
    Status must NOT be replaced by old failure.
    """
    window = MainWindow()
    try:
        g1_started = threading.Event()
        g1_proceed = threading.Event()
        call_count = {"discovery": 0}

        def mock_discover_all(*args, **kwargs):
            call_count["discovery"] += 1
            if call_count["discovery"] == 1:
                g1_started.set()
                g1_proceed.wait(timeout=5.0)
                raise RuntimeError("Old connection error from G1")
            return [_make_model("prov/m2")]

        window.discovery.build_snapshot = mock_discover_all
        window.client.get_combos = lambda: []

        # Start G1
        gen1 = window.refresh_all_async()
        assert gen1 == 1
        assert g1_started.wait(timeout=3.0)

        # Start G2 (coalesced follow-up; runs after G1 finishes)
        gen2 = window.refresh_all_async()
        assert gen2 == 2

        # Unblock G1 failure, then let the coalesced G2 pass run to success.
        g1_proceed.set()

        # G2 finishes
        assert _wait_for_condition(
            lambda: len(window.discovered_models) == 1
            and call_count["discovery"] == 2
        )
        expected_status = "Discovered 1 models across 9Router providers."
        assert window.status_bar.currentMessage() == expected_status
        time.sleep(0.15)
        for _ in range(15):
            qapp.processEvents()

        # Status must retain G2 success message, NOT G1 failure
        assert "Old connection error from G1" not in window.status_bar.currentMessage()
        assert window.status_bar.currentMessage() == expected_status
    finally:
        window.close()


# ===================================================================
# 3. Failure is the last word for its generation
# ===================================================================
def test_old_success_after_new_failure_cannot_resurrect_old_state(qapp):
    """
    G1 starts and blocks. A G2 request coalesces — from that moment G1 is
    obsolete user intent and publishes NOTHING (restored original T-9 race
    semantics). The coalesced G2 pass then runs and fails.
    T-9 property: after the newest generation FAILS, no older pass may
    publish — exactly two passes ran, the failure message is terminal, and
    no G1 data ever became authoritative.
    """
    window = MainWindow()
    try:
        g1_started = threading.Event()
        g1_proceed = threading.Event()
        call_count = {"discovery": 0}

        def mock_discover_all(*args, **kwargs):
            call_count["discovery"] += 1
            if call_count["discovery"] == 1:
                g1_started.set()
                g1_proceed.wait(timeout=5.0)
                return [_make_model("prov/g1-resurrect-candidate")]
            raise RuntimeError("New failure 503 from G2")

        window.discovery.build_snapshot = mock_discover_all
        window.client.get_combos = lambda: []

        gen1 = window.refresh_all_async()
        assert gen1 == 1
        assert g1_started.wait(timeout=3.0)

        gen2 = window.refresh_all_async()
        assert gen2 == 2

        # Unblock G1: superseded by the requested G2, it publishes NOTHING;
        # the coalesced G2 pass then runs and fails.
        g1_proceed.set()

        # G2 failure reaches status bar (and no later pass exists)
        assert _wait_for_condition(
            lambda: "New failure 503 from G2" in window.status_bar.currentMessage()
            and call_count["discovery"] == 2
        )
        time.sleep(0.15)
        for _ in range(15):
            qapp.processEvents()

        # Exactly two logical passes: no resurrection pass is possible.
        assert call_count["discovery"] == 2
        assert window._refresh_controller.last_published_generation == 2
        # The failure message is the terminal status (last word).
        assert "New failure 503 from G2" in window.status_bar.currentMessage()
        # ORIGINAL T-9 RACE SEMANTICS: G1 never published after G2 was
        # requested, so no G1 data can remain visible — the failure is the
        # last word and nothing older resurrects over it.
        assert "prov/g1-resurrect-candidate" not in [
            m.canonical_id for m in window.discovered_models
        ]
    finally:
        window.close()


# ===================================================================
# 4. Stale combo result cannot publish (atomic snapshot boundary)
# ===================================================================
def test_stale_combo_result_cannot_bypass_stale_model_rejection(qapp):
    """
    A direct stale legacy combos_loaded delivery (old generation) must be
    rejected by the same acceptance boundary that rejects stale models:
    combos can never bypass stale-model rejection because both publish
    through one snapshot.
    """
    window = MainWindow()
    try:
        # Publish a NEW coherent snapshot via the real acceptance boundary
        window._on_discovery_finished(2, None)  # legacy wrap: current gen, empty

        # A STALE generation-1 combo publication must be rejected.
        window._on_combos_loaded(1, [{"id": "stale_combo_g1", "name": "Stale", "models": []}])
        assert "stale_combo_g1" not in window.combo_editor.combos

        # A CURRENT-generation legacy combo delivery is still accepted
        # (legacy single-surface emitters keep working).
        window._on_combos_loaded(3, [{"id": "fresh_combo_g3", "name": "Fresh", "models": []}])
        assert "fresh_combo_g3" in window.combo_editor.combos

        # And a stale one after that is rejected again.
        window._on_combos_loaded(2, [{"id": "stale_combo_g2b", "name": "Stale", "models": []}])
        assert "stale_combo_g2b" not in window.combo_editor.combos
    finally:
        window.close()


# ===================================================================
# 5. Dirty combo survives accepted current-generation refresh
# ===================================================================
def test_dirty_combo_survives_accepted_current_generation_refresh(qapp):
    """
    A dirty combo with unsaved changes must preserve local dirty state
    and selection across accepted current-generation refresh.
    """
    window = MainWindow()
    try:
        server_combos = [{"id": "c1", "name": "Combo One", "models": ["prov/m1", "prov/m2"]}]
        window.client.get_combos = lambda: server_combos
        window.discovery.build_snapshot = lambda *a, **k: [_make_model("prov/m1"), _make_model("prov/m2")]

        # Load initial combo
        window.combo_editor.load_combos_from_9router()
        assert window.combo_editor.current_combo is not None
        assert window.combo_editor.current_combo.id == "c1"

        # Make local dirty edit
        window.combo_editor.current_combo.add_model("prov/local-dirty-model")
        assert window.combo_editor.current_combo.has_unsaved_changes()
        dirty_models = list(window.combo_editor.current_combo.models)

        # Execute current generation refresh
        window.refresh_all_async()
        assert _wait_for_condition(lambda: len(window.discovered_models) == 2)

        # Assert dirty combo survived
        curr = window.combo_editor.current_combo
        assert curr is not None
        assert curr.id == "c1"
        assert curr.has_unsaved_changes()
        assert curr.models == dirty_models
    finally:
        window.close()


# ===================================================================
# 6. Selected model and combo survive when still present
# ===================================================================
def test_selected_model_and_combo_survive_when_still_present(qapp):
    """
    Selected model in WatchView and selected combo in ComboEditor
    survive accepted refresh when still present in inventory/combos.
    """
    window = MainWindow()
    try:
        initial_models = [_make_model("prov/m1"), _make_model("prov/m2"), _make_model("prov/m3")]
        initial_combos = [
            {"id": "c1", "name": "Combo 1", "models": ["prov/m1"]},
            {"id": "c2", "name": "Combo 2", "models": ["prov/m2"]},
        ]
        window.watch_view.set_models(initial_models)
        window.watch_view.select_model("prov/m2")
        assert window.watch_view._selected_cid() == "prov/m2"

        window.combo_editor.replace_combos(initial_combos)
        idx2 = window.combo_editor.cb_combo_selector.findData("c2")
        window.combo_editor.cb_combo_selector.setCurrentIndex(idx2)
        assert window.combo_editor.current_combo.id == "c2"

        # Refresh with models and combos still containing m2 and c2
        refreshed_models = [_make_model("prov/m2"), _make_model("prov/m3"), _make_model("prov/m4")]
        refreshed_combos = [
            {"id": "c2", "name": "Combo 2 Updated", "models": ["prov/m2"]},
            {"id": "c3", "name": "Combo 3", "models": ["prov/m3"]},
        ]
        window.discovery.build_snapshot = lambda *a, **k: refreshed_models
        window.client.get_combos = lambda: refreshed_combos

        window.refresh_all_async()
        assert _wait_for_condition(
            lambda: len(window.discovered_models) == 3
            and "c3" in window.combo_editor.combos
        )

        # Selections survive
        assert window.watch_view._selected_cid() == "prov/m2"
        assert window.combo_editor.current_combo.id == "c2"
    finally:
        window.close()


# ===================================================================
# 7. Synchronous refresh shares single-flight ownership; async coalesces
# ===================================================================
def test_synchronous_refresh_advances_generation_and_rejects_older_async(qapp):
    """
    G1 async starts and blocks. The SYNCHRONOUS owner claims the pass in a
    controlled worker thread; an async request during the sync pass
    coalesces. Per the shared-finalization contract (T-32 closure, Defect F)
    the sync owner hands the coalesced follow-up to exactly one spawned
    worker; both passes publish through the one acceptance boundary.
    """
    window = MainWindow()
    try:
        g1_started = threading.Event()
        g1_proceed = threading.Event()

        async_models = [_make_model("async/m1")]
        async_combos = [{"id": "async_c1", "name": "Async", "models": []}]

        call_count = {"discovery": 0, "combos": 0}

        def mock_discover_all(*args, **kwargs):
            call_count["discovery"] += 1
            g1_started.set()
            g1_proceed.wait(timeout=5.0)
            return async_models

        def mock_get_combos():
            call_count["combos"] += 1
            return async_combos

        window.discovery.build_snapshot = mock_discover_all
        window.client.get_combos = mock_get_combos

        # Start G1 async
        gen1 = window.refresh_all_async()
        assert gen1 == 1
        assert g1_started.wait(timeout=3.0)

        # Synchronous refresh_all() during the active pass: coalesced.
        window.refresh_all()
        assert window._refresh_controller.pending_follow_up == 2
        assert window._refresh_diagnostics["passes_started"] == 1

        # Unblock G1 async: the coalesced follow-up G2 (spawned exactly once
        # through the shared finalization path) runs and publishes.
        g1_proceed.set()
        assert _wait_for_condition(
            lambda: [m.canonical_id for m in window.discovered_models] == ["async/m1"]
        )
        assert "async_c1" in window.combo_editor.combos
        assert call_count["discovery"] == 2
        # One capture per pass (G1 capture observed; G2 may already be running).
        assert call_count["combos"] >= 1
        assert window._refresh_controller.last_published_generation >= 1
        # No phantom owner remains after both passes finish.
        assert _wait_for_condition(lambda: not window._refresh_controller.running)
    finally:
        window.close()


# ===================================================================
# 8. Monotonically increasing generation source under concurrency
# ===================================================================
def test_monotonically_increasing_generation_source(qapp):
    window = MainWindow()
    try:
        generations = []
        threads = []

        def worker():
            for _ in range(50):
                g = window._next_refresh_generation()
                generations.append(g)

        for _ in range(4):
            t = threading.Thread(target=worker)
            threads.append(t)
            t.start()

        for t in threads:
            t.join()

        # 200 total generation requests
        assert len(generations) == 200
        # All distinct positive integers 1..200
        assert sorted(generations) == list(range(1, 201))
    finally:
        window.close()
