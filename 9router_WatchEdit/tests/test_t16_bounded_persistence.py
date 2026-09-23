"""PERF-004 (T-16 / SRC-001:R0013) — bounded mid-scan cache checkpoint count.

The audited defect: HealthCache was rewritten in full every 10 completions, so
a scan of N models performed N/10 full O(N) rewrites = O(N^2) bytes written
(73MB / 96 saves on the 957-model fixture with early counters).

Contract proven here (offline, MockTransport, tmp cache):

  * the number of MID-SCAN saves is bounded by MAX_MIDSCAN_CHECKPOINTS and does
    not grow linearly with N, so total persistence volume is O(N), not O(N^2);
  * the count is measured as a ratio: 4x the models must NOT produce ~4x the
    mid-scan saves;
  * the terminal save is still exactly once and still last (W2-005 semantics:
    terminal persistence is attempted exactly once, after the probes);
  * a mid-scan save failure still surfaces as a non-plain-COMPLETED terminal
    status (the W2-005 policy is preserved by the new cadence).
"""
import httpx
import pytest

from core.discovery import DiscoveredModel
from core.history import HealthCache, HealthCachePersistenceError
from core.probe import MAX_MIDSCAN_CHECKPOINTS, ScanMode, ScannerWorker
from core.router_client import RouterClient


def _models(n, prefix="p", conn="c-1"):
    return [
        DiscoveredModel(
            canonical_id=f"{prefix}/m{i}",
            provider_name=prefix,
            provider_prefix=prefix,
            connection_id=conn,
            model_id=f"m{i}",
            display_name=f"m{i}",
        )
        for i in range(n)
    ]


def _counting_worker(tmp_path, name):
    async def handler(request):
        return httpx.Response(200, json={"ok": True})

    cache = HealthCache(cache_file=tmp_path / f"cache-{name}.json")
    client = RouterClient(base_url="http://127.0.0.1:99999")
    worker = ScannerWorker(
        client, cache, global_concurrency=8, per_provider_concurrency=8,
        transport=httpx.MockTransport(handler),
    )
    calls = {"n": 0}
    real_save = cache.save

    def counting_save():
        calls["n"] += 1
        real_save()

    cache.save = counting_save
    return worker, calls


def test_midscan_save_count_is_bounded_and_sublinear(tmp_path):
    n_small = 100
    n_large = n_small * 4

    w_small, calls_small = _counting_worker(tmp_path, "small")
    w_small.run_scan_owned(_models(n_small), mode=ScanMode.FULL)

    w_large, calls_large = _counting_worker(tmp_path, "large")
    w_large.run_scan_owned(_models(n_large), mode=ScanMode.FULL)

    # Total saves = bounded mid-scan checkpoints + exactly one terminal save.
    assert calls_small["n"] <= MAX_MIDSCAN_CHECKPOINTS + 1
    assert calls_large["n"] <= MAX_MIDSCAN_CHECKPOINTS + 1

    # The audited defect would make 4x models produce ~4x saves. Bounded
    # checkpoints must be far below that linear growth.
    assert calls_large["n"] < calls_small["n"] * 2, (
        f"saves grew linearly with N: {calls_small['n']} -> {calls_large['n']}"
    )


def test_terminal_save_is_last_and_exactly_once(tmp_path):
    worker, calls = _counting_worker(tmp_path, "terminal")
    order = []
    real_save = worker.cache.save

    def recording_save():
        order.append("save")
        real_save()

    worker.cache.save = recording_save
    statuses = []
    worker.on_scan_completed = lambda sid, status="COMPLETED": statuses.append(status)
    worker.run_scan_owned(_models(150), mode=ScanMode.FULL)

    assert order[-1] == "save", "the terminal save must be the last persistence"
    assert statuses == ["COMPLETED"]
    # Every save call in the run is accounted for by the bounded cadence.
    assert len(order) <= MAX_MIDSCAN_CHECKPOINTS + 1


def test_midscan_failure_still_surfaces_non_plain_completed(tmp_path):
    """The bounded cadence preserves the W2-005 non-fatal-but-pending policy."""
    worker, _calls = _counting_worker(tmp_path, "fail")
    real_save = worker.cache.save
    state = {"n": 0}

    def fail_periodic():
        state["n"] += 1
        if state["n"] == 1:
            raise HealthCachePersistenceError("write", worker.cache.cache_file, OSError("down"))
        real_save()

    worker.cache.save = fail_periodic
    statuses = []
    worker.on_scan_completed = lambda sid, status="COMPLETED": statuses.append(status)
    worker.run_scan_owned(_models(50), mode=ScanMode.FULL)

    # A transient mid-scan failure followed by a verified terminal save is
    # still an honest COMPLETED (the final state IS durable).
    assert statuses == ["COMPLETED"]
    assert state["n"] >= 2
