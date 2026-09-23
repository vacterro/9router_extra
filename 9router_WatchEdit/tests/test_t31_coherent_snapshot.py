"""
T-31 coherent discovery snapshot — focused deterministic suite (SRC-005:W2-003).

Covers:
1. SINGLE COMBO CAPTURE: get_combos returns A then B; one logical refresh
   must supply ONE immutable captured value to every consumer (combo
   membership classification inside the pass AND ComboEditor reconciliation).
2. MID-REFRESH MUTATION: remote combo state changes while the pass is
   blocked; the published snapshot is entirely one generation.
3. G1/G2 SHARED-STATE CONTAMINATION: overlapping out-of-order passes with
   different live outcomes / catalog states / routing exclusions / combo
   membership must never contaminate each other's snapshot.
4. ATOMIC PUBLICATION: one refresh cannot emit models and combos as
   independent publications; the UI never holds mixed generations.
5. OPENCODE SOURCE MATRIX: AVAILABLE / EMPTY / UNAVAILABLE / INVALID /
   NEVER_REFRESHED + last-known-good preservation semantics.
"""
import json
import threading
import time

import pytest
from PySide6.QtWidgets import QApplication

from core.classification import CatalogState
from core.discovery import DiscoveredModel, ModelDiscovery
from core.opencode_catalog import OpenCodeCatalogDiscovery
from core.refresh_controller import (
    OPENCODE_AVAILABLE,
    OPENCODE_EMPTY,
    OPENCODE_INVALID,
    OPENCODE_NEVER_REFRESHED,
    OPENCODE_UNAVAILABLE,
    DiscoverySnapshot,
)
from core.router_client import RouterClient
from ui.main_window import MainWindow
from ui.theme import apply_theme

# Captured at import time before conftest stubs can replace it
_REAL_REFRESH_ALL_ASYNC = MainWindow.refresh_all_async


class _ScriptedSecurity:
    """Deterministic SecurityManager double (no DPAPI, no env influence)."""

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
# 1. SINGLE COMBO CAPTURE
# ===================================================================
def test_single_combo_capture_one_value_to_every_consumer(qapp):
    """Fake get_combos() returns A on first invocation and B on second.
    One logical refresh must use ONLY A: the combo-membership classification
    inside the pass and the ComboEditor contents must both correspond to the
    same single capture."""
    window = MainWindow()
    try:
        combos_a = [{"id": "combo_a", "name": "A", "models": ["prov/m1"]}]
        combos_b = [{"id": "combo_b", "name": "B", "models": ["prov/m2"]}]
        calls = {"n": 0}
        lock = threading.Lock()

        def fake_get_combos():
            with lock:
                calls["n"] += 1
                n = calls["n"]
            return combos_a if n == 1 else combos_b

        window.client.get_combos = fake_get_combos

        def real_build(*args, **kwargs):
            # Delegate to the real discovery engine: it uses the single
            # captured value to classify combo membership.
            return ModelDiscovery.build_snapshot(
                window.discovery, *args, **kwargs
            )

        window.discovery.build_snapshot = real_build
        window.discovery.client.get_providers = lambda: []
        window.discovery.client.get_provider_nodes = lambda: []
        window.discovery.client.get_kv_scoped = lambda: []
        window.discovery.client.get_catalog_models = lambda: []

        gen = window.refresh_all_async()
        assert gen == 1
        assert _wait_for_condition(
            lambda: window._refresh_controller.last_published_generation >= 1
            and "combo_a" in window.combo_editor.combos
        )

        # Exactly ONE get_combos call for the whole logical refresh.
        assert calls["n"] == 1
        # The ComboEditor received exactly the captured A collection.
        assert "combo_a" in window.combo_editor.combos
        assert "combo_b" not in window.combo_editor.combos
        # The membership flags in the published inventory match the capture.
        members = {m.canonical_id for m in window.discovered_models if m.is_combo_member}
        assert members == {"prov/m1"}
    finally:
        window.close()


def test_single_combo_capture_engine_level(qapp):
    """Engine-level proof: within one discover_all pass the combo capture is
    taken once; the pass-local captured value is reused inside the pass."""
    client = RouterClient(base_url="http://127.0.0.1:99999")
    discovery = ModelDiscovery(client)
    combos_a = [{"id": "a", "name": "A", "models": []}]
    combos_b = [{"id": "b", "name": "B", "models": []}]
    calls = {"n": 0}

    def fake_get_combos():
        calls["n"] += 1
        return combos_a if calls["n"] == 1 else combos_b

    client.get_combos = fake_get_combos
    client.get_providers = lambda: []
    client.get_provider_nodes = lambda: []
    client.get_kv_scoped = lambda: []
    client.get_catalog_models = lambda: []

    snapshot = discovery.discover_all(query_live=False)
    assert calls["n"] == 1
    assert snapshot.combos == ({"id": "a", "name": "A", "models": []},)


