"""PERF-001 (T-2 / SRC-001:R0005) — Retry-After deadlines and scheduler capacity.

The audited defect had two halves:

  * the server's Retry-After was truncated (`min(max(hdr, 2.0), 30.0)`) and the
    waiting task slept `min(delay, 5.0)` and then probed ANYWAY, so a 30s server
    deadline was violated after 5s;
  * that sleeping task still occupied a scheduler slot, so one cooling-down
    provider throttled every other provider's probes.

Contract proven here on a VIRTUAL clock (no wall-clock waiting, no network):

  * a 429 Retry-After of 30s (and 120s, i.e. beyond the old 30s cap) produces no
    further request for that connection before the server's deadline;
  * a 429 without the header uses the 8s default and a too-small value is raised
    to the 2s minimum — never the other way round;
  * a delayed connection never consumes the global scheduler slot, so other
    providers keep running at full concurrency while it cools down;
  * a scan whose only remaining work is a delayed connection ends promptly
    instead of parking a task asleep until the deadline.
"""
import asyncio
import json
import time

import httpx
import pytest

from core.discovery import DiscoveredModel
from core.history import HealthCache
from core.probe import (
    DEFAULT_RETRY_AFTER_SEC,
    MIN_RETRY_AFTER_SEC,
    RETRY_RESUME_TICK_SEC,
    ScanMode,
    ScannerWorker,
    parse_retry_after,
)
from core.router_client import RouterClient

import core.probe as probe_module


