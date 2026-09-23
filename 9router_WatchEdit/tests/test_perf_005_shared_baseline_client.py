import httpx

from core.discovery import ModelDiscovery
from core.router_client import RouterClient


def test_baseline_snapshot_reuses_one_safe_local_transport(tmp_path, monkeypatch):
    paths = []
    payloads = {
        "/api/combos": {"combos": []},
        "/api/providers": {"connections": []},
        "/api/provider-nodes": {"nodes": []},
        "/api/models": {"models": []},
    }

    def handler(request):
        paths.append(request.url.path)
        return httpx.Response(200, json=payloads[request.url.path])

    real_client = httpx.Client
    clients = []
    options = []

    def counted_client(**kwargs):
        options.append(kwargs)
        client = real_client(transport=httpx.MockTransport(handler), **kwargs)
        clients.append(client)
        return client

    from core import discovery
    monkeypatch.setattr(discovery.httpx, "Client", counted_client)
    router = RouterClient(base_url="http://127.0.0.1:43123", db_path=tmp_path / "db.sqlite")
    monkeypatch.setattr(router, "_require_live", lambda _op: None)
    monkeypatch.setattr(router, "_get_headers", lambda include_bearer=False: {})
    monkeypatch.setattr(router, "get_kv_scoped", lambda: [])

    snapshot = ModelDiscovery(router).build_snapshot(query_live=False)

    assert len(clients) == 1
    assert paths == [
        "/api/combos", "/api/providers", "/api/provider-nodes", "/api/models",
    ]
    assert options[0]["follow_redirects"] is False
    assert options[0]["trust_env"] is False
    assert clients[0].is_closed is True
    assert snapshot.combos == ()


def test_live_snapshot_reuses_baseline_pool_without_closing_early(tmp_path, monkeypatch):
    paths = []

    def handler(request):
        paths.append(request.url.path)
        payload = {
            "/api/combos": {"combos": []},
            "/api/providers": {"connections": [{
                "id": "conn-1", "provider": "test", "name": "Test",
                "isActive": True, "providerSpecificData": {"prefix": "test"},
            }]},
            "/api/provider-nodes": {"nodes": []},
            "/api/models": {"models": []},
            "/api/providers/conn-1/models": {"models": []},
        }[request.url.path]
        return httpx.Response(200, json=payload)

    real_client = httpx.Client
    clients = []

    def counted_client(**kwargs):
        client = real_client(transport=httpx.MockTransport(handler), **kwargs)
        clients.append(client)
        return client

    from core import discovery
    monkeypatch.setattr(discovery.httpx, "Client", counted_client)
    router = RouterClient(base_url="http://127.0.0.1:43123", db_path=tmp_path / "db.sqlite")
    monkeypatch.setattr(router, "_require_live", lambda _op: None)
    monkeypatch.setattr(router, "_get_headers", lambda include_bearer=False: {})
    monkeypatch.setattr(router, "get_kv_scoped", lambda: [])

    snapshot = ModelDiscovery(router).build_snapshot(query_live=True)

    assert len(clients) == 1
    assert paths == [
        "/api/combos", "/api/providers", "/api/provider-nodes", "/api/models",
        "/api/providers/conn-1/models",
    ]
    assert clients[0].is_closed is True
    assert snapshot.live_outcomes["conn-1"] == "OK"