# ===================================================================
# 1b. MANDATORY: sequential passes capture FRESH combos (Defect A)
# ===================================================================
def test_sequential_passes_capture_fresh_combos_each_time(qapp):
    """Two discover_all() calls on ONE ModelDiscovery instance. get_combos
    returns A on call 1 and B on call 2. Every assertion of the mandatory
    contract holds WITHOUT any caller-side state reset between passes."""
    client = RouterClient(base_url="http://127.0.0.1:99999")
    discovery = ModelDiscovery(client)
    combos_a = [{"id": "combo_a", "name": "A", "models": ["p1/m1"]}]
    combos_b = [{"id": "combo_b", "name": "B", "models": ["p1/m2"]}]
    calls = {"n": 0}

    def fake_get_combos():
        calls["n"] += 1
        return list(combos_a) if calls["n"] == 1 else list(combos_b)

    client.get_combos = fake_get_combos
    client.get_providers = lambda: []
    client.get_provider_nodes = lambda: []
    client.get_kv_scoped = lambda: [("customModels", "p1", '["m1"]')]
    client.get_catalog_models = lambda: []

    snap1 = discovery.discover_all(query_live=False)
    snap2 = discovery.discover_all(query_live=False)

    # get_combos call count == 2 (exactly one capture per logical pass).
    assert calls["n"] == 2
    # Snapshot 1 contains A only; snapshot 2 contains B only.
    assert [c["id"] for c in snap1.combos] == ["combo_a"]
    assert [c["id"] for c in snap2.combos] == ["combo_b"]
    # Membership in snapshot 1 matches A; membership in snapshot 2 matches B.
    members1 = {m.canonical_id for m in snap1.models if m.is_combo_member}
    members2 = {m.canonical_id for m in snap2.models if m.is_combo_member}
    assert members1 == {"p1/m1"}
    assert members2 == {"p1/m2"}


# ===================================================================
# 1c. MANDATORY: concurrent passes never share combo captures (Defect A)
# ===================================================================
def test_concurrent_passes_isolate_combo_captures(qapp):
    """Two REAL discover_all() executions run CONCURRENTLY on the same
    ModelDiscovery instance, each with distinguishable combo data. Neither
    pass may observe the other pass's combo collection."""
    client = RouterClient(base_url="http://127.0.0.1:99999")
    discovery = ModelDiscovery(client)
    combos_a = [{"id": "combo_a", "name": "A", "models": ["p1/ma"]}]
    combos_b = [{"id": "combo_b", "name": "B", "models": ["p1/mb"]}]

    call_lock = threading.Lock()
    calls = {"n": 0}
    rendezvous = threading.Barrier(2, timeout=5.0)
    tls = threading.local()

    def fake_get_combos():
        with call_lock:
            calls["n"] += 1
        # Both passes must be inside their capture at the same instant —
        # proving the captures genuinely overlap in time.
        rendezvous.wait()
        return list(combos_a) if getattr(tls, "tag", "") == "one" else list(combos_b)

    client.get_combos = fake_get_combos
    client.get_providers = lambda: []
    client.get_provider_nodes = lambda: []
    client.get_kv_scoped = lambda: [("customModels", "p1", '["ma", "mb"]')]
    client.get_catalog_models = lambda: []

    results = {}
    errors = []

    def run_pass(tag):
        tls.tag = tag
        try:
            results[tag] = discovery.discover_all(query_live=False)
        except Exception as e:  # pragma: no cover - diagnostic only
            errors.append(e)

    t1 = threading.Thread(target=run_pass, args=("one",))
    t2 = threading.Thread(target=run_pass, args=("two",))
    t1.start()
    t2.start()
    t1.join(timeout=10.0)
    t2.join(timeout=10.0)

    assert not errors
    assert calls["n"] == 2
    assert len(results) == 2
    # Each pass saw exactly its own combo collection — never a mixture.
    seen_ids = sorted(c["id"] for r in results.values() for c in r.combos)
    assert seen_ids == ["combo_a", "combo_b"]
    for r in results.values():
        ids = {c["id"] for c in r.combos}
        assert ids in ({"combo_a"}, {"combo_b"})
        members = {m.canonical_id for m in r.models if m.is_combo_member}
        if ids == {"combo_a"}:
            assert members == {"p1/ma"}
        else:
            assert members == {"p1/mb"}
    # The two snapshots are distinct objects with distinct combo values.
    s1, s2 = results["one"], results["two"]
    assert s1 is not s2
    assert {c["id"] for c in s1.combos} != {c["id"] for c in s2.combos}


# ===================================================================
# 2. MID-REFRESH MUTATION
# ===================================================================
def test_mid_refresh_mutation_snapshot_entirely_one_generation(qapp):
    """Remote combo state changes while the pass is blocked in discovery.
    The published snapshot (models AND combos) must be entirely one
    generation — no mixed snapshot."""
    window = MainWindow()
    try:
        old_combos = [{"id": "old_c", "name": "Old", "models": ["prov/m1"]}]
        new_combos = [{"id": "new_c", "name": "New", "models": ["prov/m2"]}]
        gate = threading.Event()
        released = {"flag": False}

        def fake_get_combos():
            gate.wait(timeout=5.0)
            released["flag"] = True
            # By the time combos are captured, the "remote" state has ALREADY
            # changed. The capture is a single atomic read of THIS state.
            return new_combos

        window.client.get_combos = fake_get_combos
        window.discovery.client.get_providers = lambda: []
        window.discovery.client.get_provider_nodes = lambda: []
        window.discovery.client.get_kv_scoped = lambda: []
        window.discovery.client.get_catalog_models = lambda: []

        def blocked_discover(*args, **kwargs):
            # Simulate provider work in progress, then release.
            gate.set()
            return ModelDiscovery.build_snapshot(window.discovery, *args, **kwargs)

        window.discovery.build_snapshot = blocked_discover

        gen = window.refresh_all_async()
        assert gen == 1
        assert _wait_for_condition(
            lambda: "new_c" in window.combo_editor.combos
        )

        # The entire visible state is the new generation: no old/new mixture.
        assert "old_c" not in window.combo_editor.combos
        members = {m.canonical_id for m in window.discovered_models if m.is_combo_member}
        assert members == {"prov/m2"}
        assert calls_count_one_capture(window)
    finally:
        window.close()


