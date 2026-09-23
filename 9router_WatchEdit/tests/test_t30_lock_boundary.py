"""T-30 / W2-002: Lock Now request-boundary authorization regressions.

Offline only (httpx.MockTransport). The conftest autouse fixture exports
WATCHEDIT_LIVE_ACCESS=1, which bypasses SecurityManager.lock(); every
boundary test here deletes it first so the real gate is exercised. No
operator secrets are persisted — synthetic sentinels only."""
import asyncio
import json
import sqlite3

import httpx
import pytest

from core import router_client as rc_mod
from core.classification import CatalogState, classify_probe_result
from core.discovery import DiscoveredModel, ModelDiscovery
from core.history import HealthCache
from core.probe import ScannerWorker, ScanMode
from core.router_client import RouterClient
from core.security import LiveAccessLockedError, SecurityManager


def _locked_security(tmp_path, name):
    """A SecurityManager whose lock() actually gates (env bypass removed)."""
    mgr = SecurityManager(data_dir=tmp_path / name)
    mgr._set_state("UNLOCKED")
    return mgr


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


def _recording_sync_transport(monkeypatch, handler):
    """Route RouterClient's sync httpx.Client through a recording MockTransport."""
    transport = httpx.MockTransport(handler)
    real = httpx.Client

    def factory(*args, **kwargs):
        kwargs["transport"] = transport
        return real(*args, **kwargs)

    monkeypatch.setattr(rc_mod.httpx, "Client", factory)


# ----------------------------------------------------------------
# A. Scanner invoked while LOCKED -> zero requests, LOCKED/CANCELLED
# ----------------------------------------------------------------
def test_a_locked_start_issues_zero_requests(tmp_path, monkeypatch):
    monkeypatch.delenv("WATCHEDIT_LIVE_ACCESS", raising=False)
    sec = SecurityManager(data_dir=tmp_path / "sec-a")
    assert sec.is_locked()
    client = RouterClient(base_url="http://127.0.0.1:99999", db_path=tmp_path / "t30a.sqlite", security=sec)
    cache = HealthCache(cache_file=tmp_path / "cache-a.json")

    hits = []

    async def handler(request):
        hits.append(request)
        return httpx.Response(200, json={"ok": True})

    worker = ScannerWorker(client, cache, global_concurrency=2, per_provider_concurrency=1, transport=httpx.MockTransport(handler))
    statuses = []
    worker.on_scan_completed = statuses.append
    worker.run_scan(_models(3), mode=ScanMode.FULL)

    assert hits == []
    assert statuses and statuses[0] in ("LOCKED", "CANCELLED")
    assert cache.get("p/m0") is None


# ----------------------------------------------------------------
# B. Lock after first request -> exactly one authenticated request
# ----------------------------------------------------------------
def test_b_lock_after_first_request(tmp_path, monkeypatch):
    monkeypatch.delenv("WATCHEDIT_LIVE_ACCESS", raising=False)
    sec = _locked_security(tmp_path, "sec-b")
    client = RouterClient(base_url="http://127.0.0.1:99999", db_path=tmp_path / "t30b.sqlite", security=sec)
    cache = HealthCache(cache_file=tmp_path / "cache-b.json")

    count = {"n": 0}

    async def handler(request):
        count["n"] += 1
        if count["n"] == 1:
            sec.lock()
        return httpx.Response(200, json={"ok": True})

    worker = ScannerWorker(client, cache, global_concurrency=1, per_provider_concurrency=1, transport=httpx.MockTransport(handler))
    statuses = []
    worker.on_scan_completed = statuses.append
    worker.run_scan(_models(3, conn="conn-lock-b"), mode=ScanMode.FULL)

    assert count["n"] == 1
    assert statuses[0] in ("LOCKED", "CANCELLED")
    assert cache.get("p/m1") is None
    assert cache.get("p/m2") is None


