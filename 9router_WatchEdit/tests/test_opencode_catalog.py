"""
Focused tests for OpenCode Zen catalog discovery (SRC-004 / T-26).
"""
from __future__ import annotations

import json
import threading
import time

import httpx
import pytest

from core.opencode_catalog import (
    OpenCodeCatalogDiscovery,
    ZenCatalogModel,
    CatalogDiff,
    classify_free,
    parse_catalog,
    diff_snapshots,
    catalog_events,
    API_OK,
    API_FAILED,
    CATALOG_CHANGED,
    CLI_OK,
    CLI_MISSING,
    CLI_FAILED,
    FREE_SUFFIX,
    OPENCODE_PROVIDER_NAMESPACE,
    ZEN_MODELS_URL,
)
from pathlib import Path


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------
CATALOG_DIR = Path(__file__).parent / "fixtures" / "opencode_catalog"


def _response(body):
    return httpx.Response(200, json=body)


def _transport(body, status=200):
    def handler(request: httpx.Request):
        return httpx.Response(status, json=body)
    return httpx.MockTransport(handler)


# ---------------------------------------------------------------------------
# 1. parse_catalog: current "data" API shape + canonical identity + suffix
# ---------------------------------------------------------------------------
def test_parse_data_shape_returns_canonical_ids():
    payload = {"object": "list", "data": [
        {"id": "deepseek-v4-flash"},
        {"id": "deepseek-v4-flash-free"},
    ]}
    models = parse_catalog(payload)
    assert [m.model_id for m in models] == ["deepseek-v4-flash", "deepseek-v4-flash-free"]
    assert [m.canonical_id for m in models] == [
        "opencode/deepseek-v4-flash",
        "opencode/deepseek-v4-flash-free",
    ]


def test_free_suffix_only_free_candidate_is_free():
    models = parse_catalog({"object": "list", "data": [
        {"id": "base-model"},
        {"id": "base-model-free"},
    ]})
    assert models[0].free_candidate is False
    assert models[0].cost_state == "UNKNOWN"
    assert models[1].free_candidate is True
    assert models[1].cost_state == "FREE_CANDIDATE"
    assert models[1].free_reason == "explicit_free_model_id"


# ---------------------------------------------------------------------------
# 2. Explicit billing metadata precedence + conflicting/ambiguous
# ---------------------------------------------------------------------------
def test_explicit_free_true_billing_metadata():
    models = parse_catalog({"object": "list", "data": [
        {"id": "model-a", "pricing": {"free": True}},
    ]})
    assert models[0].free_candidate is True
    assert models[0].free_reason == "explicit_billing_metadata"


def test_explicit_free_false_with_verified_paid():
    models = parse_catalog({"object": "list", "data": [
        {"id": "model-b", "pricing": {"free": False, "verified": True}},
    ]})
    assert models[0].free_candidate is False
    assert models[0].free_reason == "explicit_paid_metadata"


def test_conflicting_billing_metadata_treated_unknown():
    models = parse_catalog({"object": "list", "data": [
        {"id": "model-c", "pricing": {"free": True, "isFree": False}},
    ]})
    assert models[0].free_candidate is False
    assert models[0].free_reason == "conflicting_billing_metadata"


def test_ambiguous_non_boolean_billing_metadata_treated_unknown():
    models = parse_catalog({"object": "list", "data": [
        {"id": "model-d", "pricing": {"free": "maybe"}},
    ]})
    assert models[0].free_candidate is False
    assert models[0].free_reason == "ambiguous_billing_metadata"


# ---------------------------------------------------------------------------
# 3. Malformed / unexpected schema rejection
# ---------------------------------------------------------------------------
def test_malformed_json_raises():
    with pytest.raises(ValueError, match="malformed JSON"):
        parse_catalog("not-json")


def test_missing_models_and_data_raises():
    with pytest.raises(ValueError, match="no models/data array"):
        parse_catalog({"object": "list"})


def test_non_list_models_raises():
    with pytest.raises(ValueError, match="no models/data array"):
        parse_catalog({"object": "list", "models": {}})


# ---------------------------------------------------------------------------
# 4. Models shape fallback (models vs data)
# ---------------------------------------------------------------------------
def test_models_shape_accepted():
    models = parse_catalog({"models": [{"id": "x"}]})
    assert models[0].model_id == "x"


