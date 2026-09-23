"""Local control-plane boundary: offline transports and synthetic canaries only."""
import ast
import socket
from pathlib import Path
from unittest.mock import Mock

import httpx
import pytest

from core import router_client
from core.discovery import DiscoveredModel
from core.history import HealthCache
from core.probe import ScanMode, ScannerWorker
from core.router_client import RouterClient, validate_router_base_url
from tests.security.canaries import canary_access_token, canary_api_key, canary_password


SAFE_URLS = [
    "http://127.0.0.1:20128", "http://127.1.2.3:20128",
    "http://127.255.255.254:12345", "http://127.0.0.0:80",
    "http://[::1]:20128", "https://[::1]:12345",
    "http://127.0.0.1:1", "http://127.0.0.1:99999",
]
UNSAFE_URLS = [
    "http://192.168.1.1:20128", "http://10.0.0.1:20128",
    "http://172.16.0.1:20128", "http://8.8.8.8", "http://0.0.0.0:20128",
    "http://example.com", "https://attacker.example", "http://localhost:20128",
    "http://127.0.0.1.evil.example", "http://[::2]:20128",
    "http://user:pass@127.0.0.1:20128", "http://user@127.0.0.1",
    "http://[::ffff:127.0.0.1]", "http://[::1%25eth0]",
    "http://127.1", "http://2130706433", "http://0x7f000001",
    "http://127.000.000.001", "http://127.0.0.1.",
    "", "not a url", "//127.0.0.1", "ftp://127.0.0.1", "http:///missing",
    "http://[::1", "http://[::1]evil", "http://::1", "http://127.0.0.1:abc",
    "http://127.0.0.1:", "http://[::1]:", "http://127.0.0.1:１２３",
    "http://127.0.0.1:80:90", "http://127.0.0.1?host=evil.example",
    "http://127.0.0.1/#fragment", "http://127.0.0.1?", "http://127.0.0.1#",
    " http://127.0.0.1", "http://127.0.0.1\n", "http://127.0.\t0.1",
    "\x00http://127.0.0.1", "http://127.0.0.1/\\evil.example", None, 123,
]


def test_default_url():
    assert RouterClient().base_url == "http://127.0.0.1:20128"


@pytest.mark.parametrize("url", SAFE_URLS)
def test_loopback_urls(url):
    assert RouterClient(base_url=url).base_url == url


def test_normalization():
    assert validate_router_base_url("HTTP://[0:0:0:0:0:0:0:1]:20128/") == "http://[::1]:20128"
    assert validate_router_base_url("http://127.0.0.1:20128/local/") == "http://127.0.0.1:20128/local"


@pytest.mark.parametrize("url", UNSAFE_URLS)
def test_reject_before_any_secret_or_network_access(url, monkeypatch):
    traps = []
    # Restore global filesystem hooks before pytest formats any failure.
    with monkeypatch.context() as boundary_patch:
        def trap(owner, name):
            mock = Mock(side_effect=AssertionError("Forbidden access"))
            boundary_patch.setattr(owner, name, mock)
            traps.append(mock)

        for name in ("get_cli_token", "get_api_key", "_get_headers"):
            trap(RouterClient, name)
        for name in ("exists", "read_text", "open"):
            trap(Path, name)
        trap(router_client.sqlite3, "connect")
        trap(router_client, "get_default_security")
        trap(httpx, "Client")
        trap(httpx, "AsyncClient")
        trap(socket, "create_connection")
        trap(socket, "getaddrinfo")
        with pytest.raises(ValueError):
            RouterClient(base_url=url)
        for mock in traps:
            mock.assert_not_called()


def test_reassignment_cannot_bypass_boundary():
    client = RouterClient()
    with pytest.raises(ValueError):
        client.base_url = "https://attacker.example"
    assert client.base_url == "http://127.0.0.1:20128"
    client.base_url = "http://[::1]:12345"
    assert client.base_url == "http://[::1]:12345"


@pytest.fixture
def synthetic_auth(monkeypatch):
    monkeypatch.setattr(RouterClient, "get_cli_token", lambda self: canary_access_token())
    monkeypatch.setattr(RouterClient, "get_api_key", lambda self: canary_api_key())


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
@pytest.mark.parametrize("method", ["ping_model_fast", "probe_chat_completion"])
def test_sync_redirect_never_followed(status, method, monkeypatch, synthetic_auth):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(status, headers={"location": "https://attacker.example/collect"}, json={})

    real_client = httpx.Client

    def factory(**kwargs):
        assert kwargs["follow_redirects"] is False
        assert kwargs["trust_env"] is False
        return real_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(httpx, "Client", factory)
    result = getattr(RouterClient(), method)("test/model")
    assert result["status"] == status
    assert len(requests) == 1
    assert requests[0].url.host == "127.0.0.1"
    assert requests[0].headers["x-9r-cli-token"] == canary_access_token()
    if method == "probe_chat_completion":
        assert requests[0].headers["authorization"] == f"Bearer {canary_api_key()}"


