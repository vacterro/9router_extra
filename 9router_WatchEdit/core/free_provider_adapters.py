"""
9router_WatchEdit - FREE provider adapter contract (FREE-FALLBACK-001, milestone 2).

Adapters keep provider quirks out of the table/controller contract: the UI and
the scan controller only ever talk to ``FreeProviderAdapter`` and its declared
capabilities. An adapter NEVER guesses; when a documented, machine-readable
discovery contract does not exist it must be exposed as UNSUPPORTED/CONDITIONAL
instead of improvising with HTML scraping or client-identity tricks.

Capabilities are declared SEPARATELY because they answer different questions:

    supports_metadata              can a documented catalogue/metadata source be read?
    supports_strict_zero_cost_evidence   can that source prove zero monetary cost?
    supports_live_canary           is there an official, bounded canary path?
    live_canary_can_consume_quota  does the canary spend the operator's free quota?
    live_canary_can_bill           could the canary ever create a monetary charge?
    client_bound                   is the executable path bound to a local runtime?

Initial adapters cover the surfaces that are already real in this repository:

  * OpenCode / OpenCode Local Free -- metadata reuses the existing official Zen
    catalog discovery and FREE evidence; live validation reuses the EXISTING
    local ocf bridge canary (no second bridge implementation). Its free tier is
    client-bound, so it is CLIENT_BOUND_FREE, never a generic routable FREE.
  * Kira AI -- reuses the explicit provider catalogue billing metadata
    (``is_free`` and price fields) already implemented in provider_profiles.
    Missing metadata stays UNKNOWN; substring heuristics are not revived.
  * OpenRouter -- documented public model/pricing metadata; only explicit zero
    pricing or an official ``:free`` route becomes STRICT_FREE. No paid
    inference is ever issued merely to validate catalogue discovery.
  * Every other configured 9Router provider -- visible as NO FREE ADAPTER /
    CONDITIONAL, defaulting to no automatic live probing.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

import httpx

from core.free_evidence import (
    CostRisk,
    FreeEvidence,
    ProviderHealth,
    RoutingCapability,
    ScanMode,
    ScanPolicy,
    classify_free_evidence,
)
from core.provider_profiles import model_cost_hint

ADAPTER_OPENCODE_LOCAL_FREE = "opencode_local_free"
ADAPTER_KIRA_CATALOG = "kira_ai_catalog"
ADAPTER_OPENROUTER_CATALOG = "openrouter_catalog"
ADAPTER_UNSUPPORTED = "unsupported"

OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
HTTP_TIMEOUT_SECONDS = 8.0
OPENROUTER_SOURCE = "https://openrouter.ai/api/v1/models (documented public pricing metadata)"
KIRA_SOURCE = "local 9Router catalog metadata for the configured Kira connection"
OPENCODE_SOURCE = "official OpenCode Zen public catalog discovery (existing SRC-004 source)"
UNSUPPORTED_SOURCE = "no documented zero-cost metadata contract"

PROVIDER_OPENCODE = "opencode"
PROVIDER_KIRA = "kira-ai"
PROVIDER_OPENROUTER = "openrouter"

@dataclass(frozen=True)
class AdapterCapabilities:
    adapter_id: str
    provider_id: str
    display_name: str
    supports_metadata: bool
    supports_strict_zero_cost_evidence: bool
    supports_live_canary: bool
    live_canary_can_consume_quota: bool
    live_canary_can_bill: bool
    client_bound: bool
    transport: str
    metadata_source: str
    note: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "adapter_id": self.adapter_id,
            "provider_id": self.provider_id,
            "display_name": self.display_name,
            "supports_metadata": self.supports_metadata,
            "supports_strict_zero_cost_evidence": self.supports_strict_zero_cost_evidence,
            "supports_live_canary": self.supports_live_canary,
            "live_canary_can_consume_quota": self.live_canary_can_consume_quota,
            "live_canary_can_bill": self.live_canary_can_bill,
            "client_bound": self.client_bound,
            "transport": self.transport,
            "metadata_source": self.metadata_source,
            "note": self.note,
        }


@dataclass
class MetadataScanResult:
    """Outcome of a METADATA-ONLY pass: zero inference calls by construction."""

    ok: bool
    models: List[Dict[str, Any]] = field(default_factory=list)
    error_class: str = ""
    error_summary: str = ""
    retry_after_sec: float = 0.0
    authoritative: bool = True
    source: str = ""
    network_calls: int = 0
    inference_calls: int = 0
    http_status: int = 0


@dataclass
class LiveCanaryResult:
    ok: bool
    state: str = ""
    error_class: str = ""
    error_summary: str = ""
    model_id: str = ""
    network_calls: int = 0
    inference_calls: int = 0
    monetary_charge_possible: bool = False
    #: Positive provider-level health verdicts ("AUTH_FAILED"/"DEAD") that the
    #: canary PROVED, so the FREE tail can exclude the lane. Transient states
    #: (quota, timeout, one unavailable model) must leave it empty.
    blocking_health: str = ""
    healthy: bool = False


class FreeProviderAdapter:
    """Base adapter. Unsupported surfaces declare themselves, they never guess."""

    adapter_id = ADAPTER_UNSUPPORTED

    def __init__(self, provider_id: str, display_name: str = "") -> None:
        self.provider_id = str(provider_id)
        self.display_name = display_name or str(provider_id)

    # ------------------------------------------------------------ capabilities
    def capabilities(self) -> AdapterCapabilities:
        return AdapterCapabilities(
            adapter_id=self.adapter_id,
            provider_id=self.provider_id,
            display_name=self.display_name,
            supports_metadata=False,
            supports_strict_zero_cost_evidence=False,
            supports_live_canary=False,
            live_canary_can_consume_quota=False,
            live_canary_can_bill=False,
            client_bound=False,
            transport="9Router provider surface",
            metadata_source=UNSUPPORTED_SOURCE,
            note="no FREE adapter: visible as CONDITIONAL, never auto-probed",
        )

    # ------------------------------------------------------------------ actions
    def metadata_scan(self) -> MetadataScanResult:
        return MetadataScanResult(
            ok=False,
            error_class="no_free_adapter",
            error_summary="provider has no documented free-metadata adapter",
            authoritative=False,
            source=UNSUPPORTED_SOURCE,
        )

    def live_canary(self, model_id: str = "") -> LiveCanaryResult:
        return LiveCanaryResult(
            ok=False,
            state="NO_FREE_ADAPTER",
            error_class="no_free_adapter",
            error_summary="provider has no supported live canary",
            model_id=model_id,
        )

    def preferred_canary_target(self, rows: Sequence[Mapping[str, Any]]) -> str:
        """Cheapest meaningful canary target from the current FREE inventory.

        The adapter owns this decision because only the provider knows which of
        its free models is a reliable probe target. A canary against an
        arbitrary first row can report a provider-level failure that is really
        just one unlucky model.
        """
        for row in rows or ():
            if not isinstance(row, Mapping):
                continue
            if str(row.get("free_evidence") or "") in _CANARY_EVIDENCE:
                return str(row.get("upstream_model_id") or row.get("model_id") or "")
        return ""


#: Evidence classes that make a model a sensible canary candidate.
_CANARY_EVIDENCE = frozenset(
    {
        FreeEvidence.STRICT_FREE.value,
        FreeEvidence.CLIENT_BOUND_FREE.value,
        FreeEvidence.CONDITIONAL_FREE.value,
    }
)


class UnsupportedProviderAdapter(FreeProviderAdapter):
    """Configured 9Router provider without a free-adapter contract."""

    adapter_id = ADAPTER_UNSUPPORTED


class OpenCodeLocalFreeAdapter(FreeProviderAdapter):
    """OpenCode / OpenCode Local Free: existing catalog evidence + ocf bridge."""

    adapter_id = ADAPTER_OPENCODE_LOCAL_FREE

    def __init__(
        self,
        provider_id: str = PROVIDER_OPENCODE,
        *,
        catalog: Optional[Any] = None,
        bridge: Optional[Any] = None,
        display_name: str = "OpenCode / OpenCode Local Free",
    ) -> None:
        super().__init__(provider_id, display_name)
        self.catalog = catalog
        self.bridge = bridge

    def capabilities(self) -> AdapterCapabilities:
        return AdapterCapabilities(
            adapter_id=self.adapter_id,
            provider_id=self.provider_id,
            display_name=self.display_name,
            supports_metadata=self.catalog is not None,
            supports_strict_zero_cost_evidence=True,
            supports_live_canary=self.bridge is not None,
            live_canary_can_consume_quota=True,
            live_canary_can_bill=False,
            client_bound=True,
            transport="official local OpenCode CLI via the existing loopback-only ocf bridge",
            metadata_source=OPENCODE_SOURCE,
            note="client-bound free tier: eligible for SAIFREN only via the ocf bridge contract",
        )

    def metadata_scan(self) -> MetadataScanResult:
        if self.catalog is None:
            return MetadataScanResult(
                ok=False, error_class="metadata_source_unavailable",
                error_summary="no OpenCode catalog discovery bound",
                authoritative=False, source=OPENCODE_SOURCE,
            )
        calls = 0
        try:
            result = self.catalog.refresh(force=True) or {}
            calls = 1 if result.get("fetch") else 0
        except Exception as ex:  # never raises by contract; belt and braces
            return MetadataScanResult(
                ok=False, error_class="metadata_error",
                error_summary=f"{type(ex).__name__}",
                source=OPENCODE_SOURCE, network_calls=calls,
            )
        if str(result.get("status")) != "API_OK":
            error_class = str(result.get("error_class") or "api_failed")
            return MetadataScanResult(
                ok=False,
                error_class=error_class,
                error_summary=f"OpenCode Zen catalog refresh failed ({error_class})",
                retry_after_sec=0.0,
                source=OPENCODE_SOURCE,
                network_calls=calls,
            )
        rows: List[Dict[str, Any]] = []
        for model in getattr(self.catalog, "models", []) or []:
            model_id = str(getattr(model, "model_id", "") or "")
            if not model_id:
                continue
            free_candidate = bool(getattr(model, "free_candidate", False))
            row = {
                "model_id": model_id,
                "upstream_model_id": model_id,
                "canonical_id": f"ocf/{model_id}",
                "upstream_canonical_id": f"opencode/{model_id}",
                "provider_id": self.provider_id,
                "free_candidate": free_candidate,
                "is_free": True if free_candidate else None,
                "client_bound": free_candidate,
                "routing": (
                    RoutingCapability.LOCAL_BRIDGE_REQUIRED.value
                    if free_candidate else RoutingCapability.UNSUPPORTED.value
                ),
                "cost_risk": CostRisk.FREE_QUOTA_PROBE.value,
                "source": OPENCODE_SOURCE,
            }
            verdict = classify_free_evidence(row)
            row["free_evidence"] = (
                FreeEvidence.CLIENT_BOUND_FREE.value
                if free_candidate and verdict.evidence in (FreeEvidence.STRICT_FREE,
                                                           FreeEvidence.CLIENT_BOUND_FREE)
                else verdict.evidence.value
            )
            row["evidence_source"] = verdict.source
            row["evidence_note"] = verdict.reason
            rows.append(row)
        return MetadataScanResult(
            ok=True, models=rows, source=OPENCODE_SOURCE, network_calls=calls
        )

    def preferred_canary_target(self, rows: Sequence[Mapping[str, Any]]) -> str:
        """Prefer the officially known-good free model over an arbitrary row.

        The free tier is client-bound and not every advertised free model is
        usable right now, so the bounded canary targets the model the official
        runtime is known to serve before falling back to any free row.
        """
        known_good = _bridge_canary_model()
        if known_good:
            for row in rows or ():
                if not isinstance(row, Mapping):
                    continue
                model_id = str(row.get("upstream_model_id") or row.get("model_id") or "")
                if model_id == known_good and str(row.get("free_evidence") or "") in _CANARY_EVIDENCE:
                    return model_id
        return super().preferred_canary_target(rows)

    def live_canary(self, model_id: str = "") -> LiveCanaryResult:
        if self.bridge is None:
            return LiveCanaryResult(
                ok=False, state="BRIDGE_UNAVAILABLE",
                error_class="live_canary_unavailable",
                error_summary="no local OpenCode bridge bound",
                model_id=model_id,
            )
        try:
            result = self.bridge.canary(model_id)
        except Exception as ex:
            return LiveCanaryResult(
                ok=False, state="BRIDGE_BROKEN", error_class="bridge_error",
                error_summary=f"{type(ex).__name__}", model_id=model_id,
                inference_calls=0,
            )
        state = str(getattr(result, "state", "") or "BRIDGE_BROKEN")
        ok = state == "BRIDGE_OK"
        # Positive authoritative failures supersede an old "alive" verdict, and
        # transient conditions (quota, timeout, model unavailable) must not.
        blocking = ""
        if state == "BRIDGE_UPSTREAM_REJECTED":
            blocking = ProviderHealth.AUTH_FAILED.value
        elif state == "BRIDGE_RUNTIME_MISSING":
            blocking = ProviderHealth.DEAD.value
        return LiveCanaryResult(
            ok=ok,
            state=state,
            error_class=str(getattr(result, "error_class", "") or ""),
            error_summary=str(getattr(result, "detail", "") or "")[:200],
            model_id=model_id,
            network_calls=1,
            inference_calls=1,
            monetary_charge_possible=False,
            blocking_health=blocking,
            healthy=ok,
        )


class KiraAiAdapter(FreeProviderAdapter):
    """Kira AI: explicit provider catalogue billing metadata only."""

    adapter_id = ADAPTER_KIRA_CATALOG

    def __init__(
        self,
        provider_id: str = PROVIDER_KIRA,
        *,
        catalog_fetch: Optional[Callable[[], Any]] = None,
        display_name: str = "Kira AI",
    ) -> None:
        super().__init__(provider_id, display_name)
        self.catalog_fetch = catalog_fetch

    def capabilities(self) -> AdapterCapabilities:
        return AdapterCapabilities(
            adapter_id=self.adapter_id,
            provider_id=self.provider_id,
            display_name=self.display_name,
            supports_metadata=self.catalog_fetch is not None,
            supports_strict_zero_cost_evidence=True,
            # No documented zero-cost inference contract: a probe could bill.
            supports_live_canary=False,
            live_canary_can_consume_quota=True,
            live_canary_can_bill=True,
            client_bound=False,
            transport="9Router OpenAI-compatible provider connection",
            metadata_source=KIRA_SOURCE,
            note="unknown/missing billing metadata stays UNKNOWN, never FREE",
        )

    def metadata_scan(self) -> MetadataScanResult:
        if self.catalog_fetch is None:
            return MetadataScanResult(
                ok=False, error_class="metadata_source_unavailable",
                error_summary="no local 9Router catalog bound",
                authoritative=False, source=KIRA_SOURCE,
            )
        try:
            fetched = self.catalog_fetch()
        except Exception as ex:
            return MetadataScanResult(
                ok=False, error_class="metadata_error",
                error_summary=f"{type(ex).__name__}",
                authoritative=False, source=KIRA_SOURCE, network_calls=1,
            )
        if (
            isinstance(fetched, tuple)
            and len(fetched) == 2
            and isinstance(fetched[0], str)
        ):
            outcome, rows = fetched
            if outcome != "OK" or not isinstance(rows, list):
                return MetadataScanResult(
                    ok=False,
                    error_class=("metadata_" + str(outcome or "failed").lower()),
                    error_summary="local 9Router catalogue was unavailable or invalid",
                    authoritative=False,
                    source=KIRA_SOURCE,
                    network_calls=1,
                )
            catalog = rows
        elif isinstance(fetched, Sequence) and not isinstance(fetched, (str, bytes)):
            # Compatibility for adapters injected by callers/tests. The
            # installed MainWindow path uses the explicit status tuple above.
            catalog = list(fetched)
        else:
            return MetadataScanResult(
                ok=False,
                error_class="metadata_invalid",
                error_summary="local 9Router catalogue has an invalid result shape",
                authoritative=False,
                source=KIRA_SOURCE,
                network_calls=1,
            )
        rows: List[Dict[str, Any]] = []
        for entry in catalog:
            if not isinstance(entry, Mapping):
                return MetadataScanResult(
                    ok=False,
                    error_class="metadata_invalid",
                    error_summary="local 9Router catalogue contains an invalid row",
                    authoritative=False,
                    source=KIRA_SOURCE,
                    network_calls=1,
                )
            provider = str(entry.get("provider") or "").lower()
            model_id = str(
                entry.get("model") or entry.get("name") or entry.get("routedModel")
                or entry.get("fullModel") or entry.get("id") or ""
            )
            if not model_id:
                return MetadataScanResult(
                    ok=False,
                    error_class="metadata_invalid",
                    error_summary="local 9Router catalogue contains a row without a model identity",
                    authoritative=False,
                    source=KIRA_SOURCE,
                    network_calls=1,
                )
            if provider and provider not in (self.provider_id, PROVIDER_KIRA, "kira"):
                continue
            hint = model_cost_hint(entry)  # None => UNKNOWN, never FREE
            row = {
                "model_id": model_id,
                "upstream_model_id": model_id,
                "canonical_id": f"{self.provider_id}/{model_id}",
                "provider_id": self.provider_id,
                "is_free": True if hint == "FREE" else (False if hint == "PAID" else None),
                "notes": str(entry.get("description") or entry.get("promo") or ""),
                "routing": RoutingCapability.DIRECT_ROUTABLE.value,
                "cost_risk": CostRisk.FREE_QUOTA_PROBE.value,
                "source": KIRA_SOURCE,
            }
            verdict = classify_free_evidence(row)
            row["free_evidence"] = verdict.evidence.value
            row["evidence_source"] = verdict.source
            row["evidence_note"] = verdict.reason
            rows.append(row)
        return MetadataScanResult(ok=True, models=rows, source=KIRA_SOURCE, network_calls=1)


class OpenRouterAdapter(FreeProviderAdapter):
    """OpenRouter: documented public model + pricing metadata."""

    adapter_id = ADAPTER_OPENROUTER_CATALOG

    def __init__(
        self,
        provider_id: str = PROVIDER_OPENROUTER,
        *,
        transport: Optional[httpx.BaseTransport] = None,
        url: str = OPENROUTER_MODELS_URL,
        timeout: float = HTTP_TIMEOUT_SECONDS,
        display_name: str = "OpenRouter",
    ) -> None:
        super().__init__(provider_id, display_name)
        self.transport = transport
        self.url = url
        self.timeout = float(timeout)

    def capabilities(self) -> AdapterCapabilities:
        return AdapterCapabilities(
            adapter_id=self.adapter_id,
            provider_id=self.provider_id,
            display_name=self.display_name,
            supports_metadata=True,
            supports_strict_zero_cost_evidence=True,
            # Discovery proves the catalogue, never that inference is healthy.
            supports_live_canary=False,
            live_canary_can_consume_quota=True,
            live_canary_can_bill=True,
            client_bound=False,
            transport="documented public HTTPS metadata endpoint",
            metadata_source=OPENROUTER_SOURCE,
            note="no inference is issued to discover FREE eligibility",
        )

    def metadata_scan(self) -> MetadataScanResult:
        try:
            with httpx.Client(
                timeout=self.timeout,
                follow_redirects=False,
                trust_env=True,
                transport=self.transport,
            ) as client:
                response = client.get(self.url)
        except httpx.TimeoutException:
            return MetadataScanResult(
                ok=False, error_class="timeout",
                error_summary="OpenRouter metadata request timed out",
                source=OPENROUTER_SOURCE, network_calls=1,
            )
        except Exception as ex:
            return MetadataScanResult(
                ok=False, error_class="network_error",
                error_summary=f"{type(ex).__name__}",
                source=OPENROUTER_SOURCE, network_calls=1,
            )
        retry_after = _retry_after_seconds(response)
        if response.status_code == 429:
            return MetadataScanResult(
                ok=False, error_class="http_429",
                error_summary="OpenRouter metadata endpoint is rate limiting",
                retry_after_sec=retry_after or 60.0,
                source=OPENROUTER_SOURCE, network_calls=1, http_status=429,
            )
        if response.status_code in (401, 403):
            return MetadataScanResult(
                ok=False, error_class=f"http_{response.status_code}",
                error_summary="OpenRouter metadata endpoint refused the request",
                source=OPENROUTER_SOURCE, network_calls=1, http_status=response.status_code,
            )
        if response.status_code != 200:
            return MetadataScanResult(
                ok=False, error_class=f"http_{response.status_code}",
                error_summary="OpenRouter metadata endpoint returned an unexpected status",
                retry_after_sec=retry_after,
                source=OPENROUTER_SOURCE, network_calls=1, http_status=response.status_code,
            )
        try:
            payload = response.json()
            entries = payload.get("data") if isinstance(payload, Mapping) else None
            if not isinstance(entries, list):
                raise ValueError("data is not a list")
        except Exception:
            return MetadataScanResult(
                ok=False, error_class="malformed_payload",
                error_summary="OpenRouter catalog payload did not match the documented shape",
                source=OPENROUTER_SOURCE, network_calls=1, http_status=200,
            )
        rows: List[Dict[str, Any]] = []
        for entry in entries:
            if not isinstance(entry, Mapping):
                continue
            model_id = str(entry.get("id") or "")
            if not model_id:
                continue
            row = {
                "model_id": model_id,
                "upstream_model_id": model_id,
                "canonical_id": f"{self.provider_id}/{model_id}",
                "provider_id": self.provider_id,
                "pricing": entry.get("pricing") if isinstance(entry.get("pricing"), Mapping) else None,
                "notes": str(entry.get("description") or entry.get("name") or ""),
                "routing": RoutingCapability.DIRECT_ROUTABLE.value,
                "cost_risk": CostRisk.FREE_QUOTA_PROBE.value,
                "source": OPENROUTER_SOURCE,
            }
            verdict = classify_free_evidence(row)
            row["free_evidence"] = verdict.evidence.value
            row["evidence_source"] = verdict.source
            row["evidence_note"] = verdict.reason
            rows.append(row)
        return MetadataScanResult(
            ok=True, models=rows, source=OPENROUTER_SOURCE,
            network_calls=1, http_status=200,
        )


def _bridge_canary_model() -> str:
    """The existing bridge's canary model, without duplicating the bridge."""
    try:
        from core.opencode_bridge import CANARY_MODEL
    except Exception:  # pragma: no cover - bridge module is in-repo
        return ""
    return str(CANARY_MODEL or "")


