"""
9router_WatchEdit - Global Secret Redaction Boundary
Every exception, debug object, or log line crossing into UI / logs / diagnostics /
bug reports / audit exports MUST pass through this module.

- Text: pattern-based redaction (Bearer, sk-/9r- style keys, JWTs, basic-auth URLs)
- Dicts/lists (parsed JSON etc.): recursive redaction of sensitive KEYS
- Arbitrary exceptions: str() then pattern redaction

Redacted values are represented as <REDACTED>; no prefix/suffix fragments kept.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

REDACTED = "<REDACTED>"

# Case-insensitive sensitive keys (task section 4 minimum list).
SENSITIVE_KEYS = {
    "api_key", "apikey", "key", "token", "access_token", "accesstoken",
    "refresh_token", "refreshtoken", "client_secret", "clientsecret",
    "authorization", "auth", "cookie", "cookies", "password", "passwd",
    "pwd", "secret", "jwt", "bearer", "credential", "credentials",
    "private_key", "apikeyid",
}

# Credential-shaped text patterns.
_TEXT_PATTERNS = [
    re.compile(r'(?i)(bearer\s+)[a-zA-Z0-9_\-\.=+/]{8,}'),
    re.compile(r'(?i)(authorization\s*[:=]\s*)\S+'),
    re.compile(r'sk-[a-zA-Z0-9_\-\.]{12,}'),
    re.compile(r'9r-[a-zA-Z0-9_\-\.]{8,}'),
    re.compile(r'\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{4,}\b'),
    re.compile(r'-----BEGIN (?:[A-Z]+ )?PRIVATE KEY(?: BLOCK)?-----[\s\S]*?-----END (?:[A-Z]+ )?PRIVATE KEY(?: BLOCK)?-----'),
    # credentials embedded in URLs: https://user:pass@host, redis://:pass@host
    re.compile(r'(?i)([a-z][a-z0-9+.\-]*://[^/\s:@]+:)([^@\s]{4,})(@)'),
]


def redact_text(text: str) -> str:
    """Redacts credential-shaped substrings from free text."""
    if not text:
        return ""
    cleaned = str(text)
    for pattern in _TEXT_PATTERNS:
        if pattern.groups:
            cleaned = pattern.sub(lambda m: m.group(1) + REDACTED, cleaned)
        else:
            cleaned = pattern.sub(REDACTED, cleaned)
    return cleaned


def redact_mapping(obj: Any, _depth: int = 0) -> Any:
    """Recursively redacts sensitive keys in nested dict/list structures.

    Values under sensitive keys become <REDACTED> regardless of nesting depth
    (bounded to guard against pathological structures).
    """
    if _depth > 24:
        return REDACTED
    if isinstance(obj, dict):
        out: Dict[str, Any] = {}
        for k, v in obj.items():
            if str(k).strip().lower() in SENSITIVE_KEYS and isinstance(v, str) and v:
                out[str(k)] = REDACTED
            else:
                out[str(k)] = redact_mapping(v, _depth + 1)
        return out
    if isinstance(obj, (list, tuple)):
        seq: List[Any] = [redact_mapping(v, _depth + 1) for v in obj]
        return seq if isinstance(obj, list) else tuple(seq)
    if isinstance(obj, str):
        return redact_text(obj)
    return obj


def redact_exception(ex: BaseException) -> str:
    """Formats any exception into a safe, redacted diagnostic string."""
    try:
        raw = f"{type(ex).__name__}: {ex}"
    except Exception:
        raw = f"{type(ex).__name__}: <unformattable>"
    return redact_text(raw)


def looks_like_secret_marker(value: str, markers: Optional[List[str]] = None) -> bool:
    """Test helper: True when any marker survives redaction (i.e. redaction FAILED)."""
    markers = markers or ["SUPER_SECRET_TEST_VALUE_123456"]
    red = redact_text(value) + str(redact_mapping({"api_key": value, "accessToken": value}))
    return any(m in red for m in markers)
