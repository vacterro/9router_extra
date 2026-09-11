"""
CORE-001 regression: scoped KV model discovery.

Proves that ModelDiscovery honors the SQLite `kv` table's semantic `scope`
column:
  * customModels -> positive configured inventory (alias rows usable even when
    the alias is not a provider-node id, e.g. `oc`).
  * disabledModels -> exclusion metadata ONLY (never configured, never a
    routing candidate, never a scan/smart-add candidate).
  * unknown scopes -> ignored for discovery.

Fixtures are derived from tests/fixtures/provider_state_sanitized.json, the
sanitized real-state dump cited by audit/2.md CORE-001.
"""
import json
import os

from core.discovery import ModelDiscovery
from core.router_client import RouterClient


FIXTURE = os.path.join(
    os.path.dirname(__file__),
    "fixtures",
    "provider_state_sanitized.json",
)


def _load_fixture_client():
    with open(FIXTURE, "r", encoding="utf-8") as fh:
        state = json.load(fh)

    client = RouterClient(base_url="http://127.0.0.1:9")

    # Connections: expose providerSpecificData as a dict as discover_all expects.
    conns = []
    for c in state.get("providerConnections", []):
        data = c.get("data")
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except Exception:
                data = {}
        psd = (data or {}).get("providerSpecificData", {}) or {}
        conns.append({
            "id": c.get("id"),
            "provider": c.get("provider", ""),
            "name": c.get("name", ""),
            "isActive": bool(c.get("isActive", 1)),
            "providerSpecificData": psd,
        })
    client.get_providers = lambda: conns

    # Nodes: discover_all parses `data` (string or dict) and reads `prefix`.
    nodes = []
    for n in state.get("providerNodes", []):
        data = n.get("data")
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except Exception:
                data = {}
        nodes.append({
            "id": n.get("id"),
            "name": n.get("name", ""),
            "prefix": n.get("prefix") or (data or {}).get("prefix", ""),
            "data": data,
            "type": n.get("type", ""),
        })
    client.get_provider_nodes = lambda: nodes

    # kv rows preserving the semantic scope discriminator.
    client.get_kv_scoped = lambda: [
        (r.get("scope", ""), r.get("key", ""), r.get("value", ""))
        for r in state.get("kv", [])
    ]
    client.get_catalog_models = lambda: []
    combos = []
    for c in state.get("combos", []):
        cmodels = c.get("models")
        if isinstance(cmodels, str):
            try:
                cmodels = json.loads(cmodels)
            except Exception:
                cmodels = []
        combos.append({
            "id": c.get("id"),
            "name": c.get("name", ""),
            "models": cmodels or [],
        })
    client.get_combos = lambda: combos

    return client, state


# The five authoritative `oc` customModels rows from audit/2.md CORE-001.
OC_CUSTOM_MODELS = [
    "oc/muse-spark-1.2-contributor-free",
    "oc/hy3-free",
    "oc/deepseek-v4-flash-free",
    "oc/mimo-v2.5-free",
    "oc/muse-spark-1.3-contributor-free",
]


def _discover():
    client, state = _load_fixture_client()
    discovery = ModelDiscovery(client)
    models = discovery.discover_all(include_combo_models=True, query_live=False)
    return discovery, models, {m.canonical_id: m for m in models}, state


def test_A_oc_custommodels_discovered_without_provider_node_id():
    _, models, by_id, _ = _discover()
    # Requirement 5/6: valid oc customModels rows remain addressable as oc/<model>
    # even though `oc` is not a provider-node id.
    for cid in OC_CUSTOM_MODELS:
        assert cid in by_id, f"{cid} missing from discovered inventory"
        m = by_id[cid]
        assert m.configured is True, f"{cid} should be configured"
        assert m.provider_prefix == "oc", f"{cid} prefix should be oc, got {m.provider_prefix}"
        assert m.source in ("configured_node", "configured_kv")


def test_B_disabledModels_ocg_not_configured_inventory():
    _, models, by_id, state = _discover()
    # The fixture's disabledModels/ocg contains 26 model names (JSON list).
    ocg = next(r for r in state["kv"] if r.get("scope") == "disabledModels" and r.get("key") == "ocg")
    disabled_names = json.loads(ocg["value"])
    assert len(disabled_names) >= 26
    for nm in disabled_names:
        cid = f"ocg/{nm}"
        # Requirement 4: a disabledModels row alone must NEVER create a
        # configured row. These rows exist nowhere in a positive source in the
        # fixture, so they must be entirely absent.
        assert cid not in by_id, f"{cid} must not be promoted from disabledModels to inventory"