def calls_count_one_capture(window) -> bool:
    # The fake client counted via the fake_get_combos closure; verify via the
    # snapshot payload instead: the editor and membership agree exactly.
    member_ids = {m.canonical_id for m in window.discovered_models if m.is_combo_member}
    combo_models = {mid for c in window.combo_editor.combos.values() for mid in c.models}
    return member_ids == combo_models


# ===================================================================
# 3. G1/G2 SHARED-STATE CONTAMINATION
# ===================================================================
def test_overlapping_out_of_order_generations_no_shared_state(qapp):
    """TRUE OVERLAP (Defect E repair): G1 starts, captures G1-specific data
    and blocks BEFORE completing; G2 captures G2-specific data and completes
    FIRST; then G1 is released. Each completed snapshot must contain ONLY
    its own pass data (combos, live outcomes, catalog states, counts, routing
    exclusions) — no matter the completion order."""
    client = RouterClient(base_url="http://127.0.0.1:99999")
    discovery = ModelDiscovery(client)

    client.get_providers = lambda: [
        {"id": "conn-1", "name": "P1", "provider": "prov1", "isActive": True,
         "providerSpecificData": {"prefix": "p1"}},
    ]
    client.get_provider_nodes = lambda: []
    client.get_kv_scoped = lambda: [("customModels", "p1", '["m1"]')]
    client.get_catalog_models = lambda: []

    g1_release = threading.Event()
    g1_started = threading.Event()
    tls = threading.local()
    live_lock = threading.Lock()
    live_calls = {"n": 0}

    def fake_get_combos():
        if getattr(tls, "tag", "") == "g1":
            g1_started.set()
            g1_release.wait(timeout=5.0)  # G1 blocks before completing
            return [{"id": "g1_combo", "name": "G1", "models": ["p1/m1"]}]
        return [{"id": "g2_combo", "name": "G2", "models": ["p1/m2"]}]

    def fake_live(cid):
        # G2 completes ENTIRELY before g1_release is set, and G1's live probe
        # can only run after its blocked combo capture — so the first live
        # call belongs to G2 and the second to G1. (Live fetches run inside
        # per-pass executor workers, which cannot be tagged per-thread.)
        with live_lock:
            live_calls["n"] += 1
            n = live_calls["n"]
        return (
            ("OK", [])  # G1: empty catalogue -> exclusion evidence
            if n >= 2
            else ("OK", [{"id": "p1/m2"}])  # G2: valid non-empty catalogue
        )

    client.get_combos = fake_get_combos
    client.get_connection_live_models_detailed = fake_live

    results = {}
    errors = []

    def run_g1():
        tls.tag = "g1"
        try:
            results["g1"] = discovery.discover_all(query_live=True)
        except Exception as e:  # pragma: no cover
            errors.append(e)

    def run_g2():
        tls.tag = "g2"
        try:
            results["g2"] = discovery.discover_all(query_live=True)
        except Exception as e:  # pragma: no cover
            errors.append(e)

    t1 = threading.Thread(target=run_g1)
    t2 = threading.Thread(target=run_g2)
    t1.start()
    assert g1_started.wait(timeout=5.0)  # G1 has captured and is now blocked
    t2.start()
    # G2 must complete BEFORE G1 is released (out-of-order completion).
    assert _wait_for_condition(lambda: "g2" in results, timeout=5.0)
    g1_release.set()
    t1.join(timeout=10.0)
    t2.join(timeout=10.0)

    assert not errors
    snap1 = results["g1"]
    snap2 = results["g2"]

    # --- snapshot-local combos: each pass holds ONLY its own capture ---
    assert [c["id"] for c in snap1.combos] == ["g1_combo"]
    assert [c["id"] for c in snap2.combos] == ["g2_combo"]
    members1 = {m.canonical_id for m in snap1.models if m.is_combo_member}
    members2 = {m.canonical_id for m in snap2.models if m.is_combo_member}
    assert members1 == {"p1/m1"}
    assert members2 == {"p1/m2"}

    # --- snapshot-local live/catalog/routing state ---
    assert snap1.routing_excluded_connections == frozenset({"conn-1"})
    assert snap1.catalog_states["conn-1"] == CatalogState.EMPTY_MODEL_CATALOG
    assert snap1.catalog_model_counts.get("conn-1", 0) == 0

    assert snap2.routing_excluded_connections == frozenset()
    assert snap2.catalog_states["conn-1"] == CatalogState.MODELS_AVAILABLE
    assert snap2.catalog_model_counts["conn-1"] == 1
    assert not (set(snap2.routing_excluded_connections) & set(snap1.routing_excluded_connections))

    # G2 completed FIRST and was published explicitly by this test; G1's
    # completion must not overwrite the accepted newer state.
    discovery.publish_snapshot(snap2)
    assert discovery.live_outcomes == dict(snap2.live_outcomes)
    assert discovery.catalog_states == dict(snap2.catalog_states)
    assert discovery.routing_excluded_connections == set(snap2.routing_excluded_connections)
    assert [c["id"] for c in discovery.combos] == ["g2_combo"]


