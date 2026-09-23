"""
9router_WatchEdit - OCF registry: scanner-managed `ocf` inventory + SAIFREN tail sync
(OCF-001, milestones 4 and 5)

Ownership rules (never violated):
  * Only SCANNER-MANAGED `ocf` entries are created/removed by reconciliation.
    Any entry an operator added by hand is preserved verbatim, forever.
  * A successful catalog refresh that loses free evidence removes exactly the
    scanner-managed entry it owns. A catalog/source OUTAGE changes NOTHING:
    the last known good inventory survives.
  * A model becomes routing-eligible `ocf/<id>` only when free evidence is
    CURRENT, the model is not stale/withdrawn, the bridge provider canary is
    healthy, and no per-model negative bridge state blocks it.

Ordering rule ("BETTER THAN ZERO"): every eligible `ocf/*` model is appended at
the VERY BOTTOM of the SAIFREN free/fallback chain, after reliable providers.
Nothing above it is ever reordered by this module; the sync is idempotent.

Live catalog calls are deliberately absent here: eligibility is computed from
already-collected evidence (catalog snapshot + bridge health) so a catalog
refresh never live-calls every free model and never burns the scarce resource
this feature exists to rescue.
"""
from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from config import (
    LOCALAPPDATA_DIR,
    OCF_DIRECT_PREFIX,
    OCF_DIRECT_PREFIX_ALIASES,
    OCF_PROVIDER_PREFIX,
    PRIVATE_STORAGE_AVAILABLE,
    _UNAVAILABLE_ROOT,
)
from core.opencode_bridge import BridgeHealth

SCHEMA_VERSION = 1
REGISTRY_FILE_NAME = "ocf_registry.json"

# Catalog source states (core.discovery._opencode_source_state vocabulary).
SOURCE_AVAILABLE = "AVAILABLE"
SOURCE_EMPTY = "EMPTY"
SOURCE_UNAVAILABLE = "UNAVAILABLE"
SOURCE_INVALID = "INVALID"
SOURCE_NEVER_REFRESHED = "NEVER_REFRESHED"

# Direct-route states for the `oc/*` catalog identity.
DIRECT_ROUTABLE = "ROUTABLE"
DIRECT_LOCAL_BRIDGE_REQUIRED = "LOCAL BRIDGE REQUIRED"
DIRECT_UNKNOWN = "UNKNOWN"

# Bridge provider states that make the whole lane unusable.
_BRIDGE_BLOCKING_STATES = frozenset({
    "BRIDGE_UPSTREAM_REJECTED", "BRIDGE_RUNTIME_MISSING",
})
_CLIENT_BOUND_EVIDENCE = frozenset({"CLIENT_BOUND_FREE_TIER"})


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class OcfEntry:
    model_id: str                       # bare upstream free model id
    canonical_id: str                   # ocf/<model_id>
    source_canonical_id: str            # opencode/<model_id>
    free_reason: str = ""
    scanner_managed: bool = True
    first_seen: str = ""
    last_seen: str = ""
    withdrawn: bool = False
    withdrawn_at: str = ""
    last_bridge_ok_at: str = ""
    bridge_state: str = ""

    @property
    def routing_eligible(self) -> bool:
        return not self.withdrawn


@dataclass
class ReconcileResult:
    added: List[str] = field(default_factory=list)
    removed: List[str] = field(default_factory=list)
    preserved_manual: List[str] = field(default_factory=list)
    skipped_reason: str = ""

    @property
    def changed(self) -> bool:
        return bool(self.added or self.removed)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "added": list(self.added),
            "removed": list(self.removed),
            "preserved_manual": list(self.preserved_manual),
            "skipped_reason": self.skipped_reason,
            "changed": self.changed,
        }


@dataclass
class SyncReport:
    appended: List[str] = field(default_factory=list)
    removed_stale: List[str] = field(default_factory=list)
    migrated_direct: List[Tuple[str, str]] = field(default_factory=list)
    unchanged: bool = True

    def as_dict(self) -> Dict[str, Any]:
        return {
            "appended": list(self.appended),
            "removed_stale": list(self.removed_stale),
            "migrated_direct": [list(pair) for pair in self.migrated_direct],
            "unchanged": self.unchanged,
        }


def canonical_for(model_id: str) -> str:
    return f"{OCF_PROVIDER_PREFIX}/{model_id}"


def upstream_canonical_for(model_id: str) -> str:
    """Direct (catalog/provider) canonical id of the same free model: `oc/<id>`."""
    return f"{OCF_DIRECT_PREFIX}/{model_id}"