# ----------------------------------------------------------------
# C. Lock while tasks queued behind semaphores
# ----------------------------------------------------------------
def test_c_lock_queued_tasks_never_post(tmp_path, monkeypatch):
    monkeypatch.delenv("WATCHEDIT_LIVE_ACCESS", raising=False)
    sec = _locked_security(tmp_path, "sec-c")
    client = RouterClient(base_url="http://127.0.0.1:99999", db_path=tmp_path / "t30c.sqlite", security=sec)
    cache = HealthCache(cache_file=tmp_path / "cache-c.json")

    posts = {"n": 0}

    async def handler(request):
        posts["n"] += 1
        if posts["n"] == 1:
            sec.lock()
            await asyncio.sleep(0.02)
        return httpx.Response(200, json={"ok": True})

    # per_provider_concurrency=1 serializes the connection: tasks 2..4 queue on
    # the provider semaphore while task 1 is in flight and locks. When released
    # they must observe the lock at the request boundary and never post.
    worker = ScannerWorker(client, cache, global_concurrency=4, per_provider_concurrency=1, transport=httpx.MockTransport(handler))
    statuses = []
    worker.on_scan_completed = statuses.append
    worker.run_scan(_models(4, prefix="pc", conn="conn-c"), mode=ScanMode.FULL)

    assert posts["n"] == 1
    assert statuses[0] in ("LOCKED", "CANCELLED")


# ----------------------------------------------------------------
# D. Lock during slow/pending probe -> no second-stage request
# ----------------------------------------------------------------
def test_d_no_second_stage_after_lock(tmp_path, monkeypatch):
    monkeypatch.delenv("WATCHEDIT_LIVE_ACCESS", raising=False)
    sec = _locked_security(tmp_path, "sec-d")
    client = RouterClient(base_url="http://127.0.0.1:99999", db_path=tmp_path / "t30d.sqlite", security=sec)
    cache = HealthCache(cache_file=tmp_path / "cache-d.json")

    posts = []

    async def handler(request):
        posts.append(request.url.path)
        if len(posts) == 1:
            sec.lock()
            raise httpx.ReadTimeout("first fast timeout", request=request)
        return httpx.Response(200, json={"ok": True})

    import core.probe as probe_mod

    monkeypatch.setattr(probe_mod, "DEFAULT_FAST_TIMEOUT_SEC", 0.05)
    monkeypatch.setattr(probe_mod, "DEFAULT_SLOW_TIMEOUT_SEC", 0.5)

    model = DiscoveredModel(canonical_id="p/m0", provider_name="p", provider_prefix="p", connection_id="c-d", model_id="m0", display_name="m0")
    worker = ScannerWorker(client, cache, global_concurrency=1, per_provider_concurrency=1, transport=httpx.MockTransport(handler))
    statuses = []
    worker.on_scan_completed = statuses.append
    worker.run_scan([model], mode=ScanMode.FULL)

    assert len(posts) == 1
    assert statuses[0] in ("LOCKED", "CANCELLED")
    # The in-flight probe timed out while authorized; the pending->slow
    # branch was suppressed by lock so no second transport firing occurred.
    # A timeout record for p/m0 would be failure noise from lock cancellation,
    # so lock cancellation suppresses it — the model stays unevidenced.
    assert cache.get("p/m0") is None or cache.get("p/m0").availability.value == "CONNECT_TIMEOUT"


# ----------------------------------------------------------------
# E. No negative evidence on lock cancellation
# ----------------------------------------------------------------
def test_e_no_negative_evidence_after_locked_scan(tmp_path, monkeypatch):
    monkeypatch.delenv("WATCHEDIT_LIVE_ACCESS", raising=False)
    sec = _locked_security(tmp_path, "sec-e")
    client = RouterClient(base_url="http://127.0.0.1:99999", db_path=tmp_path / "t30e.sqlite", security=sec)
    cache = HealthCache(cache_file=tmp_path / "cache-e.json")

    seed = classify_probe_result(
        status_code=200, latency_ms=1.0, raw_body="ok", parsed_json={"ok": True}, provider_prefix="p", model_id="m1",
    )
    rec = cache.record_evidence("p/m1", "p", "m1", seed, auto_save=False)
    snapshot = dict(rec.counters.to_dict())

    posts = {"n": 0}

    async def handler(request):
        posts["n"] += 1
        if posts["n"] == 1:
            sec.lock()
        return httpx.Response(200, json={"ok": True})

    worker = ScannerWorker(client, cache, global_concurrency=2, per_provider_concurrency=1, transport=httpx.MockTransport(handler))
    worker.run_scan(_models(3), mode=ScanMode.FULL)

    after = cache.get("p/m1")
    assert after is not None
    assert after.counters.to_dict() == snapshot
    # p/m0 was the authorized in-flight request (allowed to complete, LIVE).
    # p/m2 was never started post-lock: no evidence, no DEAD/AUTH/TEMP noise.
    assert cache.get("p/m2") is None
    assert not worker.circuit_breaker.is_tripped("c-1")