def _retry_after_seconds(response: httpx.Response) -> float:
    raw = response.headers.get("Retry-After") if response is not None else None
    if not raw:
        return 0.0
    try:
        return max(0.0, float(str(raw).strip()))
    except (TypeError, ValueError):
        return 0.0


# --------------------------------------------------------------- provider seeds
@dataclass(frozen=True)
class ProviderSeed:
    provider_id: str
    display_name: str
    adapter_id: str
    metadata_cost_risk: CostRisk
    live_probe_cost_risk: CostRisk
    default_enabled: bool
    default_policy: ScanPolicy
    default_mode: ScanMode
    client_bound: bool = False


PROVIDER_SEEDS: tuple = (
    ProviderSeed(
        provider_id=PROVIDER_OPENCODE,
        display_name="OpenCode / OpenCode Local Free",
        adapter_id=ADAPTER_OPENCODE_LOCAL_FREE,
        metadata_cost_risk=CostRisk.ZERO_MONETARY_METADATA,
        live_probe_cost_risk=CostRisk.FREE_QUOTA_PROBE,
        # Metadata refresh only by default: zero inference until the operator
        # explicitly raises the scan mode.
        default_enabled=True,
        default_policy=ScanPolicy.STALE_ONLY,
        default_mode=ScanMode.METADATA_ONLY,
        client_bound=True,
    ),
    ProviderSeed(
        provider_id=PROVIDER_KIRA,
        display_name="Kira AI",
        adapter_id=ADAPTER_KIRA_CATALOG,
        metadata_cost_risk=CostRisk.ZERO_MONETARY_METADATA,
        live_probe_cost_risk=CostRisk.POSSIBLE_BILLING,
        default_enabled=True,
        default_policy=ScanPolicy.STALE_ONLY,
        default_mode=ScanMode.METADATA_ONLY,
    ),
    ProviderSeed(
        provider_id=PROVIDER_OPENROUTER,
        display_name="OpenRouter",
        adapter_id=ADAPTER_OPENROUTER_CATALOG,
        metadata_cost_risk=CostRisk.ZERO_MONETARY_METADATA,
        live_probe_cost_risk=CostRisk.POSSIBLE_BILLING,
        default_enabled=True,
        default_policy=ScanPolicy.STALE_ONLY,
        default_mode=ScanMode.METADATA_ONLY,
    ),
)


