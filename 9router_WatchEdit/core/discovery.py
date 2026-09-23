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
import inspect
import threading
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set, Tuple

import httpx

from core.router_client import RouterClient
from core.security import LiveAccessLockedError
from core.classification import CatalogState
from core.provider_profiles import get_provider_preset, model_cost_hint

# T-31: how many times get_combos() may be called per discover_all() pass.
# Exactly one capture; asserted by the focused T-31 suite.

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

    @staticmethod
    def _call_with_http_client(method, http_client: Optional["httpx.Client"]):
        """Reuse the pass transport when the bound endpoint supports it.

        RouterClient methods accept ``http_client``. Tests and older adapters
        often replace them with zero-argument callables, so inspect the bound
        signature instead of catching a TypeError from inside the method.
        """
        if http_client is None:
            return method()
        try:
            parameters = inspect.signature(method).parameters.values()
        except (TypeError, ValueError):
            return method(http_client=http_client)
        if any(
            parameter.name == "http_client"
            or parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in parameters
        ):
            return method(http_client=http_client)
        return method()

    def __init__(self, client: RouterClient):
        self.client = client
        # Legacy compatibility view of the LAST ACCEPTED discovery pass.
        # discover_all() no longer mutates these during a pass (T-31); they
        # are only replaced when a completed accepted snapshot is published
        # via publish_snapshot(). Callers may still read them for a coherent
        # last-accepted view; never treat them as pass-local scratch space.
        self.live_outcomes: Dict[str, str] = {}
        self.catalog_states: Dict[str, CatalogState] = {}
        self.catalog_model_counts: Dict[str, int] = {}
        self.routing_excluded_connections: Set[str] = set()
        # Last ACCEPTED combo collection (T-31): consumed by combo editor
        # reconciliation on legacy surfaces; replaced atomically with the
        # other compatibility views at each accepted publication.
        self.combos: List[Dict[str, Any]] = []
        # Coherence of the compatibility view across overlapping passes.
        self._compat_lock = threading.Lock()

    # -------------------------------------------------
    # Combo capture (Defect A repair, T-31)
    # -------------------------------------------------
    # T-31 final closure: supported combo payload is strict JSON-like data
    # (dict / list / tuple) with immutable scalars at the leaves. The bound
    # below is a structural guard against a pathological (or cyclic) payload:
    # it fails loudly instead of recursing without limit, because an infinite
    # walk would hang a refresh pass.
    _COMBO_COPY_MAX_DEPTH = 64

    @classmethod
    def _detach_payload(cls, value: Any, depth: int = 0) -> Any:
        """RECURSIVE structural detach of the supported JSON-like payload.

        Ownership rule (the documented invariant): the returned value shares
        NO dict, list, or tuple object with ``value`` — every nested
        collection is independently rebuilt, at every depth. Scalars pass
        through unchanged (they are immutable, so sharing them is safe).

        Depth is explicitly bounded: a payload nested deeper than
        ``_COMBO_COPY_MAX_DEPTH`` raises ValueError rather than recursing
        without limit, and no JSON round-trip is used to clone data.
        """
        if isinstance(value, dict):
            if depth >= cls._COMBO_COPY_MAX_DEPTH:
                raise ValueError(
                    "combo payload exceeds supported nesting depth "
                    f"({cls._COMBO_COPY_MAX_DEPTH})"
                )
            return {k: cls._detach_payload(v, depth + 1) for k, v in value.items()}
        if isinstance(value, list):
            if depth >= cls._COMBO_COPY_MAX_DEPTH:
                raise ValueError(
                    "combo payload exceeds supported nesting depth "
                    f"({cls._COMBO_COPY_MAX_DEPTH})"
                )
            return [cls._detach_payload(v, depth + 1) for v in value]
        if isinstance(value, tuple):
            if depth >= cls._COMBO_COPY_MAX_DEPTH:
                raise ValueError(
                    "combo payload exceeds supported nesting depth "
                    f"({cls._COMBO_COPY_MAX_DEPTH})"
                )
            return tuple(cls._detach_payload(v, depth + 1) for v in value)
        return value  # immutable scalar / opaque leaf: safe to share

    @classmethod
    def _copy_combo(cls, c: Any) -> Dict[str, Any]:
        """Explicit structural combo copy: the value owns EVERY mutable
        container it carries, at every nesting level.

        One explicit helper keeps the rule obvious and testable (T-31
        consumer isolation: publication envelopes and their consumers must
        never alias snapshot or source state — including nested payloads such
        as ``meta.tags``).
        """
        if not isinstance(c, dict):
            return c  # non-dict payloads pass through unchanged
        return cls._detach_payload(c, 0)

    @classmethod
    def _copy_combos(cls, combos: Any) -> List[Dict[str, Any]]:
        """Detached copy of a whole combo collection."""
        if combos is None:
            return []
        return [cls._copy_combo(c) for c in combos]

    @staticmethod
    def _capture_combos_once(
        client: RouterClient,
        http_client: Optional["httpx.Client"] = None,
    ) -> List[Dict[str, Any]]:
        """Perform the ONE authorized combo capture for one discovery pass.

        The captured value is DEEPLY DETACHED from the client transport:
        later client-side or source-side mutation cannot alter it. Ownership
        is pass-local by construction — the value exists only as the local
        variable inside one discover_all/build_snapshot execution, so two
        concurrent passes can never share a combo collection.
        """
        combos = ModelDiscovery._call_with_http_client(client.get_combos, http_client)
        if combos is None:
            return []
        # Deep-detach through the explicit combo-copy helper so the snapshot
        # owns its own state (Defect B) with one obvious ownership rule.
        return ModelDiscovery._copy_combos(combos)

    def publish_snapshot(self, snapshot) -> None:
        """Update the legacy compatibility state from one COMPLETED ACCEPTED
        snapshot. This is the only writer after T-31; it never runs while a
        pass is executing, so overlapping generations can never interleave
        mutations of these containers."""
        with self._compat_lock:
            self.live_outcomes = dict(snapshot.live_outcomes)
            self.catalog_states = dict(snapshot.catalog_states)
            self.catalog_model_counts = dict(snapshot.catalog_model_counts)
            self.routing_excluded_connections = set(
                snapshot.routing_excluded_connections
            )
            # Consumer isolation (T-31 final closure): the compatibility
            # view receives fully DETACHED combo dicts. Mutating this view
            # after publication can never reach back into the completed
            # snapshot (or any other consumer of it).
            self.combos = self._copy_combos(snapshot.combos) if snapshot.has_combos else self.combos

    @staticmethod
    def _opencode_source_state(opencode_catalog: Optional[Any]) -> Tuple[str, int]:
        """Explicit OpenCode-current source state for the snapshot envelope.

        AVAILABLE: successful refresh with at least one catalog row.
        EMPTY:     successful refresh returning zero rows (200 + [] is an
                   explicit EMPTY, never a failure disguised as PASS).
        UNAVAILABLE: transport/HTTP failure or no refresh result at all while
                   a refresh was attempted.
        INVALID:   200 whose payload violates the catalog schema.
        NEVER_REFRESHED: no fetch has ever been attempted this session.

        Pure inspection: no HTTP, no polling, no timers.
        """
        if opencode_catalog is None:
            return ("NEVER_REFRESHED", 0)
        statuses = getattr(opencode_catalog, "statuses", None) or {}
        api_status = statuses.get("api")
        models = getattr(opencode_catalog, "models", None) or []
        if api_status is None:
            return ("NEVER_REFRESHED", 0)
        if api_status == "API_FAILED":
            error_class = (getattr(opencode_catalog, "last_failure", None) or {}).get("class", "")
            if error_class == "malformed_payload":
                return ("INVALID", 0)
            return ("UNAVAILABLE", 0)
        # api_status == API_OK: success. Empty 200 payload is EMPTY.
        return ("AVAILABLE" if models else "EMPTY", len(models))

    def build_snapshot(
        self,
        include_combo_models: bool = True,
        query_live: bool = False,
        opencode_catalog: Optional[Any] = None,
    ):
        """Build a snapshot through one bounded pass-scoped local transport."""
        if not isinstance(self.client, RouterClient):
            return self._build_snapshot(
                include_combo_models=include_combo_models,
                query_live=query_live,
                opencode_catalog=opencode_catalog,
                http_client=None,
            )
        with httpx.Client(
            timeout=12.0,
            follow_redirects=False,
            trust_env=False,
            limits=httpx.Limits(max_connections=4, max_keepalive_connections=4),
        ) as http_client:
            return self._build_snapshot(
                include_combo_models=include_combo_models,
                query_live=query_live,
                opencode_catalog=opencode_catalog,
                http_client=http_client,
            )

    def _build_snapshot(
        self,
        include_combo_models: bool = True,
        query_live: bool = False,
        opencode_catalog: Optional[Any] = None,
        *,
        http_client: Optional["httpx.Client"],
    ):
        """Build one completed, detached DiscoverySnapshot — the pure
        snapshot-building path (T-31 closure, Defects A/B/D).

        Contract:
        * ONE combo capture per call: the local ``combos`` value is the only
          owner; it drives combo-membership classification AND becomes
          snapshot.combos. Two concurrent executions can never share it.
        * The snapshot owns detached state: nested combo model lists are
          copies, per-pass containers are fresh dicts — a later source or
          client mutation cannot alter a completed snapshot.
        * NO compatibility publication happens here: discover_all() no longer
          writes live_outcomes/catalog_states/… at completion time. Only the
          explicitly accepted publication boundary (publish_snapshot) may
          replace the compatibility view (Defect D).
        """
        models_map: Dict[str, DiscoveredModel] = {}
        # ---- per-pass state: NEVER shared between generations ----
        pass_live_outcomes: Dict[str, str] = {}
        pass_catalog_states: Dict[str, CatalogState] = {}
        pass_catalog_model_counts: Dict[str, int] = {}
        pass_routing_excluded: Set[str] = set()
        # ---- pass-local combo capture: exactly ONE get_combos() call ----
        combos = self._capture_combos_once(self.client, http_client=http_client)

        # 1. Fetch provider connections and provider nodes
        connections = self._call_with_http_client(self.client.get_providers, http_client)
        nodes = self._call_with_http_client(self.client.get_provider_nodes, http_client)

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
        catalog_models = self._call_with_http_client(self.client.get_catalog_models, http_client)
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
        # T-31: ONE pass-local capture for the whole pass. The same detached
        # collection later becomes snapshot.combos for ComboEditor
        # reconciliation — and it can never be shared with another pass.
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

        # 5. Live query with bounded concurrency if requested.
        # PERF-001: one pass-scoped bounded httpx.Client is reused across the
        # whole provider set (instead of P+4 one-request clients), and an
        # explicit cancellation flag stops scheduling after Lock/Close.
        if query_live:
            import threading

            from concurrent.futures import ThreadPoolExecutor, as_completed
            active_conns = [c for c in connections if c.get("id") and c.get("isActive", True)]
            cancel_event = threading.Event()

            def _should_cancel() -> bool:
                if cancel_event.is_set():
                    return True
                try:
                    if self.client.security.is_locked():
                        cancel_event.set()
                        return True
                except Exception:
                    pass
                return False

            def fetch_conn_models(conn):
                cid = conn.get("id")
                data = conn.get("providerSpecificData") or {}
                provider_id = conn.get("provider", "")
                prefix = data.get("prefix", "").strip() or provider_id
                preset = get_provider_preset(provider_id)
                if preset and not data.get("prefix"):
                    prefix = preset.id
                p_name = conn.get("name") or (preset.display_name if preset else prefix)
                # Bounded fan-out evidence: track in-flight provider requests
                # inside the worker body (T-32 stress gate: concurrency must
                # stay <= the configured bound). Tracking here measures true
                # concurrency and never monkeypatches future objects.
                owner = getattr(self.client, "_refresh_diagnostics_owner", None)
                if owner is not None:
                    with owner._refresh_thread_monitor_lock:
                        d = owner._refresh_diagnostics
                        d["provider_requests_in_flight"] += 1
                        d["max_provider_requests_in_flight"] = max(
                            d["max_provider_requests_in_flight"],
                            d["provider_requests_in_flight"],
                        )
                try:
                    try:
                        status, models = self.client.get_connection_live_models_detailed(
                            cid, http_client=shared_client, should_cancel=_should_cancel,
                        )
                    except TypeError:
                        # Legacy one-argument doubles/callers keep working.
                        status, models = self.client.get_connection_live_models_detailed(cid)
                finally:
                    if owner is not None:
                        with owner._refresh_thread_monitor_lock:
                            owner._refresh_diagnostics["provider_requests_in_flight"] -= 1
                return cid, p_name, prefix, status, models

            owns_shared_client = http_client is None
            shared_client = http_client or httpx.Client(
                timeout=12.0, follow_redirects=False, trust_env=False,
                limits=httpx.Limits(max_connections=4, max_keepalive_connections=4),
            )
            try:
                with ThreadPoolExecutor(max_workers=4) as executor:
                    futures = []
                    for c in active_conns:
                        if _should_cancel():
                            break
                        futures.append(executor.submit(fetch_conn_models, c))
                    for fut in as_completed(futures):
                        if _should_cancel():
                            # Cancellation/LOCKED: stop consuming; queued futures
                            # are cancelled and the pass stops scheduling.
                            for pending in futures:
                                pending.cancel()
                        try:
                            cid, p_name, prefix, status, live_models = fut.result()
                            # Contract violations (CORE-003): an OK/non-list pair
                            # or a catalog with any malformed row is a schema
                            # failure, never an empty catalog. Downgrade to
                            # INVALID so it lands in the unavailable branch; no
                            # catalog mutation can happen from it.
                            if status == "OK" and not ModelDiscovery._valid_live_catalog_rows(live_models):
                                status = ModelDiscovery.LIVE_INVALID
                            # Track per-connection outcome in the PASS-LOCAL
                            # container (T-31): a pass never mutates shared state.
                            pass_live_outcomes[str(cid)] = status
                            if status == "OK":
                                catalog_state = (
                                    CatalogState.MODELS_AVAILABLE
                                    if live_models
                                    else CatalogState.EMPTY_MODEL_CATALOG
                                )
                                pass_catalog_states[str(cid)] = catalog_state
                                pass_catalog_model_counts[str(cid)] = len(live_models)
                                if catalog_state == CatalogState.EMPTY_MODEL_CATALOG:
                                    pass_routing_excluded.add(str(cid))

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
                            elif status == "CANCELLED":
                                # PERF-001: cancelled in-flight work is NOT
                                # evidence; record unavailable, never EMPTY.
                                pass_catalog_states[str(cid)] = CatalogState.DISCOVERY_UNAVAILABLE
                            else:
                                # FAILED, TIMEOUT, NOT_SUPPORTED, or INVALID:
                                # discovery is unavailable, not an empty catalogue.
                                # Leave advertised_live as None (not negative
                                # evidence) and keep the provider eligible for a
                                # later probe.
                                pass_catalog_states[str(cid)] = CatalogState.DISCOVERY_UNAVAILABLE
                        except LiveAccessLockedError:
                            # LOCKED evidence: record distinct LOCKED outcome and
                            # unavailable catalog state WITHOUT any authoritative
                            # EMPTY evidence. Also note this in the returned
                            # snapshot so upstream gates can re-check
                            # authorization after Lock during discovery (T-32).
                            cancel_event.set()
                            for conn in active_conns:
                                cid = str(conn.get("id", ""))
                                if cid:
                                    pass_live_outcomes[cid] = "LOCKED"
                                    pass_catalog_states[cid] = CatalogState.DISCOVERY_UNAVAILABLE
                        except Exception:
                            pass
            finally:
                # PERF-001: any active connection left without an outcome because
                # the pass was cancelled before its worker ran is recorded as
                # LOCKED (auth boundary) or CANCELLED — never authoritative EMPTY.
                if cancel_event.is_set():
                    try:
                        locked = self.client.security.is_locked()
                    except Exception:
                        locked = False
                    fallback = "LOCKED" if locked else "CANCELLED"
                    for conn in active_conns:
                        cid = str(conn.get("id", ""))
                        if cid and cid not in pass_live_outcomes:
                            pass_live_outcomes[cid] = fallback
                            pass_catalog_states[cid] = CatalogState.DISCOVERY_UNAVAILABLE
                if owns_shared_client:
                    try:
                        shared_client.close()
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

        # T-31: freeze ONE detached publication object for this pass. The
        # legacy compatibility views are NOT touched here (Defect D): only
        # the explicitly accepted publication boundary may replace them.
        oc_state, oc_count = self._opencode_source_state(opencode_catalog)
        from core.refresh_controller import DiscoverySnapshot  # runtime-safe local binding
        snapshot = DiscoverySnapshot(
            generation=0,  # stamped by the publication boundary
            models=tuple(models_map.values()),
            combos=tuple(combos),
            live_outcomes=dict(pass_live_outcomes),
            catalog_states=dict(pass_catalog_states),
            catalog_model_counts=dict(pass_catalog_model_counts),
            routing_excluded_connections=frozenset(pass_routing_excluded),
            opencode_source_state=oc_state,
            opencode_model_count=oc_count,
        )
        return snapshot

    def discover_all(
        self,
        include_combo_models: bool = True,
        query_live: bool = False,
        opencode_catalog: Optional[Any] = None,
    ):
        """Legacy synchronous facade: build a snapshot and explicitly
        accept/publish it for standalone callers (run.py, engine-level tests).

        MainWindow refresh paths MUST NOT use this method — they use the pure
        build_snapshot() path and publish compatibility state only from the
        UI acceptance boundary (Defect D). Compatibility publication here is
        intentional for the legacy synchronous contract.
        """
        snapshot = self.build_snapshot(
            include_combo_models=include_combo_models,
            query_live=query_live,
            opencode_catalog=opencode_catalog,
        )
        self.publish_snapshot(snapshot)
        return snapshot

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
        """Return whether a discovered model may participate in active routing.

        Reads the LAST ACCEPTED compatibility view (T-31); the model row
        itself carries its own per-snapshot routing evidence."""
        if not model.routing_eligible:
            return False
        return str(model.connection_id) not in self.routing_excluded_connections

    def active_routing_models(self, models: List[DiscoveredModel]) -> List[DiscoveredModel]:
        """Filter inventory for routing without deleting provider/model rows."""
        return [m for m in models if self.is_routing_eligible(m)]

    def catalog_status_text(self, connection_id: str) -> str:
        """Stable operator-facing status for the Models API result.

        Reads the LAST ACCEPTED compatibility view (T-31)."""
        state = self.catalog_states.get(str(connection_id))
        count = self.catalog_model_counts.get(str(connection_id), 0)
        if state == CatalogState.EMPTY_MODEL_CATALOG:
            return "Models API: EMPTY (0 models)"
        if state == CatalogState.MODELS_AVAILABLE:
            return f"Models API: {count} models"
        if state == CatalogState.DISCOVERY_UNAVAILABLE:
            return "Models API: UNAVAILABLE"
        return "Models API: NOT PROBED"