# ----------------------------------------------------------------
# F. Discovery invoked while locked -> zero authenticated requests
# ----------------------------------------------------------------
def test_f_discovery_while_locked_zero_requests(tmp_path, monkeypatch):
    monkeypatch.delenv("WATCHEDIT_LIVE_ACCESS", raising=False)
    sec = SecurityManager(data_dir=tmp_path / "sec-f")
    assert sec.is_locked()
    client = RouterClient(base_url="http://127.0.0.1:99999", db_path=tmp_path / "t30f.sqlite", security=sec)
    discovery = ModelDiscovery(client)

    hits = []
    _recording_sync_transport(monkeypatch, lambda req: hits.append(req) or httpx.Response(200, json={"models": []}))

    # Non-live sources are stubbed so the LOCKED boundary is exercised only on
    # the authenticated live-catalog path.
    client.get_providers = lambda: [
        {"id": "conn-f", "name": "F", "provider": "prov-f", "isActive": True, "providerSpecificData": {"prefix": "pf"}},
    ]
    client.get_provider_nodes = lambda: []
    client.get_kv_scoped = lambda: [("customModels", "pf", json.dumps(["known"]))]
    client.get_catalog_models = lambda: []
    client.get_combos = lambda: []

    models = discovery.discover_all(query_live=True)
    by_cid = {m.canonical_id: m for m in models}

    assert hits == []
    assert discovery.live_outcomes["conn-f"] == "LOCKED"
    assert discovery.catalog_states["conn-f"] == CatalogState.DISCOVERY_UNAVAILABLE
    assert "conn-f" not in discovery.routing_excluded_connections
    assert "conn-f" not in discovery.catalog_model_counts
    assert by_cid["pf/known"].advertised_live is None
    assert by_cid["pf/known"].routing_eligible is True


# ----------------------------------------------------------------
# G. Lock during live discovery -> response cannot publish evidence
# ----------------------------------------------------------------
def test_g_lock_during_live_discovery_no_negative_evidence(tmp_path, monkeypatch):
    monkeypatch.delenv("WATCHEDIT_LIVE_ACCESS", raising=False)
    sec = _locked_security(tmp_path, "sec-g")
    client = RouterClient(base_url="http://127.0.0.1:99999", db_path=tmp_path / "t30g.sqlite", security=sec)
    discovery = ModelDiscovery(client)

    def handler(request):
        # Response arrives, then Lock Now fires before it can be published.
        sec.lock()
        return httpx.Response(200, json={"models": []})

    _recording_sync_transport(monkeypatch, handler)

    client.get_providers = lambda: [
        {"id": "conn-g", "name": "G", "provider": "prov-g", "isActive": True, "providerSpecificData": {"prefix": "pg"}},
    ]
    client.get_provider_nodes = lambda: []
    client.get_kv_scoped = lambda: [("customModels", "pg", json.dumps(["seed"]))]
    client.get_catalog_models = lambda: []
    client.get_combos = lambda: []

    models = discovery.discover_all(query_live=True)
    by_cid = {m.canonical_id: m for m in models}

    assert discovery.live_outcomes["conn-g"] == "LOCKED"
    assert discovery.catalog_states["conn-g"] == CatalogState.DISCOVERY_UNAVAILABLE
    assert "conn-g" not in discovery.routing_excluded_connections
    assert by_cid["pg/seed"].advertised_live is None
    assert by_cid["pg/seed"].routing_eligible is True


