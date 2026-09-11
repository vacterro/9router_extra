"""
9router_WatchEdit - Health & Cost Classification Engine
Decouples pure availability states from billing cost, tracks evidence-specific streaks,
and prevents false-positive DEAD classifications.
"""
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Dict, Any
import json
import re

from config import redact_secrets, FAILURE_STREAK_FOR_DEAD

class AvailabilityState(str, Enum):
    LIVE = "LIVE"
    PENDING = "PENDING"
    AUTH_REJECTED = "AUTH_REJECTED"
    AUTH = "AUTH_REJECTED"  # Backward-compatible enum alias.
    ACCESS_FORBIDDEN = "ACCESS_FORBIDDEN"
    BALANCE_REQUIRED = "BALANCE_REQUIRED"
    BALANCE = "BALANCE_REQUIRED"  # Backward-compatible enum alias.
    RATE_LIMITED = "RATE_LIMITED"
    RATE_LIMIT = "RATE_LIMITED"  # Backward-compatible enum alias.
    CONNECT_TIMEOUT = "CONNECT_TIMEOUT"
    TIMEOUT = "CONNECT_TIMEOUT"  # Backward-compatible enum alias.
    PROVIDER_ERROR = "PROVIDER_ERROR"
    TEMP_ERROR = "PROVIDER_ERROR"  # Backward-compatible enum alias.
    ENDPOINT_OR_MODEL_INVALID = "ENDPOINT_OR_MODEL_INVALID"
    ROUTE_ERROR = "ENDPOINT_OR_MODEL_INVALID"  # Backward-compatible enum alias.
    MODEL_INVALID = "MODEL_INVALID"
    MODEL_MISSING = "ENDPOINT_OR_MODEL_INVALID"  # Backward-compatible enum alias.
    MODEL_GONE = "MODEL_GONE"
    ROUTER_DEGRADED = "ROUTER_DEGRADED"
    DNS_FAILURE = "DNS_FAILURE"
    NON_API_HTML_RESPONSE = "NON_API_HTML_RESPONSE"
    WAF_BLOCKED = "WAF_BLOCKED"
    BROWSER_CHALLENGE = "BROWSER_CHALLENGE"
    MODEL_DISCOVERY_UNAVAILABLE = "MODEL_DISCOVERY_UNAVAILABLE"
    DEAD = "DEAD"
    UNKNOWN = "UNKNOWN"

class HealthState(str, Enum):
    LIVE = "LIVE"
    PENDING = "PENDING"
    AUTH_REJECTED = "AUTH_REJECTED"
    AUTH = "AUTH_REJECTED"  # Backward-compatible enum alias.
    ACCESS_FORBIDDEN = "ACCESS_FORBIDDEN"
    BALANCE_REQUIRED = "BALANCE_REQUIRED"
    BALANCE = "BALANCE_REQUIRED"  # Backward-compatible enum alias.
    RATE_LIMITED = "RATE_LIMITED"
    RATE_LIMIT = "RATE_LIMITED"  # Backward-compatible enum alias.
    CONNECT_TIMEOUT = "CONNECT_TIMEOUT"
    TIMEOUT = "CONNECT_TIMEOUT"  # Backward-compatible enum alias.
    PROVIDER_ERROR = "PROVIDER_ERROR"
    TEMP_ERROR = "PROVIDER_ERROR"  # Backward-compatible enum alias.
    ENDPOINT_OR_MODEL_INVALID = "ENDPOINT_OR_MODEL_INVALID"
    ROUTE_ERROR = "ENDPOINT_OR_MODEL_INVALID"  # Backward-compatible enum alias.
    MODEL_INVALID = "MODEL_INVALID"
    MODEL_MISSING = "ENDPOINT_OR_MODEL_INVALID"  # Backward-compatible enum alias.
    MODEL_GONE = "MODEL_GONE"
    ROUTER_DEGRADED = "ROUTER_DEGRADED"
    DNS_FAILURE = "DNS_FAILURE"
    NON_API_HTML_RESPONSE = "NON_API_HTML_RESPONSE"
    WAF_BLOCKED = "WAF_BLOCKED"
    BROWSER_CHALLENGE = "BROWSER_CHALLENGE"
    MODEL_DISCOVERY_UNAVAILABLE = "MODEL_DISCOVERY_UNAVAILABLE"
    DEAD = "DEAD"
    UNKNOWN = "UNKNOWN"
    FREE_USE = "FREE/USE"
    PAID = "PAID"
    USE_UNKNOWN = "USE/?"

class CostState(str, Enum):
    FREE = "FREE"
    PAID = "PAID"
    UNKNOWN = "UNKNOWN"


class ReachabilityState(str, Enum):
    """Whether any HTTP response was received from the provider path."""

    REACHABLE = "REACHABLE"
    UNREACHABLE = "UNREACHABLE"


class AuthState(str, Enum):
    """Authentication evidence, independent from completion availability."""

    AUTH_OK = "AUTH_OK"
    AUTH_REJECTED = "AUTH_REJECTED"
    UNKNOWN = "UNKNOWN"


class CatalogState(str, Enum):
    """The independently observed state of the provider model catalogue."""

    MODELS_AVAILABLE = "MODELS_AVAILABLE"
    EMPTY_MODEL_CATALOG = "EMPTY_MODEL_CATALOG"
    DISCOVERY_UNAVAILABLE = "DISCOVERY_UNAVAILABLE"