def direct_canonical_candidates(model_id: str) -> Tuple[str, ...]:
    """Every direct prefix alias a live install may have used for this model."""
    return tuple(f"{prefix}/{model_id}" for prefix in OCF_DIRECT_PREFIX_ALIASES)


def is_ocf_id(model_id: str) -> bool:
    return str(model_id or "").startswith(f"{OCF_PROVIDER_PREFIX}/")


def ocf_model_id(canonical_id: str) -> str:
    return str(canonical_id or "")[len(OCF_PROVIDER_PREFIX) + 1:]


class OcfRegistry:
    """Persistent, scanner-owned `ocf` inventory with fail-closed reconciliation."""

    def __init__(self, path: Optional[Path] = None):
        if path is not None:
            self.path = path
        elif PRIVATE_STORAGE_AVAILABLE:
            self.path = LOCALAPPDATA_DIR / "runtime" / "opencode_bridge" / REGISTRY_FILE_NAME
        else:
            self.path = _UNAVAILABLE_ROOT / REGISTRY_FILE_NAME
        self._lock = threading.Lock()
        self.entries: Dict[str, OcfEntry] = {}
        self.last_reconcile: Dict[str, Any] = {}
        self.last_sync: Dict[str, Any] = {}
        self.load_error: str = ""
        self._load()

    # ------------------------------------------------------------- persistence
    def _load(self) -> None:
        self.load_error = ""
        try:
            if not self.path.exists():
                return
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as ex:
            self.load_error = f"{type(ex).__name__}"
            return
        if not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION:
            self.load_error = "schema_mismatch"
            return
        entries = data.get("entries")
        if not isinstance(entries, dict):
            self.load_error = "entries_missing"
            return
        for key, raw in entries.items():
            if not isinstance(raw, dict) or not isinstance(raw.get("model_id"), str):
                continue
            known = set(OcfEntry.__dataclass_fields__)
            self.entries[str(key)] = OcfEntry(**{k: v for k, v in raw.items() if k in known})
        self.last_reconcile = data.get("last_reconcile") or {}
        self.last_sync = data.get("last_sync") or {}

    def save(self) -> bool:
        doc = {
            "schema_version": SCHEMA_VERSION,
            "entries": {k: asdict(v) for k, v in self.entries.items()},
            "last_reconcile": self.last_reconcile,
            "last_sync": self.last_sync,
        }
        tmp = self.path.with_suffix(".tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(doc, indent=2, sort_keys=True), encoding="utf-8")
            tmp.replace(self.path)
            return True
        except OSError:
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                pass
            return False

    # ---------------------------------------------------------------- ownership
    def add_manual(self, model_id: str, *, free_reason: str = "operator_added") -> OcfEntry:
        """Operator-owned entry: created once, never removed by reconciliation."""
        now = _utc_now_iso()
        with self._lock:
            entry = self.entries.get(model_id)
            if entry is None:
                entry = OcfEntry(
                    model_id=model_id,
                    canonical_id=canonical_for(model_id),
                    source_canonical_id=upstream_canonical_for(model_id),
                    free_reason=free_reason,
                    scanner_managed=False,
                    first_seen=now,
                    last_seen=now,
                )
                self.entries[model_id] = entry
            else:
                entry.scanner_managed = False
                entry.last_seen = now
                entry.withdrawn = False
        self.save()
        return entry

    def remove_manual(self, model_id: str) -> bool:
        with self._lock:
            entry = self.entries.get(model_id)
            if entry is None or entry.scanner_managed:
                return False
            del self.entries[model_id]
        self.save()
        return True

    # -------------------------------------------------------------- reconcile
    def reconcile(
        self,
        catalog_models: Sequence[Any],
        source_state: str,
        *,
        bridge_healthy: bool = True,
    ) -> ReconcileResult:
        """Reconcile scanner-managed entries against current free evidence.

        `source_state` is the catalog source state from the OpenCode scanner. Any
        non-AVAILABLE state (outage/empty/invalid/never refreshed) is reported as
        a skip: last-known-good entries survive untouched.
        """
        result = ReconcileResult()
        with self._lock:
            all_entries = list(self.entries.values())
            result.preserved_manual = sorted(
                e.canonical_id for e in all_entries if not e.scanner_managed
            )

            if source_state != SOURCE_AVAILABLE:
                result.skipped_reason = f"catalog_source_{source_state.lower() or 'unknown'}"
                self.last_reconcile = {"at": _utc_now_iso(), **result.as_dict()}
                self.save()
                return result

            free_now = {
                str(getattr(m, "model_id", "") or ""): str(getattr(m, "free_reason", "") or "")
                for m in catalog_models
                if getattr(m, "free_candidate", False) and getattr(m, "model_id", "")
            }
            now = _utc_now_iso()

            # 1. Remove scanner-managed entries whose free evidence is gone.
            for model_id, entry in list(self.entries.items()):
                if not entry.scanner_managed or entry.withdrawn:
                    continue
                if model_id in free_now:
                    continue
                del self.entries[model_id]
                result.removed.append(entry.canonical_id)

            # 2. Add scanner-managed entries for newly advertised free models.
            for model_id, reason in sorted(free_now.items()):
                entry = self.entries.get(model_id)
                if entry is None:
                    self.entries[model_id] = OcfEntry(
                        model_id=model_id,
                        canonical_id=canonical_for(model_id),
                        source_canonical_id=upstream_canonical_for(model_id),
                        free_reason=reason,
                        scanner_managed=True,
                        first_seen=now,
                        last_seen=now,
                    )
                    result.added.append(canonical_for(model_id))
                else:
                    entry.last_seen = now
                    entry.free_reason = reason
                    entry.withdrawn = False

            self.last_reconcile = {"at": now, "bridge_healthy": bool(bridge_healthy),
                                  **result.as_dict()}
        self.save()
        return result

    # ------------------------------------------------------------------ reads
    def eligible_ids(self, health: Optional[BridgeHealth] = None) -> List[str]:
        """routing-eligible bare model ids (current free evidence + bridge healthy)."""
        health = health or BridgeHealth()
        if health.provider_blocked():
            return []
        out: List[str] = []
        for entry in sorted(self.entries.values(), key=lambda e: e.model_id):
            if entry.withdrawn:
                continue
            status = health.model_status(entry.model_id)
            entry.bridge_state = str(status.get("state") or entry.bridge_state or "")
            if status.get("last_ok_at"):
                entry.last_bridge_ok_at = str(status.get("last_ok_at"))
            if status.get("blocked"):
                continue
            out.append(entry.model_id)
        return out

    def routing_eligible(self, health: Optional[BridgeHealth] = None) -> List[OcfEntry]:
        ids = set(self.eligible_ids(health))
        return [e for e in sorted(self.entries.values(), key=lambda e: e.model_id)
                if e.model_id in ids]

    def canonical_ids(self, health: Optional[BridgeHealth] = None) -> List[str]:
        return [canonical_for(model_id) for model_id in self.eligible_ids(health)]

    def snapshot(self) -> Dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "entries": {k: asdict(v) for k, v in sorted(self.entries.items())},
            "last_reconcile": self.last_reconcile,
            "last_sync": self.last_sync,
            "load_error": self.load_error,
        }

    # --------------------------------------------------------- direct-route view
    @staticmethod
    def direct_route_state(evidence: Mapping[str, Any], canonical_id: str) -> str:
        """Direct `oc/*` route state from observed probe evidence.

        CLIENT_BOUND_FREE_TIER means: the catalog model itself is valid and free,
        but an arbitrary third-party HTTP client is not permitted to call it.
        It is NOT a dead model and must never be rendered as one.
        """
        model_id = canonical_id.split("/", 1)[1] if "/" in canonical_id else canonical_id
        row: Mapping[str, Any] = {}
        for candidate in (canonical_id, *direct_canonical_candidates(model_id)):
            found = evidence.get(candidate)
            if isinstance(found, Mapping):
                row = found
                break
        state = str(row.get("availability") or row.get("state") or "")
        if state in _CLIENT_BOUND_EVIDENCE:
            return DIRECT_LOCAL_BRIDGE_REQUIRED
        if state == "LIVE":
            return DIRECT_ROUTABLE
        return DIRECT_UNKNOWN

    def inventory_rows(
        self,
        catalog_models: Sequence[Any],
        *,
        evidence: Optional[Mapping[str, Any]] = None,
        health: Optional[BridgeHealth] = None,
    ) -> List[Dict[str, Any]]:
        """Operator-facing rows: free evidence, direct route state, bridge state,
        SAIFREN eligibility, last successful bridge use and cooldown."""
        evidence = evidence or {}
        health = health or BridgeHealth()
        eligible = set(self.eligible_ids(health))
        provider_blocked = health.provider_blocked()
        rows: List[Dict[str, Any]] = []
        for model in catalog_models:
            model_id = str(getattr(model, "model_id", "") or "")
            upstream_id = upstream_canonical_for(model_id)
            ocf_id = canonical_for(model_id)
            status = health.model_status(model_id)
            entry = self.entries.get(model_id)
            direct = self.direct_route_state(evidence, upstream_id)
            bridge_state = str(status.get("state") or (entry.bridge_state if entry else ""))
            rows.append({
                "model_id": model_id,
                "upstream_canonical_id": upstream_id,
                "canonical_id": ocf_id,
                "free_evidence": bool(getattr(model, "free_candidate", False)),
                "free_reason": str(getattr(model, "free_reason", "") or ""),
                "direct_state": direct,
                "bridge_state": bridge_state,
                "bridge_blocked": bool(status.get("blocked")),
                "last_bridge_ok_at": str(status.get("last_ok_at") or ""),
                "cooldown_until": status.get("cooldown_until", 0),
                "saifren_eligible": (
                    model_id in eligible and not provider_blocked
                    and bool(getattr(model, "free_candidate", False))
                ),
                "scanner_managed": bool(entry.scanner_managed) if entry else False,
                "manual": bool(entry and not entry.scanner_managed),
            })
        return rows