def seeded_provider_ids() -> List[str]:
    return [seed.provider_id for seed in PROVIDER_SEEDS]


def build_default_adapters(
    *,
    opencode_catalog: Optional[Any] = None,
    opencode_bridge: Optional[Any] = None,
    kira_catalog_fetch: Optional[Callable[[], Sequence[Mapping[str, Any]]]] = None,
    openrouter_transport: Optional[httpx.BaseTransport] = None,
) -> Dict[str, FreeProviderAdapter]:
    """Construct the initial adapter set. Missing wiring degrades to UNSUPPORTED."""
    return {
        PROVIDER_OPENCODE: OpenCodeLocalFreeAdapter(
            catalog=opencode_catalog, bridge=opencode_bridge
        ),
        PROVIDER_KIRA: KiraAiAdapter(catalog_fetch=kira_catalog_fetch),
        PROVIDER_OPENROUTER: OpenRouterAdapter(transport=openrouter_transport),
    }


def resolve_adapter(
    provider_id: str, adapters: Mapping[str, FreeProviderAdapter]
) -> FreeProviderAdapter:
    """Every configured provider resolves to an adapter -- never to nothing.

    Without a strict-free adapter the provider is still visible (NO FREE
    ADAPTER / CONDITIONAL) and defaults to no automatic live probing.
    """
    pid = str(provider_id or "")
    found = adapters.get(pid)
    if found is not None:
        return found
    return UnsupportedProviderAdapter(pid, pid)