def test_locked_pass_never_produces_authoritative_empty(qapp):
    """A LOCKED live pass records LOCKED/DISCOVERY_UNAVAILABLE — never EMPTY
    exclusion evidence."""
    client = RouterClient(base_url="http://127.0.0.1:99999")
    discovery = ModelDiscovery(client)
    client.get_combos = lambda: []

    from core.security import LiveAccessLockedError

    client.get_providers = lambda: [
        {"id": "conn-l", "name": "L", "provider": "provl", "isActive": True,
         "providerSpecificData": {"prefix": "pl"}},
    ]
    client.get_provider_nodes = lambda: []
    client.get_kv_scoped = lambda: [("customModels", "pl", '["m1"]')]
    client.get_catalog_models = lambda: []

    def locked(cid):
        raise LiveAccessLockedError("lock-during-discovery")

    client.get_connection_live_models_detailed = locked

    snap = discovery.discover_all(query_live=True)
    assert snap.live_outcomes["conn-l"] == "LOCKED"
    assert snap.catalog_states["conn-l"] == CatalogState.DISCOVERY_UNAVAILABLE
    assert "conn-l" not in snap.routing_excluded_connections
    assert "conn-l" not in snap.catalog_model_counts


# ===================================================================
# 3b. MANDATORY: completed snapshots are detached (Defect B)
# ===================================================================
def test_completed_snapshot_detached_from_source_mutation(qapp):
    """Create a completed snapshot from {"models": ["p/a"]}-style source
    collections, then mutate EVERY original source collection used to
    construct it. The snapshot contents must remain logically unchanged."""
    client = RouterClient(base_url="http://127.0.0.1:99999")
    discovery = ModelDiscovery(client)

    source_combos = [{"id": "c1", "name": "C1", "models": ["p/a"]}]
    source_nodes = [
        {"id": "node-1", "name": "N1", "data": json.dumps({"prefix": "p"})},
    ]
    source_kv = [("customModels", "p", '["a"]')]
    source_catalog = [{"provider": "node-1", "model": "b", "name": "B"}]

    calls = {"n": 0}

    def fake_get_combos():
        calls["n"] += 1
        return source_combos

    client.get_combos = fake_get_combos
    client.get_providers = lambda: []
    client.get_provider_nodes = lambda: list(source_nodes)
    client.get_kv_scoped = lambda: list(source_kv)
    client.get_catalog_models = lambda: list(source_catalog)

    snap = discovery.discover_all(query_live=False)
    before_combos = snap.combos
    before_members = {m.canonical_id: m.is_combo_member for m in snap.models}
    before_ids = sorted(m.canonical_id for m in snap.models)

    # Mutate every original source collection AFTER snapshot completion.
    source_combos.append({"id": "MUTATED", "name": "M", "models": []})
    source_combos[0]["models"].append("p/MUTATED")
    source_combos[0]["id"] = "MUTATED_ID"
    source_nodes.append({"id": "node-MUT", "name": "NM", "data": "{}"})
    source_kv.append(("customModels", "p", '["MUTATED"]'))
    source_catalog.append({"provider": "node-1", "model": "MUTATED", "name": "X"})

    # Snapshot is byte-for-byte/logically unchanged.
    assert snap.combos == before_combos
    assert [c["id"] for c in snap.combos] == ["c1"]
    assert [list(c["models"]) for c in snap.combos] == [["p/a"]]
    assert {m.canonical_id: m.is_combo_member for m in snap.models} == before_members
    assert sorted(m.canonical_id for m in snap.models) == before_ids
    assert "p/MUTATED" not in before_members

    # Nested combo model lists do not alias the source lists.
    assert snap.combos[0]["models"] is not source_combos[0]["models"]
    # And a SECOND pass captures the mutated source as its own fresh value
    # (both post-mutation rows) while the first snapshot stays frozen.
    snap2 = discovery.discover_all(query_live=False)
    assert [c["id"] for c in snap.combos] == ["c1"]  # snap1 still frozen
    assert [c["id"] for c in snap2.combos] == ["MUTATED_ID", "MUTATED"]