# Backward compatibility alias
CostStatus = CostState

class Confidence(str, Enum):
    LIVE = "LIVE"
    LIKELY_TEMPORARY = "LIKELY_TEMPORARY"
    CONFIG_ERROR = "CONFIG_ERROR"
    MODEL_DEAD = "MODEL_DEAD"
    PROVIDER_DEAD = "PROVIDER_DEAD"

@dataclass
class EvidenceCounters:
    consecutive_model_missing: int = 0
    consecutive_timeout: int = 0
    consecutive_route_error: int = 0
    consecutive_auth: int = 0
    consecutive_rate_limit: int = 0

    def to_dict(self) -> Dict[str, int]:
        return {
            "consecutive_model_missing": self.consecutive_model_missing,
            "consecutive_timeout": self.consecutive_timeout,
            "consecutive_route_error": self.consecutive_route_error,
            "consecutive_auth": self.consecutive_auth,
            "consecutive_rate_limit": self.consecutive_rate_limit,
        }

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "EvidenceCounters":
        if not data or not isinstance(data, dict):
            return cls()
        return cls(
            consecutive_model_missing=int(data.get("consecutive_model_missing", 0)),
            consecutive_timeout=int(data.get("consecutive_timeout", 0)),
            consecutive_route_error=int(data.get("consecutive_route_error", 0)),
            consecutive_auth=int(data.get("consecutive_auth", 0)),
            consecutive_rate_limit=int(data.get("consecutive_rate_limit", 0)),
        )

@dataclass
class EvidenceRecord:
    availability: AvailabilityState = AvailabilityState.UNKNOWN
    cost: CostState = CostState.UNKNOWN
    confidence: Confidence = Confidence.LIKELY_TEMPORARY
    status_code: int = 0
    latency_ms: float = 0.0
    error_code: str = ""
    reason: str = ""
    raw_error: str = ""
    counters: EvidenceCounters = field(default_factory=EvidenceCounters)
    note: str = ""

    def __init__(
        self,
        availability: Optional[Any] = None,
        cost: Optional[Any] = None,
        confidence: Any = Confidence.LIKELY_TEMPORARY,
        status_code: int = 0,
        latency_ms: float = 0.0,
        error_code: str = "",
        reason: str = "",
        raw_error: str = "",
        counters: Optional[EvidenceCounters] = None,
        note: str = "",
        **kwargs,
    ):
        state_arg = kwargs.get("state")
        cost_status_arg = kwargs.get("cost_status")

        if availability is None:
            if state_arg is not None:
                s_val = state_arg.value if hasattr(state_arg, "value") else str(state_arg)
                if s_val in ("FREE/USE", "PAID", "USE/?", "LIVE"):
                    availability = AvailabilityState.LIVE
                else:
                    try:
                        availability = _coerce_availability(s_val)
                    except Exception:
                        availability = AvailabilityState.UNKNOWN
            else:
                availability = AvailabilityState.UNKNOWN
        elif isinstance(availability, str):
            try:
                availability = _coerce_availability(availability)
            except Exception:
                availability = AvailabilityState.UNKNOWN

        if cost is None:
            if cost_status_arg is not None:
                c_val = cost_status_arg.value if hasattr(cost_status_arg, "value") else str(cost_status_arg)
                try:
                    cost = CostState(c_val)
                except Exception:
                    cost = CostState.UNKNOWN
            elif state_arg is not None:
                s_val = state_arg.value if hasattr(state_arg, "value") else str(state_arg)
                if s_val == "FREE/USE":
                    cost = CostState.FREE
                elif s_val == "PAID":
                    cost = CostState.PAID
                else:
                    cost = CostState.UNKNOWN
            else:
                cost = CostState.UNKNOWN
        elif isinstance(cost, str):
            try:
                cost = CostState(cost)
            except Exception:
                cost = CostState.UNKNOWN

        self.availability = availability
        self.cost = cost
        self.confidence = confidence if isinstance(confidence, Confidence) else Confidence(confidence)
        self.status_code = status_code
        self.latency_ms = latency_ms
        self.error_code = error_code
        self.reason = reason
        self.raw_error = raw_error
        if counters is None:
            c = EvidenceCounters()
            if self.availability == AvailabilityState.MODEL_MISSING:
                c.consecutive_model_missing = 1
            elif self.availability == AvailabilityState.TIMEOUT:
                c.consecutive_timeout = 1
            elif self.availability == AvailabilityState.ROUTE_ERROR:
                c.consecutive_route_error = 1
            elif self.availability == AvailabilityState.AUTH:
                c.consecutive_auth = 1
            elif self.availability == AvailabilityState.RATE_LIMIT:
                c.consecutive_rate_limit = 1
            self.counters = c
        else:
            self.counters = counters
        self.note = note

    @property
    def state(self) -> str:
        """Derived UI state badge (e.g. FREE/USE, PAID, USE/?, BALANCE, DEAD)."""
        return get_ui_badge(self.availability, self.cost)

    @property
    def cost_status(self) -> CostState:
        return self.cost

