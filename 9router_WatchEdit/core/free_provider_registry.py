"""
9router_WatchEdit - FREE provider control registry (FREE-FALLBACK-001, milestone 1).

Provider-level scan policy, deliberately independent from individual model rows.
The registry is the ONLY persistence owner for the mutable operator state behind
the "FREE Fallback" tab:

  * enabled / scan policy / scan mode / cost-risk classes / trusted-alive /
    operator note / last-known-good FREE inventory / SAIFREN tail ownership.

Storage rules (never relaxed):

  * private runtime/appdata storage only -- never the source tree, never a
    CWD-relative fallback (config._UNAVAILABLE_ROOT is inert, so every write
    fails visibly instead of landing beside the code);
  * atomic temp + replace writes with a unique temp name;
  * explicit schema version with deterministic migration/defaulting for older
    files;
  * a corrupt or schema-mismatched file is SURFACED (load_state + load_error)
    and is never silently accepted as a valid empty registry -- saving over it
    is refused unless the operator forces it;
  * a failed save is reported through last_save_error and the boolean return;
  * no API keys or tokens: the serialized document is validated by the
    repository's own secret scanner BEFORE it replaces the previous file.

Trusted-alive semantics (operator override, milestone 4): trust suppresses
needless LIVE probing while it is valid. It never fabricates FREE evidence,
never overrides PAID/POSSIBLE_BILLING classification, never makes an
unsupported route routable, and expires into normal policy behaviour.
"""
from __future__ import annotations

import copy
import functools
import json
import math
import re
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from config import LOCALAPPDATA_DIR, PRIVATE_STORAGE_AVAILABLE, _UNAVAILABLE_ROOT
from core.free_evidence import (
    BLOCKING_HEALTH_STATES,
    COST_RISK_BADGES,
    NON_MONETARY_COST_RISKS,
    CostRisk,
    FreeEvidence,
    ProviderHealth,
    RoutingCapability,
    ScanMode,
    ScanPolicy,
    cost_risk_coerce,
    tail_eligibility,
)

SCHEMA_VERSION = 3
REGISTRY_FILE_NAME = "free_providers.json"

# Snapshot load contract (mirrors core.opencode_catalog / core.ocf_registry):
# ABSENT is legitimately first-run; everything else is surfaced.
LOAD_ABSENT = "ABSENT"
LOAD_LOADED = "LOADED"
LOAD_MIGRATED = "MIGRATED"
LOAD_UNREADABLE = "UNREADABLE"
LOAD_SCHEMA_MISMATCH = "SCHEMA_MISMATCH"

_INVALID_LOAD_STATES = frozenset({LOAD_UNREADABLE, LOAD_SCHEMA_MISMATCH})

# Provider status rollup (last_status column).
STATUS_UNSCANNED = "UNSCANNED"
STATUS_OK = "OK"
STATUS_STALE = "STALE"
STATUS_OUTAGE = "OUTAGE"
STATUS_ERROR = "ERROR"
STATUS_DISABLED = "DISABLED"
STATUS_NO_FREE_ADAPTER = "NO_FREE_ADAPTER"
STATUS_RATE_LIMITED = "RATE_LIMITED"
STATUS_AUTH_FAILED = "AUTH_FAILED"
STATUS_DEAD = "DEAD"
STATUS_NEVER = "NEVER"

# Freshness bands for STALE_ONLY policy.
METADATA_TTL_SECONDS = 6 * 3600.0
LIVE_TTL_SECONDS = 30 * 60.0

# Trusted-alive durations requested by the operator (bounded).
TRUST_DURATION_SECONDS: Dict[str, Optional[float]] = {
    "1h": 3600.0,
    "1d": 86400.0,
    "7d": 7 * 86400.0,
    "30d": 30 * 86400.0,
    "until_cleared": None,
}
TRUST_SOURCE_MANUAL = "manual"

#: Keys that must never appear in the serialized document.
_FORBIDDEN_KEY_HINTS = ("token", "api_key", "apikey", "secret", "password", "bearer")

#: Adapter id used when a configured provider has no strict-free adapter.
ADAPTER_UNSUPPORTED_ID = "unsupported"


def _registry_locked(method):
    """Serialize a projection or in-memory mutation with registry writes."""
    @functools.wraps(method)
    def wrapped(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return wrapped


def _registry_mutation(*, provider_arg: Optional[int] = None,
                       ledger: bool = False, prune: bool = False,
                       pending: bool = False, all_providers: bool = False):
    """Lock a mutation and restore its touched state when persistence fails."""
    def decorate(method):
        @functools.wraps(method)
        def wrapped(self, *args, **kwargs):
            with self._lock:
                provider_id = None
                if provider_arg is not None:
                    if len(args) > provider_arg:
                        provider_id = str(args[provider_arg] or "")
                    else:
                        provider_id = str(kwargs.get("provider_id") or "")
                had_provider = provider_id in self.providers if provider_id else False
                provider_before = (
                    copy.deepcopy(self.providers[provider_id]) if had_provider else None
                )
                providers_before = copy.deepcopy(self.providers) if all_providers else None
                synced_before = copy.deepcopy(self.synced) if ledger else None
                prune_before = copy.deepcopy(self.prune_candidates) if prune else None
                pending_before = copy.deepcopy(self.pending_tail_sync) if pending else None
                try:
                    result = method(self, *args, **kwargs)
                except Exception:
                    if all_providers:
                        self.providers = providers_before
                    if provider_id:
                        if had_provider:
                            self.providers[provider_id] = provider_before
                        else:
                            self.providers.pop(provider_id, None)
                    if ledger:
                        self.synced = synced_before
                    if prune:
                        self.prune_candidates = prune_before
                    if pending:
                        self.pending_tail_sync = pending_before
                    raise
                if result is False:
                    error = self.last_save_error
                    if all_providers:
                        self.providers = providers_before
                    if provider_id:
                        if had_provider:
                            self.providers[provider_id] = provider_before
                        else:
                            self.providers.pop(provider_id, None)
                    if ledger:
                        self.synced = synced_before
                    if prune:
                        self.prune_candidates = prune_before
                    if pending:
                        self.pending_tail_sync = pending_before
                    self.last_save_error = error
                return result
        return wrapped
    return decorate


def _utc_now_iso(now: Optional[float] = None) -> str:
    stamp = datetime.fromtimestamp(now, tz=timezone.utc) if now else datetime.now(timezone.utc)
    return stamp.strftime("%Y-%m-%dT%H:%M:%SZ")


def _iso_to_epoch(value: Any) -> Optional[float]:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        ).timestamp()
    except ValueError:
        return None


def _coerce_policy(value: Any, default: ScanPolicy = ScanPolicy.MANUAL) -> ScanPolicy:
    try:
        return ScanPolicy(value)
    except (TypeError, ValueError):
        return default


def _coerce_mode(value: Any, default: ScanMode = ScanMode.DISABLED) -> ScanMode:
    try:
        return ScanMode(value)
    except (TypeError, ValueError):
        return default


def _finite_json_value(value: Any) -> bool:
    """Reject non-JSON values and NaN/Infinity anywhere in persisted state."""
    if value is None or isinstance(value, (str, bool, int)):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, list):
        return all(_finite_json_value(item) for item in value)
    if isinstance(value, dict):
        return all(
            isinstance(key, str) and _finite_json_value(item)
            for key, item in value.items()
        )
    return False