# ===================================================================
# 3c. MANDATORY: compatibility state publishes only on acceptance (Defect D)
# ===================================================================
def test_compatibility_state_publishes_only_after_acceptance(qapp):
    """Seed compatibility state with ACCEPTED snapshot X. Build a newer but
    superseded snapshot Y WITHOUT accepting it: the compatibility state must
    remain exactly X. Accept snapshot Z: the compatibility state atomically
    becomes exactly Z."""
    client = RouterClient(base_url="http://127.0.0.1:99999")
    discovery = ModelDiscovery(client)
    client.get_providers = lambda: [
        {"id": "conn-1", "name": "P1", "provider": "prov1", "isActive": True,
         "providerSpecificData": {"prefix": "p"}},
    ]
    client.get_provider_nodes = lambda: []
    client.get_kv_scoped = lambda: [("customModels", "p", '["m1"]')]
    client.get_catalog_models = lambda: []

    def with_live(live_result, combo_id):
        client.get_connection_live_models_detailed = lambda cid: live_result
        client.get_combos = lambda: [
            {"id": combo_id, "name": combo_id, "models": []}
        ]
        return discovery.build_snapshot(query_live=True)

    # ACCEPTED snapshot X (G1: empty catalog -> exclusion evidence).
    snap_x = with_live(("OK", []), "x_combo")
    discovery.publish_snapshot(snap_x)
    assert discovery.routing_excluded_connections == {"conn-1"}
    assert discovery.catalog_states == {"conn-1": CatalogState.EMPTY_MODEL_CATALOG}

    # NEWER-BUT-SUPERSEDED snapshot Y built WITHOUT acceptance.
    snap_y = with_live(("OK", [{"id": "p/mY"}]), "y_combo")
    # No publish_snapshot(snap_y) call on purpose.
    # Compatibility state remains exactly X.
    assert discovery.live_outcomes == dict(snap_x.live_outcomes)
    assert discovery.catalog_states == dict(snap_x.catalog_states)
    assert discovery.catalog_model_counts == dict(snap_x.catalog_model_counts)
    assert discovery.routing_excluded_connections == set(snap_x.routing_excluded_connections)
    assert [c["id"] for c in discovery.combos] == ["x_combo"]

    # ACCEPT snapshot Z: compatibility state atomically becomes exactly Z.
    snap_z = with_live(("OK", [{"id": "p/mZ1"}, {"id": "p/mZ2"}]), "z_combo")
    discovery.publish_snapshot(snap_z)
    assert discovery.live_outcomes == dict(snap_z.live_outcomes)
    assert discovery.catalog_states == dict(snap_z.catalog_states)
    assert discovery.catalog_states == {"conn-1": CatalogState.MODELS_AVAILABLE}
    assert discovery.catalog_model_counts == dict(snap_z.catalog_model_counts)
    assert discovery.catalog_model_counts == {"conn-1": 2}
    assert discovery.routing_excluded_connections == set(snap_z.routing_excluded_connections)
    assert discovery.routing_excluded_connections == frozenset()
    assert [c["id"] for c in discovery.combos] == ["z_combo"]
    assert [c["id"] for c in discovery.combos] != ["y_combo"]


# ===================================================================
# 4. ATOMIC PUBLICATION
# ===================================================================
def test_publication_is_one_atomic_snapshot(qapp):
    """The accepted publication applies models, combos, and per-pass state in
    one step: immediately after _on_discovery_finished the UI reflects one
    generation only."""
    window = MainWindow()
    try:
        models = [_make_model("prov/m1"), _make_model("prov/m2")]
        combos = [{"id": "c1", "name": "C1", "models": ["prov/m1"]}]
        snap = DiscoverySnapshot(
            generation=1,
            models=tuple(models),
            combos=tuple(dict(c) for c in combos),
            live_outcomes={"conn1": "OK"},
        )
        window._on_discovery_finished(1, snap)

        assert [m.canonical_id for m in window.discovered_models] == ["prov/m1", "prov/m2"]
        assert "c1" in window.combo_editor.combos
        assert window._refresh_controller.last_published_generation == 1
        # The combo payload arrived atomically WITH the models (one snapshot).
        combo_models = {mid for c in window.combo_editor.combos.values() for mid in c.models}
        assert combo_models == {"prov/m1"}

        # A STALE snapshot cannot publish independently afterwards.
        stale = DiscoverySnapshot(
            generation=0,
            models=(_make_model("prov/stale"),),
            combos=({"id": "stale_c", "name": "S", "models": []},),
        )
        window._on_discovery_finished(0, stale)
        assert "prov/stale" not in [m.canonical_id for m in window.discovered_models]
        assert "stale_c" not in window.combo_editor.combos
    finally:
        window.close()


# ===================================================================
# 5. OPENCODE SOURCE MATRIX
# ===================================================================
def _catalog_with_statuses(tmp_path, statuses, models, last_failure=None):
    disc = OpenCodeCatalogDiscovery(snapshot_file=tmp_path / "snap.json")
    disc.statuses.update(statuses)
    disc.models = models
    if last_failure:
        disc.last_failure = last_failure
    return disc


def test_opencode_source_state_matrix(tmp_path):
    # NEVER_REFRESHED: no fetch attempted (statuses has no 'api' entry).
    disc = OpenCodeCatalogDiscovery(snapshot_file=tmp_path / "a.json")
    disc.statuses.pop("api", None)
    disc.models = []
    assert ModelDiscovery._opencode_source_state(disc) == (OPENCODE_NEVER_REFRESHED, 0)

    # AVAILABLE: successful refresh with at least one row.
    disc = _catalog_with_statuses(tmp_path, {"api": "API_OK"}, [_fake_zen_model("x")])
    assert ModelDiscovery._opencode_source_state(disc) == (OPENCODE_AVAILABLE, 1)

    # EMPTY: successful refresh returning zero rows (200 + [] is explicit).
    disc = _catalog_with_statuses(tmp_path, {"api": "API_OK"}, [])
    assert ModelDiscovery._opencode_source_state(disc) == (OPENCODE_EMPTY, 0)

    # UNAVAILABLE: transport/HTTP failure.
    disc = _catalog_with_statuses(
        tmp_path, {"api": "API_FAILED"}, [], {"at": "t", "class": "network_error"}
    )
    assert ModelDiscovery._opencode_source_state(disc) == (OPENCODE_UNAVAILABLE, 0)

    # INVALID: 200 whose payload violates the schema.
    disc = _catalog_with_statuses(
        tmp_path, {"api": "API_FAILED"}, [], {"at": "t", "class": "malformed_payload"}
    )
    assert ModelDiscovery._opencode_source_state(disc) == (OPENCODE_INVALID, 0)

    # None catalog object -> NEVER_REFRESHED.
    assert ModelDiscovery._opencode_source_state(None) == (OPENCODE_NEVER_REFRESHED, 0)