class _VirtualClock:
    """Deterministic clock shared by the probe code and the MockTransport."""

    def __init__(self, start: float = 1_000_000.0):
        self.now = start
        self.sleeps: list[float] = []

    def time(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += max(0.0, seconds)


@pytest.fixture
def clock(monkeypatch):
    """Virtual clock: patch the module's clock and the one sleep seam."""
    virtual = _VirtualClock()
    real_time = time

    class _TimeShim:
        def __getattr__(self, name):
            return getattr(real_time, name)

        def time(self) -> float:
            return virtual.time()

    async def _fake_sleep(seconds: float) -> None:
        virtual.sleeps.append(seconds)
        virtual.advance(seconds)
        await asyncio.sleep(0)  # yield to the event loop without waiting

    monkeypatch.setattr(probe_module, "time", _TimeShim())
    monkeypatch.setattr(probe_module, "_sleep", _fake_sleep)
    return virtual


def _model(canonical_id: str, connection_id: str) -> DiscoveredModel:
    provider, _, model_id = canonical_id.partition("/")
    return DiscoveredModel(
        canonical_id=canonical_id,
        provider_name=provider,
        provider_prefix=provider,
        connection_id=connection_id,
        model_id=model_id,
        display_name=model_id,
    )


class _Recorder:
    """Async MockTransport recording (virtual_time, canonical_id) per request."""

    def __init__(self, clock: _VirtualClock, retry_after=None, rate_limit_first=False, inflight=None):
        self.clock = clock
        self.requests: list[tuple[float, str]] = []
        self.retry_after = retry_after
        self.rate_limit_first = rate_limit_first
        self.inflight = inflight if inflight is not None else {"now": 0, "max": 0}
        self._issued_first_429 = False

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8")) if request.content else {}
        canonical_id = str(body.get("model", ""))

        self.inflight["now"] += 1
        self.inflight["max"] = max(self.inflight["max"], self.inflight["now"])
        try:
            self.requests.append((self.clock.time(), canonical_id))
            await asyncio.sleep(0)
            if self.rate_limit_first and not self._issued_first_429:
                self._issued_first_429 = True
                headers = {}
                if self.retry_after is not None:
                    headers["retry-after"] = str(self.retry_after)
                return httpx.Response(429, headers=headers, json={"error": {"message": "rate limited"}})
            return httpx.Response(200, json={"ok": True, "usage": {"prompt_tokens": 1}})
        finally:
            self.inflight["now"] -= 1


def _run_scan(tmp_path, clock, models, recorder, **worker_kwargs):
    transport = httpx.MockTransport(recorder)
    client = RouterClient(base_url="http://127.0.0.1:99999")
    cache = HealthCache(cache_file=tmp_path / "cache.json")
    worker = ScannerWorker(client, cache, transport=transport, **worker_kwargs)
    worker.run_scan(models, mode=ScanMode.QUICK)
    return worker, cache


# --------------------------------------------------------------------------
# 1. The server deadline is honored in full
# --------------------------------------------------------------------------
def test_no_second_request_before_full_retry_after_deadline(tmp_path, clock):
    models = [_model(f"prov/a{i}", "conn-a") for i in (1, 2, 3)]
    recorder = _Recorder(clock, retry_after=30, rate_limit_first=True)
    worker, cache = _run_scan(tmp_path, clock, models, recorder)

    deadline = recorder.requests[0][0] + 30.0
    first_model = recorder.requests[0][1]
    later = [t for t, cid in recorder.requests if cid != first_model]

    assert later, "the cooling connection must still be probed after its deadline"
    assert min(later) >= deadline, f"request before the Retry-After deadline: {min(later) - recorder.requests[0][0]:.1f}s"
    assert [t for t, _cid in recorder.requests if t < deadline] == [recorder.requests[0][0]]
    assert worker._provider_backoffs["conn-a"] == pytest.approx(recorder.requests[0][0] + 30.0)
    for m in models:
        assert cache.get(m.canonical_id) is not None


def test_retry_after_longer_than_the_old_30s_cap_is_honored_in_full(tmp_path, clock):
    models = [_model(f"prov/b{i}", "conn-b") for i in (1, 2)]
    recorder = _Recorder(clock, retry_after=120, rate_limit_first=True)
    worker, _cache = _run_scan(tmp_path, clock, models, recorder)

    t0 = recorder.requests[0][0]
    assert worker._provider_backoffs["conn-b"] == pytest.approx(t0 + 120.0)
    later = [t for t, cid in recorder.requests if cid != recorder.requests[0][1]]
    assert later and min(later) >= t0 + 120.0, "a 120s server deadline must not be truncated"


def test_retry_after_default_and_minimum_are_exact(tmp_path, clock):
    # no header -> documented 8s default
    models = [_model(f"prov/c{i}", "conn-c") for i in (1, 2)]
    recorder = _Recorder(clock, retry_after=None, rate_limit_first=True)
    worker, _ = _run_scan(tmp_path, clock, models, recorder)
    t0 = recorder.requests[0][0]
    assert worker._provider_backoffs["conn-c"] == pytest.approx(t0 + DEFAULT_RETRY_AFTER_SEC)
    assert min(t for t, cid in recorder.requests if cid != recorder.requests[0][1]) >= t0 + DEFAULT_RETRY_AFTER_SEC

    # a too-small header is raised to the minimum, never used to probe sooner
    models = [_model(f"prov/d{i}", "conn-d") for i in (1, 2)]
    recorder = _Recorder(clock, retry_after=1, rate_limit_first=True)
    worker, _ = _run_scan(tmp_path, clock, models, recorder)
    t0 = recorder.requests[0][0]
    assert worker._provider_backoffs["conn-d"] == pytest.approx(t0 + MIN_RETRY_AFTER_SEC)
    assert min(t for t, cid in recorder.requests if cid != recorder.requests[0][1]) >= t0 + MIN_RETRY_AFTER_SEC


# --------------------------------------------------------------------------
# 2. Backoff waiting consumes NO scheduler capacity
# --------------------------------------------------------------------------
def test_backoff_wait_does_not_consume_global_scheduler_capacity(tmp_path, clock):
    """global_concurrency=1: a cooling provider must not starve other providers.

    Under the old in-task sleep the single global slot was held by the sleeping
    task, so conn-b's probes were pushed behind conn-a's truncated 5s wait.
    """
    inflight = {"now": 0, "max": 0}
    models = [
        _model("prov/a1", "conn-a"), _model("prov/a2", "conn-a"),
        _model("prov/b1", "conn-b"), _model("prov/b2", "conn-b"),
    ]
    recorder = _Recorder(clock, retry_after=30, rate_limit_first=True, inflight=inflight)
    worker, cache = _run_scan(
        tmp_path, clock, models, recorder, global_concurrency=1, per_provider_concurrency=1
    )

    t0 = recorder.requests[0][0]
    healthy = [t for t, cid in recorder.requests if cid.startswith("prov/b")]
    assert len(healthy) >= 2
    assert max(healthy) < t0 + 1.0, "healthy provider wait was throttled by the cooling provider"
    assert inflight["max"] == 1, "global concurrency bound must still hold"
    for m in models:
        assert cache.get(m.canonical_id) is not None
    assert worker._provider_backoffs["conn-a"] == pytest.approx(t0 + 30.0)


def test_probe_task_never_sleeps_while_cooling_down(tmp_path, clock, monkeypatch):
    """PERF-001: backoff waiting belongs to the scheduler, never to a probe task.

    The audited defect was `await asyncio.sleep(min(delay, 5.0))` INSIDE the
    scheduled task: the task existed, so it occupied a scheduler slot (and
    probed anyway after the truncated wait). Any non-zero raw sleep inside a
    probe run is therefore a contract violation, and this test refuses it.
    """
    raw_sleeps: list[float] = []
    real_sleep = asyncio.sleep

    async def _raw_sleep_guard(seconds, *args, **kwargs):
        if seconds and seconds > 0:
            raw_sleeps.append(seconds)
        await real_sleep(0)

    monkeypatch.setattr(probe_module.asyncio, "sleep", _raw_sleep_guard)

    models = [_model(f"prov/g{i}", "conn-g") for i in (1, 2, 3)]
    recorder = _Recorder(clock, retry_after=30, rate_limit_first=True)
    worker, cache = _run_scan(tmp_path, clock, models, recorder)

    assert raw_sleeps == [], f"a probe task slept inside the run: {raw_sleeps}"
    assert worker._provider_backoffs["conn-g"] == pytest.approx(recorder.requests[0][0] + 30.0)
    for m in models:
        assert cache.get(m.canonical_id) is not None


def test_scan_ends_promptly_when_only_a_delayed_connection_remains(tmp_path, clock):
    models = [_model("prov/e1", "conn-e")]
    recorder = _Recorder(clock, retry_after=30, rate_limit_first=True)
    _worker, cache = _run_scan(tmp_path, clock, models, recorder)

    assert len(recorder.requests) == 1, "no work is left for this connection, so nothing is retried"
    assert clock.sleeps == [], "the scan must not park a task asleep until the deadline"
    assert cache.get("prov/e1") is not None


def test_delayed_wait_uses_the_heap_tick_not_a_busy_spin(tmp_path, clock):
    models = [_model(f"prov/f{i}", "conn-f") for i in range(1, 4)]
    recorder = _Recorder(clock, retry_after=30, rate_limit_first=True)
    _worker, _ = _run_scan(tmp_path, clock, models, recorder)

    assert clock.sleeps, "the scheduler must wait on the delay heap"
    assert all(s <= RETRY_RESUME_TICK_SEC for s in clock.sleeps), "tick must stay bounded"
    assert len(clock.sleeps) <= 200, "waiting must not busy-spin"


# --------------------------------------------------------------------------
# 3. Header parsing contract
# --------------------------------------------------------------------------
def test_parse_retry_after_contract():
    assert parse_retry_after("30") == 30.0
    assert parse_retry_after(" 120 ") == 120.0
    assert parse_retry_after("1") == MIN_RETRY_AFTER_SEC
    assert parse_retry_after("0") == MIN_RETRY_AFTER_SEC
    assert parse_retry_after("nan") is None
    assert parse_retry_after("inf") is None
    assert parse_retry_after("nonsense") is None
    assert parse_retry_after(None) is None
    assert parse_retry_after("") is None
    # HTTP-date form is accepted and honored relative to the current time
    future = time.time() + 45
    http_date = time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime(future))
    assert parse_retry_after(http_date) == pytest.approx(45, abs=3)
