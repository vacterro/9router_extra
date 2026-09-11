"""
9router_WatchEdit - Provider & Model Discovery Engine
Authoritative discovery across all sources:
1. Configured provider nodes (AMD, NIM, Kilo, B.AI, etc. stored in 9Router kv table & catalog)
2. Connected native/OAuth providers (OpenAI, DeepSeek, Antigravity, OpenRouter, etc.)
3. Supplemental catalog metadata (/api/models)
4. Active combo references
5. Optional live connection probing (non-blocking)
"""
import json
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set, Tuple

from core.router_client import RouterClient
from core.classification import CatalogState
from core.provider_profiles import get_provider_preset, model_cost_hint

@dataclass
class DiscoveredModel:
    canonical_id: str
    provider_name: str
    provider_prefix: str
    connection_id: str
    model_id: str
    display_name: str
    is_combo_member: bool = False
    source: str = "configured_node"
    configured: bool = False
    catalog: bool = False
    combo: bool = False
    advertised_live: Optional[bool] = None
    # False means this model must not be used for active routing. The model
    # remains in the inventory so a later live discovery can restore it.
    routing_eligible: bool = True
    routing_exclusion_reason: str = ""
    # Explicit catalogue billing metadata, when the provider publishes it.
    # None means unknown and must remain USE/? rather than becoming FREE.
    cost_hint: Optional[str] = None