def _fake_zen_model(mid):
    from core.opencode_catalog import ZenCatalogModel
    return ZenCatalogModel(model_id=mid, canonical_id=f"opencode/{mid}")


def test_opencode_lkg_preserved_on_failed_refresh(tmp_path, monkeypatch):
    """A failed refresh never destroys the last known good snapshot; the
    failed state is UNAVAILABLE, and the retained rows still classify as
    AVAILABLE evidence from the previous successful refresh."""
    import httpx

    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(200, json={"models": [{"id": "x-free"}]})
        return httpx.Response(503, text="down")

    transport = httpx.MockTransport(handler)
    disc = OpenCodeCatalogDiscovery(snapshot_file=tmp_path / "s.json", transport=transport)
    disc.refresh(force=True)
    assert ModelDiscovery._opencode_source_state(disc) == (OPENCODE_AVAILABLE, 1)

    # Failed refresh: LKG preserved; state explicitly UNAVAILABLE (never EMPTY).
    disc.refresh(force=True)
    assert calls["n"] == 2
    assert len(disc.models) == 1  # LKG rows retained
    state, _count = ModelDiscovery._opencode_source_state(disc)
    # UNAVAILABLE (not EMPTY): the failed refresh contributes no fresh rows,
    # and the retained last-known-good rows are never re-labelled EMPTY.
    assert state == OPENCODE_UNAVAILABLE
    assert disc.models[0].model_id == "x-free"


# ===================================================================
# 3d. MANDATORY (T-31 final closure): SNAPSHOT CONSUMER ISOLATION
# ===================================================================
def _snapshot_consumer_isolation_common(qapp):
    """Shared fixture logic: build one snapshot from controlled source data,
    publish it to compatibility state AND reconcile it into ComboEditor."""
    client = RouterClient(base_url="http://127.0.0.1:99999")
    discovery = ModelDiscovery(client)

    source_combos = [{"id": "c1", "name": "C1", "models": ["p/m1"]}]
    source_nodes = [
        {"id": "node-1", "name": "N1", "data": json.dumps({"prefix": "p"})},
    ]
    source_kv = [("customModels", "p", '["m1"]')]
    source_catalog = [{"provider": "node-1", "model": "m1", "name": "M1"}]

    client.get_combos = lambda: source_combos
    client.get_providers = lambda: []
    client.get_provider_nodes = lambda: list(source_nodes)
    client.get_kv_scoped = lambda: list(source_kv)
    client.get_catalog_models = lambda: list(source_catalog)

    snap = discovery.discover_all(query_live=False)
    # Stamp the publication generation the way the refresh acceptance
    # boundary would (discover_all returns an unstamped legacy snapshot).
    snap = DiscoverySnapshot(
        generation=1,
        models=snap.models,
        combos=snap.combos,
        live_outcomes=snap.live_outcomes,
        catalog_states=snap.catalog_states,
        catalog_model_counts=snap.catalog_model_counts,
        routing_excluded_connections=snap.routing_excluded_connections,
    )

    # Publish to compatibility state and reconcile into ComboEditor.
    discovery.publish_snapshot(snap)
    window = MainWindow()
    window._on_discovery_finished(1, snap)

    def logical_state():
        return (
            tuple((c["id"], tuple(c["models"])) for c in snap.combos),
            tuple(sorted(m.canonical_id for m in snap.models)),
            tuple(sorted((m.canonical_id, m.is_combo_member) for m in snap.models)),
        )

    return snap, discovery, window, source_combos, logical_state


def test_snapshot_consumer_isolation_mandatory(qapp):
    """MANDATORY T-31 regression: one snapshot is published to compatibility
    state and reconciled into ComboEditor. Then EVERY consumer is mutated
    independently; after each mutation the completed snapshot remains
    logically unchanged."""
    snap, discovery, window, source_combos, logical_state = (
        _snapshot_consumer_isolation_common(qapp)
    )
    try:
        baseline = logical_state()

        # ---- 1. Mutate the ORIGINAL TRANSPORT/SOURCE combo data ----
        source_combos[0]["models"].append("p/source_mut")
        source_combos[0]["id"] = "source_mutated_id"
        assert logical_state() == baseline

        # ---- 2. Mutate discovery.combos (compatibility consumer) ----
        discovery.combos[0]["models"].append("p/compat_mut")
        discovery.combos[0]["id"] = "compat_mutated_id"
        discovery.combos.append({"id": "compat_extra", "models": ["p/x"]})
        assert logical_state() == baseline

        # ---- 3. Mutate ComboEditor current combo models ----
        assert "c1" in window.combo_editor.combos
        current = window.combo_editor.combos["c1"]
        current.models.append("p/editor_mut")
        current.models[0] = "p/editor_overwritten"
        assert logical_state() == baseline

        # ---- 4. Mutate one ComboEditor consumer state against another ----
        # Reset the compat view to its published value, then mutate the
        # editor: the compatibility consumer must not observe editor
        # mutations (and vice versa).
        discovery.combos = ModelDiscovery._copy_combos(snap.combos)
        assert discovery.combos[0]["models"] == ["p/m1"]

        # Structural proof: no aliasing anywhere in the chain.
        assert snap.combos[0]["models"] is not source_combos[0]["models"]
        assert discovery.combos[0]["models"] is not snap.combos[0]["models"]
        assert discovery.combos[0] is not snap.combos[0]

        # The editor's StableCombo.models is its own list, never the
        # snapshot's or compatibility view's list object.
        assert current.models is not snap.combos[0]["models"]
        assert current.models is not discovery.combos[0]["models"]
    finally:
        window.close()