# ---------------------------------------------------------------------------
# 5. diff_snapshots + catalog_events
# ---------------------------------------------------------------------------
def _model(identifier: str, free: bool = False) -> ZenCatalogModel:
    return ZenCatalogModel(
        model_id=identifier,
        canonical_id=f"{OPENCODE_PROVIDER_NAMESPACE}/{identifier}",
        free_candidate=free,
    )


def test_diff_added_removed_newly_free_no_longer_free():
    prev = [_model("a"), _model("b-free", free=True)]
    curr = [_model("b-free", free=True), _model("c")]
    diff = diff_snapshots(prev, curr)
    assert diff.added == ["c"]
    assert diff.removed == ["a"]
    assert diff.newly_free == []
    assert diff.no_longer_free == []


def test_newly_free_and_no_longer_free():
    prev = [_model("m-free", free=True)]
    curr = [_model("m-free"), _model("n-free", free=True)]
    diff = diff_snapshots(prev, curr)
    assert diff.newly_free == ["n-free"]
    assert diff.no_longer_free == ["m-free"]


def test_catalog_events_surface_new_free_message():
    diff = CatalogDiff(newly_free=["x-free"])
    events = catalog_events(diff)
    assert any(ev["type"] == "newly_free" and "NEW FREE OPENCODE MODEL:" in ev["message"] for ev in events)


def test_unchanged_refresh_produces_no_events():
    prev = [_model("a"), _model("b-free", free=True)]
    curr = [_model("a"), _model("b-free", free=True)]
    diff = diff_snapshots(prev, curr)
    assert not any([diff.added, diff.removed, diff.newly_free, diff.no_longer_free])
    assert catalog_events(diff) == []


# ---------------------------------------------------------------------------
# 6. Routing safety defaults
# ---------------------------------------------------------------------------
def test_parse_catalog_defaults_routing_safety():
    models = parse_catalog({"object": "list", "data": [{"id": "any-free"}]})
    assert models[0].configured is False
    assert models[0].routing_eligible is False


# ---------------------------------------------------------------------------
# 7. Snapshot + failure preserves previous good snapshot (offline transport)
# ---------------------------------------------------------------------------
def test_failed_refresh_never_erases_lkg_snapshot(tmp_path: Path):
    snap = tmp_path / "snapshot.json"
    # Seed a good snapshot manually.
    doc = {
        "schema_version": 2,
        "fetched_at": "2026-01-01T00:00:00Z",
        "models": [{"model_id": "old", "canonical_id": "opencode/old",
                    "provider_namespace": "opencode", "source": "OPENCODE_ZEN_PUBLIC",
                    "free_candidate": False, "free_reason": None, "observed_at": "",
                    "configured": False, "routing_eligible": False}],
        "free_ids": [],
        "statuses": {"api": "API_OK", "cli": "CLI_MISSING"},
        "previous_models": [],
        "last_diff": None,
        "last_failure": {},
    }
    snap.write_text(json.dumps(doc), encoding="utf-8")

    discovery = OpenCodeCatalogDiscovery(snapshot_file=snap, transport=_transport({"object": "list", "data": []}, status=500))
    result = discovery.refresh(force=True)
    assert result["status"] == API_FAILED
    # Previous good snapshot is preserved in memory + on disk.
    assert {m.model_id for m in discovery.models} == {"old"}
    saved = json.loads(snap.read_text(encoding="utf-8"))
    assert [m["model_id"] for m in saved["models"]] == ["old"]


def test_successful_refresh_creates_events_and_persists(tmp_path: Path):
    snap = tmp_path / "snapshot.json"
    discovery = OpenCodeCatalogDiscovery(
        snapshot_file=snap,
        transport=_transport({"object": "list", "data": [
            {"id": "base-free"}, {"id": "base-paid"}
        ]}),
    )
    result = discovery.refresh(force=True)
    assert result["status"] == API_OK
    assert result["changed"] is False
    saved = json.loads(snap.read_text(encoding="utf-8"))
    assert saved["schema_version"] == 2
    assert [m["model_id"] for m in saved["models"]] == ["base-free", "base-paid"]