def seed_registry(registry, adapters: Mapping[str, FreeProviderAdapter]) -> List[str]:
    """Ensure every seeded provider exists with its declared cost classes."""
    for seed in PROVIDER_SEEDS:
        adapter = adapters.get(seed.provider_id) or UnsupportedProviderAdapter(seed.provider_id)
        registry.ensure_provider(
            seed.provider_id,
            seed.display_name,
            adapter=seed.adapter_id,
            metadata_cost_risk=seed.metadata_cost_risk,
            live_probe_cost_risk=seed.live_probe_cost_risk,
            default_enabled=seed.default_enabled,
            default_policy=seed.default_policy,
            default_mode=seed.default_mode,
        )
        registry.describe_adapter(seed.provider_id, adapter.capabilities().as_dict())
    return seeded_provider_ids()


def register_discovered_providers(
    registry, adapters: Mapping[str, FreeProviderAdapter], provider_ids: Sequence[str]
) -> List[str]:
    """Surface every configured 9Router provider in the table.

    Providers without a strict-free adapter appear as NO FREE ADAPTER and stay
    disabled with NEVER/DISABLED policy: no automatic live probing, ever.
    """
    added: List[str] = []
    for raw in provider_ids or ():
        pid = str(raw or "").strip()
        if not pid or registry.get(pid) is not None:
            continue
        adapter = resolve_adapter(pid, adapters)
        caps = adapter.capabilities()
        registry.ensure_provider(
            pid,
            caps.display_name or pid,
            adapter=caps.adapter_id,
            metadata_cost_risk=CostRisk.UNKNOWN,
            live_probe_cost_risk=CostRisk.UNKNOWN,
            default_enabled=False,
            default_policy=ScanPolicy.NEVER,
            default_mode=ScanMode.DISABLED,
        )
        registry.describe_adapter(pid, caps.as_dict())
        added.append(pid)
    return added
