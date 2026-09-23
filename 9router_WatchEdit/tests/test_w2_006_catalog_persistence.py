"""
W2-006 regressions - catalog persistence failures must be observable.

Defect (audit/3.md W2-006): _save_snapshot swallowed every failure, and
_refresh_locked returned fresh=True/API_OK regardless of durability, so a
session reported normal success while restart lost the whole baseline; a
malformed existing snapshot was silently treated as first-load, erasing the
diff baseline and hiding transitions.

Contract under test:
  * a successful fetch whose snapshot cannot be persisted reports non-durable
    (durable=False / NON_DURABLE), not an indistinguishable API_OK;
  * a malformed existing snapshot is surfaced (SNAPSHOT_UNREADABLE) and does not
    masquerade as first-load;
  * successful atomic save/restart preserves models and the diff baseline.
"""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from core.opencode_catalog import (
    API_DURABILITY_FAILED,
    API_DURABILITY_OK,
    API_OK,
    SNAPSHOT_ABSENT,
    SNAPSHOT_LOADED,
    SNAPSHOT_UNREADABLE,
    OpenCodeCatalogDiscovery,
)


def _transport(body, status=200):
    def handler(request: httpx.Request):
        return httpx.Response(status, json=body)
    return httpx.MockTransport(handler)


def test_successful_refresh_is_durable(tmp_path: Path):
    snap = tmp_path / "snapshot.json"
    discovery = OpenCodeCatalogDiscovery(
        snapshot_file=snap,
        transport=_transport({"object": "list", "data": [{"id": "a-free"}]}),
    )
    result = discovery.refresh(force=True)
    assert result["status"] == API_OK
    assert result["durable"] is True
    assert result["durability"] == API_DURABILITY_OK
    assert snap.exists()


def test_persistence_failure_is_reported_non_durable(tmp_path: Path, monkeypatch):
    from core import opencode_catalog as oc

    snap = tmp_path / "snapshot.json"
    discovery = OpenCodeCatalogDiscovery(
        snapshot_file=snap,
        transport=_transport({"object": "list", "data": [{"id": "a"}]}),
    )

    # Inject a replace failure: the fetch succeeds but the write cannot land.
    real_replace = Path.replace

    def failing_replace(self, target):
        raise OSError("replace refused")

    monkeypatch.setattr(Path, "replace", failing_replace)
    result = discovery.refresh(force=True)
    monkeypatch.setattr(Path, "replace", real_replace)

    # Models stay usable in memory, but durability is NOT claimed.
    assert result["fresh"] is True
    assert result["durable"] is False
    assert result["durability"] == API_DURABILITY_FAILED
    assert {m.model_id for m in discovery.models} == {"a"}
    assert not snap.exists()


def test_malformed_snapshot_is_surfaced_not_first_load(tmp_path: Path):
    snap = tmp_path / "snapshot.json"
    snap.write_text("{not valid json", encoding="utf-8")
    discovery = OpenCodeCatalogDiscovery(snapshot_file=snap, transport=_transport({"object": "list", "data": []}))
    assert discovery.snapshot_load_state == SNAPSHOT_UNREADABLE
    assert discovery.snapshot_load_error


def test_schema_mismatch_snapshot_surfaced(tmp_path: Path):
    snap = tmp_path / "snapshot.json"
    snap.write_text(json.dumps({"schema_version": 1, "fetched_at": "", "models": [], "free_ids": [], "statuses": {}}), encoding="utf-8")
    discovery = OpenCodeCatalogDiscovery(snapshot_file=snap, transport=_transport({"object": "list", "data": []}))
    assert discovery.snapshot_load_state in (SNAPSHOT_UNREADABLE, "SCHEMA_MISMATCH")


def test_absent_snapshot_is_absent_state(tmp_path: Path):
    discovery = OpenCodeCatalogDiscovery(snapshot_file=tmp_path / "none.json", transport=_transport({"object": "list", "data": []}))
    assert discovery.snapshot_load_state == SNAPSHOT_ABSENT


def test_atomic_save_restart_preserves_models_and_baseline(tmp_path: Path):
    snap = tmp_path / "snapshot.json"
    first = OpenCodeCatalogDiscovery(
        snapshot_file=snap,
        transport=_transport({"object": "list", "data": [{"id": "a"}, {"id": "b-free"}]}),
    )
    r1 = first.refresh(force=True)
    assert r1["durable"] is True

    second = OpenCodeCatalogDiscovery(
        snapshot_file=snap,
        transport=_transport({"object": "list", "data": [{"id": "a"}, {"id": "b-free"}, {"id": "c"}]}),
    )
    assert second.snapshot_load_state == SNAPSHOT_LOADED
    assert {m.model_id for m in second.models} == {"a", "b-free"}
    # The diff baseline survived restart: the new id is a real change.
    r2 = second.refresh(force=True)
    assert r2["changed"] is True
    assert "c" in {m.model_id for m in second.models}
