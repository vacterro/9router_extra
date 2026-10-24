"""
9router_WatchEdit - Upstream 9router attribution

Answers "which upstream am I actually talking to?" for a diagnostic: the
loopback endpoint, whether it answers, which process owns the port, and what
version the upstream reports. The attribution is what makes a downstream
observation (a combo failover, a model error) attributable to the upstream
engine instead of to a local guess.

Nothing here writes, and nothing here reads a secret: the version endpoint
and the process list carry no credential material.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

#: Attribution states, mirroring the client's offline-ownership vocabulary.
ATTRIBUTION_LIVE = "LIVE"
ATTRIBUTION_OFFLINE = "OFFLINE_VERIFIED"
ATTRIBUTION_UNKNOWN = "UNKNOWN"


def _port_of(base_url: str) -> Optional[int]:
    try:
        from core.router_client import _port_from_base_url

        return int(_port_from_base_url(base_url))
    except Exception:
        return None


def attribute_upstream(client: Any = None) -> Dict[str, Any]:
    """Attribute the upstream 9router instance behind ``client``.

    Returns a JSON-friendly dict. Every field degrades honestly: an
    unreachable upstream is UNKNOWN reachability, never "offline", unless the
    client can positively prove the absence of a runtime owner.
    """
    from core.router_client import runtime_owner_processes

    base_url = ""
    if client is not None:
        try:
            base_url = str(client.base_url or "")
        except Exception:
            base_url = ""

    report: Dict[str, Any] = {
        "base_url": base_url,
        "port": _port_of(base_url) if base_url else None,
        "reachable": False,
        "state": ATTRIBUTION_UNKNOWN,
        "version": None,
        "owner_processes": [],
        "notes": [],
    }

    owners = runtime_owner_processes(base_url) if base_url else None
    if owners is None:
        report["notes"].append(
            "runtime owner could not be determined (indeterminate, not offline)"
        )
    else:
        report["owner_processes"] = [
            {"pid": item.get("pid"), "name": item.get("name")} for item in owners
        ]
        if not owners:
            report["notes"].append("no 9router runtime owner process is live")

    if client is None:
        report["notes"].append("no router client: reachability not probed")
        return report

    try:
        report["reachable"] = bool(client.is_server_reachable())
    except Exception as exc:
        report["notes"].append(f"reachability probe refused: {type(exc).__name__}")
        return report

    if not report["reachable"]:
        report["notes"].append("upstream did not answer the version endpoint")
        if owners is not None and not owners:
            report["state"] = ATTRIBUTION_OFFLINE
        return report

    report["state"] = ATTRIBUTION_LIVE
    try:
        report["version"] = client.get_server_version()
    except Exception as exc:
        report["notes"].append(f"version unreadable: {type(exc).__name__}")
    return report


def owner_summary(attribution: Dict[str, Any]) -> str:
    """One-line human summary of an attribution report."""
    base_url = str(attribution.get("base_url") or "")
    state = str(attribution.get("state") or ATTRIBUTION_UNKNOWN)
    version = attribution.get("version")
    owners: List[Dict[str, Any]] = list(attribution.get("owner_processes") or [])
    parts = [f"Upstream: {base_url or 'unknown'} {state}"]
    if version:
        parts.append(f"v{version}")
    if owners:
        names = ", ".join(
            f"{item.get('name')}#{item.get('pid')}" for item in owners[:3]
        )
        parts.append(f"owner {names}")
    return " | ".join(parts)