def _valid_timestamp(value: Any, *, optional: bool = True) -> bool:
    if not isinstance(value, str):
        return False
    if not value:
        return optional
    return _iso_to_epoch(value) is not None


def _valid_model_row(row: Any) -> bool:
    if not isinstance(row, dict):
        return False
    if not isinstance(row.get("canonical_id"), str) or not row["canonical_id"].strip():
        return False
    string_fields = (
        "upstream_model_id", "provider_id", "evidence_source", "exclusion_reason",
        "note", "synced_at",
    )
    if any(key in row and not isinstance(row[key], str) for key in string_fields):
        return False
    for key, enum_type in (
        ("free_evidence", FreeEvidence),
        ("provider_health", ProviderHealth),
        ("routing", RoutingCapability),
        ("cost_risk", CostRisk),
    ):
        if key in row:
            try:
                enum_type(row[key])
            except (TypeError, ValueError):
                return False
    if "scanner_managed" in row and type(row["scanner_managed"]) is not bool:
        return False
    if "last_seen" in row and not _valid_timestamp(row["last_seen"]):
        return False
    return _finite_json_value(row)


def _valid_provider_row(key: Any, row: Any) -> bool:
    if not isinstance(key, str) or not key.strip() or not isinstance(row, dict):
        return False
    string_fields = {
        "provider_id", "display_name", "adapter", "trusted_alive_set_at",
        "trusted_alive_source", "last_scan_at", "last_metadata_success_at",
        "last_live_probe_success_at", "last_error_class", "last_error_summary",
        "last_status", "next_scan_due", "operator_note", "adapter_transport",
        "adapter_metadata_source", "last_scan_action",
    }
    bool_fields = {
        "enabled", "allow_billing_probe", "trusted_alive",
        "adapter_metadata_supported", "adapter_live_supported", "adapter_client_bound",
    }
    numeric_fields = {
        "last_metadata_success_epoch", "last_live_probe_success_epoch",
        "next_scan_due_epoch", "backoff_until_epoch",
    }
    integer_fields = {"models_discovered", "strict_free_count", "conditional_count"}
    if "provider_id" in row and row["provider_id"] != key:
        return False
    if any(name in row and not isinstance(row[name], str) for name in string_fields):
        return False
    if any(name in row and type(row[name]) is not bool for name in bool_fields):
        return False
    if any(
        name in row and (
            isinstance(row[name], bool)
            or not isinstance(row[name], (int, float))
            or not math.isfinite(float(row[name]))
        )
        for name in numeric_fields
    ):
        return False
    if "trusted_alive_until" in row:
        value = row["trusted_alive_until"]
        if value is not None and (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
        ):
            return False
    if any(
        name in row and (type(row[name]) is not int or row[name] < 0)
        for name in integer_fields
    ):
        return False
    if "scan_policy" in row:
        try:
            ScanPolicy(row["scan_policy"])
        except (TypeError, ValueError):
            return False
    if "scan_mode" in row:
        try:
            ScanMode(row["scan_mode"])
        except (TypeError, ValueError):
            return False
    for name in ("metadata_cost_risk", "live_probe_cost_risk"):
        if name in row:
            try:
                CostRisk(row[name])
            except (TypeError, ValueError):
                return False
    if "last_status" in row and not re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", row["last_status"]):
        return False
    for name in (
        "trusted_alive_set_at", "last_scan_at", "last_metadata_success_at",
        "last_live_probe_success_at", "next_scan_due",
    ):
        if name in row and not _valid_timestamp(row[name]):
            return False
    models = row.get("models", [])
    if not isinstance(models, list) or any(not _valid_model_row(model) for model in models):
        return False
    known = set(FreeProviderRecord.__dataclass_fields__)
    return not any(name not in known for name in row) and _finite_json_value(row)


def _valid_synced_record(canonical_id: Any, row: Any) -> bool:
    return (
        isinstance(canonical_id, str) and bool(canonical_id.strip())
        and isinstance(row, dict)
        and isinstance(row.get("provider_id"), str)
        and isinstance(row.get("combo"), str) and bool(row["combo"].strip())
        and _valid_timestamp(row.get("at"), optional=False)
        and _finite_json_value(row)
    )


def _valid_prune_record(row: Any) -> bool:
    return (
        isinstance(row, dict)
        and isinstance(row.get("canonical_id"), str) and bool(row["canonical_id"].strip())
        and isinstance(row.get("provider_id"), str)
        and _valid_timestamp(row.get("at"), optional=False)
        and isinstance(row.get("reason"), str)
        and _finite_json_value(row)
    )


def _valid_tail_sync_intent(pending: Any) -> bool:
    return (
        isinstance(pending, dict)
        and type(pending.get("schema_version")) is int
        and pending.get("schema_version") == 1
        and isinstance(pending.get("combo_name"), str)
        and bool(pending.get("combo_name", "").strip())
        and all(
            isinstance(pending.get(name), list)
            and all(isinstance(value, str) for value in pending.get(name, []))
            for name in ("old_models", "new_models", "appended", "removed")
        )
        and isinstance(pending.get("provider_ids"), dict)
        and all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in pending.get("provider_ids", {}).items()
        )
        and _valid_timestamp(pending.get("staged_at"), optional=False)
        and _finite_json_value(pending)
    )


@dataclass
class FreeProviderRecord:
    """One provider's mutable operator state + last-known-good FREE inventory."""

    provider_id: str
    display_name: str = ""
    adapter: str = "unsupported"
    enabled: bool = False
    scan_policy: str = ScanPolicy.MANUAL.value
    scan_mode: str = ScanMode.DISABLED.value
    # Separate cost classes: money risk is never collapsed into quota risk.
    metadata_cost_risk: str = CostRisk.UNKNOWN.value
    live_probe_cost_risk: str = CostRisk.UNKNOWN.value
    allow_billing_probe: bool = False
    trusted_alive: bool = False
    trusted_alive_until: Optional[float] = None
    trusted_alive_set_at: str = ""
    trusted_alive_source: str = ""
    last_scan_at: str = ""
    last_metadata_success_at: str = ""
    last_live_probe_success_at: str = ""
    last_metadata_success_epoch: float = 0.0
    last_live_probe_success_epoch: float = 0.0
    last_error_class: str = ""
    last_error_summary: str = ""
    last_status: str = STATUS_UNSCANNED
    models_discovered: int = 0
    strict_free_count: int = 0
    conditional_count: int = 0
    next_scan_due: str = ""
    next_scan_due_epoch: float = 0.0
    backoff_until_epoch: float = 0.0
    operator_note: str = ""
    # Adapter capability projection (written from the adapter contract).
    adapter_metadata_supported: bool = False
    adapter_live_supported: bool = False
    adapter_client_bound: bool = False
    adapter_transport: str = ""
    adapter_metadata_source: str = ""
    # Last-known-good inventory: retained across an authoritative-source outage.
    models: List[Dict[str, Any]] = field(default_factory=list)
    # Result of the most recent scan action (never a routing authority).
    last_scan_action: str = ""

    # ------------------------------------------------------------ projections
    @property
    def evidence_models(self) -> List[FreeEvidence]:
        out: List[FreeEvidence] = []
        for row in self.models:
            try:
                out.append(FreeEvidence(row.get("free_evidence")))
            except (TypeError, ValueError):
                out.append(FreeEvidence.UNKNOWN_COST)
        return out

    @property
    def has_strict_free(self) -> bool:
        return any(e == FreeEvidence.STRICT_FREE for e in self.evidence_models)

    @property
    def live_probe_cost(self) -> CostRisk:
        return cost_risk_coerce(self.live_probe_cost_risk)

    @property
    def metadata_cost(self) -> CostRisk:
        return cost_risk_coerce(self.metadata_cost_risk)

    def cost_safe_for_live(self) -> bool:
        """POSSIBLE_BILLING/UNKNOWN/ACCOUNT_CONDITIONAL are refused by default."""
        if self.live_probe_cost in NON_MONETARY_COST_RISKS:
            return True
        return bool(self.allow_billing_probe)

    def trust_valid(self, now: Optional[float] = None) -> bool:
        if not self.trusted_alive:
            return False
        if self.trusted_alive_until is None:
            return True
        now = now if now is not None else datetime.now(timezone.utc).timestamp()
        return float(self.trusted_alive_until) > float(now)

    def trust_text(self, now: Optional[float] = None) -> str:
        if not self.trusted_alive:
            return "-"
        if self.trusted_alive_until is None:
            return "trusted (until cleared)"
        now = now if now is not None else datetime.now(timezone.utc).timestamp()
        remaining = float(self.trusted_alive_until) - float(now)
        if remaining <= 0:
            return "expired"
        return f"trusted {_human_duration(remaining)} left"