# --------------------------------------------------------------- SAIFREN sync
def sync_saifren_bottom(
    combo_models: Sequence[str],
    eligible_ids: Sequence[str],
    *,
    migrate_direct: bool = False,
    direct_migration_map: Optional[Mapping[str, str]] = None,
    scanner_owned_ids: Optional[Sequence[str]] = None,
    confirmed_prune_ids: Optional[Sequence[str]] = None,
) -> Tuple[List[str], SyncReport]:
    """Safe compatibility planner; live UI sync delegates to free_saifren_sync.

    Namespace alone never grants removal authority. Old scanner routes require
    both exact durable ownership and positive prune confirmation; unowned OCF
    rows and duplicates stay byte-for-byte in their original positions.
    """
    report = SyncReport()
    wanted = [canonical_for(model_id) for model_id in eligible_ids]
    wanted_set = set(wanted)
    owned = set(str(value) for value in (scanner_owned_ids or ())) | wanted_set
    prune = set(str(value) for value in (confirmed_prune_ids or ()))
    migrations = direct_migration_map or {}
    base: List[str] = []
    existing_owned: List[str] = []
    seen_owned = set()
    original = [str(model) for model in combo_models if str(model)]
    for raw in original:
        model = str(raw)
        if migrate_direct and model in migrations and migrations[model] in wanted_set:
            replacement = str(migrations[model])
            report.migrated_direct.append((model, replacement))
            continue
        if is_ocf_id(model) and model in owned:
            if model not in wanted_set and model in prune:
                report.removed_stale.append(model)
                continue
            if model in wanted_set:
                if model in seen_owned:
                    continue
                seen_owned.add(model)
                existing_owned.append(model)
                continue
            # Stale ownership without authoritative prune proof remains in place.
            base.append(model)
            continue
        base.append(model)

    tail = list(existing_owned)
    for canonical_id in wanted:
        if canonical_id in seen_owned:
            continue
        seen_owned.add(canonical_id)
        tail.append(canonical_id)
        report.appended.append(canonical_id)

    result = base + tail
    report.unchanged = result == original
    return result, report


def plan_direct_free_migration(
    combo_models: Sequence[str],
    registry: OcfRegistry,
    catalog_models: Sequence[Any],
) -> Dict[str, str]:
    """Map direct client-bound `oc/<id>` entries to verified `ocf/<id>` entries.

    Only free-evidenced models with a scanner-managed, live `ocf` entry are
    mapped. Manual `oc/*` entries are never touched outside this explicit plan,
    and the plan itself only replaces the combo slot (the catalog keeps the
    direct row for diagnostics).
    """
    free_ids = {
        str(getattr(m, "model_id", "") or "")
        for m in catalog_models
        if getattr(m, "free_candidate", False)
    }
    managed = set(registry.eligible_ids())
    mapping: Dict[str, str] = {}
    for raw in combo_models:
        model = str(raw)
        if "/" not in model:
            continue
        prefix, model_id = model.split("/", 1)
        if prefix not in OCF_DIRECT_PREFIX_ALIASES:
            continue
        if model_id in free_ids and model_id in managed:
            mapping[model] = canonical_for(model_id)
    return mapping