def get_ui_badge(availability: AvailabilityState, cost: CostState) -> str:
    """Computes operator UI badge by combining availability and cost."""
    if availability == AvailabilityState.LIVE:
        if cost == CostState.FREE:
            return "FREE/USE"
        elif cost == CostState.PAID:
            return "PAID"
        else:
            return "USE/?"
    return availability.value

# Known free providers or provider models
KNOWN_FREE_PROVIDERS = {
    "antigravity", "ag", "gemini-cli", "free", "freetier",
    "moyuu", "bai", "oc", "ocg", "cl"
}

# Known paid commercial endpoints
KNOWN_PAID_PROVIDERS = {
    "openai", "anthropic", "cohere", "voyage", "deepseek-direct",
    "azure", "bedrock", "vertex", "groq-paid", "mistral-paid",
}

# Regex keywords for balance / quota depletion
BALANCE_KEYWORDS = re.compile(
    r'(?i)(insufficient_quota|insufficient balance|out of credit|balance is not enough|'
    r'quota_exceeded|exceeded.*quota|credit.*expired|billing|arrears|'
    r'maximum \$1 per period|no credit|daily usage limit exceeded|'
    r'预扣费额度失败|用户剩余额度|剩余额度|欠费|余额不足|额度已用尽)'
)

# Regex keywords for authentication / permission
AUTH_KEYWORDS = re.compile(
    r'(?i)(invalid_api_key|invalid key|unauthorized|authentication failed|'
    r'forbidden|access denied|permission_denied|expired token|token has expired|'
    r'account suspended|invalid_token|401 unauthorized)'
)

MODEL_GONE_KEYWORDS = re.compile(
    r'(?i)(model.*(?:gone|removed|retired|deprecated|no longer available)|'
    r'(?:gone|removed|retired|deprecated).*model|endpoint.*(?:gone|removed|deprecated))'
)

DNS_KEYWORDS = re.compile(
    r'(?i)(dns|name resolution|getaddrinfo|nodename nor servname|no such host|'
    r'could not resolve|temporary failure in name resolution)'
)

WAF_BODY_MARKERS = re.compile(
    r'(?i)(attention required\s*\|\s*cloudflare|you have been blocked|'
    r'this website is using a security service|cf-chl-|challenge-platform|turnstile)'
)

BROWSER_CHALLENGE_MARKERS = re.compile(
    r'(?i)(cf-chl-|challenge-platform|turnstile|just a moment\.\.\.)'
)

_LEGACY_AVAILABILITY_VALUES = {
    "AUTH": AvailabilityState.AUTH_REJECTED,
    "BALANCE": AvailabilityState.BALANCE_REQUIRED,
    "RATE LIMIT": AvailabilityState.RATE_LIMITED,
    "TIMEOUT": AvailabilityState.CONNECT_TIMEOUT,
    "TEMP ERROR": AvailabilityState.PROVIDER_ERROR,
    "ROUTE ERROR": AvailabilityState.ENDPOINT_OR_MODEL_INVALID,
    "MODEL MISSING": AvailabilityState.ENDPOINT_OR_MODEL_INVALID,
}


def _coerce_availability(value: Any) -> AvailabilityState:
    if isinstance(value, AvailabilityState):
        return value
    if value in _LEGACY_AVAILABILITY_VALUES:
        return _LEGACY_AVAILABILITY_VALUES[value]
    return AvailabilityState(value)


def _normalise_response_headers(headers: Optional[Dict[str, Any]]) -> Dict[str, str]:
    """Return lower-case response headers without ever touching request secrets."""
    if not headers:
        return {}
    try:
        return {str(key).lower(): str(value) for key, value in headers.items()}
    except AttributeError:
        return {}


def _is_html_body(raw_body: str, content_type: str = "") -> bool:
    content_type = (content_type or "").lower()
    if "text/html" in content_type or "application/xhtml" in content_type:
        return True
    return bool(re.search(r'(?is)<!doctype\s+html|<html\b|<!--\[if\s+lt\s+ie', raw_body or ""))


def _has_waf_evidence(status_code: int, raw_body: str, headers: Dict[str, str]) -> bool:
    if status_code != 403 or not _is_html_body(raw_body, headers.get("content-type", "")):
        return False
    has_waf_header = any(
        name in headers
        for name in ("server", "cf-ray", "cf-mitigated", "cf-cache-status", "x-sucuri-id", "x-waf-event")
    ) and (
        "cloudflare" in headers.get("server", "").lower()
        or "cf-ray" in headers
        or "cf-mitigated" in headers
        or "x-sucuri-id" in headers
        or "x-waf-event" in headers
    )
    return has_waf_header or bool(WAF_BODY_MARKERS.search(raw_body or ""))

# Regex keywords for rate limits
RATE_LIMIT_KEYWORDS = re.compile(
    r'(?i)(rate_limit_exceeded|too many requests|rate limit|resource_exhausted|'
    r'slow down|quota per period exceeded|concurrency limit|exceeded.*rate limit|'
    r'429 too many requests|throughput limit)'
)

# Regex keywords for explicit model not found
SEMANTIC_MODEL_MISSING_KEYWORDS = re.compile(
    r'(?i)(model.*not found|model_not_found|model.*does not exist|model.*not available|unsupported model|'
    r'model is not supported|requested (model|entity).*not found|cannot find model|'
    r'unknown model|no such model|model_missing|invalid_model|entity.*not found)'
)

