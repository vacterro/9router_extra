"""
PERF-005 regression - bounded stale-record retention.

Defect (audit/3.md PERF-005): HealthCache retained every record forever; save
serialized all of them and _initial_fast_load materialized them all, so
catalog churn grew memory/startup/save cost without bound.

Contract under test: prune_stale_records runs only after an authoritative
reconciliation and removes only records proven absent, unreferenced, without a
manual override, and older than the recent window; it never prunes on empty
input (failed/LOCKED discovery) and preserves current/combo/override evidence.
"""
from datetime import datetime, timedelta

from core.classification import (
    AvailabilityState,
    Confidence,
    CostState,
    EvidenceRecord,
)
from core.history import HealthCache


def _seed(cache, cid, age_days=0, override=None):
    ev = EvidenceRecord(
        availability=AvailabilityState.LIVE,
        cost=CostState.FREE,
        confidence=Confidence.LIVE,
    )
    rec = cache.record_evidence(cid, "p", cid.split("/")[-1], ev, auto_save=False)
    stamp = (datetime.now() - timedelta(days=age_days)).isoformat()
    rec.last_tested_at = stamp
    rec.cost_override = override
    return rec


def test_old_stale_pruned_current_combo_override_kept(tmp_path):
    cache = HealthCache(cache_file=tmp_path / "c.json")
    _seed(cache, "cur/1", age_days=30)          # current inventory -> kept
    _seed(cache, "combo/1", age_days=30)        # combo-referenced -> kept
    _seed(cache, "over/1", age_days=30, override="FREE")  # override -> kept
    _seed(cache, "recent/1", age_days=1)        # inside window -> kept
    _seed(cache, "old/1", age_days=30)          # eligible -> pruned

    pruned = cache.prune_stale_records(
        current_ids={"cur/1"},
        referenced_ids={"combo/1"},
        recent_window_seconds=7 * 86400,
    )
    assert pruned == 1
    assert set(cache.records.keys()) == {"cur/1", "combo/1", "over/1", "recent/1"}


def test_empty_current_ids_prunes_nothing(tmp_path):
    """Failed/LOCKED/absent discovery is never evidence of removal."""
    cache = HealthCache(cache_file=tmp_path / "c.json")
    _seed(cache, "old/1", age_days=90)
    assert cache.prune_stale_records(current_ids=set()) == 0
    assert "old/1" in cache.records


def test_churn_converges_to_bound(tmp_path):
    cache = HealthCache(cache_file=tmp_path / "c.json")
    for i in range(500):
        _seed(cache, f"gen{i}/m", age_days=30)

    # A fresh authoritative inventory with a single current model.
    cache.prune_stale_records(current_ids={"gen0/m"}, recent_window_seconds=7 * 86400)
    assert "gen0/m" in cache.records
    # Every other record is old-stale and absent -> pruned; count is bounded.
    assert len(cache.records) == 1
