"""T-116 regression: combo write failures must be distinguishable.

Before T-116, create_combo / update_combo / rename_combo wrapped the whole
request in 'except Exception: pass' and returned None, making a transport
timeout, an HTTP 4xx/5xx rejection, and a legitimate empty-but-success
indistinguishable at the UI call sites (all collapsed to 'if not res').

These tests prove the new contract: ComboWriteError is raised on transport
and HTTP failures, and a dict is always returned on success.
"""
import httpx
import pytest

from core.router_client import ComboWriteError, RouterClient


def _client_with_security(monkeypatch):
    """Build a RouterClient whose security gate is a no-op, so we can test
    the HTTP layer without a live 9router."""
    client = RouterClient(base_url="http://127.0.0.1:99999")
    monkeypatch.setattr(client.security, "require_live", lambda *a, **kw: None)
    monkeypatch.setattr(client, "_get_headers", lambda: {"Authorization": "test"})
    return client


def _httpx_mock(status_code=200, body=None):
    """Return a fake httpx.Client whose .put/.post returns a canned response."""
    class _Resp:
        def __init__(self):
            self.status_code = status_code
            self.text = body or ""
            self.content = b"" if not body else body.encode()

        def json(self):
            if not self.content:
                return {}
            import json
            return json.loads(self.content)

    class _Client:
        def __init__(self, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def post(self, *a, **kw):
            return _Resp()

        def put(self, *a, **kw):
            return _Resp()

    return _Client


def test_create_combo_raises_on_http_rejection(monkeypatch):
    client = _client_with_security(monkeypatch)
    monkeypatch.setattr(
        "core.router_client.httpx.Client",
        _httpx_mock(status_code=500, body="internal error"),
    )
    with pytest.raises(ComboWriteError) as exc_info:
        client.create_combo(name="test", models=[])
    assert exc_info.value.kind == "http"
    assert exc_info.value.status_code == 500
    assert "internal error" in exc_info.value.message_for_ui()


def test_create_combo_raises_on_transport_error(monkeypatch):
    client = _client_with_security(monkeypatch)

    class _TransportClient:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def post(self, *a, **kw):
            raise httpx.ConnectError("connection refused")

    monkeypatch.setattr("core.router_client.httpx.Client", lambda **kw: _TransportClient())
    with pytest.raises(ComboWriteError) as exc_info:
        client.create_combo(name="test", models=[])
    assert exc_info.value.kind == "transport"
    assert exc_info.value.status_code is None
    assert "connect" in exc_info.value.message_for_ui().lower()


def test_create_combo_returns_synthesized_dict_on_204(monkeypatch):
    client = _client_with_security(monkeypatch)
    monkeypatch.setattr(
        "core.router_client.httpx.Client",
        _httpx_mock(status_code=204, body=None),
    )
    result = client.create_combo(name="test", models=["m1", "m2"])
    assert isinstance(result, dict)
    assert result["name"] == "test"
    assert result["models"] == ["m1", "m2"]


def test_update_combo_raises_on_http_rejection(monkeypatch):
    client = _client_with_security(monkeypatch)
    monkeypatch.setattr(
        "core.router_client.httpx.Client",
        _httpx_mock(status_code=409, body="combo is locked"),
    )
    with pytest.raises(ComboWriteError) as exc_info:
        client.update_combo(combo_id="abc", name="test", models=["m1"])
    assert exc_info.value.kind == "http"
    assert exc_info.value.status_code == 409
    assert "combo is locked" in exc_info.value.message_for_ui()


def test_update_combo_raises_on_transport_error(monkeypatch):
    client = _client_with_security(monkeypatch)

    class _TransportClient:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def put(self, *a, **kw):
            raise httpx.ReadTimeout("timed out")

    monkeypatch.setattr("core.router_client.httpx.Client", lambda **kw: _TransportClient())
    with pytest.raises(ComboWriteError) as exc_info:
        client.update_combo(combo_id="abc", name="test", models=["m1"])
    assert exc_info.value.kind == "transport"
    assert exc_info.value.status_code is None


def test_rename_combo_propagates_update_combo_error(monkeypatch):
    client = _client_with_security(monkeypatch)
    monkeypatch.setattr(
        "core.router_client.httpx.Client",
        _httpx_mock(status_code=403, body="forbidden"),
    )
    with pytest.raises(ComboWriteError) as exc_info:
        client.rename_combo(combo_id="abc", new_name="renamed", current_models=["m1"])
    assert exc_info.value.kind == "http"
    assert exc_info.value.status_code == 403


def test_combo_write_error_message_for_ui_does_not_leak_base_url():
    """The UI message must not include self.base_url (loopback-only trust boundary)."""
    err = ComboWriteError("http", 500, "internal server error")
    msg = err.message_for_ui()
    assert "127.0.0.1" not in msg
    assert "9Router" in msg


def test_combo_write_error_message_for_ui_transport():
    err = ComboWriteError("transport", None, "connection refused")
    msg = err.message_for_ui()
    assert "Could not reach 9Router" in msg


def test_create_combo_success_returns_json_body(monkeypatch):
    client = _client_with_security(monkeypatch)
    monkeypatch.setattr(
        "core.router_client.httpx.Client",
        _httpx_mock(status_code=201, body='{"id":"new-id","name":"test","models":["m1"]}'),
    )
    result = client.create_combo(name="test", models=["m1"])
    assert result["id"] == "new-id"
    assert result["name"] == "test"