def is_provider_or_model_known_free(provider_prefix: str, model_id: str) -> bool:
    p = (provider_prefix or "").lower()
    m = (model_id or "").lower()
    if p in KNOWN_FREE_PROVIDERS:
        return True
    # Kira publishes per-model billing metadata. Do not apply the generic
    # substring heuristic to Kira: a manual model ID is USE/? until current
    # catalogue metadata or an explicit operator override establishes cost.
    if p == "kira-ai":
        return False
    if "free" in m or "trial" in m:
        return True
    return False

def is_provider_known_paid(provider_prefix: str) -> bool:
    p = (provider_prefix or "").lower()
    return p in KNOWN_PAID_PROVIDERS

def classify_probe_result(
    status_code: int,
    latency_ms: float = 0.0,
    raw_body: str = "",
    parsed_json: Optional[Dict[str, Any]] = None,
    provider_prefix: str = "",
    model_id: str = "",
    previous_counters: Optional[EvidenceCounters] = None,
    cost_override: Optional[str] = None,
    # Explicit provider catalogue metadata. This is separate from the
    # operator override so unknown billing never silently becomes FREE/PAID.
    cost_hint: Optional[str] = None,
    is_timeout: bool = False,
    previous_streak: Optional[int] = None,
    response_headers: Optional[Dict[str, Any]] = None,
    **kwargs,
) -> EvidenceRecord:
    """
    Classifies a probe response using strict operational rules.
    - Decouples availability from cost.
    - Never turns unknown successful models to PAID.
    - Requires semantic proof for MODEL_MISSING; naked 404 is ROUTE_ERROR.
    - DEAD requires repeated evidence of the same dead condition.
    """
    if not raw_body and "body" in kwargs:
        raw_body = str(kwargs["body"])
    if previous_counters is None and "prev_counters" in kwargs:
        previous_counters = kwargs["prev_counters"]
    if response_headers is None:
        response_headers = kwargs.get("headers")
    response_headers = _normalise_response_headers(response_headers)

    counters = EvidenceCounters.from_dict(previous_counters.to_dict()) if previous_counters else EvidenceCounters()
    if previous_streak is not None and not previous_counters:
        counters.consecutive_model_missing = previous_streak
        counters.consecutive_route_error = previous_streak
        counters.consecutive_timeout = previous_streak
        counters.consecutive_rate_limit = previous_streak
        counters.consecutive_auth = previous_streak
    clean_body = redact_secrets(raw_body or "").strip()

    # 1. Hard Timeout
    if is_timeout or status_code == 408:
        counters.consecutive_timeout += 1
        counters.consecutive_model_missing = 0
        counters.consecutive_auth = 0
        counters.consecutive_rate_limit = 0
        counters.consecutive_route_error = 0
        return EvidenceRecord(
            availability=AvailabilityState.TIMEOUT,
            cost=CostState.UNKNOWN,
            confidence=Confidence.LIKELY_TEMPORARY,
            status_code=408 if status_code == 0 else status_code,
            latency_ms=latency_ms,
            error_code="TIMEOUT",
            reason=f"Hard timeout reached (> threshold). Consecutive timeouts: {counters.consecutive_timeout}.",
            raw_error=clean_body[:300] if clean_body else "Request timed out.",
            counters=counters,
        )

    # 2. Extract provider error structure
    error_msg = ""
    error_code = ""
    error_type = ""
    if parsed_json and isinstance(parsed_json, dict):
        if parsed_json.get("error") is not None:
            err = parsed_json["error"]
            if isinstance(err, dict):
                error_msg = str(err.get("message") or err.get("msg") or "")
                error_code = str(err.get("code") or "")
                error_type = str(err.get("type") or "")
            else:
                error_msg = str(err)
        elif parsed_json.get("msg") is not None:
            error_msg = str(parsed_json.get("msg"))
            error_code = str(parsed_json.get("status") or "")
        elif parsed_json.get("message") is not None:
            error_msg = str(parsed_json.get("message"))
            error_code = str(parsed_json.get("status") or "")

    # Extract the router's outer status and any embedded upstream status. The
    # local model probe can return HTTP 503 with an upstream "[403]: HTML"
    # detail; classify the rejecting layer instead of the wrapper.
    upstream_status_code = None
    if parsed_json and isinstance(parsed_json, dict):
        json_status = parsed_json.get("status")
        if isinstance(json_status, int) and json_status not in (0, 200) and status_code == 200:
            status_code = json_status

    if error_msg:
        http_match = re.search(r'\bHTTP\s+(\d{3})\b', error_msg)
        if http_match:
            status_code = int(http_match.group(1))
        inner_match = re.search(r'\[(\d{3})\]\s*:', error_msg)
        if inner_match:
            candidate = int(inner_match.group(1))
            if candidate != status_code and status_code in (200, 500, 502, 503, 504):
                upstream_status_code = candidate
                status_code = candidate

    combined_text = f"{status_code} {error_code} {error_type} {error_msg} {clean_body}".lower()
    content_type = response_headers.get("content-type", "")
    is_html = _is_html_body(clean_body, content_type)

    # HTML is not an API error payload. Cloudflare/WAF HTML is classified
    # before generic auth handling, including when nested in a 9Router 503.
    if _has_waf_evidence(status_code, clean_body, response_headers):
        challenge = bool(BROWSER_CHALLENGE_MARKERS.search(clean_body))
        return EvidenceRecord(
            availability=AvailabilityState.BROWSER_CHALLENGE if challenge else AvailabilityState.WAF_BLOCKED,
            cost=CostState.UNKNOWN,
            confidence=Confidence.LIKELY_TEMPORARY,
            status_code=status_code,
            latency_ms=latency_ms,
            error_code="BROWSER_CHALLENGE" if challenge else "WAF_BLOCKED",
            reason="Upstream returned an HTML WAF/challenge response; this is not an API-key JSON rejection.",
            raw_error=clean_body[:300],
            counters=counters,
            note=f"upstream_status={upstream_status_code}" if upstream_status_code else "response_type=HTML",
        )

    if is_html:
        return EvidenceRecord(
            availability=AvailabilityState.NON_API_HTML_RESPONSE,
            cost=CostState.UNKNOWN,
            confidence=Confidence.LIKELY_TEMPORARY,
            status_code=status_code,
            latency_ms=latency_ms,
            error_code="NON_API_HTML_RESPONSE",
            reason="Expected JSON API response, received HTML.",
            raw_error=clean_body[:300],
            counters=counters,
        )

    if DNS_KEYWORDS.search(combined_text) and error_code in ("network_error", ""):
        return EvidenceRecord(
            availability=AvailabilityState.DNS_FAILURE,
            cost=CostState.UNKNOWN,
            confidence=Confidence.LIKELY_TEMPORARY,
            status_code=status_code,
            latency_ms=latency_ms,
            error_code="DNS_FAILURE",
            reason="DNS resolution failed before an HTTP response was received.",
            raw_error=clean_body[:300],
            counters=counters,
        )

    # 3. Check Success (HTTP 200) - Strict Contract: requires ok=true or verified inference evidence
    if status_code == 200:
        is_explicit_ok_false = bool(parsed_json and isinstance(parsed_json, dict) and parsed_json.get("ok") is False)
        is_explicit_ok_true = bool(parsed_json and isinstance(parsed_json, dict) and parsed_json.get("ok") is True)

        has_inference_evidence = False
        note = ""
        if parsed_json and isinstance(parsed_json, dict):
            choices = parsed_json.get("choices")
            if isinstance(choices, list) and len(choices) > 0:
                first = choices[0]
                if isinstance(first, dict):
                    msg = first.get("message") or {}
                    content = msg.get("content") or first.get("text") or ""
                    reasoning = msg.get("reasoning_content") or ""
                    finish_reason = first.get("finish_reason")
                    if str(content).strip() or str(reasoning).strip() or finish_reason in ("stop", "length"):
                        has_inference_evidence = True
                        if finish_reason == "length" or (reasoning and not str(content).strip()):
                            note = "reasoning-only response (length-limited soft-pass)"
            elif parsed_json.get("text") or parsed_json.get("content") or parsed_json.get("response"):
                has_inference_evidence = True

        has_error_field = bool(error_msg or (error_code and error_code not in ("200", "0", "ok")))
        if is_explicit_ok_true:
            has_error_field = False

        is_verified_live = (is_explicit_ok_true or has_inference_evidence) and not has_error_field and not is_explicit_ok_false

        if is_verified_live:
            # Reset failure streaks on legitimate success
            counters = EvidenceCounters()

            # Determine cost classification
            cost = CostState.UNKNOWN
            if cost_override:
                norm_override = cost_override.upper()
                if norm_override in (CostState.FREE.value, CostState.PAID.value):
                    cost = CostState(norm_override)
            if cost == CostState.UNKNOWN and cost_hint:
                normalized_hint = str(cost_hint).upper()
                if normalized_hint in (CostState.FREE.value, CostState.PAID.value):
                    cost = CostState(normalized_hint)
            if cost == CostState.UNKNOWN:
                if is_provider_or_model_known_free(provider_prefix, model_id):
                    cost = CostState.FREE
                elif is_provider_known_paid(provider_prefix):
                    cost = CostState.PAID
                else:
                    cost = CostState.UNKNOWN  # Displays as USE/?

            return EvidenceRecord(
                availability=AvailabilityState.LIVE,
                cost=cost,
                confidence=Confidence.LIVE,
                status_code=200,
                latency_ms=latency_ms,
                error_code="OK",
                reason="Request succeeded with valid inference token/choice.",
                raw_error="",
                counters=counters,
                note=note,
            )
        elif not has_error_field and not is_explicit_ok_false and not clean_body:
            # Empty 200 body -> UNKNOWN / TEST_FAILED
            counters.consecutive_model_missing = 0
            counters.consecutive_timeout = 0
            counters.consecutive_auth = 0
            counters.consecutive_rate_limit = 0
            counters.consecutive_route_error = 0
            return EvidenceRecord(
                availability=AvailabilityState.UNKNOWN,
                cost=CostState.UNKNOWN,
                confidence=Confidence.LIKELY_TEMPORARY,
                status_code=200,
                latency_ms=latency_ms,
                error_code="TEST_FAILED",
                reason="HTTP 200 returned empty body without inference content.",
                raw_error="",
                counters=counters,
            )
        elif is_explicit_ok_false or (isinstance(parsed_json, dict) and not has_inference_evidence and not has_error_field):
            # 200 with ok: false, empty choices [], or empty dict {} -> UNKNOWN / TEST_FAILED
            counters.consecutive_model_missing = 0
            counters.consecutive_timeout = 0
            counters.consecutive_auth = 0
            counters.consecutive_rate_limit = 0
            counters.consecutive_route_error = 0
            return EvidenceRecord(
                availability=AvailabilityState.UNKNOWN,
                cost=CostState.UNKNOWN,
                confidence=Confidence.LIKELY_TEMPORARY,
                status_code=200,
                latency_ms=latency_ms,
                error_code="TEST_FAILED",
                reason="HTTP 200 returned no verified inference choices/content (or ok=false).",
                raw_error=clean_body[:300],
                counters=counters,
            )

    # 4. Check BALANCE (402 or balance/quota keywords in text)
    if status_code == 402 or BALANCE_KEYWORDS.search(combined_text):
        counters.consecutive_model_missing = 0
        counters.consecutive_timeout = 0
        counters.consecutive_auth = 0
        counters.consecutive_rate_limit = 0
        counters.consecutive_route_error = 0
        return EvidenceRecord(
            availability=AvailabilityState.BALANCE,
            cost=CostState.PAID,
            confidence=Confidence.CONFIG_ERROR,
            status_code=status_code or 402,
            latency_ms=latency_ms,
            error_code=error_code or "insufficient_balance",
            reason="Insufficient balance or quota limit reached on provider account.",
            raw_error=clean_body[:300] if clean_body else "HTTP 402 / Insufficient balance",
            counters=counters,
        )

    # 5. Distinguish credential rejection from a valid but forbidden request.
    if status_code == 403:
        return EvidenceRecord(
            availability=AvailabilityState.ACCESS_FORBIDDEN,
            cost=CostState.UNKNOWN,
            confidence=Confidence.CONFIG_ERROR,
            status_code=status_code,
            latency_ms=latency_ms,
            error_code=error_code or "ACCESS_FORBIDDEN",
            reason="API returned JSON HTTP 403 access forbidden.",
            raw_error=clean_body[:300],
            counters=counters,
        )

    if (status_code == 401 or AUTH_KEYWORDS.search(combined_text)) and not BALANCE_KEYWORDS.search(combined_text) and not RATE_LIMIT_KEYWORDS.search(combined_text):
        counters.consecutive_auth += 1
        counters.consecutive_model_missing = 0
        counters.consecutive_timeout = 0
        counters.consecutive_rate_limit = 0
        counters.consecutive_route_error = 0
        return EvidenceRecord(
            availability=AvailabilityState.AUTH_REJECTED,
            cost=CostState.UNKNOWN,
            confidence=Confidence.CONFIG_ERROR,
            status_code=status_code,
            latency_ms=latency_ms,
            error_code=error_code or "unauthorized",
            reason=f"API returned JSON HTTP 401 authentication rejected (streak: {counters.consecutive_auth}).",
            raw_error=clean_body[:300],
            counters=counters,
        )

    # 6. Check RATE LIMIT (429 or throttling keywords)
    if status_code == 429 or RATE_LIMIT_KEYWORDS.search(combined_text):
        counters.consecutive_rate_limit += 1
        counters.consecutive_model_missing = 0
        counters.consecutive_timeout = 0
        counters.consecutive_auth = 0
        counters.consecutive_route_error = 0
        return EvidenceRecord(
            availability=AvailabilityState.RATE_LIMIT,
            cost=CostState.UNKNOWN,
            confidence=Confidence.LIKELY_TEMPORARY,
            status_code=status_code or 429,
            latency_ms=latency_ms,
            error_code=error_code or "rate_limit_exceeded",
            reason=f"Rate limit or concurrency throttling exceeded (temporary streak: {counters.consecutive_rate_limit}).",
            raw_error=clean_body[:300],
            counters=counters,
        )

    # 7. HTTP 410 distinguishes a removed model from a degraded router.
    if status_code == 410:
        is_model_gone = bool(MODEL_GONE_KEYWORDS.search(combined_text) or "model" in error_code.lower())
        return EvidenceRecord(
            availability=AvailabilityState.MODEL_GONE if is_model_gone else AvailabilityState.ROUTER_DEGRADED,
            cost=CostState.UNKNOWN,
            confidence=Confidence.CONFIG_ERROR if is_model_gone else Confidence.LIKELY_TEMPORARY,
            status_code=410,
            latency_ms=latency_ms,
            error_code=error_code or ("MODEL_GONE" if is_model_gone else "ROUTER_DEGRADED"),
            reason="Provider reported a gone model." if is_model_gone else "Provider/router reported HTTP 410 without model-gone semantics.",
            raw_error=clean_body[:300],
            counters=counters,
        )

    # 8. Check MODEL MISSING vs ROUTE ERROR (HTTP 404)
    if status_code == 404:
        # A naked 404 (e.g. empty, generic nginx/html 404, or no semantic model error) is a ROUTE_ERROR, NOT MODEL_MISSING
        has_semantic_model_missing = bool(
            SEMANTIC_MODEL_MISSING_KEYWORDS.search(combined_text)
            or (error_code and "model" in error_code.lower())
        )

        if has_semantic_model_missing:
            counters.consecutive_model_missing += 1
            counters.consecutive_timeout = 0
            counters.consecutive_auth = 0
            counters.consecutive_rate_limit = 0
            counters.consecutive_route_error = 0
            is_dead = counters.consecutive_model_missing >= FAILURE_STREAK_FOR_DEAD
            avail = AvailabilityState.DEAD if is_dead else AvailabilityState.MODEL_MISSING
            conf = Confidence.MODEL_DEAD if is_dead else Confidence.LIKELY_TEMPORARY
            reason = f"Model missing or unsupported on provider endpoint (semantic streak: {counters.consecutive_model_missing})."
            if is_dead:
                reason += f" Strong evidence threshold reached (>= {FAILURE_STREAK_FOR_DEAD})."
            return EvidenceRecord(
                availability=avail,
                cost=CostState.UNKNOWN,
                confidence=conf,
                status_code=404,
                latency_ms=latency_ms,
                error_code=error_code or "model_missing",
                reason=reason,
                raw_error=clean_body[:300],
                counters=counters,
            )
        else:
            # Naked 404 -> ROUTE_ERROR
            counters.consecutive_route_error += 1
            counters.consecutive_model_missing = 0
            counters.consecutive_timeout = 0
            counters.consecutive_auth = 0
            counters.consecutive_rate_limit = 0
            is_dead = counters.consecutive_route_error >= (FAILURE_STREAK_FOR_DEAD + 2)  # Higher threshold for route error
            avail = AvailabilityState.DEAD if is_dead else AvailabilityState.ROUTE_ERROR
            conf = Confidence.PROVIDER_DEAD if is_dead else Confidence.LIKELY_TEMPORARY
            return EvidenceRecord(
                availability=avail,
                cost=CostState.UNKNOWN,
                confidence=conf,
                status_code=404,
                latency_ms=latency_ms,
                error_code=error_code or "naked_404_route_error",
                reason=f"Endpoint or upstream route not found (naked 404 without model semantics, streak: {counters.consecutive_route_error}).",
                raw_error=clean_body[:300],
                counters=counters,
            )

    # 9. Check Transient Errors (500, 502, 503, 504)
    if status_code in (500, 502, 503, 504):
        # Inspect if 503 wrapped an inner semantic 404
        if SEMANTIC_MODEL_MISSING_KEYWORDS.search(combined_text):
            counters.consecutive_model_missing += 1
            counters.consecutive_timeout = 0
            counters.consecutive_auth = 0
            counters.consecutive_rate_limit = 0
            counters.consecutive_route_error = 0
            is_dead = counters.consecutive_model_missing >= FAILURE_STREAK_FOR_DEAD
            return EvidenceRecord(
                availability=AvailabilityState.DEAD if is_dead else AvailabilityState.MODEL_MISSING,
                cost=CostState.UNKNOWN,
                confidence=Confidence.MODEL_DEAD if is_dead else Confidence.LIKELY_TEMPORARY,
                status_code=status_code,
                latency_ms=latency_ms,
                error_code="model_missing_wrapped",
                reason=f"Provider returned semantic model missing inside 5xx response (streak: {counters.consecutive_model_missing}).",
                raw_error=clean_body[:300],
                counters=counters,
            )

        counters.consecutive_model_missing = 0
        counters.consecutive_timeout = 0
        counters.consecutive_auth = 0
        counters.consecutive_rate_limit = 0
        counters.consecutive_route_error = 0
        return EvidenceRecord(
            availability=AvailabilityState.TEMP_ERROR,
            cost=CostState.UNKNOWN,
            confidence=Confidence.LIKELY_TEMPORARY,
            status_code=status_code,
            latency_ms=latency_ms,
            error_code=error_code or f"HTTP_{status_code}",
            reason=f"Transient server/gateway error ({status_code}), overloaded provider or reset.",
            raw_error=clean_body[:300],
            counters=counters,
        )

    # 9. Fallback UNKNOWN
    counters.consecutive_model_missing = 0
    counters.consecutive_timeout = 0
    counters.consecutive_auth = 0
    counters.consecutive_rate_limit = 0
    counters.consecutive_route_error = 0
    return EvidenceRecord(
        availability=AvailabilityState.UNKNOWN,
        cost=CostState.UNKNOWN,
        confidence=Confidence.LIKELY_TEMPORARY,
        status_code=status_code,
        latency_ms=latency_ms,
        error_code=error_code or "unknown_response",
        reason="Unclassified provider response.",
        raw_error=clean_body[:300],
        counters=counters,
    )


