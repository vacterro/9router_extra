"""Rich model explorer -- presentation model for the Combo workspace.

Pure, Qt-free logic for searching, filtering, sorting and grouping the two
model lists of the Combo editor (current combo members on the left, available
models on the right).

This module deliberately owns every matching rule so no widget callback has to
invent its own. Two properties are load-bearing:

* Nothing here mutates a combo. ``sorted()`` and friends return presentation
  order only; routing priority lives in ``StableCombo.models`` and is never
  rewritten by a view operation.
* Nothing here performs I/O. Filtering, searching and sorting read the
  discovery and health snapshots already in memory, so no keystroke can issue
  a network request or a cache write.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, replace
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from core.activation import (
    ACTIVATION_ALL,
    ACTIVATION_BLOCKED,
    ACTIVATION_DISABLED,
    ACTIVATION_ENABLED,
    ACTIVATION_OPTIONS,
    ACTIVATION_PENDING,
    ACTIVATION_ROUTABLE,
)
from core.classification import AvailabilityState, CostState

#: Health buckets, in the order they are offered. The taxonomy is the one the
#: existing single "Show" dropdown used, preserved verbatim so badge
#: semantics and the right panel's old default stay compatible.
HEALTH_ALL = "all"
HEALTH_USABLE = "usable"
HEALTH_UNTESTED = "untested"
HEALTH_ATTENTION = "attention"
HEALTH_DEAD = "dead"

HEALTH_OPTIONS: Tuple[str, ...] = (
    HEALTH_ALL, HEALTH_USABLE, HEALTH_UNTESTED, HEALTH_ATTENTION, HEALTH_DEAD,
)

#: Canonical non-LIVE states that need operator attention, including the
#: distinct timeout/route/model-invalid taxonomy (CORE-003). A temporary
#: provider failure is NEVER folded into DEAD.
ATTENTION_AVAILABILITY: frozenset = frozenset({
    "AUTH_REJECTED", "ACCESS_FORBIDDEN", "BALANCE_REQUIRED",
    "RATE_LIMITED", "CONNECT_TIMEOUT", "PROVIDER_ERROR",
    "ENDPOINT_OR_MODEL_INVALID", "MODEL_INVALID", "MODEL_MISSING",
    "ROUTE_ERROR", "MODEL_GONE", "ROUTER_DEGRADED", "DNS_FAILURE",
    "NON_API_HTML_RESPONSE", "WAF_BLOCKED", "BROWSER_CHALLENGE",
    "MODEL_DISCOVERY_UNAVAILABLE",
})

COST_FREE = "free"
COST_PAID = "paid"
COST_UNKNOWN = "unknown"
COST_ALL = "all"
COST_OPTIONS: Tuple[str, ...] = (COST_ALL, COST_FREE, COST_PAID, COST_UNKNOWN)

TESTED_ALL = "all"
TESTED_TESTED = "tested"
TESTED_NEVER = "never"

SORT_DISCOVERY = "discovery"
SORT_NAME_ASC = "name-asc"
SORT_NAME_DESC = "name-desc"
SORT_PROVIDER_ASC = "provider-asc"
SORT_PROVIDER_DESC = "provider-desc"
SORT_HEALTH = "health"
SORT_COST = "cost"
SORT_LATENCY_ASC = "latency-asc"
SORT_LATENCY_DESC = "latency-desc"
SORT_LAST_TESTED = "last-tested"
SORT_OPTIONS: Tuple[Tuple[str, str], ...] = (
    (SORT_DISCOVERY, "Original discovery order"),
    (SORT_NAME_ASC, "Model name A-Z"),
    (SORT_NAME_DESC, "Model name Z-A"),
    (SORT_PROVIDER_ASC, "Provider A-Z"),
    (SORT_PROVIDER_DESC, "Provider Z-A"),
    (SORT_HEALTH, "Health status"),
    (SORT_COST, "Cost category"),
    (SORT_LATENCY_ASC, "Latency (fastest first)"),
    (SORT_LATENCY_DESC, "Latency (slowest first)"),
    (SORT_LAST_TESTED, "Last tested (newest first)"),
)

#: Query prefixes recognised in a free-text search. They are a shortcut, never
#: a requirement: plain text is always a plain substring search.
QUERY_PREFIXES: Tuple[str, ...] = ("provider", "status", "cost")

#: Values accepted after a query prefix, mapped onto the canonical buckets.
_PREFIX_ALIASES: Dict[str, Dict[str, str]] = {
    "provider": {},
    "status": {
        "usable": HEALTH_USABLE, "live": HEALTH_USABLE, "healthy": HEALTH_USABLE,
        "untested": HEALTH_UNTESTED, "never": HEALTH_UNTESTED,
        "attention": HEALTH_ATTENTION, "dead": HEALTH_DEAD,
    },
    "cost": {
        "free": COST_FREE, "paid": COST_PAID, "unknown": COST_UNKNOWN,
        "use?": COST_UNKNOWN, "use": COST_UNKNOWN,
    },
}

#: Latency under this is "low"; ``latency_ms`` of 0 means never measured, so it
#: is never treated as fast.
LOW_LATENCY_MS = 1500.0
#: "Recently" window for tested/successful, in seconds.
RECENT_SECONDS = 24 * 3600.0


def _as_text(value: Any) -> str:
    """Missing/None metadata becomes "", never an exception and never a guess."""
    return value if isinstance(value, str) else ""


def health_bucket(availability: Optional[str]) -> str:
    """Which health bucket a record's availability string belongs to.

    ``None`` (no record at all) is UNTESTED. ``UNKNOWN`` and any other
    unmapped value match only "All", exactly as the previous single-dropdown
    filter behaved -- an unmapped state is never silently promoted to usable.
    """
    if availability is None:
        return HEALTH_UNTESTED
    value = str(availability).upper()
    if value == AvailabilityState.LIVE.value:
        return HEALTH_USABLE
    if value == AvailabilityState.DEAD.value:
        return HEALTH_DEAD
    if value in ATTENTION_AVAILABILITY:
        return HEALTH_ATTENTION
    return HEALTH_ALL


def cost_bucket(record_cost: Optional[str], cost_hint: Optional[str] = None) -> str:
    """Cost category, free/paid/unknown.

    Authoritative health evidence wins. ``cost_hint`` (published catalog
    billing metadata) is consulted only when the health record carries no
    classification at all, and only for an explicit FREE/PAID hint. Missing
    price information is NEVER read as FREE.
    """
    value = _as_text(record_cost).upper()
    if value == CostState.FREE.value:
        return COST_FREE
    if value == CostState.PAID.value:
        return COST_PAID
    hint = _as_text(cost_hint).upper()
    if value == CostState.UNKNOWN.value or not value:
        if hint == CostState.FREE.value:
            return COST_FREE
        if hint == CostState.PAID.value:
            return COST_PAID
    return COST_UNKNOWN


def has_evidence(record: Optional[Dict[str, Any]]) -> bool:
    """True when a record exists and carries any authoritative evidence.

    An absent record -- including the empty mapping ``build_row`` produces for
    it -- is the only "never tested" case. A record that exists is authoritative
    even when it has no timestamp, as long as its availability is not UNKNOWN.
    """
    if not record:
        return False
    if _as_text(record.get("last_tested_at")):
        return True
    return str(record.get("availability") or "").upper() != AvailabilityState.UNKNOWN.value


def _epoch(value: Any) -> float:
    """Best-effort ISO-8601 -> epoch. Unparseable or absent values are 0.0."""
    text = _as_text(value)
    if not text:
        return 0.0
    try:
        from datetime import datetime

        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            return parsed.timestamp()
        return parsed.timestamp()
    except (ValueError, TypeError, OSError):
        return 0.0


@dataclass(frozen=True)
class ModelRow:
    """One immutable presentation row.

    Everything a filter, a search or a sort can need, derived once from the
    discovery + health snapshots. ``discovery_index`` is the stable tiebreaker
    that makes every sort deterministic.
    """

    canonical_id: str
    model_id: str
    display_name: str
    provider_name: str
    provider_prefix: str
    provider_key: str
    provider_label: str
    health: str
    cost: str
    routing_eligible: bool
    latency_ms: float
    last_tested_at: str
    last_success_at: str
    tested: bool
    within_combo: bool
    discovery_index: int
    #: Manual per-combo activation (the operator's ON/OFF), independent of any
    #: engine-derived reason a member may also be unroutable.
    manual_enabled: bool = True
    #: Engine-derived exclusions: provider/global policy or discovery says the
    #: router will not select this member even though it is manually ON.
    blocked: bool = False
    blocked_reason: str = ""
    #: Local activation edit not yet published to the engine.
    pending_change: bool = False

    @property
    def effective_routable(self) -> bool:
        """True only when the router would actually select this member.

        Manual ON is necessary but not sufficient: a member can be switched on
        and still blocked, and the row says so rather than showing a checked
        box that implies it is routable.
        """
        return self.manual_enabled and not self.blocked

    @property
    def activation_state(self) -> str:
        """One of the ACTIVATION_* filter buckets this row belongs to."""
        if not self.manual_enabled:
            return ACTIVATION_DISABLED
        if self.pending_change:
            return ACTIVATION_PENDING
        if self.blocked:
            return ACTIVATION_BLOCKED
        return ACTIVATION_ENABLED

    @property
    def effective_reason(self) -> str:
        """Why a manually-enabled member is not routable ("" when it is)."""
        if not self.manual_enabled:
            return "manually disabled in this combo"
        if self.pending_change:
            return "activation change not yet applied"
        return self.blocked_reason

    @property
    def model_blob(self) -> str:
        """Searchable text belonging to the model identity only."""
        return " ".join((
            self.canonical_id, self.model_id, self.display_name,
        )).lower()

    @property
    def provider_blob(self) -> str:
        """Searchable text belonging to the provider identity only."""
        return " ".join((
            self.provider_name, self.provider_prefix, self.provider_label,
            self.provider_key,
        )).lower()

    @property
    def search_blob(self) -> str:
        """Lower-cased concatenation of every searchable field."""
        return f"{self.model_blob} {self.provider_blob}".strip()

    @property
    def health_badge(self) -> str:
        if self.health == HEALTH_UNTESTED:
            return "UNTESTED"
        return self.health.upper()

    @property
    def cost_badge(self) -> str:
        return {COST_FREE: "FREE", COST_PAID: "PAID"}.get(self.cost, "UNKNOWN")


UNKNOWN_PROVIDER_KEY = "\x00unknown-provider"
UNKNOWN_PROVIDER_LABEL = "(provider unavailable)"


def provider_identity(model: Any) -> Tuple[str, str]:
    """Stable (key, human label) for a discovered model's provider.

    ``provider_prefix`` is the stable routing identity and is preferred;
    ``provider_name`` is the readable fallback. When neither exists the model
    is still kept -- it lands in an explicit "(provider unavailable)" bucket
    rather than being dropped or attributed to an unrelated provider.
    """
    prefix = _as_text(getattr(model, "provider_prefix", "")).strip()
    name = _as_text(getattr(model, "provider_name", "")).strip()
    if prefix:
        return prefix, name or prefix
    if name:
        return name, name
    canonical = _as_text(getattr(model, "canonical_id", ""))
    head = canonical.split("/", 1)[0] if "/" in canonical else canonical
    if head:
        return head, head
    return UNKNOWN_PROVIDER_KEY, UNKNOWN_PROVIDER_LABEL


def _record_fields(record: Optional[Any]) -> Dict[str, Any]:
    """Normalise a health record into a plain mapping of the fields we read.

    The UI holds a ``HealthCache`` whose lookup yields ``ModelHealthRecord``
    objects; tests and callers may hand over a plain mapping. Both shapes read
    identically, so a row never silently loses latency or timestamps.
    """
    if record is None:
        return {}
    if isinstance(record, dict):
        return record
    return {
        "availability": getattr(record, "availability", None),
        "cost_status": getattr(record, "cost_status", None),
        "latency_ms": getattr(record, "latency_ms", 0.0),
        "last_tested_at": getattr(record, "last_tested_at", ""),
        "last_success_at": getattr(record, "last_success_at", ""),
    }


def build_row(model: Any, record: Optional[Any], discovery_index: int,
              within_combo: bool = False) -> ModelRow:
    """Derive one presentation row from a DiscoveredModel and its health record."""
    canonical_id = _as_text(getattr(model, "canonical_id", ""))
    provider_key, provider_label = provider_identity(model)
    fields = _record_fields(record)

    latency = 0.0
    try:
        latency = float(fields.get("latency_ms") or 0.0)
    except (TypeError, ValueError):
        latency = 0.0
    availability = fields.get("availability")
    availability = str(availability) if availability is not None else None

    return ModelRow(
        canonical_id=canonical_id,
        model_id=_as_text(getattr(model, "model_id", "")),
        display_name=_as_text(getattr(model, "display_name", "")),
        provider_name=_as_text(getattr(model, "provider_name", "")),
        provider_prefix=_as_text(getattr(model, "provider_prefix", "")),
        provider_key=provider_key,
        provider_label=provider_label,
        health=health_bucket(availability),
        cost=cost_bucket(fields.get("cost_status"), getattr(model, "cost_hint", None)),
        routing_eligible=bool(getattr(model, "routing_eligible", True)),
        latency_ms=latency,
        last_tested_at=_as_text(fields.get("last_tested_at")),
        last_success_at=_as_text(fields.get("last_success_at")),
        tested=has_evidence(fields),
        within_combo=within_combo,
        discovery_index=discovery_index,
    )


FILTER_SCOPE_MODELS = "models"
FILTER_SCOPE_PROVIDERS = "providers"
FILTER_SCOPE_COMBINED = "both"
FILTER_SCOPE_ALL = FILTER_SCOPE_COMBINED
FILTER_SCOPE_OPTIONS = (
    (FILTER_SCOPE_COMBINED, "Models + providers"),
    (FILTER_SCOPE_MODELS, "Models only"),
    (FILTER_SCOPE_PROVIDERS, "Providers only"),
)
FILTER_SCOPES = tuple(v for v, _ in FILTER_SCOPE_OPTIONS)


@dataclass(frozen=True)
class ExplorerQuery:
    """One composable query over a model list.

    Categories compose with AND; the selected alternatives inside a category
    compose with OR. An empty selection means "no constraint".
    """

    text: str = ""
    providers: frozenset = frozenset()
    health: frozenset = frozenset()
    cost: frozenset = frozenset()
    tested: str = TESTED_ALL
    routing_eligible_only: bool = False
    low_latency_only: bool = False
    recently_tested: bool = False
    recently_successful: bool = False
    #: Activation bucket: one of ACTIVATION_OPTIONS. ACTIVATION_ALL is no
    #: constraint. Composes with every other category.
    activation: str = ACTIVATION_ALL
    #: Filter scope: one of FILTER_SCOPES. Controls which identity dimension
    #: the free-text search and the category filters apply to.
    filter_mode: str = FILTER_SCOPE_COMBINED
    scope: str = FILTER_SCOPE_COMBINED
    sort: str = SORT_DISCOVERY
    group_by_provider: bool = False
    low_latency_ms: float = LOW_LATENCY_MS
    recent_seconds: float = RECENT_SECONDS

    def __post_init__(self):
        value = self.filter_mode if self.filter_mode in FILTER_SCOPES else (
            self.scope if self.scope in FILTER_SCOPES else FILTER_SCOPE_COMBINED
        )
        if value != self.filter_mode:
            object.__setattr__(self, "filter_mode", value)
        if value != self.scope:
            object.__setattr__(self, "scope", value)

    def is_filtering(self) -> bool:
        scope = self.filter_mode if self.filter_mode in FILTER_SCOPES else FILTER_SCOPE_COMBINED
        uses_models = scope != FILTER_SCOPE_PROVIDERS
        uses_providers = scope != FILTER_SCOPE_MODELS
        return bool(
            (uses_providers and (self.text.strip() or self.providers))
            or (uses_models and (
                self.text.strip() or self.health or self.cost
                or self.tested != TESTED_ALL or self.routing_eligible_only
                or self.low_latency_only or self.recently_tested
                or self.recently_successful or self.activation != ACTIVATION_ALL
            ))
        )

    def to_dict(self) -> Dict[str, Any]:
        """A JSON-safe form, so a remembered view survives a restart.

        Presentation only: this serialises how the list is *shown*, never a
        combo's routing order.
        """
        return {
            "text": self.text,
            "providers": sorted(self.providers),
            "health": sorted(self.health),
            "cost": sorted(self.cost),
            "tested": self.tested,
            "routing_eligible_only": self.routing_eligible_only,
            "low_latency_only": self.low_latency_only,
            "recently_tested": self.recently_tested,
            "recently_successful": self.recently_successful,
            "activation": self.activation,
            "filter_mode": self.filter_mode,
            "scope": self.filter_mode,
            "sort": self.sort,
            "group_by_provider": self.group_by_provider,
        }

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "ExplorerQuery":
        """Rebuild a remembered query, ignoring unknown or malformed fields."""
        data = data if isinstance(data, dict) else {}
        activation = str(data.get("activation") or ACTIVATION_ALL)
        if activation not in ACTIVATION_OPTIONS:
            activation = ACTIVATION_ALL
        scope_raw = data.get("filter_mode", data.get("scope", FILTER_SCOPE_COMBINED))
        scope = str(scope_raw) if scope_raw in FILTER_SCOPES else FILTER_SCOPE_COMBINED
        try:
            return cls(
                text=str(data.get("text") or ""),
                providers=frozenset(str(v) for v in (data.get("providers") or [])),
                health=frozenset(str(v) for v in (data.get("health") or [])),
                cost=frozenset(str(v) for v in (data.get("cost") or [])),
                tested=str(data.get("tested") or TESTED_ALL),
                routing_eligible_only=bool(data.get("routing_eligible_only")),
                low_latency_only=bool(data.get("low_latency_only")),
                recently_tested=bool(data.get("recently_tested")),
                recently_successful=bool(data.get("recently_successful")),
                activation=activation,
                filter_mode=scope,
                scope=scope,
                sort=str(data.get("sort") or SORT_DISCOVERY),
                group_by_provider=bool(data.get("group_by_provider")),
            )
        except (TypeError, ValueError):
            return cls()


def parse_text(raw: str) -> Tuple[List[str], Dict[str, List[str]]]:
    """Split a free-text query into plain terms and prefixed constraints.

    ``status:dead gpt`` -> (["gpt"], {"status": ["dead"]}). Whitespace
    separates terms; an unmatched ``prefix:`` stays a literal term so typing a
    colon never loses text.
    """
    terms: List[str] = []
    prefixed: Dict[str, List[str]] = {}
    for token in (raw or "").split():
        lowered = token.lower()
        head, sep, tail = lowered.partition(":")
        if sep and head in QUERY_PREFIXES and tail:
            canonical = _PREFIX_ALIASES[head].get(tail, tail)
            prefixed.setdefault(head, []).append(canonical)
            continue
        terms.append(lowered)
    return terms, prefixed


def matches(row: ModelRow, query: ExplorerQuery, now: Optional[float] = None) -> bool:
    """Does this row satisfy every active constraint?"""
    if not query.text.strip() and not query.providers and not query.health \
            and not query.cost and query.tested == TESTED_ALL \
            and not any((query.routing_eligible_only, query.low_latency_only,
                         query.recently_tested, query.recently_successful)) \
            and query.activation == ACTIVATION_ALL:
        return True

    terms, prefixed = parse_text(query.text)

    scope = query.filter_mode if query.filter_mode in FILTER_SCOPES else FILTER_SCOPE_COMBINED
    uses_models = scope != FILTER_SCOPE_PROVIDERS
    uses_providers = scope != FILTER_SCOPE_MODELS

    # Plain terms: AND across terms, OR across the row's searchable fields.
    # The scope selects which identity dimension those fields come from.
    blobs = []
    if uses_models:
        blobs.append(row.model_blob)
    if uses_providers:
        blobs.append(row.provider_blob)
    blob = " ".join(blobs)
    for term in terms:
        if term not in blob:
            return False

    # ``provider:`` composes with (and narrows) the provider selector, but
    # only when the current scope actually uses the provider dimension.
    wanted_providers = set(query.providers) if uses_providers else set()
    if uses_providers:
        for value in prefixed.get("provider", []):
            wanted_providers.add(value)
    if wanted_providers:
        haystack = (
            row.provider_key.lower(), row.provider_label.lower(), row.provider_name.lower(),
        )
        if not any(wanted in haystack for wanted in wanted_providers):
            return False

    if uses_models and query.health:
        if row.health not in query.health and row.health != HEALTH_ALL:
            return False
        # An unmapped availability matches only "All", never a specific bucket.
        if row.health == HEALTH_ALL and HEALTH_ALL not in query.health:
            return False

    # ``cost:`` and ``status:`` constrain on their own, and additionally
    # narrow an already-selected category. They are model-level facets, so
    # they only apply when the scope uses the model dimension.
    if uses_models:
        wanted_cost = set(query.cost)
        for value in prefixed.get("cost", []):
            wanted_cost.add(value)
        if wanted_cost and row.cost not in wanted_cost:
            return False

        if prefixed.get("status"):
            wanted_health = set(query.health) | {
                _PREFIX_ALIASES["status"].get(value, value) for value in prefixed["status"]
            }
            if row.health not in wanted_health and row.health != HEALTH_ALL:
                return False

    if uses_models:
        if query.tested == TESTED_TESTED and not row.tested:
            return False
        if query.tested == TESTED_NEVER and row.tested:
            return False

        if query.routing_eligible_only and not row.routing_eligible:
            return False

    if uses_models and query.activation != ACTIVATION_ALL:
        if query.activation == ACTIVATION_ENABLED and not row.manual_enabled:
            return False
        if query.activation == ACTIVATION_DISABLED and row.manual_enabled:
            return False
        if query.activation == ACTIVATION_ROUTABLE and not row.effective_routable:
            return False
        if query.activation == ACTIVATION_BLOCKED and not (row.manual_enabled and row.blocked):
            return False
        if query.activation == ACTIVATION_PENDING and not row.pending_change:
            return False

    if uses_models and query.low_latency_only:
        if row.latency_ms <= 0.0 or row.latency_ms > query.low_latency_ms:
            return False

    clock = time.time() if now is None else now
    if uses_models and query.recently_tested:
        tested_at = _epoch(row.last_tested_at)
        if not tested_at or clock - tested_at > query.recent_seconds:
            return False
    if uses_models and query.recently_successful:
        succeeded_at = _epoch(row.last_success_at)
        if not succeeded_at or clock - succeeded_at > query.recent_seconds:
            return False

    return True


# Health sort rank: usable first, then attention, dead, untested, then unmapped.
_HEALTH_RANK = {
    HEALTH_USABLE: 0, HEALTH_ATTENTION: 1, HEALTH_DEAD: 2,
    HEALTH_UNTESTED: 3, HEALTH_ALL: 4,
}
_COST_RANK = {COST_FREE: 0, COST_PAID: 1, COST_UNKNOWN: 2}


def sort_rows(rows: Sequence[ModelRow], query: ExplorerQuery) -> List[ModelRow]:
    """Presentation order only. Never touches combo routing priority.

    Unknown values (no latency, no test timestamp) always sort last and never
    outrank a measured value, in either direction.
    """
    if query.sort == SORT_DISCOVERY:
        return list(rows)
    if query.sort == SORT_NAME_ASC:
        return sorted(rows, key=lambda r: (r.display_name.casefold(), r.canonical_id))
    if query.sort == SORT_NAME_DESC:
        return sorted(rows, key=lambda r: r.display_name.casefold(), reverse=True)
    if query.sort == SORT_PROVIDER_ASC:
        return sorted(rows, key=lambda r: (r.provider_label.casefold(), r.canonical_id))
    if query.sort == SORT_PROVIDER_DESC:
        return sorted(rows, key=lambda r: r.provider_label.casefold(), reverse=True)
    if query.sort == SORT_HEALTH:
        return sorted(
            rows, key=lambda r: (_HEALTH_RANK.get(r.health, 9), r.canonical_id)
        )
    if query.sort == SORT_COST:
        return sorted(rows, key=lambda r: (_COST_RANK.get(r.cost, 9), r.canonical_id))
    if query.sort == SORT_LATENCY_ASC:
        return sorted(rows, key=lambda r: (r.latency_ms <= 0.0, r.latency_ms, r.canonical_id))
    if query.sort == SORT_LATENCY_DESC:
        return sorted(
            rows, key=lambda r: (r.latency_ms <= 0.0, -r.latency_ms, r.canonical_id)
        )
    if query.sort == SORT_LAST_TESTED:
        return sorted(
            rows, key=lambda r: (not _epoch(r.last_tested_at), -_epoch(r.last_tested_at),
                                 r.canonical_id)
        )
    return list(rows)


def group_rows(rows: Sequence[ModelRow]) -> List[ModelRow]:
    """Contiguous provider blocks, each block internally discovery-stable.

    Grouping is a reordering of the presentation list; it adds no synthetic
    rows, so the caller's keyed row index stays a true row index.
    """
    blocks: Dict[str, List[ModelRow]] = {}
    for row in rows:
        blocks.setdefault(row.provider_key, []).append(row)
    ordered_keys = sorted(
        blocks,
        key=lambda key: (
            key == UNKNOWN_PROVIDER_KEY,
            min(r.provider_label.casefold() for r in blocks[key]),
        ),
    )
    grouped: List[ModelRow] = []
    for key in ordered_keys:
        grouped.extend(
            sorted(blocks[key], key=lambda r: (r.discovery_index, r.canonical_id))
        )
    return grouped


@dataclass
class ProviderEntry:
    """A provider option: stable identity, readable label and model count."""

    key: str
    label: str
    count: int
    available: bool = True


def provider_entries(rows: Iterable[ModelRow],
                     previous: Optional[Dict[str, ProviderEntry]] = None
                     ) -> List[ProviderEntry]:
    """Provider catalog for the selector, with per-provider model counts.

    A provider the user has already selected that disappears from the refreshed
    catalog keeps its selection (count 0, marked unavailable) so their
    intention stays visible and is never silently swapped for another provider.
    """
    counts: Dict[str, Tuple[str, int]] = {}
    for row in rows:
        label, count = counts.get(row.provider_key, (row.provider_label, 0))
        counts[row.provider_key] = (label or row.provider_label, count + 1)

    entries: List[ProviderEntry] = []
    for key, (label, count) in counts.items():
        entries.append(ProviderEntry(key=key, label=label, count=count))
    for key, entry in (previous or {}).items():
        if key not in counts:
            entries.append(replace(entry, count=0, available=False))
    entries.sort(key=lambda e: (e.key == UNKNOWN_PROVIDER_KEY, e.label.casefold()))
    return entries


def summarize(query: ExplorerQuery, visible: int, total: int) -> str:
    """Compact, readable active-filter summary plus the visible/total counts."""
    scope = query.filter_mode if query.filter_mode in FILTER_SCOPES else FILTER_SCOPE_COMBINED
    uses_models = scope != FILTER_SCOPE_PROVIDERS
    uses_providers = scope != FILTER_SCOPE_MODELS
    parts: List[str] = []
    if scope != FILTER_SCOPE_COMBINED:
        parts.append(f"scope: {scope}")
    if query.text.strip():
        parts.append(f'text "{query.text.strip()}"')
    if uses_providers and query.providers:
        count = len(query.providers)
        parts.append(f"{count} provider" + ("s" if count > 1 else ""))
    if uses_models:
        if query.health:
            names = ", ".join(sorted(query.health))
            parts.append(f"health: {names}")
        if query.cost:
            names = ", ".join(sorted(query.cost))
            parts.append(f"cost: {names}")
        if query.tested == TESTED_TESTED:
            parts.append("tested")
        elif query.tested == TESTED_NEVER:
            parts.append("never tested")
        if query.activation != ACTIVATION_ALL:
            parts.append(f"activation: {query.activation}")
        for flag, label in (
            (query.routing_eligible_only, "routing eligible"),
            (query.low_latency_only, "low latency"),
            (query.recently_tested, "recently tested"),
            (query.recently_successful, "recently successful"),
        ):
            if flag:
                parts.append(label)
    active = " · ".join(parts)
    where = f"Showing {visible} of {total} models"
    return f"{where} · {active}" if active else where


class ModelExplorer:
    """The presentation model itself: rows in, ordered visible rows out.

    The caller keeps the widget rows in canonical order and hides/reorders them
    from ``visible``; this class never writes to a widget and never mutates a
    combo, which is what keeps the routing-order invariant structural.
    """

    def __init__(self, rows: Optional[Sequence[ModelRow]] = None):
        self._rows: List[ModelRow] = list(rows or [])
        self._query: ExplorerQuery = ExplorerQuery()
        self._visible: List[ModelRow] = list(self._rows)

    @property
    def rows(self) -> List[ModelRow]:
        return list(self._rows)

    @property
    def query(self) -> ExplorerQuery:
        return self._query

    @property
    def visible(self) -> List[ModelRow]:
        return list(self._visible)

    def ordered_all(self) -> List[ModelRow]:
        """Every row, in the current presentation order, nothing hidden.

        The panel needs all rows present in the widget (so hiding a row is a
        view operation and never a membership change), which is why filtering
        and ordering are reported separately.
        """
        ordered = sort_rows(self._rows, self._query)
        return group_rows(ordered) if self._query.group_by_provider else ordered

    def set_rows(self, rows: Sequence[ModelRow]) -> None:
        self._rows = list(rows)
        self.reapply()

    def set_query(self, query: ExplorerQuery) -> None:
        self._query = query
        self.reapply()

    def patch_query(self, **changes: Any) -> None:
        self._query = replace(self._query, **changes)
        self.reapply()

    def reapply(self) -> None:
        matched = [row for row in self._rows if matches(row, self._query)]
        ordered = sort_rows(matched, self._query)
        self._visible = group_rows(ordered) if self._query.group_by_provider else ordered

    @property
    def total(self) -> int:
        return len(self._rows)

    @property
    def visible_count(self) -> int:
        return len(self._visible)

    def summary(self) -> str:
        return summarize(self._query, len(self._visible), len(self._rows))


# --------------------------------------------------------------------------
# One runnable self-check for the non-trivial logic above (no framework).
# --------------------------------------------------------------------------
def _self_check() -> None:  # pragma: no cover - executed via __main__
    from core.discovery import DiscoveredModel

    models = [
        DiscoveredModel("alpha/gpt-4", "Alpha Labs", "alpha", "c", "gpt-4", "GPT-4"),
        DiscoveredModel("beta/claude", "Beta Cloud", "beta", "c", "claude", "Claude"),
        DiscoveredModel("alpha/gpt-3", "Alpha Labs", "alpha", "c", "gpt-3", "GPT-3",
                        routing_eligible=False),
        DiscoveredModel("beta/mystery", "", "", "c", "mystery", "Mystery"),
    ]
    rows = [build_row(m, None, i) for i, m in enumerate(models)]
    by_id = {r.canonical_id: r for r in rows}

    # Unknown provider metadata neither crashes nor drops the model.
    assert rows[3].provider_key and rows[3].provider_label
    # Untested models are never usable and never dead.
    assert all(r.health == HEALTH_UNTESTED for r in rows)
    # Missing price information is never FREE.
    assert all(r.cost == COST_UNKNOWN for r in rows)

    explorer = ModelExplorer(rows)
    assert explorer.visible_count == 4

    # Case-insensitive partial-name matching across any field.
    explorer.patch_query(text="CLAU")
    assert [r.canonical_id for r in explorer.visible] == ["beta/claude"]
    explorer.patch_query(text="alpha")
    assert explorer.visible_count == 2  # both canonical_id and provider label match

    # Multiple whitespace terms are ANDed.
    explorer.patch_query(text="alpha gpt-4")
    assert [r.canonical_id for r in explorer.visible] == ["alpha/gpt-4"]

    # Prefix shortcuts.
    explorer.patch_query(text="provider:beta")
    assert {r.canonical_id for r in explorer.visible} == {"beta/claude", "beta/mystery"}
    explorer.patch_query(text="status:dead")
    assert explorer.visible_count == 0
    explorer.patch_query(text="cost:free")
    assert explorer.visible_count == 0  # no cost metadata anywhere yet

    # Provider multi-select composes with everything else.
    explorer.patch_query(text="", providers=frozenset({"alpha", "beta"}))
    assert explorer.visible_count == 4
    explorer.patch_query(providers=frozenset({"alpha"}))
    assert explorer.visible_count == 2

    # Routing eligibility is a real filter, not a hint.
    explorer.patch_query(providers=frozenset(), routing_eligible_only=True)
    assert explorer.visible_count == 3

    # Sorting is presentation-only: the source order is untouched.
    before = [r.discovery_index for r in explorer.rows]
    explorer.patch_query(sort=SORT_NAME_DESC)
    assert [r.discovery_index for r in explorer.rows] == before
    assert explorer.visible[0].display_name == "Mystery"
    explorer.patch_query(sort=SORT_DISCOVERY)
    assert explorer.visible[0].discovery_index == 0

    # Grouping produces contiguous provider blocks.
    explorer.patch_query(group_by_provider=True)
    keys = [r.provider_key for r in explorer.visible]
    assert keys == sorted(keys, key=lambda k: (k == UNKNOWN_PROVIDER_KEY, k))

    # A health record's cost and availability drive the buckets.
    seeded = build_row(
        models[0], {"availability": "LIVE", "cost_status": "FREE", "latency_ms": 900.0,
                    "last_tested_at": "2026-10-18T00:00:00"}, 0,
    )
    assert seeded.health == HEALTH_USABLE
    assert seeded.cost == COST_FREE
    assert seeded.tested is True
    explorer.set_rows([seeded, rows[1]])
    explorer.patch_query(routing_eligible_only=False, group_by_provider=False,
                         sort=SORT_DISCOVERY)
    explorer.patch_query(health=frozenset({HEALTH_USABLE}))
    assert [r.canonical_id for r in explorer.visible] == ["alpha/gpt-4"]
    explorer.patch_query(health=frozenset())
    explorer.patch_query(low_latency_only=True)
    assert [r.canonical_id for r in explorer.visible] == ["alpha/gpt-4"]
    explorer.patch_query(low_latency_only=False)
    explorer.patch_query(tested=TESTED_TESTED)
    assert [r.canonical_id for r in explorer.visible] == ["alpha/gpt-4"]

    # A vanished provider keeps its entry, marked unavailable.
    entries = provider_entries([seeded])
    entries = provider_entries([seeded], {e.key: e for e in entries})
    assert entries and all(e.available for e in entries)
    vanished = provider_entries([], {e.key: e for e in entries})
    assert vanished and all(not e.available and e.count == 0 for e in vanished)

    print("model_explorer self-check OK")


if __name__ == "__main__":  # pragma: no cover
    _self_check()