class ModelDiscovery:
    # Per-connection live discovery outcome codes (P1-5)
    LIVE_OK = "OK"
    LIVE_FAILED = "FAILED"
    LIVE_TIMEOUT = "TIMEOUT"
    LIVE_NOT_SUPPORTED = "NOT_SUPPORTED"
    # HTTP 200 whose body violates the live catalog schema. Distinct from
    # FAILED (transport) and from OK-with-empty-list: invalid payloads are
    # unavailable discovery, never authoritative emptiness (CORE-003).
    LIVE_INVALID = "INVALID"

    @staticmethod
    def _valid_live_catalog_rows(live_models: Any) -> bool:
        """Catalog row contract, enforced atomically before any mutation.

        Every row must be an object carrying a usable non-empty string `id`
        or `name`. One violating row rejects the whole catalog: valid rows are
        never partially merged ahead of a malformed one (CORE-003).
        """
        if not isinstance(live_models, list):
            return False
        for row in live_models:
            if not isinstance(row, dict):
                return False
            row_id = row.get("id")
            row_name = row.get("name")
            has_id = isinstance(row_id, str) and row_id.strip()
            has_name = isinstance(row_name, str) and row_name.strip()
            if not has_id and not has_name:
                return False
        return True

    def __init__(self, client: RouterClient):
        self.client = client
        # connection_id -> outcome of last live discovery pass
        # (OK / FAILED / TIMEOUT / NOT_SUPPORTED / INVALID). An empty
        # successful model list is LIVE_OK and is distinct from a failed or
        # schema-invalid discovery.
        self.live_outcomes: Dict[str, str] = {}
        # Independent provider catalog state from the last live discovery.
        self.catalog_states: Dict[str, CatalogState] = {}
        self.catalog_model_counts: Dict[str, int] = {}
        self.routing_excluded_connections: Set[str] = set()

    def discover_all(self, include_combo_models: bool = True, query_live: bool = False) -> List[DiscoveredModel]:
        """
        Discovers authoritative model inventory from all configured providers & nodes.
        Guarantees that models for offline/rate-limited/timing-out providers (like AMD, NIM)
        are fully discovered and knockable.
        """
        models_map: Dict[str, DiscoveredModel] = {}
        self.live_outcomes.clear()
        self.catalog_states.clear()
        self.catalog_model_counts.clear()
        self.routing_excluded_connections.clear()

        # 1. Fetch provider connections and provider nodes
        connections = self.client.get_providers()
        nodes = self.client.get_provider_nodes()

        node_by_id: Dict[str, Dict[str, str]] = {}
        prefix_to_info: Dict[str, Dict[str, str]] = {}

        for node in nodes:
            nid = node.get("id", "")
            nname = node.get("name") or nid
            data = node.get("data") or {}
            if isinstance(data, str):
                try:
                    data = json.loads(data)
                except Exception:
                    data = {}
            nprefix = data.get("prefix") or node.get("prefix") or ""
            node_by_id[nid] = {"name": nname, "prefix": nprefix, "id": nid}
            if nprefix:
                prefix_to_info[nprefix.lower()] = node_by_id[nid]

        conn_providers: Set[str] = set()
        for conn in connections:
            cid = conn.get("id", "")
            cname = conn.get("name") or cid
            cprov = conn.get("provider", "").lower()
            data = conn.get("providerSpecificData") or {}
            cprefix = data.get("prefix", "").strip()

            if not cprefix:
                # Standard built-in provider prefix defaults
                if cprov in ("antigravity", "gemini-cli"):
                    cprefix = "ag"
                elif cprov == "workbuddy":
                    cprefix = "wb"
                elif cprov in ("cline", "clinepass"):
                    cprefix = "cl"
                elif cprov == "kiro":
                    cprefix = "kgw"
                elif cprov == "codex":
                    cprefix = "cx"
                elif cprov == "cloudflare-workers-ai":
                    cprefix = "cloudflare-ai"
                else:
                    cprefix = cprov

            # A first-class preset has a stable public prefix even when the
            # engine returns a provider-specific connection id. This keeps
            # local namespaces predictable (kira-ai/model-id).
            preset = get_provider_preset(cprov)
            if preset and not data.get("prefix"):
                cprefix = preset.id

            conn_providers.add(cprov)
            if cprefix:
                p_entry = {
                    "name": cname,
                    "prefix": cprefix,
                    "id": cid,
                }
                prefix_to_info.setdefault(cprefix.lower(), p_entry)
                prefix_to_info.setdefault(cprov.lower(), p_entry)

        # 2. Extract models from 9Router SQLite kv table (configured custom
        #    node & provider models). The kv table carries a semantic `scope`
        #    discriminator that MUST be honored at this boundary:
        #      * customModels  -> positive authoritative configured-model source
        #      * disabledModels -> exclusion metadata ONLY (never configured)
        #      * any other scope -> ignored for discovery
        # Without the scope, disabled alias lists were promoted to configured
        # inventory and valid customModels aliases that are not provider-node
        # ids were silently dropped (CORE-001).
        kv_rows = self.client.get_kv_scoped()
        # Canonical ids excluded by `disabledModels` (exclusion metadata only).
        disabled_ids: Set[str] = set()
        for scope, k, v in kv_rows:
            if scope == "disabledModels":
                try:
                    names = json.loads(v)
                    if isinstance(names, list):
                        for nm in names:
                            if isinstance(nm, str) and nm.strip():
                                disabled_ids.add(self._canon_for_alias(k, nm.strip()))
                except Exception:
                    pass
                continue
            if scope != "customModels":
                # Unknown/other scope: do not infer configured inventory.
                continue

            if "|" in k:
                # Custom node format: <node_id>|<model_id>|llm where the value
                # JSON carries the semantic providerAlias/id/type/name.
                parts = k.split("|")
                p_id = parts[0]
                m_id = parts[1] if len(parts) > 1 else ""

                alias = None
                sem_id = m_id
                try:
                    meta = json.loads(v) if v else {}
                except Exception:
                    meta = {}
                if isinstance(meta, dict):
                    alias = meta.get("providerAlias")
                    if meta.get("id"):
                        sem_id = meta["id"]
                if not alias and p_id not in node_by_id:
                    # No stored alias and the key node id is not an observed
                    # provider node (or the row JSON is malformed): the row
                    # cannot be attributed to a configured provider. Skip
                    # deterministically instead of guessing (CORE-001 req 5).
                    continue
                # Parse the semantic id rather than requiring the pipe node id
                # to be a known provider-node id (CORE-001 req 5/6).
                namespace = alias or p_id
                model_id = sem_id or m_id

                name, prefix, cid = self._resolve_provider(namespace, node_by_id, prefix_to_info)
                if prefix:
                    if model_id.startswith(f"{prefix}/"):
                        canon_id = model_id
                        clean_mid = model_id[len(prefix) + 1:]
                    else:
                        canon_id = f"{prefix}/{model_id}"
                        clean_mid = model_id
                elif namespace:
                    canon_id = f"{namespace}/{model_id}" if model_id else namespace
                    clean_mid = model_id
                else:
                    canon_id = model_id
                    clean_mid = model_id
                self._add_configured(models_map, canon_id, name, prefix, cid, clean_mid, "configured_node")
            else:
                # Legacy alias list form: key = provider alias, value = JSON
                # list of model ids. Treated as a positive customModels source.
                try:
                    val_json = json.loads(v)
                    if isinstance(val_json, list) and len(val_json) > 0 and isinstance(val_json[0], str):
                        alias = k
                        name, prefix, cid = self._resolve_provider(alias, node_by_id, prefix_to_info)
                        for m_id in val_json:
                            if not isinstance(m_id, str) or not m_id.strip():
                                continue
                            m_id = m_id.strip()
                            if prefix:
                                if m_id.startswith(f"{prefix}/"):
                                    canon_id = m_id
                                    clean_mid = m_id[len(prefix) + 1:]
                                else:
                                    canon_id = f"{prefix}/{m_id}"
                                    clean_mid = m_id
                            else:
                                canon_id = f"{alias}/{m_id}" if m_id else alias
                                clean_mid = m_id
                            self._add_configured(models_map, canon_id, name, prefix, cid, clean_mid, "configured_kv")
                except Exception:
                    pass

        # 3. Supplemental catalog metadata (/api/models)
        catalog_models = self.client.get_catalog_models()
        for cm in catalog_models:
            c_prov = cm.get("provider", "")
            routed = cm.get("routedModel") or cm.get("fullModel") or ""
            m_name = cm.get("model") or ""
            catalog_cost_hint = model_cost_hint(cm)

            if c_prov in node_by_id:
                ninfo = node_by_id[c_prov]
                prefix = ninfo["prefix"]
                if prefix:
                    canon_id = m_name if m_name.startswith(f"{prefix}/") else f"{prefix}/{m_name}"
                    clean_mid = m_name[len(prefix) + 1:] if m_name.startswith(f"{prefix}/") else m_name
                else:
                    canon_id = routed or m_name
                    clean_mid = m_name

                if canon_id not in models_map:
                    models_map[canon_id] = DiscoveredModel(
                        canonical_id=canon_id,
                        provider_name=ninfo["name"],
                        provider_prefix=prefix,
                        connection_id=c_prov,
                        model_id=clean_mid,
                        display_name=cm.get("name") or clean_mid,
                        is_combo_member=False,
                        source="catalog",
                        catalog=True,
                        cost_hint=catalog_cost_hint,
                    )
                else:
                    models_map[canon_id].catalog = True
                    if catalog_cost_hint:
                        models_map[canon_id].cost_hint = catalog_cost_hint
                    if cm.get("name"):
                        models_map[canon_id].display_name = cm["name"]
            elif c_prov in conn_providers or c_prov.lower() in prefix_to_info:
                pinfo = prefix_to_info.get(c_prov.lower(), {"name": c_prov, "prefix": c_prov, "id": ""})
                preset = get_provider_preset(c_prov)
                if preset:
                    pinfo = {"name": preset.display_name, "prefix": preset.id, "id": pinfo.get("id", "")}
                prefix = pinfo["prefix"]
                canon_id = routed or (f"{prefix}/{m_name}" if not m_name.startswith(f"{prefix}/") else m_name)
                clean_mid = m_name

                if canon_id not in models_map:
                    models_map[canon_id] = DiscoveredModel(
                        canonical_id=canon_id,
                        provider_name=pinfo["name"],
                        provider_prefix=prefix,
                        connection_id=pinfo.get("id", ""),
                        model_id=clean_mid,
                        display_name=cm.get("name") or clean_mid,
                        is_combo_member=False,
                        source="catalog",
                        catalog=True,
                        cost_hint=catalog_cost_hint,
                    )
                else:
                    models_map[canon_id].catalog = True
                    if catalog_cost_hint:
                        models_map[canon_id].cost_hint = catalog_cost_hint
                    if cm.get("name"):
                        models_map[canon_id].display_name = cm["name"]

        # 4. Incorporate Active Combos (ensure every combo model exists in inventory)
        combos = self.client.get_combos()
        for combo in combos:
            for item in combo.get("models", []):
                if not isinstance(item, str) or not item.strip():
                    continue
                canonical_id = item.strip()
                if canonical_id in models_map:
                    models_map[canonical_id].combo = True
                    models_map[canonical_id].is_combo_member = True
                elif include_combo_models:
                    prefix = canonical_id.split("/")[0] if "/" in canonical_id else ""
                    model_id = canonical_id.split("/", 1)[1] if "/" in canonical_id else canonical_id
                    pinfo = prefix_to_info.get(prefix.lower(), {"name": prefix, "id": "", "prefix": prefix})
                    models_map[canonical_id] = DiscoveredModel(
                        canonical_id=canonical_id,
                        provider_name=pinfo["name"],
                        provider_prefix=prefix,
                        connection_id=pinfo["id"],
                        model_id=model_id,
                        display_name=model_id,
                        is_combo_member=True,
                        source="combo",
                        combo=True,
                    )

        # 5. Live query with bounded concurrency if requested
        if query_live:
            from concurrent.futures import ThreadPoolExecutor, as_completed

            active_conns = [c for c in connections if c.get("id") and c.get("isActive", True)]

            def fetch_conn_models(conn):
                cid = conn.get("id")
                data = conn.get("providerSpecificData") or {}
                provider_id = conn.get("provider", "")
                prefix = data.get("prefix", "").strip() or provider_id
                preset = get_provider_preset(provider_id)
                if preset and not data.get("prefix"):
                    prefix = preset.id
                p_name = conn.get("name") or (preset.display_name if preset else prefix)
                status, models = self.client.get_connection_live_models_detailed(cid)
                return cid, p_name, prefix, status, models

            with ThreadPoolExecutor(max_workers=4) as executor:
                futures = [executor.submit(fetch_conn_models, c) for c in active_conns]
                for fut in as_completed(futures):
                    try:
                        cid, p_name, prefix, status, live_models = fut.result()
                        # Contract violations (CORE-003): an OK/non-list pair
                        # or a catalog with any malformed row is a schema
                        # failure, never an empty catalog. Downgrade to
                        # INVALID so it lands in the unavailable branch; no
                        # catalog mutation can happen from it.
                        if status == "OK" and not ModelDiscovery._valid_live_catalog_rows(live_models):
                            status = ModelDiscovery.LIVE_INVALID
                        # Track per-connection outcome: failed discovery is a
                        # distinct fact from an empty successful model list.
                        self.live_outcomes[str(cid)] = status
                        if status == "OK":
                            catalog_state = (
                                CatalogState.MODELS_AVAILABLE
                                if live_models
                                else CatalogState.EMPTY_MODEL_CATALOG
                            )
                            self.catalog_states[str(cid)] = catalog_state
                            self.catalog_model_counts[str(cid)] = len(live_models)
                            if catalog_state == CatalogState.EMPTY_MODEL_CATALOG:
                                self.routing_excluded_connections.add(str(cid))

                            live_cids: Set[str] = set()
                            for m in live_models:
                                raw_mid = m.get("id") or m.get("name") or ""
                                if not raw_mid:
                                    continue
                                model_id = raw_mid.split("/", 1)[1] if "/" in raw_mid and raw_mid.startswith(f"{prefix}/") else raw_mid
                                canonical_id = f"{prefix}/{model_id}" if prefix else model_id
                                live_cids.add(canonical_id)

                                if canonical_id in models_map:
                                    models_map[canonical_id].advertised_live = True
                                    if not models_map[canonical_id].connection_id:
                                        models_map[canonical_id].connection_id = cid
                                else:
                                    models_map[canonical_id] = DiscoveredModel(
                                        canonical_id=canonical_id,
                                        provider_name=p_name,
                                        provider_prefix=prefix,
                                        connection_id=cid,
                                        model_id=model_id,
                                        display_name=m.get("name") or model_id,
                                        is_combo_member=False,
                                        source="live_connection",
                                        advertised_live=True,
                                    )

                                # Preserve explicit billing metadata for
                                # callers that want to classify a discovered
                                # model without assuming the whole provider is
                                # free. The health cache remains evidence-based
                                # and therefore does not persist this hint here.
                                if canonical_id in models_map:
                                    models_map[canonical_id].cost_hint = model_cost_hint(m)
                                else:
                                    # This branch is defensive: the row is
                                    # normally inserted immediately above.
                                    pass

                            # A successful empty catalogue is positive evidence
                            # that this connection currently advertises no
                            # routable models. Keep inventory rows for audit and
                            # re-probe, but exclude them from active routing.
                            for m in models_map.values():
                                is_this_conn = (
                                    (m.connection_id and m.connection_id == cid)
                                    or (not m.connection_id and prefix and m.provider_prefix.lower() == prefix.lower())
                                )
                                if not is_this_conn:
                                    continue
                                if catalog_state == CatalogState.EMPTY_MODEL_CATALOG:
                                    m.routing_eligible = False
                                    m.routing_exclusion_reason = CatalogState.EMPTY_MODEL_CATALOG.value
                                else:
                                    m.routing_eligible = True
                                    m.routing_exclusion_reason = ""

                            # Connection discovery succeeded (OK). Any model registered for this connection
                            # not present in live_cids is confirmed absent: advertised_live = False.
                            for m in models_map.values():
                                is_this_conn = (m.connection_id and m.connection_id == cid) or (prefix and m.provider_prefix.lower() == prefix.lower())
                                if is_this_conn and m.canonical_id not in live_cids:
                                    if m.advertised_live is None:
                                        m.advertised_live = False
                        else:
                            # FAILED, TIMEOUT, NOT_SUPPORTED, or INVALID:
                            # discovery is unavailable, not an empty catalogue.
                            # Leave advertised_live as None (not negative
                            # evidence) and keep the provider eligible for a
                            # later probe.
                            self.catalog_states[str(cid)] = CatalogState.DISCOVERY_UNAVAILABLE
                    except Exception:
                        pass

        # 6. Apply `disabledModels` exclusion metadata. A disabled-only id
        #    never produced an inventory row, so it stays absent entirely; a
        #    model independently discovered from a positive source keeps its
        #    positive provenance (configured/catalog/combo flags) but is
        #    removed from active routing per the explicit disabled policy.
        for did in disabled_ids:
            if did in models_map:
                models_map[did].routing_eligible = False
                models_map[did].routing_exclusion_reason = "disabledModels"

        return list(models_map.values())

    @staticmethod
    def _canon_for_alias(alias: str, model_id: str) -> str:
        """Canonical id for an alias/model pair without inventing mappings."""
        if not alias:
            return model_id
        if not model_id:
            return alias
        return model_id if model_id.startswith(f"{alias}/") else f"{alias}/{model_id}"

    @staticmethod
    def _resolve_provider(namespace, node_by_id: Dict[str, Dict[str, str]], prefix_to_info: Dict[str, Dict[str, str]]):
        """Resolve provider metadata from OBSERVED mappings only.

        Looks up the namespace among provider-node ids and the stored
        connection/provider prefix+alias map. Never hardcodes alias
        equivalences (e.g. oc/ocg/opencode-go); an unmatched namespace stays
        its own canonical alias so stored rows remain addressable.
        """
        if namespace:
            info = prefix_to_info.get(namespace.lower())
            if info:
                return info["name"], info["prefix"], info.get("id", "")
            if namespace in node_by_id:
                ni = node_by_id[namespace]
                return ni["name"], ni["prefix"], ni["id"]
        return namespace, namespace, ""

    @staticmethod
    def _add_configured(models_map: Dict[str, DiscoveredModel], canon_id, name, prefix, cid, clean_mid, source):
        """Register a positively configured model, merging duplicates."""
        if canon_id in models_map:
            models_map[canon_id].configured = True
            return
        models_map[canon_id] = DiscoveredModel(
            canonical_id=canon_id,
            provider_name=name,
            provider_prefix=prefix,
            connection_id=cid,
            model_id=clean_mid,
            display_name=clean_mid,
            is_combo_member=False,
            source=source,
            configured=True,
        )

    def is_routing_eligible(self, model: DiscoveredModel) -> bool:
        """Return whether a discovered model may participate in active routing."""
        if not model.routing_eligible:
            return False
        return str(model.connection_id) not in self.routing_excluded_connections

    def active_routing_models(self, models: List[DiscoveredModel]) -> List[DiscoveredModel]:
        """Filter inventory for routing without deleting provider/model rows."""
        return [m for m in models if self.is_routing_eligible(m)]

    def catalog_status_text(self, connection_id: str) -> str:
        """Stable operator-facing status for the Models API result."""
        state = self.catalog_states.get(str(connection_id))
        count = self.catalog_model_counts.get(str(connection_id), 0)
        if state == CatalogState.EMPTY_MODEL_CATALOG:
            return "Models API: EMPTY (0 models)"
        if state == CatalogState.MODELS_AVAILABLE:
            return f"Models API: {count} models"
        if state == CatalogState.DISCOVERY_UNAVAILABLE:
            return "Models API: UNAVAILABLE"
        return "Models API: NOT PROBED"
