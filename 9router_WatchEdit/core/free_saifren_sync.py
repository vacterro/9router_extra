"""
9router_WatchEdit - strict FREE tail sync for SAIFREN (FREE-FALLBACK-001, milestone 8).

One explicit operator action appends strict FREE (and bridge-eligible
client-bound) routes at the VERY BOTTOM of the SAIFREN combo.

Guarantees (each one is mirrored by a deterministic test):

  * existing reliable routes keep their order -- nothing above the FREE tail is
    ever reordered;
  * only SCANNER-OWNED entries recorded in the tail ledger may be removed, and
    only after a SUCCESSFUL AUTHORITATIVE metadata refresh positively dropped
    their FREE evidence or existence (registry.pending_prune_ids);
  * a provider/source outage prunes nothing;
  * manual entries survive reconciliation verbatim;
  * the operation is idempotent: planning the same state twice yields no change;
  * PAID / POSSIBLE_BILLING / UNKNOWN_COST / conditional / auth-failed /
    withdrawn / DEAD routes are never auto-added.

The planner is pure: it mutates nothing and performs no I/O. The caller shows
the diff, applies it through the existing verified combo-apply path, and only
then records the ledger.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence

from core.free_provider_registry import FreeProviderRegistry

#: Upper bound for the "excluded from the tail" diagnostic list.
EXCLUDED_DIAGNOSTIC_LIMIT = 50


@dataclass
class TailSyncPlan:
    combo_name: str = ""
    appended: List[str] = field(default_factory=list)
    moved_to_tail: List[str] = field(default_factory=list)
    removed: List[str] = field(default_factory=list)
    migrated_direct: List[tuple[str, str]] = field(default_factory=list)
    kept_manual: List[str] = field(default_factory=list)
    excluded: List[Dict[str, str]] = field(default_factory=list)
    unchanged: bool = True
    models: List[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return not self.unchanged

    def summary_text(self) -> str:
        lines = [
            f"Append at the bottom: {len(self.appended)}",
            f"Move owned FREE routes to the bottom: {len(self.moved_to_tail)}",
            f"Remove stale scanner-owned: {len(self.removed)}",
            f"Explicit direct-route migrations: {len(self.migrated_direct)}",
            f"Manual entries preserved: {len(self.kept_manual)}",
        ]
        if self.excluded:
            lines.append(f"Excluded (never auto-added): {len(self.excluded)}")
        return "\n".join(lines)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "combo_name": self.combo_name,
            "appended": list(self.appended),
            "moved_to_tail": list(self.moved_to_tail),
            "removed": list(self.removed),
            "migrated_direct": [list(pair) for pair in self.migrated_direct],
            "kept_manual": list(self.kept_manual),
            "excluded": [dict(row) for row in self.excluded],
            "unchanged": bool(self.unchanged),
            "models": list(self.models),
        }


def plan_tail_sync(
    combo_models: Sequence[str],
    registry: FreeProviderRegistry,
    *,
    combo_name: str = "SAIFREN",
    bridge_eligible: Optional[Sequence[str]] = None,
    direct_migration_map: Optional[Mapping[str, str]] = None,
) -> TailSyncPlan:
    """Compute the tail sync. Pure planning only -- nothing is mutated."""
    plan = TailSyncPlan(combo_name=combo_name)
    bridge = {str(cid) for cid in (bridge_eligible or ())}

    with registry._lock:
        wanted: List[str] = []
        for canonical_id in registry.eligible_tail_ids(bridge_eligible=bridge or None):
            if canonical_id not in wanted:
                wanted.append(canonical_id)

        ledger = {
            cid: registry.synced.get(cid, {})
            for cid in registry.synced_ids(combo_name)
        }
        prune_confirmable = {
            str(row.get("canonical_id"))
            for row in registry.pending_prune_ids(combo_name)
        }

        original: List[str] = [str(model) for model in combo_models if str(model)]
        wanted_set = set(wanted)
        migrations = {
            str(source): str(target)
            for source, target in (direct_migration_map or {}).items()
            if isinstance(source, str) and isinstance(target, str)
            and source and target in wanted_set
        }
        base: List[str] = []
        base_seen = set()
        manual_ids = {
            model for model in original
            if model not in ledger and model not in migrations
        }
        owned_present = set()
        owned_order: List[str] = []
        first_owned_position: Dict[str, int] = {}
        removed_seen = set()

        for index, model in enumerate(original):
            target = migrations.get(model)
            if target is not None:
                plan.migrated_direct.append((model, target))
                continue
            in_ledger = model in ledger
            if in_ledger and model not in wanted_set and model in prune_confirmable:
                if model not in removed_seen:
                    plan.removed.append(model)
                    removed_seen.add(model)
                continue
            if in_ledger and model in wanted_set:
                if model not in owned_present:
                    owned_present.add(model)
                    owned_order.append(model)
                    first_owned_position[model] = index
                # Eligible owned routes are emitted once in the final tail.
                continue
            if not in_ledger:
                plan.kept_manual.append(model)
                # A manual route with the same text as a scanner candidate is
                # still operator-owned and stays byte-for-byte in place.
                base.append(model)
                continue
            # Unconfirmed stale ownership is retained fail-closed. Duplicate
            # scanner-owned rows collapse, but manual duplicates do not.
            if model not in base_seen:
                base.append(model)
                base_seen.add(model)

        # Keep the relative order of already-owned routes; append newly eligible
        # ids in registry order. This makes re-planning a clean tail a no-op.
        tail: List[str] = [canonical_id for canonical_id in owned_order
                           if canonical_id not in manual_ids]
        for canonical_id in wanted:
            if canonical_id in manual_ids or canonical_id in owned_present:
                continue
            tail.append(canonical_id)
            plan.appended.append(canonical_id)

        result = base + tail
        result_positions = {model: index for index, model in enumerate(result)}
        for canonical_id in tail:
            if canonical_id in owned_present:
                final_index = result_positions[canonical_id]
                if final_index != first_owned_position[canonical_id]:
                    plan.moved_to_tail.append(canonical_id)

    # Diagnostics only: rows that are deliberately NOT auto-added. Bounded so a
    # large paid catalogue cannot turn the plan into a ledger dump.
    for pid in sorted(registry.providers):
        for row in registry.model_rows(pid, bridge_eligible=bridge or None):
            if len(plan.excluded) >= EXCLUDED_DIAGNOSTIC_LIMIT:
                break
            if row.get("saifren_eligible"):
                continue
            evidence = str(row.get("free_evidence") or "")
            if evidence in ("PAID", "WITHDRAWN", "CONDITIONAL_FREE", "UNKNOWN_COST"):
                plan.excluded.append({
                    "canonical_id": str(row.get("canonical_id") or ""),
                    "evidence": evidence,
                    "reason": str(row.get("exclusion_reason") or ""),
                })

    plan.models = result
    plan.unchanged = result == original
    return plan


def commit_tail_sync(
    registry: FreeProviderRegistry,
    plan: TailSyncPlan,
    *,
    provider_ids: Optional[Mapping[str, str]] = None,
) -> bool:
    """Record the ledger AFTER the combo mutation was verified.

    ``provider_ids`` maps canonical_id -> provider_id for appended entries (the
    registry already knows the owning provider for scanner-owned rows; this is
    only needed for ids not present in any inventory).
    """
    if registry.pending_tail_sync is not None:
        return registry.finalize_tail_sync_intent(plan.models)

    owners: Dict[str, str] = {}
    with registry._lock:
        for provider_id, record in registry.providers.items():
            for row in record.models:
                canonical_id = str(row.get("canonical_id") or "")
                if canonical_id:
                    owners.setdefault(canonical_id, provider_id)
    supplied = dict(provider_ids or {})
    for canonical_id in plan.appended:
        owners.setdefault(canonical_id, str(supplied.get(canonical_id) or ""))
    return registry.commit_tail_sync_delta({
        "combo_name": plan.combo_name,
        "appended": list(plan.appended),
        "removed": list(plan.removed),
        "provider_ids": owners,
    })
