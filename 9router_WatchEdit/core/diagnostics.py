"""
9router_WatchEdit - Safe Diagnostic Bundle Export
Produces a shareable diagnostic JSON (safe for external coding agents) containing
version/runtime info, app state, provider + model names, health classifications,
redacted errors and the configuration SCHEMA (never values).

The internal secret scanner gates the bundle: if any credential-shaped material
survives redaction, the bundle is NOT written.
"""
from __future__ import annotations

import json
import os
import platform
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from config import DIAGNOSTICS_DIR
from core.history import HealthCache
from core.redaction import redact_exception, redact_mapping, redact_text
from core.secret_scanner import Finding, scan_tree

CONFIG_SCHEMA = {
    "router_base_url": "str (localhost URL of 9Router)",
    "secret_backend": "str (windows | dpapi | vault)",
    "live_provider_access": "bool",
    "trusted_os_unlock": "bool (local machine only, never in repository)",
    "router_credential_ref": "str (secret ID reference, never a secret value)",
}


def build_diagnostic_data(
    cache: Optional[HealthCache] = None,
    discovery_outcomes: Optional[Dict[str, str]] = None,
    recent_errors: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Assembles the diagnostic payload with every string passed through redaction."""
    models: List[Dict[str, Any]] = []
    if cache is not None:
        with cache._lock:
            for cid, rec in list(cache.records.items()):
                models.append({
                    "canonical_id": redact_text(cid),
                    "provider": redact_text(rec.provider),
                    "availability": rec.availability,
                    "cost": rec.cost_status,
                    "confidence": rec.confidence,
                    "latency_ms": rec.latency_ms,
                    "status_code": rec.status_code,
                    "note": redact_text(rec.note or ""),
                })

    data: Dict[str, Any] = {
        "bundle": "9router_WatchEdit diagnostic",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "app": {
            "name": "9router_WatchEdit",
            "version": "1.0.0",
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "os": platform.system() + " " + platform.release(),
        },
        "state_summary": {
            "cached_model_records": len(models),
            "last_scan_time": getattr(cache, "last_scan_time", None) if cache else None,
        },
        "live_discovery_outcomes": redact_mapping(dict(discovery_outcomes or {})),
        "models": models,
        "recent_errors": [redact_text(e) for e in (recent_errors or [])][-25:],
        "configuration_schema": CONFIG_SCHEMA,
        "security_note": "Secrets are never included. Errors and names are redacted.",
    }
    return redact_mapping(data)


def export_diagnostic_bundle(
    cache: Optional[HealthCache] = None,
    discovery_outcomes: Optional[Dict[str, str]] = None,
    recent_errors: Optional[List[str]] = None,
    out_dir: Path = DIAGNOSTICS_DIR,
) -> Path:
    """Builds, scans and writes the diagnostic bundle. Raises RuntimeError on findings."""
    import json
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    data = build_diagnostic_data(cache, discovery_outcomes, recent_errors)
    payload = json.dumps(data, indent=2, ensure_ascii=False)

    # Gate: scan the payload text before it is ever written to disk.
    tmp_scan = out_dir / ".pending_diag_scan.txt"
    try:
        tmp_scan.write_text(payload, encoding="utf-8")
        findings: List[Finding] = scan_tree(out_dir, include=[tmp_scan])
    finally:
        if tmp_scan.exists():
            tmp_scan.unlink()

    if findings:
        raise RuntimeError(
            "SAFE EXPORT BLOCKED: diagnostic bundle contained credential-shaped "
            "material and was not written. "
            + "; ".join(f"{f.file}:{f.reason}" for f in findings)
        )

    out_path = out_dir / f"diagnostic_bundle_{time.strftime('%Y%m%d_%H%M%S')}.json"
    # EXPORT-004: atomic promotion — no half-built bundle under the final name
    tmp_out = out_dir / f".diag_{time.strftime('%Y%m%d_%H%M%S')}.partial"
    tmp_out.write_text(payload, encoding="utf-8")
    os.replace(tmp_out, out_path)
    return out_path
