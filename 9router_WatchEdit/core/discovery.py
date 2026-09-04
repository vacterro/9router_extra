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
from typing import Dict, List, Optional, Set, Tuple

from core.router_client import RouterClient

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

class ModelDiscovery:
    # Per-connection live discovery outcome codes (P1-5)
    LIVE_OK = "OK"
    LIVE_FAILED = "FAILED"
    LIVE_TIMEOUT = "TIMEOUT"
    LIVE_NOT_SUPPORTED = "NOT_SUPPORTED"

    def __init__(self, client: RouterClient):
        self.client = client
        # connection_id -> outcome of last live discovery pass
        # (OK / FAILED / TIMEOUT / NOT_SUPPORTED). An empty successful model
        # list is LIVE_OK and is distinct from a failed discovery.
        self.live_outcomes: Dict[str, str] = {}

    def discover_all(self, include_combo_models: bool = True, query_live: bool = False) -> List[DiscoveredModel]:
        """
        Discovers authoritative model inventory from all configured providers & nodes.
        Guarantees that models for offline/rate-limited/timing-out providers (like AMD, NIM)
        are fully discovered and knockable.
        """
        models_map: Dict[str, DiscoveredModel] = {}
        self.live_outcomes.clear()

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

            conn_providers.add(cprov)
            if cprefix:
                p_entry = {
                    "name": cname,
                    "prefix": cprefix,
                    "id": cid,
                }
                prefix_to_info.setdefault(cprefix.lower(), p_entry)
                prefix_to_info.setdefault(cprov.lower(), p_entry)

        # 2. Extract models from 9Router SQLite kv table (configured custom node & provider models)
        kv_rows = self.client.get_kv()
        for k, v in kv_rows:
            if "|" in k:
                # Custom node format: <node_id>|<model_id>|llm
                parts = k.split("|")
                p_id = parts[0]
                m_id = parts[1]

                if p_id in node_by_id:
                    ninfo = node_by_id[p_id]
                    prefix = ninfo["prefix"]
                    p_name = ninfo["name"]

                    if prefix:
                        if m_id.startswith(f"{prefix}/"):
                            canon_id = m_id
                            clean_mid = m_id[len(prefix) + 1:]
                        else:
                            canon_id = f"{prefix}/{m_id}"
                            clean_mid = m_id
                    else:
                        canon_id = m_id
                        clean_mid = m_id

                    models_map[canon_id] = DiscoveredModel(
                        canonical_id=canon_id,
                        provider_name=p_name,
                        provider_prefix=prefix,
                        connection_id=p_id,
                        model_id=clean_mid,
                        display_name=clean_mid,
                        is_combo_member=False,
                        source="configured_node",
                        configured=True,
                    )
            else:
                # Direct provider list format: key = provider_alias -> JSON list of models
                try:
                    val_json = json.loads(v)
                    if isinstance(val_json, list) and len(val_json) > 0 and isinstance(val_json[0], str):
                        prefix = k
                        pinfo = prefix_to_info.get(prefix.lower(), {"name": prefix, "prefix": prefix, "id": ""})
                        for m_id in val_json:
                            if "/" in m_id:
                                canon_id = m_id if m_id.startswith(f"{prefix}/") else f"{prefix}/{m_id}"
                                clean_mid = m_id.split("/", 1)[1] if m_id.startswith(f"{prefix}/") else m_id
                            else:
                                canon_id = f"{prefix}/{m_id}"
                                clean_mid = m_id

                            if canon_id in models_map:
                                models_map[canon_id].configured = True
                            else:
                                models_map[canon_id] = DiscoveredModel(
                                    canonical_id=canon_id,
                                    provider_name=pinfo["name"],
                                    provider_prefix=prefix,
                                    connection_id=pinfo.get("id", ""),
                                    model_id=clean_mid,
                                    display_name=clean_mid,
                                    is_combo_member=False,
                                    source="configured_kv",
                                    configured=True,
                                )
                except Exception:
                    pass

        # 3. Supplemental catalog metadata (/api/models)
        catalog_models = self.client.get_catalog_models()
        for cm in catalog_models:
            c_prov = cm.get("provider", "")
            routed = cm.get("routedModel") or cm.get("fullModel") or ""
            m_name = cm.get("model") or ""

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
                    )
                else:
                    models_map[canon_id].catalog = True
                    if cm.get("name"):
                        models_map[canon_id].display_name = cm["name"]
            elif c_prov in conn_providers or c_prov.lower() in prefix_to_info:
                pinfo = prefix_to_info.get(c_prov.lower(), {"name": c_prov, "prefix": c_prov, "id": ""})
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
                    )
                else:
                    models_map[canon_id].catalog = True
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
                prefix = data.get("prefix", "").strip() or conn.get("provider", "")
                p_name = conn.get("name") or prefix
                status, models = self.client.get_connection_live_models_detailed(cid)
                return cid, p_name, prefix, status, models

            with ThreadPoolExecutor(max_workers=4) as executor:
                futures = [executor.submit(fetch_conn_models, c) for c in active_conns]
                for fut in as_completed(futures):
                    try:
                        cid, p_name, prefix, status, live_models = fut.result()
                        # Track per-connection outcome: failed discovery is a
                        # distinct fact from an empty successful model list.
                        self.live_outcomes[str(cid)] = status
                        if status == "OK":
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
                            # Connection discovery succeeded (OK). Any model registered for this connection
                            # not present in live_cids is confirmed absent: advertised_live = False.
                            for m in models_map.values():
                                is_this_conn = (m.connection_id and m.connection_id == cid) or (prefix and m.provider_prefix.lower() == prefix.lower())
                                if is_this_conn and m.canonical_id not in live_cids:
                                    if m.advertised_live is None:
                                        m.advertised_live = False
                        else:
                            # FAILED, TIMEOUT, or NOT_SUPPORTED: leave advertised_live as None (not negative evidence)
                            pass
                    except Exception:
                        pass

        return list(models_map.values())