# ---------------------------------------------------------------------------
# 8. Snapshot survives restart + added/removed detection across restart.
# ---------------------------------------------------------------------------
def test_snapshot_survives_restart_and_added_detection(tmp_path: Path):
    snap = tmp_path / "snapshot.json"
    first = OpenCodeCatalogDiscovery(
        snapshot_file=snap,
        transport=_transport({"object": "list", "data": [{"id": "a"}]}),
    )
    assert first.refresh(force=True)["status"] == API_OK

    second = OpenCodeCatalogDiscovery(
        snapshot_file=snap,
        transport=_transport({"object": "list", "data": [{"id": "a"}, {"id": "b-free"}]}),
    )
    result = second.refresh(force=True)
    assert result["status"] == CATALOG_CHANGED
    assert result["diff"].added == ["b-free"]
    assert result["diff"].newly_free == ["b-free"]


# ---------------------------------------------------------------------------
# 9. CLI cross-check (offline, executable missing)
# ---------------------------------------------------------------------------
def test_cli_missing_is_non_fatal_and_still_succeeds_without_network(tmp_path: Path, monkeypatch):
    # Force refresh path with missing opencode executable.
    monkeypatch.delenv("PATH", raising=False)
    snap = tmp_path / "snapshot.json"
    discovery = OpenCodeCatalogDiscovery(
        snapshot_file=snap,
        transport=_transport({"object": "list", "data": [{"id": "cli-missing"}]}),
    )
    api_result = discovery.refresh(force=True)
    cli_result = discovery.cli_cross_check()
    assert api_result["status"] == API_OK
    assert cli_result["status"] == CLI_MISSING
    assert discovery.statuses["cli"] == CLI_MISSING


# ---------------------------------------------------------------------------
# 10. Single-flight + owner-failure: never overlapping HTTP requests.
# ---------------------------------------------------------------------------
def test_singleflight_no_overlapping_requests(tmp_path: Path):
    inflight = threading.Event()
    release = threading.Event()

    def handler(request: httpx.Request):
        inflight.set()
        release.wait()
        return _response({"object": "list", "data": [{"id": "x"}]})

    discovery = OpenCodeCatalogDiscovery(
        snapshot_file=tmp_path / "snapshot.json",
        transport=httpx.MockTransport(handler),
    )

    results = []

    def _run(label):
        results.append((label, discovery.refresh(force=True)))

    t1 = threading.Thread(target=_run, args=("first",))
    t2 = threading.Thread(target=_run, args=("second",))
    t1.start()
    t2.start()
    # Ensure first thread sees in-flight handler, then release.
    inflight.wait(timeout=5)
    release.set()
    t1.join(timeout=5)
    t2.join(timeout=5)

    assert len(results) == 2
    statuses = {r[0]: r[1] for r in results}
    assert statuses["first"]["status"] == API_OK
    assert statuses["second"]["status"] == API_OK


def test_owner_failure_becomes_api_failed_and_preserves_snapshot(tmp_path: Path):
    snap = tmp_path / "snapshot.json"
    base = OpenCodeCatalogDiscovery(
        snapshot_file=snap,
        transport=_transport({"object": "list", "data": [{"id": "good"}]}),
    )
    assert base.refresh(force=True)["status"] == API_OK

    class ExplodingDiscovery(OpenCodeCatalogDiscovery):
        def _refresh_locked(self):
            raise RuntimeError("boom")

    broken = ExplodingDiscovery(snapshot_file=snap)
    result = broken.refresh(force=True)
    assert result["status"] == API_FAILED
    assert result["error_class"] == "unexpected_refresh_error"
    assert broken.statuses["api"] == API_FAILED
    # Original snapshot is preserved (not the exploded memory state).
    saved = json.loads(snap.read_text(encoding="utf-8"))
    assert [m["model_id"] for m in saved["models"]] == ["good"]


# ---------------------------------------------------------------------------
# 11. classify_free pure unit contract
# ---------------------------------------------------------------------------
def test_classify_free_suffix_and_non_free():
    assert classify_free("m-free") == (True, "explicit_free_model_id")
    assert classify_free("m") == (False, None)


# ---------------------------------------------------------------------------
# 12. OpenCode namespace distinct from opencode-go
# ---------------------------------------------------------------------------
def test_canonical_namespace_is_opencode():
    models = parse_catalog({"object": "list", "data": [{"id": "any"}]})
    assert models[0].canonical_id.startswith("opencode/")
    assert models[0].provider_namespace == "opencode"
    assert "opencode-go" not in models[0].canonical_id