@dataclass(frozen=True)
class ProviderHealthSummary:
    """Provider health dimensions kept independent from one another.

    ``provider_state``, ``model_state`` and ``discovery_state`` remain for
    callers of the original API.  The explicit dimensions below are the
    authoritative representation for routing and UI decisions.
    """

    provider_state: AvailabilityState
    model_state: AvailabilityState
    discovery_state: str
    completion: EvidenceRecord
    reachability: ReachabilityState
    auth: AuthState
    catalog: CatalogState
    completion_state: AvailabilityState
    usable: bool
    active_routing: bool

    @property
    def routing_allowed(self) -> bool:
        """Compatibility/readability alias for active routing decisions."""
        return self.active_routing


def _classify_models_catalog(
    models_status_code: Optional[int],
    models_raw_body: str,
    models_parsed_json: Optional[Dict[str, Any]] = None,
) -> CatalogState:
    """Classify `/models` without confusing HTTP 200 + zero rows with PASS."""
    if models_status_code != 200:
        return CatalogState.DISCOVERY_UNAVAILABLE

    payload = models_parsed_json
    if payload is None and models_raw_body:
        try:
            decoded = json.loads(models_raw_body)
            payload = decoded if isinstance(decoded, dict) else None
        except (TypeError, ValueError):
            payload = None

    if not isinstance(payload, dict):
        return CatalogState.DISCOVERY_UNAVAILABLE

    # OpenAI-compatible providers use `data`; 9Router's normalized local
    # provider route uses `models`. Support both, while never treating an
    # absent/malformed list as a healthy catalogue.
    rows = payload.get("data") if "data" in payload else payload.get("models")
    if not isinstance(rows, list):
        return CatalogState.DISCOVERY_UNAVAILABLE
    return CatalogState.MODELS_AVAILABLE if rows else CatalogState.EMPTY_MODEL_CATALOG