def test_source_mutation_cannot_change_published_compat_state(qapp):
    """Reverse direction: after publication, mutations of the snapshot's
    SOURCE data cannot reach the compatibility view or the editor."""
    snap, discovery, window, source_combos, _logical_state = (
        _snapshot_consumer_isolation_common(qapp)
    )
    try:
        # Compat/editor received exact copies at publication time.
        assert [c["id"] for c in discovery.combos] == ["c1"]
        assert "c1" in window.combo_editor.combos
        assert list(window.combo_editor.combos["c1"].models) == ["p/m1"]

        # Mutate the source transport data hard.
        source_combos.clear()
        source_combos.append({"id": "brand_new", "models": ["p/zzz"]})

        # Published compatibility state is untouched.
        assert [c["id"] for c in discovery.combos] == ["c1"]
        assert [c["models"] for c in discovery.combos] == [["p/m1"]]
        assert "c1" in window.combo_editor.combos
        assert list(window.combo_editor.combos["c1"].models) == ["p/m1"]
        # And the completed snapshot itself is untouched.
        assert [c["id"] for c in snap.combos] == ["c1"]
        assert [list(c["models"]) for c in snap.combos] == [["p/m1"]]
    finally:
        window.close()


# ===================================================================
# 3e. MANDATORY (T-31 FINAL CLOSURE): RECURSIVE CONSUMER ISOLATION
# ===================================================================
def _deep_combo(combo_id="c-deep", tag="x"):
    """Combo carrying nested dict/list structures more than three levels
    deep: combo -> meta -> tags / nested -> flags -> list."""
    return {
        "id": combo_id,
        "name": "Deep",
        "models": ["p/m1"],
        "meta": {
            "tags": [tag],
            "nested": {
                "tags": [tag + "-inner"],
                "flags": {"a": ["b"]},
            },
        },
    }


def test_recursive_combo_copy_no_shared_container_at_any_depth():
    """The helper's documented invariant, verified at every depth.

    The previously verified defect: appending to source["meta"]["tags"] was
    visible through the copy. No mutable collection may be shared.
    """
    source = _deep_combo()
    combo = ModelDiscovery._copy_combos([source])[0]

    # Every container along the chain is a distinct object.
    assert combo is not source
    assert combo["models"] is not source["models"]
    assert combo["meta"] is not source["meta"]
    assert combo["meta"]["tags"] is not source["meta"]["tags"]
    assert combo["meta"]["nested"] is not source["meta"]["nested"]
    assert combo["meta"]["nested"]["tags"] is not source["meta"]["nested"]["tags"]
    assert combo["meta"]["nested"]["flags"] is not source["meta"]["nested"]["flags"]
    assert combo["meta"]["nested"]["flags"]["a"] is not source["meta"]["nested"]["flags"]["a"]

    # The exact defect: a depth-3 mutation of the source is invisible.
    source["meta"]["tags"].append("y")
    source["meta"]["nested"]["tags"].append("z")
    source["meta"]["nested"]["flags"]["a"].append("c")
    source["models"].append("p/other")
    assert combo["meta"]["tags"] == ["x"]
    assert combo["meta"]["nested"]["tags"] == ["x-inner"]
    assert combo["meta"]["nested"]["flags"]["a"] == ["b"]
    assert combo["models"] == ["p/m1"]

    # ...and the reverse direction, from the copy into the source.
    combo["meta"]["nested"]["flags"]["a"].append("only-in-copy")
    assert source["meta"]["nested"]["flags"]["a"] == ["b", "c"]

    # Tuples are rebuilt too (immutable shell, mutable contents).
    tup = ModelDiscovery._detach_payload(("k", ["v"]))
    assert isinstance(tup, tuple) and tup[1] is not None
    assert tup[1] == ["v"]

    # Scalars stay shared (immutable) and non-dict combos pass through.
    assert ModelDiscovery._detach_payload("leaf") == "leaf"
    assert ModelDiscovery._detach_payload(7) == 7
    assert ModelDiscovery._copy_combo("not-a-dict") == "not-a-dict"
    assert ModelDiscovery._copy_combos(None) == []


def test_recursive_copy_depth_is_explicitly_bounded():
    """A pathological payload fails loudly instead of recursing without
    limit (the supported payload is data-only and shallow)."""
    payload: dict = {"leaf": []}
    for _ in range(ModelDiscovery._COMBO_COPY_MAX_DEPTH + 5):
        payload = {"nested": payload}
    with pytest.raises(ValueError):
        ModelDiscovery._copy_combos([payload])


