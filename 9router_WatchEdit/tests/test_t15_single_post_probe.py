"""PERF-003 (T-15 / SRC-001:R0012) — one POST task observed at two thresholds.

The audited defect: the two-stage probe was cancel-and-resubmit. Stage 1 issued
a POST with the fast budget, cancelled it on timeout, then stage 2 issued a
SECOND POST for the slow budget, so a slow model cost 2 authenticated requests.

Contract proven here (deterministic, offline, single virtual model):

  * a slow FULL/combo model issues exactly ONE POST; the fast threshold emits
    PENDING while the SAME request stays in flight, and the terminal result
    arrives from that one request;
  * a slow QUICK non-combo model is cut at the fast cutoff with exactly ONE
    POST (the single task is cancelled, never resubmitted);
  * no second transport firing occurs in either path.
"""
import asyncio
import threading
import time

import httpx
import pytest

from core.discovery import DiscoveredModel
from core.history import HealthCache
from core.probe import ScanMode, ScannerWorker
from core.router_client import RouterClient

import core.probe as probe_module


def _model(canonical_id="p/m0", connection_id="c-1", is_combo_member=False):
    provider, _, model_id = canonical_id.partition("/")
    return DiscoveredModel(
        canonical_id=canonical_id,
        provider_name=provider,
        provider_prefix=provider,
        connection_id=connection_id,
        model_id=model_id,
        display_name=model_id,
        is_combo_member=is_combo_member,
    )


class _SlowRecorder:
    """Counts POSTs; each request parks until released (controllable in-flight)."""

    def __init__(self):
        self.posts = 0
        self.release = asyncio.Event()
        self.first_seen = threading.Event()

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.posts += 1
        self.first_seen.set()
        await self.release.wait()
        return httpx.Response(200, json={"ok": True})


def _worker(tmp_path, transport):
    client = RouterClient(base_url="http://127.0.0.1:99999")
    cache = HealthCache(cache_file=tmp_path / "cache.json")
    return ScannerWorker(
        client, cache, global_concurrency=1, per_provider_concurrency=1,
        transport=httpx.MockTransport(transport),
    )


def test_slow_full_model_issues_exactly_one_post(tmp_path, monkeypatch):
    """FULL is extended: fast threshold emits PENDING on the SAME in-flight POST."""
    monkeypatch.setattr(probe_module, "DEFAULT_FAST_TIMEOUT_SEC", 0.05)
    monkeypatch.setattr(probe_module, "DEFAULT_SLOW_TIMEOUT_SEC", 5.0)

    rec = _SlowRecorder()
    worker = _worker(tmp_path, rec)
    pending = []
    worker.on_probe_pending = lambda sid, cid, sec: pending.append(cid)

    model = _model()

    def _release_later():
        # Wait until the fast threshold has crossed (>0.05s) then complete the
        # single request; release from another thread via the event loop.
        while not rec.first_seen.is_set():
            time.sleep(0.005)
        time.sleep(0.2)
        worker._execution.owned_loop().call_soon_threadsafe(rec.release.set)

    t = threading.Thread(target=_release_later, daemon=True)
    t.start()
    worker.run_scan([model], mode=ScanMode.FULL)
    t.join(timeout=5.0)

    assert rec.posts == 1, f"FULL slow model must issue exactly 1 POST, got {rec.posts}"
    assert pending and pending[0] == "p/m0", "PENDING must be emitted at the fast threshold"
    got = worker.cache.get("p/m0")
    assert got is not None and got.availability == "LIVE"


def test_slow_quick_noncombo_model_issues_exactly_one_post(tmp_path, monkeypatch):
    """QUICK non-combo: cut at the fast cutoff, ONE POST, no second firing."""
    monkeypatch.setattr(probe_module, "DEFAULT_FAST_TIMEOUT_SEC", 0.05)
    monkeypatch.setattr(probe_module, "DEFAULT_SLOW_TIMEOUT_SEC", 5.0)

    rec = _SlowRecorder()
    worker = _worker(tmp_path, rec)

    model = _model(is_combo_member=False)
    worker.run_scan([model], mode=ScanMode.QUICK)

    assert rec.posts == 1, f"QUICK slow non-combo model must issue exactly 1 POST, got {rec.posts}"
    got = worker.cache.get("p/m0")
    # A cut at the fast cutoff is a timeout record, never a second request.
    assert got is not None
    assert got.availability in ("CONNECT_TIMEOUT", "TEST_TIMEOUT", "TEMP_ERROR", "UNKNOWN")


class _ScriptedSecurity:
    """Deterministic lock-state double (no DPAPI, no env influence)."""

    def __init__(self, state="UNLOCKED"):
        self._state = state
        self._listeners = []

    @property
    def state(self):
        return self._state

    def is_live_allowed(self):
        return self._state in ("OS_VAULT", "UNLOCKED")

    def is_locked(self):
        return not self.is_live_allowed()

    def require_live(self, operation):
        if not self.is_live_allowed():
            from core.security import LiveAccessLockedError
            raise LiveAccessLockedError(operation)

    def on_state_changed(self, cb):
        self._listeners.append(cb)

    def _set(self, state):
        self._state = state
        for cb in list(self._listeners):
            cb(state)

    def lock(self):
        self._set("LOCKED")

    def unlock(self):
        self._set("UNLOCKED")


def test_no_second_post_under_lock_mid_probe(tmp_path, monkeypatch):
    """A lock at the fast threshold stops the single in-flight POST; no resubmit."""
    monkeypatch.setattr(probe_module, "DEFAULT_FAST_TIMEOUT_SEC", 0.05)
    monkeypatch.setattr(probe_module, "DEFAULT_SLOW_TIMEOUT_SEC", 0.5)

    rec = _SlowRecorder()
    worker = _worker(tmp_path, rec)
    worker.client.security = _ScriptedSecurity()
    worker.client.security.on_state_changed(worker._on_security_state_changed)

    model = _model()

    def _lock_later():
        while not rec.first_seen.is_set():
            time.sleep(0.005)
        time.sleep(0.15)
        worker.client.security.lock()

    t = threading.Thread(target=_lock_later, daemon=True)
    t.start()
    worker.run_scan([model], mode=ScanMode.FULL)
    t.join(timeout=5.0)

    assert rec.posts == 1, f"lock mid-probe must not resubmit: {rec.posts} POSTs"