@pytest.mark.parametrize("url", ["http://127.0.0.1:20128", "http://[::1]:20128", "http://127.0.0.1:99999"])
@pytest.mark.parametrize("status", [200, 301, 302, 303, 307, 308])
def test_worker_loopback_and_redirects(url, status, tmp_path, monkeypatch, synthetic_auth):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(status, headers={"location": "https://attacker.example/collect"}, json={"ok": True})

    real_client = httpx.AsyncClient

    def factory(**kwargs):
        assert kwargs["follow_redirects"] is False
        assert kwargs["trust_env"] is False
        return real_client(**kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    cache = HealthCache(cache_file=tmp_path / "cache.json")
    worker = ScannerWorker(RouterClient(base_url=url), cache, transport=httpx.MockTransport(handler))
    model = DiscoveredModel(canonical_id="test/model", provider_name="test", provider_prefix="test",
                            connection_id="test-connection", model_id="model", display_name="model")
    completed = []
    worker.on_scan_completed = completed.append
    worker.run_scan([model], mode=ScanMode.QUICK)
    assert completed == ["COMPLETED"]
    assert len(requests) == 1
    assert requests[0].url.host in ("127.0.0.1", "::1")
    assert requests[0].headers["x-9r-cli-token"] == canary_access_token()
    assert cache.get(model.canonical_id) is not None
    if status == 200:
        assert cache.get(model.canonical_id).is_healthy()


def test_environment_proxy_not_consulted(monkeypatch, synthetic_auth):
    # Exercise real httpx construction, but intercept send before any sockets.
    import httpx._client

    env_lookup = Mock(side_effect=AssertionError("Environment proxy consulted"))
    monkeypatch.setattr(httpx._client, "get_environment_proxies", env_lookup)
    monkeypatch.setenv("HTTP_PROXY", "http://attacker.example:8080")
    monkeypatch.setenv("HTTPS_PROXY", "http://attacker.example:8080")
    monkeypatch.setenv("ALL_PROXY", "http://attacker.example:8080")
    monkeypatch.setenv("NO_PROXY", "")
    monkeypatch.setattr(httpx.Client, "send", lambda self, request, **kw: httpx.Response(200, json={}, request=request))
    assert RouterClient().probe_chat_completion("test/model")["status"] == 200
    env_lookup.assert_not_called()


def test_every_sync_client_has_explicit_transport_boundary():
    tree = ast.parse(Path(router_client.__file__).read_text(encoding="utf-8"))
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
             and isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name)
             and node.func.value.id == "httpx" and node.func.attr == "Client"]
    assert calls
    for call in calls:
        flags = {kw.arg: ast.literal_eval(kw.value) for kw in call.keywords
                 if kw.arg in ("follow_redirects", "trust_env")}
        assert flags == {"follow_redirects": False, "trust_env": False}


@pytest.mark.parametrize("url", ["https://attacker.example", "http://localhost:20128", "http://[broken"])
def test_cli_rejection_is_clean(url, monkeypatch, capsys):
    import run

    monkeypatch.setattr(run.sys, "argv", ["watchedit", "--cli", "--router-url", url])
    startup = Mock(side_effect=AssertionError("CLI started"))
    monkeypatch.setattr(run, "run_cli_mode", startup)
    with pytest.raises(SystemExit) as exc:
        run.main()
    assert exc.value.code == 2
    output = capsys.readouterr().err
    assert "--router-url:" in output
    assert "Traceback" not in output
    assert url not in output
    startup.assert_not_called()


def test_cli_userinfo_not_echoed(monkeypatch, capsys):
    url = f"http://user:{canary_password()}@127.0.0.1:20128"
    test_cli_rejection_is_clean(url, monkeypatch, capsys)


@pytest.mark.parametrize("url", SAFE_URLS)
def test_cli_custom_local_ports(url, monkeypatch):
    import run

    monkeypatch.setattr(run.sys, "argv", ["watchedit", "--cli", "--router-url", url])
    startup = Mock(return_value=0)
    monkeypatch.setattr(run, "run_cli_mode", startup)
    with pytest.raises(SystemExit):
        run.main()
    assert startup.call_args.args[0].router_url == url