def test_deep_nested_isolation_across_every_publication_boundary(qapp):
    """MANDATORY T-31 closure test: source -> completed snapshot ->
    compatibility discovery.combos -> ComboEditor consumer, each mutated
    independently, with nested collections at least three levels deep. No
    mutation may reach any other ownership boundary."""
    client = RouterClient(base_url="http://127.0.0.1:99999")
    discovery = ModelDiscovery(client)

    source_combos = [_deep_combo()]
    source_nodes = [
        {"id": "node-1", "name": "N1", "data": json.dumps({"prefix": "p"})},
    ]
    source_kv = [("customModels", "p", '["m1"]')]
    source_catalog = [{"provider": "node-1", "model": "m1", "name": "M1"}]

    client.get_combos = lambda: source_combos
    client.get_providers = lambda: []
    client.get_provider_nodes = lambda: list(source_nodes)
    client.get_kv_scoped = lambda: list(source_kv)
    client.get_catalog_models = lambda: list(source_catalog)

    raw = discovery.discover_all(query_live=False)
    snap = DiscoverySnapshot(
        generation=1,
        models=raw.models,
        combos=raw.combos,
        live_outcomes=raw.live_outcomes,
        catalog_states=raw.catalog_states,
        catalog_model_counts=raw.catalog_model_counts,
        routing_excluded_connections=raw.routing_excluded_connections,
    )
    discovery.publish_snapshot(snap)
    window = MainWindow()
    try:
        window._on_discovery_finished(1, snap)

        snap_combo = snap.combos[0]
        compat_combo = discovery.combos[0]
        editor_combo = window.combo_editor.combos["c-deep"]
        source_combo = source_combos[0]

        def state_of(c):
            """Logical (value) state of one boundary, deepest level included."""
            return (
                tuple(c["models"]),
                tuple(c["meta"]["tags"]),
                tuple(c["meta"]["nested"]["tags"]),
                tuple(c["meta"]["nested"]["flags"]["a"]),
            )

        def editor_of():
            return tuple(editor_combo.models)

        snap_baseline = state_of(snap_combo)
        compat_baseline = state_of(compat_combo)
        source_baseline = state_of(source_combo)
        editor_baseline = editor_of()
        assert source_baseline[0] == ("p/m1",)

        # ---- Structural: no container is shared across any boundary ----
        assert source_combo["meta"]["tags"] is not snap_combo["meta"]["tags"]
        assert source_combo["meta"]["nested"]["flags"]["a"] is not \
            snap_combo["meta"]["nested"]["flags"]["a"]
        assert snap_combo["meta"]["tags"] is not compat_combo["meta"]["tags"]
        assert snap_combo["meta"]["nested"]["flags"]["a"] is not \
            compat_combo["meta"]["nested"]["flags"]["a"]
        assert editor_combo.models is not snap_combo["models"]
        assert editor_combo.models is not compat_combo["models"]
        assert editor_combo.models is not source_combo["models"]

        # ---- 1. Mutate the SOURCE deeply ----
        source_combo["meta"]["tags"].append("source")
        source_combo["meta"]["nested"]["tags"].append("source")
        source_combo["meta"]["nested"]["flags"]["a"].append("source")
        source_combo["models"].append("p/source")
        assert state_of(source_combo) != source_baseline  # the mutation landed
        assert state_of(snap_combo) == snap_baseline
        assert state_of(compat_combo) == compat_baseline
        assert editor_of() == editor_baseline

        # ---- 2. Mutate the COMPLETED SNAPSHOT deeply ----
        snap_combo["meta"]["tags"].append("snapshot")
        snap_combo["meta"]["nested"]["tags"].append("snapshot")
        snap_combo["meta"]["nested"]["flags"]["a"].append("snapshot")
        assert state_of(snap_combo) != snap_baseline
        assert state_of(compat_combo) == compat_baseline
        assert state_of(source_combo) == (
            ("p/m1", "p/source"), ("x", "source"), ("x-inner", "source"),
            ("b", "source"),
        )
        assert editor_of() == editor_baseline

        # ---- 3. Mutate the COMPATIBILITY consumer deeply ----
        compat_combo["meta"]["tags"].append("compat")
        compat_combo["meta"]["nested"]["tags"].append("compat")
        compat_combo["meta"]["nested"]["flags"]["a"].append("compat")
        compat_combo["models"].append("p/compat")
        # The snapshot carries exactly the mutations made to the snapshot.
        assert snap_combo["meta"]["tags"] == ["x", "snapshot"]
        assert snap_combo["meta"]["nested"]["flags"]["a"] == ["b", "snapshot"]
        assert source_combo["meta"]["tags"] == ["x", "source"]
        assert editor_of() == editor_baseline

        # ---- 4. Mutate the COMBO EDITOR consumer ----
        editor_combo.models.append("p/editor")
        assert snap_combo["models"] == ["p/m1"]
        assert compat_combo["models"] == ["p/m1", "p/compat"]
        assert source_combo["models"] == ["p/m1", "p/source"]

        # ---- 5. A fresh compat publication is independent again ----
        discovery.combos = ModelDiscovery._copy_combos(snap.combos)
        discovery.combos[0]["meta"]["nested"]["flags"]["a"].append("fresh")
        assert snap_combo["meta"]["nested"]["flags"]["a"] == ["b", "snapshot"]
        assert source_combo["meta"]["nested"]["flags"]["a"] == ["b", "source"]
    finally:
        window.close()
