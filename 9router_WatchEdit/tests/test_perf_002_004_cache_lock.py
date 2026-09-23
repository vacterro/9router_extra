"""
PERF-002 + PERF-004 regressions - cache lock contention + concurrent Tools.

PERF-002: HealthCache.save held the shared memory RLock across the whole
serialization + temp write + fsync + replace, so a slow disk/fsync parked every
concurrent cache reader (the UI). The lock must cover ONLY the coherent snapshot
capture; durable I/O runs under a separate writer mutex.

PERF-004: Combo Tools Preview/Apply iterated cache.records directly with no
lock, so a concurrent record_evidence insert raised 'dictionary changed size
during iteration'. A locked snapshot_records() must be used instead.
"""
import threading
import time

from core.history import HealthCache


def _rec(cid):
    from core.classification import (
        AvailabilityState, CostState, Confidence, EvidenceCounters,
    )
    from core.history import ModelHealthRecord
    return ModelHealthRecord(
        canonical_id=cid, provider="p", model_id=cid,
        availability=AvailabilityState.LIVE.value,
        cost=CostState.FREE.value,
        confidence=Confidence.LIVE.value,
    )


def test_save_io_does_not_hold_memory_lock(tmp_path, monkeypatch):
    cache = HealthCache(cache_file=tmp_path / "cache.json")
    for i in range(50):
        cache.records[f"m/{i}"] = _rec(f"m/{i}")

    import core.history as history

    real_write = history._write_temp_file
    release = threading.Event()
    entered = threading.Event()
    observed = {}

    def slow_write(temp_file, payload):
        entered.set()
        # Hold the I/O stage briefly; a reader must NOT be blocked by it.
        release.wait(timeout=2.0)
        return real_write(temp_file, payload)

    monkeypatch.setattr(history, "_write_temp_file", slow_write)

    t = threading.Thread(target=cache.save)
    t.start()
    assert entered.wait(timeout=2.0)

    # While I/O is stalled, a concurrent get() must complete quickly.
    start = time.perf_counter()
    cache.get("m/0")
    elapsed = time.perf_counter() - start
    observed["read"] = elapsed

    release.set()
    t.join(timeout=5)

    assert observed["read"] < 0.2, f"cache read blocked by save I/O for {observed['read']:.3f}s"


def test_generation_stays_dirty_when_records_change_mid_save(tmp_path, monkeypatch):
    cache = HealthCache(cache_file=tmp_path / "cache.json")
    cache.records["m/0"] = _rec("m/0")

    import core.history as history
    real_write = history._write_temp_file
    release = threading.Event()
    entered = threading.Event()

    def blocking_write(temp_file, payload):
        entered.set()
        release.wait(timeout=2.0)
        return real_write(temp_file, payload)

    monkeypatch.setattr(history, "_write_temp_file", blocking_write)
    t = threading.Thread(target=cache.save)
    t.start()
    assert entered.wait(timeout=2.0)

    # Mutate after the snapshot was captured but before the replace completes.
    with cache._lock:
        cache.records["m/1"] = _rec("m/1")
        cache._record_generation += 1

    release.set()
    t.join(timeout=5)

    # The newer generation is NOT marked durable: the next save must persist it.
    assert cache._durable_generation < cache._record_generation


def test_snapshot_records_is_a_coherent_copy(tmp_path):
    cache = HealthCache(cache_file=tmp_path / "cache.json")
    cache.records["a"] = _rec("a")

    snapshot = cache.snapshot_records()
    # Mutating the live mapping must not affect the snapshot.
    cache.records["b"] = _rec("b")
    assert set(snapshot.keys()) == {"a"}


def test_snapshot_records_survives_concurrent_inserts(tmp_path):
    cache = HealthCache(cache_file=tmp_path / "cache.json")
    for i in range(200):
        cache.records[f"seed/{i}"] = _rec(f"seed/{i}")

    stop = threading.Event()
    error = []

    def writer():
        i = 0
        while not stop.is_set():
            cache.records[f"new/{i}"] = _rec(f"new/{i}")
            i += 1

    t = threading.Thread(target=writer)
    t.start()
    try:
        for _ in range(200):
            snap = cache.snapshot_records()
            # Iterating the snapshot is always safe while the live dict grows.
            list(snap.items())
    except RuntimeError as ex:
        error.append(ex)
    finally:
        stop.set()
        t.join(timeout=5)

    assert not error, f"snapshot iteration raised: {error}"
