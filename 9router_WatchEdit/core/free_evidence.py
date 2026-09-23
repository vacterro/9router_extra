"""
9router_WatchEdit - FREE fallback evidence model (FREE-FALLBACK-001).

This module owns the vocabulary shared by the provider registry, the provider
adapters, the cost-aware scan controller and the FREE Fallback tab.

Four dimensions are deliberately kept independent; collapsing them into one
``free=True`` flag is what makes a scanner burn quota and money on routes that
were never free in the first place:

  free_evidence     - was ZERO MONETARY COST proven by authoritative metadata?
  provider_health   - is the provider/route currently observed working?
  routing_capability- can this 9Router installation actually route the model?
  cost_risk         - what would PROBING it cost (money vs. free quota)?

Evidence rules (never inferred, only classified):

  * STRICT_FREE requires machine-verifiable zero-cost evidence from an
    authoritative provider source: explicit zero pricing, an official free
    route, or an explicit ``is_free`` catalogue flag. Nothing else.
  * Trial/signup/promotional credit, coupons and "first N requests free"
    marketing are CONDITIONAL_FREE at best and are NEVER auto-synced into the
    strict FREE fallback tail.
  * ``oc/*``-style client-bound free models are CLIENT_BOUND_FREE: the model is
    genuinely free, but an arbitrary third-party HTTP client may not call it.
    Only the official local runtime bridge may execute them.
  * A failed live probe never retroactively erases authoritative FREE evidence;
    only a successful authoritative refresh that no longer advertises the model
    withdraws it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping, Optional, Sequence, Tuple

# ----------------------------------------------------------------- scan policy
class ScanPolicy(str, Enum):
    """WHEN a provider may be scanned at all."""

    ALWAYS = "ALWAYS"
    STALE_ONLY = "STALE_ONLY"
    MANUAL = "MANUAL"
    NEVER = "NEVER"


class ScanMode(str, Enum):
    """WHAT a provider scan is allowed to do."""

    METADATA_ONLY = "METADATA_ONLY"
    METADATA_AND_LIVE_PROBE = "METADATA_AND_LIVE_PROBE"
    DISABLED = "DISABLED"


# ------------------------------------------------------------------ cost risks
class CostRisk(str, Enum):
    """Monetary billing risk vs. quota consumption, never a single boolean."""

    ZERO_MONETARY_METADATA = "ZERO_MONETARY_METADATA"
    FREE_QUOTA_PROBE = "FREE_QUOTA_PROBE"
    ACCOUNT_CONDITIONAL = "ACCOUNT_CONDITIONAL"
    POSSIBLE_BILLING = "POSSIBLE_BILLING"
    UNKNOWN = "UNKNOWN"


#: Risks that can never create a monetary charge for the operator.
NON_MONETARY_COST_RISKS = frozenset(
    {CostRisk.ZERO_MONETARY_METADATA, CostRisk.FREE_QUOTA_PROBE}
)

COST_RISK_BADGES = {
    CostRisk.ZERO_MONETARY_METADATA: "$0 METADATA",
    CostRisk.FREE_QUOTA_PROBE: "FREE QUOTA",
    CostRisk.ACCOUNT_CONDITIONAL: "ACCOUNT CONDITIONAL",
    CostRisk.POSSIBLE_BILLING: "POSSIBLE COST",
    CostRisk.UNKNOWN: "UNKNOWN COST",
}


def cost_risk_coerce(value: Any, default: CostRisk = CostRisk.UNKNOWN) -> CostRisk:
    try:
        return CostRisk(value)
    except (TypeError, ValueError):
        return default


def cost_risk_blocks_live_probe(value: Any) -> bool:
    """True when a live probe is REFUSED unless the operator explicitly consents.

    POSSIBLE_BILLING and UNKNOWN are refused by default; ACCOUNT_CONDITIONAL is
    refused as well because account-scoped credit is not proof of zero monetary
    cost. Only proven zero-monetary metadata and free-quota probes pass.
    """
    return cost_risk_coerce(value) not in NON_MONETARY_COST_RISKS


# -------------------------------------------------------------- free evidence
class FreeEvidence(str, Enum):
    """Model-level eligibility classification (milestone 3 vocabulary)."""

    STRICT_FREE = "STRICT_FREE"
    CONDITIONAL_FREE = "CONDITIONAL_FREE"
    UNKNOWN_COST = "UNKNOWN_COST"
    PAID = "PAID"
    CLIENT_BOUND_FREE = "CLIENT_BOUND_FREE"
    WITHDRAWN = "WITHDRAWN"


FREE_EVIDENCE_ORDER = (
    FreeEvidence.STRICT_FREE,
    FreeEvidence.CLIENT_BOUND_FREE,
    FreeEvidence.CONDITIONAL_FREE,
    FreeEvidence.UNKNOWN_COST,
    FreeEvidence.PAID,
    FreeEvidence.WITHDRAWN,
)


class ProviderHealth(str, Enum):
    HEALTHY = "HEALTHY"
    UNVERIFIED = "UNVERIFIED"
    DEGRADED = "DEGRADED"
    RATE_LIMITED = "RATE_LIMITED"
    AUTH_FAILED = "AUTH_FAILED"
    DEAD = "DEAD"
    UNKNOWN = "UNKNOWN"


class RoutingCapability(str, Enum):
    DIRECT_ROUTABLE = "DIRECT_ROUTABLE"
    LOCAL_BRIDGE_REQUIRED = "LOCAL_BRIDGE_REQUIRED"
    UNSUPPORTED = "UNSUPPORTED"
    UNKNOWN = "UNKNOWN"


#: Health states that positively exclude a route from the FREE tail. UNKNOWN is
#: NOT here: under the "better than zero" policy an unverified but provably
#: zero-cost route may still sit at the absolute tail.
BLOCKING_HEALTH_STATES = frozenset(
    {ProviderHealth.DEAD, ProviderHealth.AUTH_FAILED}
)

# ------------------------------------------------------------------- sources
SOURCE_EXPLICIT_ZERO_PRICE = "explicit_zero_price"
SOURCE_EXPLICIT_FREE_ROUTE = "explicit_free_route"
SOURCE_PROVIDER_CATALOG_IS_FREE = "provider_catalog_is_free"
SOURCE_EXPLICIT_FREE_MODEL_ID = "explicit_free_model_id"
SOURCE_CLIENT_BOUND_FREE_ID = "explicit_free_model_id_client_bound"
SOURCE_PAID_PRICING = "paid_pricing_metadata"
SOURCE_PROMO_OR_CREDIT = "promotional_credit_metadata"
SOURCE_PROVIDER_WITHDREW = "provider_withdrew_model"
SOURCE_UNKNOWN = "no_authoritative_free_evidence"

#: Marketing/credit markers. Their presence NEVER yields STRICT_FREE.
PROMO_MARKERS = re.compile(
    r"(?i)\b(trial|promo|promotion|promotional|coupon|signup|sign-up|sign up|"
    r"credit|credits|bonus|introductory|freebies|first\s+\d+\s+|"
    r"limited[-_ ]time)\b"
)

FREE_ROUTE_SUFFIX = ":free"
FREE_MODEL_ID_SUFFIX = "-free"

_PRICE_KEYS = (
    "prompt", "completion", "input", "output", "request", "image",
    "price_input_vnd", "price_output_vnd", "price_cache_read_vnd",
    "price_cache_write_vnd",
)


def _as_number(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        text = str(value).strip()
        if not text:
            return None
        return float(text)
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class EvidenceVerdict:
    evidence: FreeEvidence
    source: str
    reason: str = ""

    @property
    def strict(self) -> bool:
        return self.evidence == FreeEvidence.STRICT_FREE

    @property
    def auto_tail_eligible(self) -> bool:
        """CLIENT_BOUND_FREE may reach SAIFREN only through the ocf bridge path."""
        return self.evidence == FreeEvidence.STRICT_FREE


def _pricing_values(pricing: Any) -> Tuple[Optional[float], int]:
    """Return (max_price, counted_fields) for a provider pricing mapping."""
    if not isinstance(pricing, Mapping):
        return None, 0
    values = []
    for key, raw in pricing.items():
        if str(key).lower() not in _PRICE_KEYS:
            continue
        number = _as_number(raw)
        if number is not None:
            values.append(number)
    if not values:
        return None, 0
    return max(values), len(values)


def _promo_text(row: Mapping[str, Any]) -> str:
    parts = []
    for key in ("promo", "promo_text", "notes", "description", "marketing", "title"):
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(value)
    return " ".join(parts)


def classify_free_evidence(row: Mapping[str, Any]) -> EvidenceVerdict:
    """Classify ONE model row into the strict free-evidence ladder.

    Deterministic order: withdrawal > explicit paid pricing > promotional
    credit > explicit zero-cost evidence > unknown. Ambiguous marketing text is
    never promoted to STRICT_FREE.
    """
    if not isinstance(row, Mapping):
        return EvidenceVerdict(FreeEvidence.UNKNOWN_COST, SOURCE_UNKNOWN, "no metadata row")

    if row.get("withdrawn") is True:
        return EvidenceVerdict(FreeEvidence.WITHDRAWN, SOURCE_PROVIDER_WITHDREW,
                               "provider no longer advertises this model")

    model_id = str(row.get("model_id") or row.get("upstream_model_id") or row.get("id") or "")
    promo = _promo_text(row)
    is_free = row.get("is_free")
    price_max, price_fields = _pricing_values(row.get("pricing"))

    # 1. Explicit paid evidence always wins over a free-looking name.
    if is_free is False or row.get("is_partner") is True or row.get("billing") == "paid":
        return EvidenceVerdict(FreeEvidence.PAID, SOURCE_PAID_PRICING,
                               "provider metadata marks the model as billed")
    if price_max is not None and price_max > 0:
        return EvidenceVerdict(FreeEvidence.PAID, SOURCE_PAID_PRICING,
                               "provider pricing metadata is non-zero")

    # 2. Promotional/credit evidence is conditional, never strict.
    if PROMO_MARKERS.search(promo):
        return EvidenceVerdict(FreeEvidence.CONDITIONAL_FREE, SOURCE_PROMO_OR_CREDIT,
                               "promotional/credit wording is not permanent zero cost")

    # 3. Explicit zero-cost evidence.
    if is_free is True:
        return EvidenceVerdict(FreeEvidence.STRICT_FREE, SOURCE_PROVIDER_CATALOG_IS_FREE,
                               "authoritative catalogue declares is_free")
    if price_fields and price_max == 0:
        return EvidenceVerdict(FreeEvidence.STRICT_FREE, SOURCE_EXPLICIT_ZERO_PRICE,
                               "authoritative pricing metadata is exactly zero")
    if model_id.endswith(FREE_ROUTE_SUFFIX):
        return EvidenceVerdict(FreeEvidence.STRICT_FREE, SOURCE_EXPLICIT_FREE_ROUTE,
                               "official free route")
    if model_id.endswith(FREE_MODEL_ID_SUFFIX):
        if row.get("client_bound") is True:
            return EvidenceVerdict(FreeEvidence.CLIENT_BOUND_FREE, SOURCE_CLIENT_BOUND_FREE_ID,
                                   "free tier is restricted to the official client")
        return EvidenceVerdict(FreeEvidence.STRICT_FREE, SOURCE_EXPLICIT_FREE_MODEL_ID,
                               "explicit free model identity")

    return EvidenceVerdict(FreeEvidence.UNKNOWN_COST, SOURCE_UNKNOWN,
                           "no authoritative zero-cost evidence")


@dataclass
class ModelEvidence:
    """One discovered model with all four dimensions kept separate."""

    canonical_id: str
    upstream_model_id: str
    provider_id: str
    evidence: FreeEvidence = FreeEvidence.UNKNOWN_COST
    evidence_source: str = SOURCE_UNKNOWN
    provider_health: ProviderHealth = ProviderHealth.UNKNOWN
    routing: RoutingCapability = RoutingCapability.UNKNOWN
    cost_risk: CostRisk = CostRisk.UNKNOWN
    scanner_managed: bool = True
    synced_to_saifren: bool = False
    synced_at: str = ""
    last_seen: str = ""
    last_success_use: str = ""
    exclusion_reason: str = ""
    note: str = ""

    @property
    def saifren_eligible(self) -> bool:
        eligible, _ = tail_eligibility(
            self.evidence,
            provider_health=self.provider_health,
            routing=self.routing,
            cost_risk=self.cost_risk,
        )
        return eligible

    def as_dict(self) -> dict:
        return {
            "canonical_id": self.canonical_id,
            "upstream_model_id": self.upstream_model_id,
            "provider_id": self.provider_id,
            "free_evidence": self.evidence.value,
            "evidence_source": self.evidence_source,
            "provider_health": self.provider_health.value,
            "routing": self.routing.value,
            "cost_risk": self.cost_risk.value,
            "scanner_managed": bool(self.scanner_managed),
            "synced_to_saifren": bool(self.synced_to_saifren),
            "synced_at": self.synced_at,
            "last_seen": self.last_seen,
            "last_success_use": self.last_success_use,
            "saifren_eligible": bool(self.saifren_eligible),
            "exclusion_reason": self.exclusion_reason or self.exclusion_text(),
            "note": self.note,
        }

    def exclusion_text(self) -> str:
        if self.evidence == FreeEvidence.STRICT_FREE:
            if self.provider_health in BLOCKING_HEALTH_STATES:
                return f"health {self.provider_health.value}"
            if self.routing == RoutingCapability.UNSUPPORTED:
                return "no supported routing path"
            return ""
        if self.evidence == FreeEvidence.CLIENT_BOUND_FREE:
            return "client-bound free tier: ocf bridge contract only"
        return f"free evidence {self.evidence.value}"


def tail_eligibility(
    evidence: FreeEvidence,
    *,
    provider_health: ProviderHealth,
    routing: RoutingCapability,
    cost_risk: CostRisk,
    client_bound_bridge_eligible: bool = False,
) -> Tuple[bool, str]:
    """Can this route be appended to the SAIFREN FREE tail?

    Rules ("better than zero", never "better than reliable"):

      * monetary cost must be strictly zero for the executed path;
      * the routing mechanism must be supported by this installation;
      * positively DEAD/AUTH_FAILED routes are excluded even with old FREE
        evidence;
      * health UNKNOWN stays eligible when everything else proves zero cost;
      * CLIENT_BOUND_FREE is eligible only through the official bridge contract.
    """
    if evidence == FreeEvidence.WITHDRAWN:
        return False, "withdrawn"
    if evidence == FreeEvidence.PAID:
        return False, "paid"
    if evidence == FreeEvidence.CONDITIONAL_FREE:
        return False, "conditional/free-credit only"
    if evidence == FreeEvidence.UNKNOWN_COST:
        return False, "unknown cost"
    if provider_health in BLOCKING_HEALTH_STATES:
        return False, f"health {provider_health.value}"
    if routing == RoutingCapability.UNSUPPORTED:
        return False, "no supported routing path"
    if evidence == FreeEvidence.CLIENT_BOUND_FREE:
        if not client_bound_bridge_eligible:
            return False, "client-bound free tier: ocf bridge contract only"
        if routing not in (RoutingCapability.LOCAL_BRIDGE_REQUIRED,
                          RoutingCapability.DIRECT_ROUTABLE):
            return False, "client-bound free tier: no local bridge"
        return True, ""
    # STRICT_FREE
    if cost_risk not in NON_MONETARY_COST_RISKS:
        return False, f"probe/route cost risk {cost_risk.value}"
    return True, ""


def summarise(evidences: Sequence[FreeEvidence]) -> Tuple[int, int]:
    """(strict_free_count, conditional_or_unknown_count)."""
    strict = sum(1 for e in evidences if e == FreeEvidence.STRICT_FREE)
    conditional = sum(
        1 for e in evidences
        if e in (FreeEvidence.CONDITIONAL_FREE, FreeEvidence.UNKNOWN_COST)
    )
    return strict, conditional