def _human_duration(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    if seconds >= 86400:
        return f"{int(round(seconds / 86400.0))}d"
    if seconds >= 3600:
        return f"{int(round(seconds / 3600.0))}h"
    return f"{int(round(seconds / 60.0))}m"


def provider_badges(record: FreeProviderRecord, now: Optional[float] = None) -> List[str]:
    """Explanatory badges only -- the policy/evidence fields stay authoritative."""
    badges: List[str] = []
    if record.adapter in ("", ADAPTER_UNSUPPORTED_ID):
        badges.append("NO FREE ADAPTER")
    if record.metadata_cost == CostRisk.ZERO_MONETARY_METADATA:
        badges.append(COST_RISK_BADGES[CostRisk.ZERO_MONETARY_METADATA])
    if record.live_probe_cost == CostRisk.FREE_QUOTA_PROBE:
        badges.append(COST_RISK_BADGES[CostRisk.FREE_QUOTA_PROBE])
    if record.live_probe_cost in (CostRisk.ACCOUNT_CONDITIONAL, CostRisk.UNKNOWN):
        badges.append(COST_RISK_BADGES[CostRisk.ACCOUNT_CONDITIONAL])
    if record.live_probe_cost == CostRisk.POSSIBLE_BILLING:
        badges.append(COST_RISK_BADGES[CostRisk.POSSIBLE_BILLING])
    if record.adapter_client_bound:
        badges.append("CLIENT BOUND")
    if record.last_status == STATUS_RATE_LIMITED:
        badges.append("RATE LIMITED")
    if record.has_strict_free:
        badges.append("STRICT FREE")
    if record.trust_valid(now):
        badges.append("TRUSTED")
    if record.allow_billing_probe:
        badges.append("BILLING CONSENT")
    if _coerce_policy(record.scan_policy) == ScanPolicy.NEVER:
        badges.append("SCANNING DISABLED")
    return badges


class FreeProviderRegistry:
    """Persistent provider-control registry with fail-closed persistence."""

    def __init__(self, path: Optional[Path] = None, *, clock=None):
        if path is not None:
            self.path = Path(path)
        elif PRIVATE_STORAGE_AVAILABLE:
            self.path = LOCALAPPDATA_DIR / "runtime" / REGISTRY_FILE_NAME
        else:
            self.path = _UNAVAILABLE_ROOT / REGISTRY_FILE_NAME
        self.storage_available = bool(PRIVATE_STORAGE_AVAILABLE)
        self._clock = clock
        self._lock = threading.RLock()
        self.providers: Dict[str, FreeProviderRecord] = {}
        self.synced: Dict[str, Dict[str, Any]] = {}
        self.prune_candidates: List[Dict[str, Any]] = []
        self.pending_tail_sync: Optional[Dict[str, Any]] = None
        self.load_state: str = LOAD_ABSENT
        self.load_error: str = ""
        self.migrated_from: int = 0
        self.last_save_error: str = ""
        self.saved_at: str = ""
        self.scan_evidence_dirty: bool = False
        self._load()

    # ------------------------------------------------------------------ clock
    def _now(self) -> float:
        if self._clock is not None:
            return float(self._clock())
        return datetime.now(timezone.utc).timestamp()

    @property
    def state_valid(self) -> bool:
        """False when the on-disk file exists but could not be trusted."""
        return self.load_state not in _INVALID_LOAD_STATES

    # ------------------------------------------------------------ persistence
    def _default_record(self, provider_id: str) -> FreeProviderRecord:
        return FreeProviderRecord(provider_id=provider_id, display_name=provider_id)

    def _load(self) -> None:
        self.load_state = LOAD_ABSENT
        self.load_error = ""
        self.migrated_from = 0
        try:
            if not self.path.exists():
                return
            raw = self.path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except (OSError, ValueError) as ex:
            self.load_state = LOAD_UNREADABLE
            self.load_error = f"{type(ex).__name__}: registry file could not be read"
            return
        if not isinstance(data, dict):
            self.load_state = LOAD_UNREADABLE
            self.load_error = "registry root is not an object"
            return
        if not _finite_json_value(data):
            self.load_state = LOAD_UNREADABLE
            self.load_error = "registry contains invalid JSON values or non-finite numbers"
            return

        version = data.get("schema_version")
        if type(version) is not int or version < 1:
            self.load_state = LOAD_SCHEMA_MISMATCH
            self.load_error = "missing schema_version"
            return
        if version > SCHEMA_VERSION:
            self.load_state = LOAD_SCHEMA_MISMATCH
            self.load_error = f"schema_version {version} is newer than {SCHEMA_VERSION}"
            return
        migrated = version < SCHEMA_VERSION
        if not migrated and not all(
            name in data for name in
            ("saved_at", "providers", "synced", "prune_candidates", "pending_tail_sync")
        ):
            self.load_state = LOAD_UNREADABLE
            self.load_error = "current registry schema is missing a required section"
            return
        if "saved_at" in data and not _valid_timestamp(data["saved_at"], optional=False):
            self.load_state = LOAD_UNREADABLE
            self.load_error = "saved_at is not a valid UTC timestamp"
            return

        providers = data.get("providers")
        if providers is None:
            providers = {}
        if not isinstance(providers, dict):
            self.load_state = LOAD_UNREADABLE
            self.load_error = "providers section is not an object"
            return

        known = set(FreeProviderRecord.__dataclass_fields__)
        loaded: Dict[str, FreeProviderRecord] = {}
        for key, row in providers.items():
            if not _valid_provider_row(key, row):
                self.load_state = LOAD_UNREADABLE
                self.load_error = f"provider record '{key}' has invalid persisted fields"
                return
            if not migrated and not known.issubset(row):
                self.load_state = LOAD_UNREADABLE
                self.load_error = f"provider record '{key}' is missing current schema fields"
                return
            values = {
                k: v for k, v in row.items()
                if k in known and k not in ("models", "provider_id")
            }
            record = FreeProviderRecord(provider_id=str(key), **values)
            record.models = self._sanitize_models(row.get("models"))
            if migrated:
                # v1 -> v2: the newer fields simply take their deterministic
                # defaults; nothing operator-visible is invented.
                record.next_scan_due = record.next_scan_due or ""
                record.allow_billing_probe = bool(record.allow_billing_probe)
            loaded[str(key)] = record
        synced = data.get("synced", {} if migrated else None)
        if not isinstance(synced, dict):
            self.load_state = LOAD_UNREADABLE
            self.load_error = "synced section is not an object"
            return
        clean_synced: Dict[str, Dict[str, Any]] = {}
        for canonical_id, row in synced.items():
            if not _valid_synced_record(canonical_id, row):
                self.load_state = LOAD_UNREADABLE
                self.load_error = "synced section contains an invalid ownership record"
                return
            clean_synced[canonical_id] = dict(row)
        pruned = data.get("prune_candidates", [] if migrated else None)
        if not isinstance(pruned, list):
            self.load_state = LOAD_UNREADABLE
            self.load_error = "prune_candidates section is not a list"
            return
        clean_pruned: List[Dict[str, Any]] = []
        for row in pruned:
            if not _valid_prune_record(row):
                self.load_state = LOAD_UNREADABLE
                self.load_error = "prune_candidates section contains an invalid record"
                return
            clean_pruned.append(dict(row))
        pending = data.get("pending_tail_sync")
        if pending is not None and not _valid_tail_sync_intent(pending):
            self.load_state = LOAD_UNREADABLE
            self.load_error = "pending_tail_sync section is invalid"
            return
        self.providers = loaded
        self.synced = clean_synced
        self.prune_candidates = clean_pruned
        self.pending_tail_sync = copy.deepcopy(pending)
        self.saved_at = str(data.get("saved_at") or "")
        self.migrated_from = version if migrated else 0
        self.load_state = LOAD_MIGRATED if migrated else LOAD_LOADED

    @staticmethod
    def _sanitize_models(raw: Any) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        if not isinstance(raw, list):
            return rows
        for row in raw:
            if not isinstance(row, dict):
                continue
            canonical_id = str(row.get("canonical_id") or "")
            if not canonical_id:
                continue
            clean = {str(k): v for k, v in row.items() if isinstance(k, str)}
            rows.append(clean)
        return rows

    def document(self) -> Dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "saved_at": _utc_now_iso(self._now()),
            "providers": {pid: asdict(record) for pid, record in self.providers.items()},
            "synced": {k: dict(v) for k, v in self.synced.items()},
            "prune_candidates": [dict(row) for row in self.prune_candidates],
            "pending_tail_sync": copy.deepcopy(self.pending_tail_sync),
        }

    def _secret_findings(self, tmp: Path) -> List[str]:
        """Validate the freshly written document with the repository scanner."""
        try:
            from core.secret_scanner import scan_file
        except Exception:  # pragma: no cover - scanner always importable in-repo
            return []
        try:
            findings = scan_file(tmp)
        except Exception as ex:  # fail closed
            return [f"scanner_error:{type(ex).__name__}"]
        return [f"{f.reason}" for f in findings]

    def save(self, *, force: bool = False) -> bool:
        """Atomic temp + replace. Returns False and records WHY on any refusal."""
        with self._lock:
            if not self.state_valid and not force:
                self.last_save_error = (
                    f"registry state {self.load_state}: refusing to overwrite unreadable/"
                    "untrusted file (nothing was written)"
                )
                return False
            if not self.storage_available:
                self.last_save_error = "private storage unavailable; nothing was written"
                return False
            doc = self.document()
            if (
                not _valid_timestamp(doc.get("saved_at"), optional=False)
                or not _finite_json_value(doc)
                or any(not _valid_provider_row(pid, asdict(record))
                       for pid, record in self.providers.items())
                or any(not _valid_synced_record(cid, row) for cid, row in self.synced.items())
                or any(not _valid_prune_record(row) for row in self.prune_candidates)
                or (self.pending_tail_sync is not None
                    and not _valid_tail_sync_intent(self.pending_tail_sync))
            ):
                self.last_save_error = "refused: registry contains invalid persisted fields"
                return False
            for name in FreeProviderRecord.__dataclass_fields__:
                if any(hint in name.lower() for hint in _FORBIDDEN_KEY_HINTS):
                    self.last_save_error = f"refused: forbidden field '{name}'"
                    return False
            # Each attempt owns a unique staging path. The registry lock covers
            # snapshot, serialization, secret scan and promotion, so another
            # writer cannot validate or publish a different document midway.
            tmp = self.path.with_name(
                f"{self.path.name}.{threading.get_ident()}.{uuid.uuid4().hex}.tmp"
            )
            try:
                payload = json.dumps(doc, indent=2, sort_keys=True).encode("utf-8")
                self.path.parent.mkdir(parents=True, exist_ok=True)
                tmp.write_bytes(payload)
                findings = self._secret_findings(tmp)
                if findings:
                    self.last_save_error = (
                        "refused: serialized registry contains credential-shaped material ("
                        + ", ".join(sorted(set(findings))[:4]) + ")"
                    )
                    tmp.unlink(missing_ok=True)
                    return False
                tmp.replace(self.path)
            except OSError as ex:
                self.last_save_error = f"{type(ex).__name__}: registry could not be written"
                try:
                    tmp.unlink(missing_ok=True)
                except OSError:
                    pass
                return False
            self.saved_at = doc["saved_at"]
            self.last_save_error = ""
            self.scan_evidence_dirty = False
            if not self.state_valid:
                self.load_state = LOAD_LOADED
                self.load_error = ""
            return True

    # ------------------------------------------------------------------ records
    @_registry_mutation(provider_arg=0)
    def ensure_provider(
        self,
        provider_id: str,
        display_name: str = "",
        *,
        adapter: str = "unsupported",
        metadata_cost_risk: CostRisk = CostRisk.UNKNOWN,
        live_probe_cost_risk: CostRisk = CostRisk.UNKNOWN,
        default_enabled: bool = False,
        default_policy: ScanPolicy = ScanPolicy.MANUAL,
        default_mode: ScanMode = ScanMode.DISABLED,
        save: bool = False,
    ) -> FreeProviderRecord:
        """Create a provider record with deterministic defaults, or return it.

        Existing operator state is NEVER overwritten by this call: it only fills
        an absent record and refreshes the adapter/capability description.
        """
        pid = str(provider_id or "").strip()
        if not pid:
            raise ValueError("provider_id is required")
        with self._lock:
            record = self.providers.get(pid)
            if record is None:
                record = self._default_record(pid)
                record.display_name = display_name or pid
                record.adapter = adapter
                record.metadata_cost_risk = metadata_cost_risk.value
                record.live_probe_cost_risk = live_probe_cost_risk.value
                record.enabled = bool(default_enabled)
                record.scan_policy = default_policy.value
                record.scan_mode = default_mode.value
                record.last_status = STATUS_UNSCANNED if record.enabled else STATUS_DISABLED
                self.providers[pid] = record
            else:
                if display_name:
                    record.display_name = display_name
                if adapter and adapter != "unsupported":
                    record.adapter = adapter
        if save and not self.save():
            return False
        return record

    @_registry_locked
    def get(self, provider_id: str) -> Optional[FreeProviderRecord]:
        return self.providers.get(str(provider_id or ""))

    @_registry_mutation(provider_arg=0)
    def set_enabled(self, provider_id: str, enabled: bool) -> bool:
        record = self.get(provider_id)
        if record is None:
            return False
        record.enabled = bool(enabled)
        if not record.enabled:
            record.last_status = STATUS_DISABLED
        elif record.last_status == STATUS_DISABLED:
            record.last_status = STATUS_UNSCANNED
        return self.save()

    @_registry_mutation(provider_arg=0)
    def set_policy(self, provider_id: str, policy: Any) -> bool:
        record = self.get(provider_id)
        if record is None:
            return False
        record.scan_policy = _coerce_policy(policy).value
        return self.save()

    @_registry_mutation(provider_arg=0)
    def set_mode(self, provider_id: str, mode: Any) -> bool:
        record = self.get(provider_id)
        if record is None:
            return False
        record.scan_mode = _coerce_mode(mode).value
        return self.save()

    @_registry_mutation(provider_arg=0)
    def set_note(self, provider_id: str, note: str) -> bool:
        record = self.get(provider_id)
        if record is None:
            return False
        record.operator_note = str(note or "")
        return self.save()

    @_registry_mutation(provider_arg=0)
    def set_live_probe_consent(self, provider_id: str, allowed: bool) -> bool:
        record = self.get(provider_id)
        if record is None:
            return False
        record.allow_billing_probe = bool(allowed)
        return self.save()

    @_registry_locked
    def describe_adapter(self, provider_id: str, capabilities: Mapping[str, Any]) -> None:
        """Persist the adapter contract projection (no I/O, no network)."""
        record = self.get(provider_id)
        if record is None:
            return
        record.adapter = str(capabilities.get("adapter_id") or record.adapter)
        record.adapter_metadata_supported = bool(capabilities.get("supports_metadata"))
        record.adapter_live_supported = bool(capabilities.get("supports_live_canary"))
        record.adapter_client_bound = bool(capabilities.get("client_bound"))
        record.adapter_transport = str(capabilities.get("transport") or "")
        record.adapter_metadata_source = str(capabilities.get("metadata_source") or "")

    # ------------------------------------------------------------------- trust
    @_registry_mutation(provider_arg=0)
    def mark_trusted_alive(self, provider_id: str, duration: str) -> bool:
        record = self.get(provider_id)
        if record is None:
            return False
        if duration not in TRUST_DURATION_SECONDS:
            return False
        seconds = TRUST_DURATION_SECONDS[duration]
        now = self._now()
        record.trusted_alive = True
        record.trusted_alive_until = None if seconds is None else now + float(seconds)
        record.trusted_alive_set_at = _utc_now_iso(now)
        record.trusted_alive_source = TRUST_SOURCE_MANUAL
        # Trust never invents health or FREE evidence: only the live-probe
        # suppression window changes.
        return self.save()

    @_registry_mutation(provider_arg=0)
    def clear_trusted_alive(self, provider_id: str) -> bool:
        record = self.get(provider_id)
        if record is None:
            return False
        record.trusted_alive = False
        record.trusted_alive_until = None
        record.trusted_alive_set_at = ""
        record.trusted_alive_source = ""
        return self.save()

    @_registry_locked
    def trust_valid(self, provider_id: str) -> bool:
        record = self.get(provider_id)
        return bool(record and record.trust_valid(self._now()))

    # ----------------------------------------------------------- scan results
    @_registry_locked
    def record_scan_started(self, provider_id: str, action: str = "") -> bool:
        record = self.get(provider_id)
        if record is None:
            return False
        record.last_scan_at = _utc_now_iso(self._now())
        record.last_scan_action = str(action or "")
        return True

    @_registry_locked
    def set_backoff(self, provider_id: str, seconds: float) -> None:
        record = self.get(provider_id)
        if record is None or not seconds:
            return
        record.backoff_until_epoch = self._now() + max(0.0, float(seconds))
        record.next_scan_due = _utc_now_iso(record.backoff_until_epoch)
        record.next_scan_due_epoch = record.backoff_until_epoch

    @_registry_mutation(provider_arg=0, prune=True)
    def record_metadata_result(
        self,
        provider_id: str,
        *,
        ok: bool,
        models: Optional[Sequence[Mapping[str, Any]]] = None,
        error_class: str = "",
        error_summary: str = "",
        retry_after_sec: float = 0.0,
        authoritative: bool = True,
        status: str = "",
        persist: bool = True,
    ) -> bool:
        """Apply a metadata-scan outcome. FAITHFUL to source outages.

        A failed metadata scan NEVER erases the last-known-good inventory and
        never prunes SAIFREN tail ownership. Only a SUCCESSFUL authoritative
        result may drop models (and then only the scanner-owned ones whose FREE
        evidence positively disappeared).
        """
        record = self.get(provider_id)
        if record is None:
            return False
        now = self._now()
        record.last_scan_at = _utc_now_iso(now)
        if not ok:
            record.last_error_class = str(error_class or "metadata_error")
            record.last_error_summary = str(error_summary or "")[:400]
            record.last_status = (
                status or (STATUS_RATE_LIMITED if retry_after_sec else STATUS_OUTAGE)
            )
            if retry_after_sec:
                self.set_backoff(provider_id, retry_after_sec)
            elif record.enabled:
                record.next_scan_due = ""
                record.next_scan_due_epoch = 0.0
            return self.save() if persist else self._stage_scan_evidence()

        incoming = list(models or [])
        manual_rows = [row for row in record.models if not row.get("scanner_managed", True)]
        record.models = self._merge_incoming(record, incoming, manual_rows, authoritative, now)
        self._refresh_counts(record)
        record.last_metadata_success_at = _utc_now_iso(now)
        record.last_metadata_success_epoch = now
        record.last_error_class = ""
        record.last_error_summary = ""
        record.last_status = STATUS_OK
        record.backoff_until_epoch = 0.0
        record.next_scan_due = ""
        record.next_scan_due_epoch = 0.0
        return self.save() if persist else self._stage_scan_evidence()

    def _stage_scan_evidence(self) -> bool:
        """Mark scanner state for the next bounded/terminal durable checkpoint."""
        self.scan_evidence_dirty = True
        return True

    def _merge_incoming(
        self,
        record: FreeProviderRecord,
        incoming: Sequence[Mapping[str, Any]],
        manual_rows: List[Dict[str, Any]],
        authoritative: bool,
        now: float,
    ) -> List[Dict[str, Any]]:
        previous = {str(row.get("canonical_id")): row for row in record.models}
        manual_by_id = {
            str(row.get("canonical_id")): row for row in manual_rows
            if row.get("canonical_id")
        }
        incoming_by_id: Dict[str, Dict[str, Any]] = {}
        for raw in incoming:
            canonical_id = str(raw.get("canonical_id") or "")
            if not canonical_id or canonical_id in manual_by_id:
                # Operator-owned rows win an identity collision; scanner data
                # must never take ownership of or rewrite them.
                continue
            incoming_by_id.setdefault(canonical_id, dict(raw))

        def merged(canonical_id: str, raw: Mapping[str, Any]) -> Dict[str, Any]:
            row = dict(raw)
            old = previous.get(canonical_id) or {}
            row["scanner_managed"] = True
            row["last_seen"] = row.get("last_seen") or _utc_now_iso(now)
            if old.get("synced_to_saifren"):
                # Synced state belongs to the tail-sync ledger, not the catalog.
                row["synced_to_saifren"] = True
                row["synced_at"] = old.get("synced_at", "")
            if old.get("last_success_use"):
                row["last_success_use"] = old["last_success_use"]
            return row

        seen = set(incoming_by_id)
        rows: List[Dict[str, Any]] = []
        if not authoritative:
            # Partial data can update positive observations, but absence from
            # that partial result says nothing about existing inventory.
            for old in record.models:
                canonical_id = str(old.get("canonical_id") or "")
                if not old.get("scanner_managed", True):
                    rows.append(dict(old))
                elif canonical_id in incoming_by_id:
                    rows.append(merged(canonical_id, incoming_by_id[canonical_id]))
                else:
                    rows.append(dict(old))
            existing = {
                str(row.get("canonical_id") or "") for row in rows
            }
            for canonical_id, raw in incoming_by_id.items():
                if canonical_id not in existing:
                    rows.append(merged(canonical_id, raw))
            return rows

        for canonical_id, raw in incoming_by_id.items():
            rows.append(merged(canonical_id, raw))
        rows.extend(dict(row) for row in manual_rows)
        self._schedule_prune(record, previous, seen, now)
        return rows

    def _schedule_prune(
        self,
        record: FreeProviderRecord,
        previous: Mapping[str, Dict[str, Any]],
        seen: Iterable[str],
        now: float,
    ) -> None:
        """Mark tail entries whose FREE evidence positively disappeared.

        Only SCANNER-OWNED entries reach this list, and only after a SUCCESSFUL
        authoritative refresh: a provider outage never prunes anything.
        """
        seen_set = set(seen)
        existing_candidates = {
            str(item.get("canonical_id") or "") for item in self.prune_candidates
        }
        for canonical_id, row in previous.items():
            if not row.get("scanner_managed", True):
                continue
            if canonical_id in seen_set:
                continue
            if not row.get("synced_to_saifren"):
                continue
            if canonical_id in existing_candidates:
                continue
            self.prune_candidates.append({
                "canonical_id": canonical_id,
                "provider_id": record.provider_id,
                "at": _utc_now_iso(now),
                "reason": "authoritative metadata no longer advertises this FREE model",
            })
            existing_candidates.add(canonical_id)

    @_registry_mutation(provider_arg=0)
    def record_live_probe_result(
        self,
        provider_id: str,
        *,
        ok: bool,
        state: str = "",
        error_class: str = "",
        error_summary: str = "",
        blocking_health: Optional[ProviderHealth] = None,
        persist: bool = True,
    ) -> bool:
        """Live health evidence. Never rewrites FREE evidence in either direction.

        A successful canary is positive health evidence and clears an older
        failure verdict; a proven blocking failure (auth revoked, runtime gone)
        marks the lane unhealthy so the FREE tail excludes it. Transient
        failures leave the evidence rows alone.
        """
        record = self.get(provider_id)
        if record is None:
            return False
        now = self._now()
        # A REFUSED live probe never erases the metadata pass that just ran:
        # the caller records this refusal in the outcome before calling here.
        if not ok and not self.live_probe_needed(provider_id):
            refused_live = True
        else:
            refused_live = False
        if not refused_live:
            record.last_scan_at = _utc_now_iso(now)
        if ok:
            record.last_live_probe_success_at = _utc_now_iso(now)
            record.last_live_probe_success_epoch = now
            record.last_error_class = ""
            record.last_error_summary = ""
            record.last_status = STATUS_OK
            self.set_health(provider_id, ProviderHealth.HEALTHY)
            return self.save() if persist else self._stage_scan_evidence()
        record.last_error_class = str(error_class or state or "live_probe_failed")
        record.last_error_summary = str(error_summary or state or "")[:400]
        if blocking_health is not None:
            if not self.record_health_state(provider_id, blocking_health, persist=persist):
                return False
            return True
        record.last_status = str(state or STATUS_ERROR)
        return self.save() if persist else self._stage_scan_evidence()

    @_registry_locked
    def set_health(self, provider_id: str, health: ProviderHealth) -> bool:
        """Apply a provider-level health verdict to the inventory rows."""
        record = self.get(provider_id)
        if record is None:
            return False
        for row in record.models:
            row["provider_health"] = ProviderHealth(health).value
            if health in BLOCKING_HEALTH_STATES:
                row["exclusion_reason"] = f"health {ProviderHealth(health).value}"
            else:
                row["exclusion_reason"] = ""
        return True

    @_registry_mutation(provider_arg=0)
    def record_health_state(self, provider_id: str, health: ProviderHealth,
                            status: str = "", *, persist: bool = True) -> bool:
        """Apply an authoritative positive health verdict (e.g. auth revoked)."""
        record = self.get(provider_id)
        if record is None:
            return False
        self.set_health(provider_id, health)
        if status:
            record.last_status = status
        elif health == ProviderHealth.AUTH_FAILED:
            record.last_status = STATUS_AUTH_FAILED
        elif health == ProviderHealth.DEAD:
            record.last_status = STATUS_DEAD
        return self.save() if persist else self._stage_scan_evidence()

    def _refresh_counts(self, record: FreeProviderRecord) -> None:
        strict = 0
        conditional = 0
        for row in record.models:
            value = row.get("free_evidence")
            if value == FreeEvidence.STRICT_FREE.value:
                strict += 1
            elif value in (FreeEvidence.CONDITIONAL_FREE.value, FreeEvidence.UNKNOWN_COST.value):
                conditional += 1
        record.strict_free_count = strict
        record.conditional_count = conditional
        record.models_discovered = len([r for r in record.models])

    # ------------------------------------------------------- manual inventory
    @_registry_mutation(provider_arg=0)
    def add_manual_model(self, provider_id: str, canonical_id: str, *,
                         free_evidence: FreeEvidence = FreeEvidence.UNKNOWN_COST,
                         note: str = "") -> bool:
        """Operator-owned model row: never removed by reconciliation.

        The default evidence is UNKNOWN_COST on purpose: an operator adding a
        row is not authoritative proof of zero monetary cost, so a manual row
        is never silently promoted into the strict FREE tail.
        """
        record = self.get(provider_id)
        if record is None:
            return False
        for row in record.models:
            if row.get("canonical_id") == canonical_id:
                row["scanner_managed"] = False
                return self.save()
        record.models.append({
            "canonical_id": canonical_id,
            "upstream_model_id": str(canonical_id).split("/", 1)[-1],
            "provider_id": provider_id,
            "free_evidence": FreeEvidence(free_evidence).value,
            "provider_health": ProviderHealth.UNKNOWN.value,
            "routing": RoutingCapability.UNKNOWN.value,
            "cost_risk": CostRisk.UNKNOWN.value,
            "scanner_managed": False,
            "last_seen": _utc_now_iso(self._now()),
            "note": note,
        })
        self._refresh_counts(record)
        return self.save()

    # -------------------------------------------------------------- tail sync
    @_registry_mutation(provider_arg=1, ledger=True)
    def mark_synced(self, canonical_ids: Sequence[str], provider_id: str,
                    combo: str) -> bool:
        now = self._now()
        for canonical_id in canonical_ids:
            self.synced[str(canonical_id)] = {
                "provider_id": str(provider_id),
                "combo": str(combo),
                "at": _utc_now_iso(now),
            }
        record = self.get(provider_id)
        if record is not None:
            wanted = set(canonical_ids)
            for row in record.models:
                if row.get("canonical_id") in wanted:
                    row["synced_to_saifren"] = True
                    row["synced_at"] = _utc_now_iso(now)
        return self.save()

    @_registry_mutation(ledger=True)
    def drop_synced(self, canonical_ids: Sequence[str]) -> bool:
        for canonical_id in canonical_ids:
            self.synced.pop(str(canonical_id), None)
        return self.save()

    @_registry_locked
    def synced_ids(self, combo: Optional[str] = None) -> List[str]:
        return sorted(
            cid for cid, row in self.synced.items()
            if combo is None or row.get("combo") == combo
        )

    @_registry_locked
    def pending_prune_ids(self, combo: Optional[str] = None) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for row in self.prune_candidates:
            cid = str(row.get("canonical_id"))
            ledger = self.synced.get(cid)
            if ledger is None:
                continue
            if combo is not None and ledger.get("combo") != combo:
                continue
            out.append(dict(row))
        return out

    @_registry_mutation(ledger=True, prune=True)
    def confirm_pruned(self, canonical_ids: Sequence[str]) -> bool:
        wanted = {str(c) for c in canonical_ids}
        self.prune_candidates = [
            row for row in self.prune_candidates
            if str(row.get("canonical_id")) not in wanted
        ]
        for canonical_id in wanted:
            self.synced.pop(canonical_id, None)
        return self.save()

    @_registry_mutation(pending=True)
    def stage_tail_sync_intent(self, intent: Mapping[str, Any]) -> bool:
        """Durably record a two-store SAIFREN update before touching 9Router."""
        if self.pending_tail_sync is not None:
            self.last_save_error = "RECOVERY_REQUIRED: a prior SAIFREN sync is pending"
            return False
        required_lists = ("old_models", "new_models", "appended", "removed")
        if any(
            not isinstance(intent.get(name), list)
            or any(not isinstance(item, str) for item in intent.get(name, []))
            for name in required_lists
        ):
            self.last_save_error = "invalid SAIFREN sync intent"
            return False
        combo_name = str(intent.get("combo_name") or "").strip()
        provider_ids = intent.get("provider_ids", {})
        if not combo_name or not isinstance(provider_ids, dict):
            self.last_save_error = "invalid SAIFREN sync intent"
            return False
        self.pending_tail_sync = {
            "schema_version": 1,
            "combo_name": combo_name,
            "old_models": list(intent["old_models"]),
            "new_models": list(intent["new_models"]),
            "appended": list(intent["appended"]),
            "removed": list(intent["removed"]),
            "provider_ids": {
                str(cid): str(pid) for cid, pid in provider_ids.items()
                if isinstance(cid, str) and isinstance(pid, str)
            },
            "staged_at": _utc_now_iso(self._now()),
        }
        return self.save()

    def _apply_tail_sync_intent(self, intent: Mapping[str, Any]) -> None:
        """Apply the ledger delta in memory; caller owns lock and save."""
        owners: Dict[str, str] = {}
        for provider_id, record in self.providers.items():
            for row in record.models:
                canonical_id = str(row.get("canonical_id") or "")
                if canonical_id:
                    owners.setdefault(canonical_id, provider_id)
        provider_ids = intent.get("provider_ids") or {}
        now_text = _utc_now_iso(self._now())
        combo_name = str(intent["combo_name"])
        for canonical_id in intent.get("appended", []):
            owner = str(provider_ids.get(canonical_id) or owners.get(canonical_id) or "")
            self.synced[str(canonical_id)] = {
                "provider_id": owner,
                "combo": combo_name,
                "at": now_text,
            }
            record = self.providers.get(owner)
            if record is not None:
                for row in record.models:
                    if row.get("canonical_id") == canonical_id:
                        row["synced_to_saifren"] = True
                        row["synced_at"] = now_text
        removed = {str(cid) for cid in intent.get("removed", [])}
        for canonical_id in removed:
            self.synced.pop(canonical_id, None)
        if removed:
            self.prune_candidates = [
                row for row in self.prune_candidates
                if str(row.get("canonical_id") or "") not in removed
            ]

    @_registry_mutation(ledger=True, prune=True, all_providers=True)
    def commit_tail_sync_delta(self, intent: Mapping[str, Any]) -> bool:
        """Persist one already-verified SAIFREN ownership delta atomically."""
        if self.pending_tail_sync is not None:
            self.last_save_error = "RECOVERY_REQUIRED: reconcile the staged SAIFREN sync first"
            return False
        if not isinstance(intent.get("combo_name"), str) or not intent.get("combo_name"):
            self.last_save_error = "invalid SAIFREN sync delta"
            return False
        self._apply_tail_sync_intent(intent)
        return self.save()

    @_registry_mutation(pending=True, ledger=True, prune=True, all_providers=True)
    def finalize_tail_sync_intent(self, actual_models: Sequence[str]) -> bool:
        """Commit ownership only after the verified backend combo matches intent."""
        intent = self.pending_tail_sync
        if intent is None:
            self.last_save_error = "no staged SAIFREN sync intent"
            return False
        actual = [str(item) for item in actual_models]
        if actual != intent.get("new_models"):
            self.last_save_error = "RECOVERY_REQUIRED: verified combo differs from staged intent"
            return False
        self._apply_tail_sync_intent(intent)
        self.pending_tail_sync = None
        return self.save()

    @_registry_mutation(pending=True, ledger=True, prune=True, all_providers=True)
    def reconcile_pending_tail_sync(self, actual_models: Sequence[str]):
        """Recover staged combo/ledger split before another ownership mutation."""
        intent = self.pending_tail_sync
        if intent is None:
            return "CLEAN"
        actual = [str(item) for item in actual_models]
        if actual == intent.get("new_models"):
            self._apply_tail_sync_intent(intent)
            self.pending_tail_sync = None
            if not self.save():
                return False
            return "COMPLETED"
        if actual == intent.get("old_models"):
            self.pending_tail_sync = None
            if not self.save():
                return False
            return "ROLLED_BACK"
        self.last_save_error = (
            "RECOVERY_REQUIRED: SAIFREN matches neither the staged old nor new combo"
        )
        return "RECOVERY_REQUIRED"

    # ------------------------------------------------------------- scheduling
    def is_due(self, provider_id: str) -> bool:
        """SCHEDULED-due test: automatic/background scheduling only.

        MANUAL is a scheduling policy, not a refusal policy: a MANUAL provider
        is never *automatically* due, but an explicit operator action is not
        scheduling and must not consult this method (see
        ``operator_scan_requested``). NEVER refuses every action outright.
        """
        record = self.get(provider_id)
        if record is None or not record.enabled:
            return False
        policy = _coerce_policy(record.scan_policy)
        if policy in (ScanPolicy.NEVER, ScanPolicy.MANUAL):
            return False
        now = self._now()
        if record.backoff_until_epoch and record.backoff_until_epoch > now:
            return False
        if policy == ScanPolicy.ALWAYS:
            return True
        if not record.last_metadata_success_epoch:
            return True
        return (now - record.last_metadata_success_epoch) >= METADATA_TTL_SECONDS

    def operator_scan_requested(self, provider_id: str) -> bool:
        """Explicit operator action test (never automatic scheduling).

        True whenever the operator may run THIS provider right now by explicit
        request, cost/trust/canary guards aside: enabled and not NEVER, with
        backoff still honoured (a server Retry-After is a real-world fact, not
        a scheduling choice).
        """
        record = self.get(provider_id)
        if record is None or not record.enabled:
            return False
        policy = _coerce_policy(record.scan_policy)
        if policy == ScanPolicy.NEVER:
            return False
        now = self._now()
        if record.backoff_until_epoch and record.backoff_until_epoch > now:
            return False
        return True

    def live_probe_needed(self, provider_id: str) -> bool:
        """False while trusted-alive is valid or a recent live success exists."""
        record = self.get(provider_id)
        if record is None:
            return False
        if record.trust_valid(self._now()):
            return False
        now = self._now()
        if record.last_live_probe_success_epoch and (
            now - record.last_live_probe_success_epoch
        ) < LIVE_TTL_SECONDS:
            return False
        return True

    # ------------------------------------------------------------------ reads
    def provider_rows(self) -> List[Dict[str, Any]]:
        now = self._now()
        rows: List[Dict[str, Any]] = []
        for record in self.providers.values():
            trust_valid = record.trust_valid(now)
            # Counts are recomputed from the inventory rows: the stored numbers
            # are a persistence echo, never an independent truth.
            evidences = record.evidence_models
            strict_free = sum(1 for e in evidences if e == FreeEvidence.STRICT_FREE)
            conditional = sum(
                1 for e in evidences
                if e in (FreeEvidence.CONDITIONAL_FREE, FreeEvidence.UNKNOWN_COST)
            )
            metadata_stale = (
                not record.last_metadata_success_epoch
                or (now - record.last_metadata_success_epoch) >= METADATA_TTL_SECONDS
            )
            rows.append({
                "provider_id": record.provider_id,
                "display_name": record.display_name or record.provider_id,
                "adapter": record.adapter,
                "enabled": bool(record.enabled),
                "scan_policy": record.scan_policy,
                "scan_mode": record.scan_mode,
                "metadata_cost_risk": record.metadata_cost_risk,
                "live_probe_cost_risk": record.live_probe_cost_risk,
                "allow_billing_probe": bool(record.allow_billing_probe),
                "trusted_alive": trust_valid,
                "trusted_text": record.trust_text(now),
                "last_scan_at": record.last_scan_at,
                "last_success_at": record.last_metadata_success_at,
                "last_metadata_success_at": record.last_metadata_success_at,
                "last_live_probe_success_at": record.last_live_probe_success_at,
                "last_error_class": record.last_error_class,
                "last_error_summary": record.last_error_summary,
                "last_status": record.last_status,
                "models_discovered": int(len(record.models)),
                "strict_free_count": int(strict_free),
                "conditional_count": int(conditional),
                "next_scan_due": record.next_scan_due,
                "operator_note": record.operator_note,
                "metadata_stale": bool(metadata_stale),
                "has_strict_free": bool(strict_free),
                "cost_safe": bool(record.cost_safe_for_live()),
                "live_supported": bool(record.adapter_live_supported),
                "metadata_supported": bool(record.adapter_metadata_supported),
                "client_bound": bool(record.adapter_client_bound),
                "adapter_transport": record.adapter_transport,
                "adapter_metadata_source": record.adapter_metadata_source,
                "badges": provider_badges(record, now),
            })
        return rows

    def model_rows(
        self,
        provider_id: str,
        *,
        bridge_eligible: Optional[Sequence[str]] = None,
    ) -> List[Dict[str, Any]]:
        """Per-model drilldown rows with eligibility computed from live state.

        ``bridge_eligible`` names the canonical ids the existing ocf bridge
        reports usable: a CLIENT_BOUND_FREE model reaches SAIFREN only through
        that contract, never as a generic routable FREE route.
        """
        record = self.get(provider_id)
        if record is None:
            return []
        bridge = {str(cid) for cid in (bridge_eligible or ())}
        rows = []
        for row in record.models:
            item = dict(row)
            try:
                evidence = FreeEvidence(item.get("free_evidence"))
            except (TypeError, ValueError):
                evidence = FreeEvidence.UNKNOWN_COST
            try:
                health = ProviderHealth(item.get("provider_health") or ProviderHealth.UNKNOWN.value)
            except (TypeError, ValueError):
                health = ProviderHealth.UNKNOWN
            try:
                routing = RoutingCapability(item.get("routing") or RoutingCapability.UNKNOWN.value)
            except (TypeError, ValueError):
                routing = RoutingCapability.UNKNOWN
            cost = cost_risk_coerce(item.get("cost_risk"), record.live_probe_cost)
            eligible, reason = tail_eligibility(
                evidence,
                provider_health=health,
                routing=routing,
                cost_risk=cost,
                client_bound_bridge_eligible=str(item.get("canonical_id")) in bridge,
            )
            item["saifren_eligible"] = bool(eligible)
            item["exclusion_reason"] = reason or item.get("exclusion_reason", "")
            rows.append(item)
        return rows

    def eligible_tail_ids(
        self,
        provider_ids: Optional[Sequence[str]] = None,
        *,
        bridge_eligible: Optional[Sequence[str]] = None,
    ) -> List[str]:
        """Strict-FREE (plus bridge-eligible client-bound) tail candidates.

        Ordered by provider then canonical id so the append order is stable and
        the operation is idempotent.
        """
        wanted = {str(p) for p in provider_ids} if provider_ids is not None else None
        out: List[str] = []
        for pid in sorted(self.providers):
            if wanted is not None and pid not in wanted:
                continue
            rows = sorted(
                self.model_rows(pid, bridge_eligible=bridge_eligible),
                key=lambda row: str(row.get("canonical_id") or ""),
            )
            for row in rows:
                if row.get("saifren_eligible"):
                    out.append(str(row.get("canonical_id")))
        return out

    def snapshot(self) -> Dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "load_state": self.load_state,
            "load_error": self.load_error,
            "last_save_error": self.last_save_error,
            "storage_available": self.storage_available,
            "providers": {pid: asdict(r) for pid, r in self.providers.items()},
            "synced": {k: dict(v) for k, v in self.synced.items()},
            "prune_candidates": [dict(r) for r in self.prune_candidates],
        }
