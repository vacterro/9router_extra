"""Provider presets for integrations represented by the existing generic transport.

This module deliberately contains configuration and normalization only. HTTP,
streaming, retries, authentication, and error handling remain in 9Router's
existing OpenAI-compatible provider implementation.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional
from urllib.parse import urlsplit, urlunsplit

from core.classification import CostState


@dataclass(frozen=True)
class ProviderPreset:
    """Provider-creation fields consumed by an OpenAI-compatible connection UI."""

    id: str
    display_name: str
    base_url: str
    protocol: str
    auth_header: str
    auth_scheme: str
    default_endpoint: str


KIRA_AI_PRESET = ProviderPreset(
    id="kira-ai",
    display_name="Kira AI",
    base_url="https://kiraai.vn/api/v1",
    protocol="openai",
    auth_header="Authorization",
    auth_scheme="bearer",
    default_endpoint="chat/completions",
)


_PROVIDER_PRESETS = {KIRA_AI_PRESET.id: KIRA_AI_PRESET}


def get_provider_preset(provider_id: str) -> Optional[ProviderPreset]:
    """Return a first-class preset, or ``None`` for generic/manual providers."""
    return _PROVIDER_PRESETS.get((provider_id or "").strip().lower())


def compose_openai_endpoint(base_url: str, endpoint: str = "chat/completions") -> str:
    """Compose an OpenAI-compatible endpoint without duplicating path segments.

    Kira's configured base already includes ``/api/v1``. Users sometimes paste
    the endpoint itself, so both forms are accepted while the resulting URL is
    always exactly ``<base>/chat/completions``.
    """
    if not isinstance(base_url, str) or not base_url.strip():
        raise ValueError("Provider base URL is required")
    if not isinstance(endpoint, str) or not endpoint.strip():
        raise ValueError("Provider endpoint is required")

    base = base_url.strip().rstrip("/")
    path = endpoint.strip().strip("/")
    parts = urlsplit(base)
    if parts.query or parts.fragment:
        raise ValueError("Provider base URL must not contain a query or fragment")

    base_path = parts.path.rstrip("/")
    # Accept either the documented endpoint-relative form or a pasted
    # `/v1/...` form without creating `/api/v1/v1/...`.
    base_lower = base_path.lower()
    if path.lower().startswith("api/v1/") and base_lower.endswith("/api/v1"):
        path = path[7:]
    elif path.lower().startswith("v1/") and base_lower.endswith("/v1"):
        path = path[3:]
    suffix = f"/{path}"
    if base_path.lower().endswith(suffix.lower()):
        final_path = base_path
    else:
        final_path = f"{base_path}{suffix}"

    return urlunsplit((parts.scheme, parts.netloc, final_path, "", ""))


def upstream_model_id(model_id: str, provider_id: str = "kira-ai") -> str:
    """Strip the public provider namespace before an upstream request."""
    model = (model_id or "").strip()
    provider = provider_id.strip().lower()
    prefix = f"{provider}/"
    if model.lower().startswith(prefix):
        return model[len(prefix):]
    return model


def model_cost_hint(model: Mapping[str, Any]) -> Optional[str]:
    """Translate explicit Kira catalogue billing metadata into a cost hint.

    ``None`` means unknown; it is intentionally not treated as FREE. The
    helper accepts the fields published by Kira's ``/api/v1/models`` response
    and does not infer billing from account state or promotional availability.
    """
    if not isinstance(model, Mapping):
        return None
    if model.get("is_free") is True:
        return CostState.FREE.value
    if model.get("is_free") is False or model.get("is_partner") is True:
        return CostState.PAID.value

    for key in ("price_input_vnd", "price_output_vnd", "price_cache_read_vnd", "price_cache_write_vnd"):
        value = model.get(key)
        try:
            if value is not None and float(value) > 0:
                return CostState.PAID.value
        except (TypeError, ValueError):
            continue
    return None
