import httpx
import pytest

from core.router_client import RouterClient


def _client(tmp_path, monkeypatch, body, status=200):
    router = RouterClient(base_url="http://127.0.0.1:43123", db_path=tmp_path / "router.db")
    monkeypatch.setattr(router, "_get_headers", lambda include_bearer=False: {})

    def response(request):
        return httpx.Response(status, json=body)

    http_client = httpx.Client(transport=httpx.MockTransport(response))
    return router, http_client


@pytest.mark.parametrize("body,status,expected", [
    ({"combos": []}, 200, ("OK", [])),
    ({"combos": [{"id": "c1", "name": "SAIFREN", "models": ["a/model"]}]},
     200, ("OK", [{"id": "c1", "name": "SAIFREN", "models": ["a/model"]}])),
    ({"combos": [{"id": "c1", "name": "SAIFREN", "models": [None]}]},
     200, ("INVALID", [])),
    ({"combos": {}}, 200, ("INVALID", [])),
    ({"combos": []}, 503, ("FAILED", [])),
])
def test_detailed_combo_read_distinguishes_live_outcomes(
    tmp_path, monkeypatch, body, status, expected,
):
    router, http_client = _client(tmp_path, monkeypatch, body, status)
    try:
        assert router.get_combos_detailed(http_client) == expected
        assert http_client.is_closed is False
    finally:
        http_client.close()


def test_detailed_catalog_read_uses_the_caller_owned_client(tmp_path, monkeypatch):
    body = {"models": [{"id": "provider/model-free"}]}
    router, http_client = _client(tmp_path, monkeypatch, body)
    try:
        assert router.get_catalog_models_detailed(http_client) == ("OK", body["models"])
        assert http_client.is_closed is False
    finally:
        http_client.close()
