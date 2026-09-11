"""
CORE-003: live catalog schema validation and atomic discovery.

The live provider models endpoint must only publish authoritative catalog
evidence from a fully schema-valid HTTP 200 payload. Invalid payloads yield a
distinct INVALID outcome that behaves like unavailable discovery: no
EMPTY_MODEL_CATALOG, no routing exclusion, no advertised_live=False negative
evidence. A genuine empty list is the only authoritative empty catalog.
"""
import json

import httpx
import pytest

from core import router_client
from core.classification import CatalogState
from core.discovery import ModelDiscovery
from core.router_client import RouterClient


# -------------------------------------------------------------
# Hermetic RouterClient over httpx.MockTransport
# -------------------------------------------------------------
def _install_transport(monkeypatch, handler):
    transport = httpx.MockTransport(handler)
    real_client = httpx.Client

    def factory(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr(router_client.httpx, "Client", factory)


def _offline_client(monkeypatch, tmp_path):
    machine = tmp_path / "machine-id"
    secret = tmp_path / "cli-secret"
    machine.write_text("core003-machine", encoding="utf-8")
    secret.write_text("core003-secret", encoding="utf-8")
    monkeypatch.setattr(router_client, "MACHINE_ID_FILE", machine)
    monkeypatch.setattr(router_client, "CLI_SECRET_FILE", secret)
    return RouterClient(base_url="http://127.0.0.1:99999", db_path=tmp_path / "absent.sqlite")


def _json_handler(payload, status=200, raw=None):
    def handler(request: httpx.Request) -> httpx.Response:
        if raw is not None:
            return httpx.Response(status, content=raw)
        return httpx.Response(status, content=json.dumps(payload).encode("utf-8"))

    return handler


# -------------------------------------------------------------
# A. Genuine empty catalog is the only authoritative emptiness
# -------------------------------------------------------------
def test_a_genuine_empty_catalog_is_ok_and_empty(monkeypatch, tmp_path):
    _install_transport(monkeypatch, _json_handler({"models": []}))
    client = _offline_client(monkeypatch, tmp_path)
    status, models = client.get_connection_live_models_detailed("conn-1")
    assert status == "OK"
    assert models == []


# -------------------------------------------------------------
# B-E. Schema-invalid payloads must never produce OK
# -------------------------------------------------------------
@pytest.mark.parametrize(
    "payload",
    [
        {"models": None},          # B: null models
        {},                        # C: missing models key
        {"models": "x"},           # D: wrong type
        {"models": 123},           # D: wrong type
        {"models": {}},            # D: wrong container
        [],                        # E: top-level list
        None,                      # E: top-level null
        "foo",                     # E: top-level string
        {"Models": []},            # near-miss key casing is not the contract
    ],
)
def test_be_invalid_payloads_rejected(monkeypatch, tmp_path, payload):
    _install_transport(monkeypatch, _json_handler(payload))
    client = _offline_client(monkeypatch, tmp_path)
    status, models = client.get_connection_live_models_detailed("conn-1")
    assert status == "INVALID"
    assert models == []


def test_f_malformed_json_body_rejected(monkeypatch, tmp_path):
    _install_transport(monkeypatch, _json_handler(None, raw=b"{not json"))
    client = _offline_client(monkeypatch, tmp_path)
    status, models = client.get_connection_live_models_detailed("conn-1")
    assert status == "INVALID"
    assert models == []


# -------------------------------------------------------------
# G/H. Atomic row contract: one bad row rejects the whole catalog
# -------------------------------------------------------------
@pytest.mark.parametrize(
    "rows",
    [
        [{"id": "good"}, None],    # G: valid row followed by null
        [None, {"id": "good"}],    # G: malformed row first
        [{}],                      # H: no identity
        [{"id": ""}],              # H: empty id
        [{"id": None}],            # H: null id
        [{"id": "   "}],           # H: whitespace-only id
        [{"name": ""}],            # H: empty name and no id
        [{"id": 123}],             # H: non-string id
    ],
)
def test_gh_malformed_row_rejects_whole_catalog(monkeypatch, tmp_path, rows):
    _install_transport(monkeypatch, _json_handler({"models": rows}))
    client = _offline_client(monkeypatch, tmp_path)
    status, models = client.get_connection_live_models_detailed("conn-1")
    assert status == "INVALID"
    assert models == []


def test_row_identity_via_name_is_accepted(monkeypatch, tmp_path):
    _install_transport(monkeypatch, _json_handler({"models": [{"name": "only-name"}]}))
    client = _offline_client(monkeypatch, tmp_path)
    status, models = client.get_connection_live_models_detailed("conn-1")
    assert status == "OK"
    assert models == [{"name": "only-name"}]


# -------------------------------------------------------------
# I. Valid non-empty catalog unchanged
# -------------------------------------------------------------
def test_i_valid_nonempty_catalog(monkeypatch, tmp_path):
    _install_transport(monkeypatch, _json_handler({"models": [{"id": "x/a"}, {"id": "x/b"}]}))
    client = _offline_client(monkeypatch, tmp_path)
    status, models = client.get_connection_live_models_detailed("conn-1")
    assert status == "OK"
    assert [m["id"] for m in models] == ["x/a", "x/b"]


# -------------------------------------------------------------
# K. Existing statuses unchanged
# -------------------------------------------------------------
@pytest.mark.parametrize("code", [404, 405, 501])
def test_k_not_supported_statuses(monkeypatch, tmp_path, code):
    _install_transport(monkeypatch, _json_handler({"error": "no"}, status=code))
    client = _offline_client(monkeypatch, tmp_path)
    status, models = client.get_connection_live_models_detailed("conn-1")
    assert status == "NOT_SUPPORTED"
    assert models == []


def test_k_other_http_error_is_failed(monkeypatch, tmp_path):
    _install_transport(monkeypatch, _json_handler({"error": "boom"}, status=500))
    client = _offline_client(monkeypatch, tmp_path)
    status, models = client.get_connection_live_models_detailed("conn-1")
    assert status == "FAILED"
    assert models == []


def test_k_timeout_status(monkeypatch, tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    _install_transport(monkeypatch, handler)
    client = _offline_client(monkeypatch, tmp_path)
    status, models = client.get_connection_live_models_detailed("conn-1")
    assert status == "TIMEOUT"
    assert models == []


def test_get_connection_live_models_returns_list_only_on_ok(monkeypatch, tmp_path):
    payloads = [({"models": []}, []), ({"models": None}, [])]
    for payload, expected in payloads:
        _install_transport(monkeypatch, _json_handler(payload))
        client = _offline_client(monkeypatch, tmp_path)
        assert client.get_connection_live_models("conn-1") == expected


# -------------------------------------------------------------
# Discovery-level mapping: INVALID behaves like unavailable discovery
# -------------------------------------------------------------
def _discovery_with(monkeypatch, live_by_cid):
    client = RouterClient(base_url="http://127.0.0.1:99999")
    client.get_providers = lambda: [
        {"id": cid, "name": cid, "provider": f"prov-{cid}", "isActive": True,
         "providerSpecificData": {"prefix": prefix}}
        for cid, (_live, prefix) in live_by_cid.items()
    ]
    client.get_provider_nodes = lambda: []
    client.get_kv_scoped = lambda: [
        ("customModels", prefix, json.dumps(["known-model"]))
        for _live, prefix in live_by_cid.values()
    ]
    client.get_catalog_models = lambda: []
    client.get_combos = lambda: []
    client.get_connection_live_models_detailed = lambda cid: live_by_cid[cid][0]
    return ModelDiscovery(client)


def test_b_invalid_payload_is_discovery_unavailable_not_empty(monkeypatch):
    discovery = _discovery_with(monkeypatch, {"conn-1": (("INVALID", []), "p1")})
    models = discovery.discover_all(query_live=True)
    by_cid = {m.canonical_id: m for m in models}

    assert discovery.live_outcomes["conn-1"] == "INVALID"
    assert discovery.catalog_states["conn-1"] == CatalogState.DISCOVERY_UNAVAILABLE
    assert "conn-1" not in discovery.routing_excluded_connections
    assert "conn-1" not in discovery.catalog_model_counts
    # No negative evidence: configured model stays eligible, unknown liveness
    assert by_cid["p1/known-model"].advertised_live is None
    assert by_cid["p1/known-model"].routing_eligible is True
    assert by_cid["p1/known-model"] in discovery.active_routing_models(models)


@pytest.mark.parametrize("fake_models", [None, [{"id": "good"}, None], [{"id": ""}], {}])
def test_ok_contract_violations_downgraded_to_invalid(monkeypatch, fake_models):
    # Defense in depth: a client violating the OK contract (fake, bug, or
    # future regression) can never publish catalog evidence from it.
    discovery = _discovery_with(monkeypatch, {"conn-1": (("OK", fake_models), "p1")})
    models = discovery.discover_all(query_live=True)
    by_cid = {m.canonical_id: m for m in models}

    assert discovery.live_outcomes["conn-1"] == "INVALID"
    assert discovery.catalog_states["conn-1"] == CatalogState.DISCOVERY_UNAVAILABLE
    assert "conn-1" not in discovery.routing_excluded_connections
    # No partial merge of the 'good' row
    assert "p1/good" not in by_cid
    assert by_cid["p1/known-model"].advertised_live is None
    assert by_cid["p1/known-model"].routing_eligible is True


def test_empty_catalog_keeps_negative_evidence(monkeypatch):
    discovery = _discovery_with(monkeypatch, {"conn-1": (("OK", []), "p1")})
    models = discovery.discover_all(query_live=True)
    by_cid = {m.canonical_id: m for m in models}

    assert discovery.live_outcomes["conn-1"] == "OK"
    assert discovery.catalog_states["conn-1"] == CatalogState.EMPTY_MODEL_CATALOG
    assert discovery.catalog_model_counts["conn-1"] == 0
    assert "conn-1" in discovery.routing_excluded_connections
    assert by_cid["p1/known-model"].advertised_live is False
    assert by_cid["p1/known-model"].routing_eligible is False
    assert by_cid["p1/known-model"].routing_exclusion_reason == CatalogState.EMPTY_MODEL_CATALOG.value
    # Inventory retained for inspection/re-probe
    assert by_cid["p1/known-model"] in models


def test_j_recovery_from_genuine_empty_via_valid_nonempty(monkeypatch):
    # Pass 1: genuine empty -> excluded. Pass 2: valid non-empty -> restored.
    live = {"conn-1": (("OK", []), "p1")}
    discovery = _discovery_with(monkeypatch, live)
    discovery.discover_all(query_live=True)
    assert "conn-1" in discovery.routing_excluded_connections

    live["conn-1"] = (("OK", [{"id": "p1/known-model"}]), "p1")
    models = discovery.discover_all(query_live=True)
    by_cid = {m.canonical_id: m for m in models}

    assert discovery.catalog_states["conn-1"] == CatalogState.MODELS_AVAILABLE
    assert discovery.catalog_model_counts["conn-1"] == 1
    assert "conn-1" not in discovery.routing_excluded_connections
    assert by_cid["p1/known-model"].advertised_live is True
    assert by_cid["p1/known-model"].routing_eligible is True
    assert by_cid["p1/known-model"] in discovery.active_routing_models(models)


def test_invalid_does_not_break_later_ok_connection(monkeypatch):
    # One invalid connection must not poison a concurrent valid one.
    discovery = _discovery_with(
        monkeypatch,
        {
            "conn-bad": (("INVALID", []), "pb"),
            "conn-good": (("OK", [{"id": "pg/live-1"}]), "pg"),
        },
    )
    models = discovery.discover_all(query_live=True)
    by_cid = {m.canonical_id: m for m in models}

    assert discovery.live_outcomes["conn-bad"] == "INVALID"
    assert discovery.catalog_states["conn-bad"] == CatalogState.DISCOVERY_UNAVAILABLE
    assert discovery.catalog_states["conn-good"] == CatalogState.MODELS_AVAILABLE
    assert discovery.catalog_model_counts["conn-good"] == 1
    assert by_cid["pg/live-1"].advertised_live is True
    assert by_cid["pg/live-1"].routing_eligible is True
    assert by_cid["pb/known-model"].advertised_live is None
    assert by_cid["pb/known-model"].routing_eligible is True
