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
    AUTH = "AUTH"
    BALANCE = "BALANCE"
    RATE_LIMIT = "RATE LIMIT"
    TIMEOUT = "TIMEOUT"
    TEMP_ERROR = "TEMP ERROR"
    ROUTE_ERROR = "ROUTE ERROR"
    MODEL_MISSING = "MODEL MISSING"
    DEAD = "DEAD"
    UNKNOWN = "UNKNOWN"

class HealthState(str, Enum):
    LIVE = "LIVE"
    PENDING = "PENDING"
    AUTH = "AUTH"
    BALANCE = "BALANCE"
    RATE_LIMIT = "RATE LIMIT"
    TIMEOUT = "TIMEOUT"
    TEMP_ERROR = "TEMP ERROR"
    ROUTE_ERROR = "ROUTE ERROR"
    MODEL_MISSING = "MODEL MISSING"
    DEAD = "DEAD"
    UNKNOWN = "UNKNOWN"
    FREE_USE = "FREE/USE"
    PAID = "PAID"
    USE_UNKNOWN = "USE/?"

class CostState(str, Enum):
    FREE = "FREE"
    PAID = "PAID"
    UNKNOWN = "UNKNOWN"

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
                        availability = AvailabilityState(s_val)
                    except Exception:
                        availability = AvailabilityState.UNKNOWN
            else:
                availability = AvailabilityState.UNKNOWN
        elif isinstance(availability, str):
            try:
                availability = AvailabilityState(availability)
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
    is_timeout: bool = False,
    previous_streak: Optional[int] = None,
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

    # Extract embedded HTTP status from error string if present (e.g. "HTTP 404: ...")
    if error_msg:
        http_match = re.search(r'\bHTTP\s+(\d{3})\b', error_msg)
        if http_match:
            status_code = int(http_match.group(1))

    combined_text = f"{status_code} {error_code} {error_type} {error_msg} {clean_body}".lower()

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

    # 5. Check AUTH (401, 403 or invalid credential keywords)
    if (status_code in (401, 403) or AUTH_KEYWORDS.search(combined_text)) and not BALANCE_KEYWORDS.search(combined_text) and not RATE_LIMIT_KEYWORDS.search(combined_text):
        counters.consecutive_auth += 1
        counters.consecutive_model_missing = 0
        counters.consecutive_timeout = 0
        counters.consecutive_rate_limit = 0
        counters.consecutive_route_error = 0
        return EvidenceRecord(
            availability=AvailabilityState.AUTH,
            cost=CostState.UNKNOWN,
            confidence=Confidence.CONFIG_ERROR,
            status_code=status_code,
            latency_ms=latency_ms,
            error_code=error_code or "unauthorized",
            reason=f"Invalid API key, expired credential, or forbidden account access (streak: {counters.consecutive_auth}).",
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

    # 7. Check MODEL MISSING vs ROUTE ERROR (HTTP 404)
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

    # 8. Check Transient Errors (500, 502, 503, 504)
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