def _classify_reachability(
    completion: EvidenceRecord,
    models_status_code: Optional[int],
) -> ReachabilityState:
    """Any received HTTP response proves reachability of that request path."""
    if models_status_code is not None and models_status_code >= 100:
        return ReachabilityState.REACHABLE
    if completion.status_code >= 100:
        return ReachabilityState.REACHABLE
    if completion.availability in (AvailabilityState.DNS_FAILURE, AvailabilityState.CONNECT_TIMEOUT):
        return ReachabilityState.UNREACHABLE
    return ReachabilityState.UNREACHABLE


def _classify_auth(
    completion: EvidenceRecord,
    models_status_code: Optional[int],
) -> AuthState:
    """Use only explicit auth evidence; WAF/HTML is not an auth rejection."""
    if models_status_code == 401 or completion.availability == AvailabilityState.AUTH_REJECTED:
        return AuthState.AUTH_REJECTED
    if models_status_code == 200 or completion.availability == AvailabilityState.LIVE:
        return AuthState.AUTH_OK
    return AuthState.UNKNOWN


def classify_provider_state(
    completion: EvidenceRecord,
    models_status_code: Optional[int] = None,
    models_raw_body: str = "",
    models_parsed_json: Optional[Dict[str, Any]] = None,
) -> ProviderHealthSummary:
    """Classify provider dimensions without collapsing them into ``DEAD``.

    A successful catalog call plus an invalid configured model means the
    provider is reachable while that model is ``MODEL_INVALID``. A successful
    completion plus a failed catalog call means the completion is LIVE with
    ``DISCOVERY_UNAVAILABLE``. A successful empty catalog is an explicit
    ``EMPTY_MODEL_CATALOG`` state and makes the provider unusable for active
    routing, even if a completion probe returns LIVE.
    """
    catalog = _classify_models_catalog(models_status_code, models_raw_body, models_parsed_json)
    reachability = _classify_reachability(completion, models_status_code)
    auth = _classify_auth(completion, models_status_code)
    model_state = completion.availability
    if model_state == AvailabilityState.ENDPOINT_OR_MODEL_INVALID and models_status_code == 200:
        model_state = AvailabilityState.MODEL_INVALID

    provider_state = completion.availability
    if completion.availability in (
        AvailabilityState.LIVE,
        AvailabilityState.MODEL_INVALID,
        AvailabilityState.ENDPOINT_OR_MODEL_INVALID,
    ) and models_status_code == 200:
        provider_state = AvailabilityState.LIVE
    elif completion.availability == AvailabilityState.LIVE:
        provider_state = AvailabilityState.LIVE

    # A provider is routable only after the endpoint is reachable, auth is
    # not rejected, the catalogue has at least one model, and completion is
    # a verified LIVE result. In particular, `/models` 200 + [] is not PASS.
    usable = (
        reachability == ReachabilityState.REACHABLE
        and auth == AuthState.AUTH_OK
        and catalog == CatalogState.MODELS_AVAILABLE
        and completion.availability == AvailabilityState.LIVE
    )
    if catalog == CatalogState.EMPTY_MODEL_CATALOG:
        provider_state = AvailabilityState.MODEL_DISCOVERY_UNAVAILABLE

    if models_status_code == 200 and models_raw_body:
        discovery_state = catalog.value
    else:
        # Preserve the old AVAILABLE value for callers that only supplied an
        # HTTP status, while the explicit `catalog` field remains authoritative.
        discovery_state = "MODEL_DISCOVERY_UNAVAILABLE" if catalog == CatalogState.DISCOVERY_UNAVAILABLE and models_status_code != 200 else "AVAILABLE"

    return ProviderHealthSummary(
        provider_state=provider_state,
        model_state=model_state,
        discovery_state=discovery_state,
        completion=completion,
        reachability=reachability,
        auth=auth,
        catalog=catalog,
        completion_state=completion.availability,
        usable=usable,
        active_routing=usable,
    )