def test_C_disabled_only_model_cannot_enter_routing():
    discovery, models, by_id, state = _discover()
    ocg = next(r for r in state["kv"] if r.get("scope") == "disabledModels" and r.get("key") == "ocg")
    disabled_names = json.loads(ocg["value"])
    # Disabled-only models absent from inventory cannot be routing candidates.
    absent = [f"ocg/{nm}" for nm in disabled_names if f"ocg/{nm}" not in by_id]
    assert absent, "fixture should contain disabled-only ocg models to test"
    routing = discovery.active_routing_models(models)
    routing_ids = {m.canonical_id for m in routing}
    assert not (set(absent) & routing_ids), "disabled-only models leaked into active routing"


def test_D_malformed_duplicate_combo_only_deterministic():
    client, state = _load_fixture_client()
    extra = [
        # Alias-backed customModels row (providerAlias not a node id).
        ("customModels", "abc|X-1|llm", '{"providerAlias":"abc","id":"X-1","type":"llm","name":"X-1"}'),
        # Malformed customModels JSON: must be skipped, no crash.
        ("customModels", "bad|Y|llm", "not-json{"),
        # Duplicate of the first alias row: must merge, not double-register.
        ("customModels", "abc|X-1|llm", '{"providerAlias":"abc","id":"X-1","type":"llm","name":"X-1"}'),
        # Unknown scope: ignored for discovery.
        ("mysteryScope", "zzz", '["should-not-appear"]'),
    ]
    client.get_kv_scoped = lambda: [
        (r.get("scope", ""), r.get("key", ""), r.get("value", "")) for r in state["kv"]
    ] + extra
    client.get_combos = lambda: [{"id": "c", "name": "C", "models": ["ghost/only-combo"]}]

    discovery = ModelDiscovery(client)
    models = discovery.discover_all(include_combo_models=True, query_live=False)
    by_id = {m.canonical_id: m for m in models}

    # Alias row discovered, configured, namespaced by the stored alias.
    assert by_id["abc/X-1"].configured is True
    assert by_id["abc/X-1"].provider_prefix == "abc"
    # Only one canonical entry despite the duplicate kv row.
    assert sum(1 for m in models if m.canonical_id == "abc/X-1") == 1
    # Malformed JSON did not create a row.
    assert "bad/Y" not in by_id
    # Unknown scope ignored.
    assert "zzz" not in by_id
    # Combo-only unknown model remains inspectable but not configured. Its
    # routing_eligible stays True, preserving existing combo scan behavior.
    assert by_id["ghost/only-combo"].is_combo_member is True
    assert by_id["ghost/only-combo"].configured is False
    assert by_id["ghost/only-combo"].routing_eligible is True


def test_E_existing_configured_provider_discovery_still_works():
    _, models, by_id, state = _discover()
    # node-id customModels rows (e.g. openrouter|...) must still resolve via
    # the provider node id mapping.
    or_rows = [r for r in state["kv"] if r.get("scope") == "customModels" and r.get("key", "").startswith("openrouter|")]
    assert or_rows, "fixture should contain openrouter customModels rows"
    node_id = or_rows[0]["key"].split("|")[0]
    # The node id must map to a discovered configured row (canonical id carries
    # the resolved provider prefix from observed mappings).
    prefix = or_rows[0]["value"]
    try:
        prefix = (json.loads(prefix) or {}).get("providerAlias") or node_id
    except Exception:
        pass
    matches = [m for m in models if m.configured and m.canonical_id.startswith(f"{prefix}/")]
    assert matches, f"node-id customModels {node_id} not discovered"


def test_G_disabled_combo_member_visible_not_configured_not_routable():
    client, state = _load_fixture_client()
    # `ocg/grok-4.6` is in disabledModels/ocg (exclusion metadata). Wire a combo
    # that references it to prove combo visibility persists while configured /
    # routing flags stay correct.
    client.get_combos = lambda: [
        {"id": "c1", "name": "C1", "models": ["ocg/grok-4.6", "oc/muse-spark-1.2-contributor-free"]},
    ]
    discovery = ModelDiscovery(client)
    models = discovery.discover_all(include_combo_models=True, query_live=False)
    by_id = {m.canonical_id: m for m in models}

    m = by_id["ocg/grok-4.6"]
    assert m.is_combo_member is True, "disabled combo member must stay inspectable"
    assert m.configured is False, "disabledModels must not create configured inventory"
    assert m.routing_eligible is False, "disabled combo member must not be routable"
    assert m.routing_exclusion_reason == "disabledModels"
    # At least one combo member is retained for inspection (combo visibility).
    combo_members = [m for m in models if m.is_combo_member]
    assert combo_members, "combo members must remain inspectable"