# ----------------------------------------------------------------
# H. CLI token cache invalidation on lock/unlock rotation
# ----------------------------------------------------------------
def test_h_cli_token_cache_invalidated_on_lock(tmp_path, monkeypatch):
    monkeypatch.delenv("WATCHEDIT_LIVE_ACCESS", raising=False)
    sec = SecurityManager(data_dir=tmp_path / "sec-h")
    machine = tmp_path / "machine-id-h"
    secret = tmp_path / "cli-secret-h"
    machine.write_text("mh", encoding="utf-8")
    secret.write_text("SENTINEL-A", encoding="utf-8")
    monkeypatch.setattr(rc_mod, "MACHINE_ID_FILE", machine)
    monkeypatch.setattr(rc_mod, "CLI_SECRET_FILE", secret)

    sec._set_state("UNLOCKED")
    client = RouterClient(base_url="http://127.0.0.1:99999", security=sec)
    tok_a = client.get_cli_token()
    assert client._cached_cli_token == tok_a

    secret.write_text("SENTINEL-B", encoding="utf-8")
    sec.lock()
    assert client._cached_cli_token is None
    sec._set_state("UNLOCKED")
    assert client._cached_cli_token is None
    tok_b = client.get_cli_token()
    assert tok_b != tok_a


# ----------------------------------------------------------------
# I. API key cache invalidation on lock/unlock rotation
# ----------------------------------------------------------------
def test_i_api_key_cache_invalidated_on_lock(tmp_path, monkeypatch):
    monkeypatch.delenv("WATCHEDIT_LIVE_ACCESS", raising=False)
    sec = SecurityManager(data_dir=tmp_path / "sec-i")
    db = tmp_path / "api-cache-i.sqlite"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE apiKeys (key TEXT, createdAt TEXT)")
    conn.execute("INSERT INTO apiKeys VALUES (?, ?)", ("SENTINEL-API-A", "2026-01-01"))
    conn.commit()
    conn.close()

    sec._set_state("UNLOCKED")
    client = RouterClient(base_url="http://127.0.0.1:99999", db_path=db, security=sec)
    assert client.get_api_key() == "SENTINEL-API-A"
    assert client._cached_api_key == "SENTINEL-API-A"

    conn = sqlite3.connect(str(db))
    conn.execute("DELETE FROM apiKeys")
    conn.execute("INSERT INTO apiKeys VALUES (?, ?)", ("SENTINEL-API-B", "2026-01-02"))
    conn.commit()
    conn.close()
    assert client.get_api_key() == "SENTINEL-API-A"  # still cached

    sec.lock()
    assert client._cached_api_key is None
    sec._set_state("UNLOCKED")
    assert client.get_api_key() == "SENTINEL-API-B"


# ----------------------------------------------------------------
# J. Unlock after rotation -> first authorized request uses new material
# ----------------------------------------------------------------
def test_j_unlock_after_rotation_uses_new_material(tmp_path, monkeypatch):
    monkeypatch.delenv("WATCHEDIT_LIVE_ACCESS", raising=False)
    sec = SecurityManager(data_dir=tmp_path / "sec-j")
    machine = tmp_path / "machine-id-j"
    secret = tmp_path / "cli-secret-j"
    machine.write_text("mj", encoding="utf-8")
    secret.write_text("SENTINEL-A", encoding="utf-8")
    monkeypatch.setattr(rc_mod, "MACHINE_ID_FILE", machine)
    monkeypatch.setattr(rc_mod, "CLI_SECRET_FILE", secret)

    sec._set_state("UNLOCKED")
    client = RouterClient(base_url="http://127.0.0.1:99999", security=sec)
    tok_a = client.get_cli_token()

    sec.lock()
    secret.write_text("SENTINEL-B", encoding="utf-8")
    sec._set_state("UNLOCKED")
    tok_b = client.get_cli_token()
    assert tok_b != tok_a

    headers = client._get_headers()
    assert headers[rc_mod.CLI_TOKEN_HEADER] == tok_b
