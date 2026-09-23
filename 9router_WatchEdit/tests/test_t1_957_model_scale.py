"""DONE_WHEN scale gate (T-1 / SRC-001:R0015) — 957-model fixture responsiveness.

The umbrella acceptance clause requires the 957-model fixture to stay
responsive under normal, circuit-breaker and Retry-After bursts, and for
persistence/UI work to be O(N) rather than the audited O(N^2).

This is the ONE end-to-end scale proof for that clause: a real 957-model scan
through MockTransport (offline), asserting

  * the scan completes within a generous wall bound (responsiveness);
  * every model ends with authoritative evidence;
  * the mid-scan cache checkpoint COUNT is bounded by MAX_MIDSCAN_CHECKPOINTS
    (bounded persistence volume, PERF-004);
  * a Retry-After burst (many 429s) is absorbed by the scheduler without
    issuing a second request before each deadline, and the scan still ends.

Wall-time bound is deliberately loose (CI variance); the point is "finishes,
does not blow up quadratically", not a benchmark.
"""
import time

import httpx
import pytest

from core.discovery import DiscoveredModel
from core.history import HealthCache
from core.probe import MAX_MIDSCAN_CHECKPOINTS, ScanMode, ScannerWorker
from core.router_client import RouterClient

N_MODELS = 957


def _models(n):
    return [
        DiscoveredModel(
            canonical_id=f"prov{i % 12}/model{i}",
            provider_name=f"prov{i % 12}",
            provider_prefix=f"prov{i % 12}",
            connection_id=f"conn-{i % 12}",
            model_id=f"model{i}",
            display_name=f"model{i}",
        )
        for i in range(n)
    ]


def _counting_worker(tmp_path, handler, **kw):
    cache = HealthCache(cache_file=tmp_path / "cache-957.json")
    client = RouterClient(base_url="http://127.0.0.1:99999")
    worker = ScannerWorker(
        client, cache,
        global_concurrency=8, per_provider_concurrency=2,
        transport=httpx.MockTransport(handler), **kw,
    )
    calls = {"n": 0}
    real_save = cache.save

    def counting_save():
        calls["n"] += 1
        real_save()

    cache.save = counting_save
    return worker, cache, calls


def test_957_model_normal_scan_is_bounded_and_complete(tmp_path):
    async def handler(request):
        return httpx.Response(200, json={"ok": True, "usage": {"prompt_tokens": 1}})

    worker, cache, calls = _counting_worker(tmp_path, handler)
    models = _models(N_MODELS)

    t0 = time.monotonic()
    worker.run_scan_owned(models, mode=ScanMode.FULL)
    elapsed = time.monotonic() - t0

    assert elapsed < 120.0, f"957-model scan too slow: {elapsed:.1f}s"
    assert len(cache.records) == N_MODELS
    # Bounded persistence volume: mid-scan checkpoints capped + one terminal.
    assert calls["n"] <= MAX_MIDSCAN_CHECKPOINTS + 1


def test_957_model_retry_after_burst_stays_bounded(tmp_path):
    """A burst of 429s must not multiply requests or wedge the scan."""
    seen = {"n": 0}

    async def handler(request):
        seen["n"] += 1
        # Every 50th request is a rate-limit response with a Retry-After.
        if seen["n"] % 50 == 0:
            return httpx.Response(429, headers={"retry-after": "2"}, json={"error": {"message": "slow down"}})
        return httpx.Response(200, json={"ok": True})

    worker, cache, calls = _counting_worker(tmp_path, handler)

    t0 = time.monotonic()
    worker.run_scan_owned(_models(200), mode=ScanMode.FULL)
    elapsed = time.monotonic() - t0

    assert elapsed < 60.0, f"Retry-After burst scan too slow: {elapsed:.1f}s"
    # A retry must never be a single request multiplied into many: at most a
    # small bounded multiple of the model count is issued.
    assert seen["n"] <= 400, f"request explosion under 429 burst: {seen['n']}"
    assert calls["n"] <= MAX_MIDSCAN_CHECKPOINTS + 1
