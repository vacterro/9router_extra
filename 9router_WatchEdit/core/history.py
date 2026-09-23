"""
9router_WatchEdit - Health Cache & Operational History
Maintains persistent local health cache, latency records, evidence-specific streaks,
scan deltas, and manual cost overrides across sessions with thread-safe persistence.
"""
from dataclasses import dataclass, asdict, field
from datetime import datetime
from enum import Enum
import json
import os
from pathlib import Path
import threading
from typing import Dict, List, Optional, Any, Tuple

from config import HEALTH_CACHE_FILE
from core.classification import (
    AvailabilityState,
    CostState,
    Confidence,
    EvidenceRecord,
    EvidenceCounters,
    get_ui_badge,
)

@dataclass
class ModelHealthRecord:
    canonical_id: str
    provider: str
    model_id: str
    availability: str = AvailabilityState.UNKNOWN.value
    cost: str = CostState.UNKNOWN.value
    confidence: str = Confidence.LIKELY_TEMPORARY.value
    cost_override: Optional[str] = None
    latency_ms: float = 0.0
    status_code: int = 0
    last_error: str = ""
    reason: str = ""
    last_tested_at: str = ""
    last_success_at: Optional[str] = None
    success_streak: int = 0
    counters: EvidenceCounters = field(default_factory=EvidenceCounters)
    previous_availability: Optional[str] = None
    delta: str = ""
    note: str = ""

    def __init__(
        self,
        canonical_id: str,
        provider: str,
        model_id: str,
        availability: Optional[str] = None,
        cost: Optional[str] = None,
        confidence: str = Confidence.LIKELY_TEMPORARY.value,
        cost_override: Optional[str] = None,
        latency_ms: float = 0.0,
        status_code: int = 0,
        last_error: str = "",
        reason: str = "",
        last_tested_at: str = "",
        last_success_at: Optional[str] = None,
        success_streak: int = 0,
        counters: Optional[EvidenceCounters] = None,
        previous_availability: Optional[str] = None,
        delta: str = "",
        note: str = "",
        **kwargs,
    ):
        self.canonical_id = canonical_id
        self.provider = provider
        self.model_id = model_id
        if availability is None:
            legacy_state = kwargs.get("state", AvailabilityState.UNKNOWN.value)
            if legacy_state in ("FREE/USE", "PAID", "USE/?"):
                availability = AvailabilityState.LIVE.value
            else:
                availability = legacy_state
        self.availability = availability

        if cost is None:
            legacy_cost = kwargs.get("cost_status")
            if legacy_cost:
                cost = legacy_cost
            elif kwargs.get("state") == "FREE/USE":
                cost = CostState.FREE.value
            elif kwargs.get("state") == "PAID":
                cost = CostState.PAID.value
            else:
                cost = CostState.UNKNOWN.value
        self.cost = cost

        self.confidence = confidence
        self.cost_override = cost_override
        self.latency_ms = latency_ms
        self.status_code = status_code
        self.last_error = last_error
        self.reason = reason
        self.last_tested_at = last_tested_at
        self.last_success_at = last_success_at
        self.success_streak = success_streak
        self.counters = counters if counters is not None else EvidenceCounters()
        self.previous_availability = previous_availability
        self.delta = delta
        self.note = note

    @property
    def failure_streak(self) -> int:
        return (
            self.counters.consecutive_model_missing
            + self.counters.consecutive_timeout
            + self.counters.consecutive_route_error
            + self.counters.consecutive_auth
            + self.counters.consecutive_rate_limit
        )

    @property
    def state(self) -> str:
        """Derived operator UI badge (FREE/USE, PAID, USE/?, BALANCE, etc.)."""
        cost_val = self.cost_override.upper() if self.cost_override else self.cost
        try:
            avail_enum = AvailabilityState(self.availability)
            cost_enum = CostState(cost_val)
            return get_ui_badge(avail_enum, cost_enum)
        except Exception:
            return self.availability

    @property
    def cost_status(self) -> str:
        return self.cost_override.upper() if self.cost_override else self.cost

    def is_healthy(self) -> bool:
        return self.availability == AvailabilityState.LIVE.value

    def is_free(self) -> bool:
        return (self.cost_override.upper() if self.cost_override else self.cost) == CostState.FREE.value

    def is_dead(self) -> bool:
        return self.availability == AvailabilityState.DEAD.value

    def is_fresh(self, ttl_seconds: float = 300.0) -> bool:
        if not self.last_tested_at:
            return False
        try:
            tested_dt = datetime.fromisoformat(self.last_tested_at)
            age = (datetime.now() - tested_dt).total_seconds()
            return age < ttl_seconds
        except Exception:
            return False

class HealthCachePersistenceError(Exception):
    """W2-005: ONE unambiguous persistence contract for HealthCache.save().

    Raised on ANY failed persistence stage instead of returning normally:
    parent-directory creation, serialization, temporary-file write,
    flush/fsync, or the atomic replace. ``stage`` names the stage, ``path``
    the file it applies to, and ``cleanup_error`` (when set) records that the
    temporary artifact could not be removed after the primary failure — the
    primary failure is always the raised one, never swallowed.
    """

    def __init__(
        self,
        stage: str,
        path: Any,
        cause: Optional[BaseException] = None,
        cleanup_error: Optional[BaseException] = None,
    ):
        self.stage = stage
        self.path = str(path)
        self.cause = cause
        self.cleanup_error = cleanup_error
        message = f"Health cache persistence failed at stage '{stage}' ({self.path})"
        if cause is not None:
            message += f": {type(cause).__name__}: {cause}"
        if cleanup_error is not None:
            message += (
                f"; temporary-file cleanup also failed: "
                f"{type(cleanup_error).__name__}: {cleanup_error}"
            )
        super().__init__(message)


class CacheLoadState(str, Enum):
    """W2-005: distinguishable HealthCache.load() outcomes.

    A corrupt source file is NEVER silently presented as a clean empty cache:
    the state and ``load_error`` expose it, and the original bytes stay
    recoverable at ``quarantine_path``.
    """

    ABSENT = "ABSENT"                    # no cache file at all
    LOADED = "LOADED"                    # valid cache loaded
    MALFORMED_JSON = "MALFORMED_JSON"    # malformed / truncated JSON
    INVALID_RECORD = "INVALID_RECORD"    # bad record structure or value
    IO_ERROR = "IO_ERROR"                # the file exists but could not be read


CORRUPT_LOAD_STATES = (CacheLoadState.MALFORMED_JSON, CacheLoadState.INVALID_RECORD)


def _write_temp_file(temp_file: Path, payload: str) -> None:
    """Write + flush + fsync the temporary cache file.

    Each failure names its own persistence stage so the raised error is
    actionable; nothing here is swallowed.
    """
    try:
        with open(temp_file, "w", encoding="utf-8") as handle:
            try:
                handle.write(payload)
                handle.flush()
            except OSError as ex:
                raise HealthCachePersistenceError("write", temp_file, ex) from ex
            try:
                os.fsync(handle.fileno())
            except OSError as ex:
                raise HealthCachePersistenceError("fsync", temp_file, ex) from ex
    except HealthCachePersistenceError:
        raise
    except OSError as ex:
        raise HealthCachePersistenceError("write", temp_file, ex) from ex


class HealthCache:
    # Deterministic recovery name for a corrupt source: <cache>.corrupt
    # (and <cache>.corrupt.N if an earlier quarantine is already there).
    QUARANTINE_SUFFIX = ".corrupt"

    def __init__(self, cache_file: Path = HEALTH_CACHE_FILE):
        self.cache_file = Path(cache_file)
        self._lock = threading.RLock()
        # PERF-002: a dedicated writer mutex so persistence never needs the
        # memory lock for the slow serialization/write/fsync/replace section.
        self._save_lock = threading.Lock()
        self._record_generation = 0
        self._durable_generation = 0
        self.records: Dict[str, ModelHealthRecord] = {}
        self.last_scan_time: Optional[str] = None
        # W2-005 explicit load state: always inspectable, never implied.
        self.load_state: CacheLoadState = CacheLoadState.ABSENT
        self.load_error: str = ""
        self.quarantine_path: Optional[str] = None
        self.load()

    @property
    def corruption_detected(self) -> bool:
        return self.load_state in CORRUPT_LOAD_STATES

    # ------------------------------------------------------------- loading
    def _quarantine_target(self) -> Path:
        """Deterministic recovery name (first free when one already exists)."""
        base = self.cache_file.with_name(self.cache_file.name + self.QUARANTINE_SUFFIX)
        if not base.exists():
            return base
        n = 2
        while True:
            candidate = base.with_name(base.name + f".{n}")
            if not candidate.exists():
                return candidate
            n += 1

    def _corrupt(
        self,
        state: CacheLoadState,
        error: BaseException,
        raw_bytes: bytes,
    ) -> CacheLoadState:
        """Preserve the corrupt source byte-for-byte and expose the state.

        Nothing from a corrupt source is trusted into ``records`` (no partial
        merge), the corrupt bytes are quarantined under a deterministic
        recovery name, and only then is the unreadable source moved out of
        the authoritative path. If quarantine itself fails, the ORIGINAL file
        is left exactly as it was.
        """
        self.records = {}
        self.load_state = state
        self.load_error = f"{type(error).__name__}: {error}"
        self.quarantine_path = None
        target = self._quarantine_target()
        try:
            target.write_bytes(raw_bytes)
        except OSError as quarantine_failure:
            self.load_error += (
                "; quarantine failed, original bytes left untouched: "
                f"{type(quarantine_failure).__name__}: {quarantine_failure}"
            )
            return self.load_state
        self.quarantine_path = str(target)
        try:
            self.cache_file.unlink()
        except OSError as unlink_failure:
            # The bytes are already preserved at the recovery name; the
            # corrupt source simply stays in place and is re-reported on the
            # next load. Never delete anything we could not preserve.
            self.load_error += (
                "; corrupt source could not be moved aside: "
                f"{type(unlink_failure).__name__}: {unlink_failure}"
            )
        return self.load_state

    @staticmethod
    def _record_from_raw(cid: Any, raw: Any) -> "ModelHealthRecord":
        """Validated conversion of ONE persisted record.

        Raises ValueError/TypeError with the record identity on any invalid
        structure or value conversion: the caller turns that into an explicit
        INVALID_RECORD load state instead of silently dropping the row.
        """
        if not isinstance(raw, dict):
            raise ValueError(f"record {cid!r} is not an object")
        raw_counters = raw.get("counters") or {}
        if not isinstance(raw_counters, dict):
            raise ValueError(f"record {cid!r} counters are not an object")
        counters = EvidenceCounters.from_dict(raw_counters)

        # Backward compatibility for older records storing 'state'
        raw_avail = raw.get("availability")
        raw_cost = raw.get("cost")
        if not raw_avail:
            old_state = raw.get("state", AvailabilityState.UNKNOWN.value)
            if old_state in ("FREE/USE", "PAID", "USE/?"):
                raw_avail = AvailabilityState.LIVE.value
                raw_cost = CostState.FREE.value if old_state == "FREE/USE" else (CostState.PAID.value if old_state == "PAID" else CostState.UNKNOWN.value)
            else:
                raw_avail = old_state
                raw_cost = raw.get("cost_status", CostState.UNKNOWN.value)

        try:
            latency_ms = float(raw.get("latency_ms", 0.0))
        except (TypeError, ValueError) as ex:
            raise ValueError(f"record {cid!r} latency_ms is not numeric ({ex})") from ex
        try:
            status_code = int(raw.get("status_code", 0))
            success_streak = int(raw.get("success_streak", 0))
        except (TypeError, ValueError) as ex:
            raise ValueError(f"record {cid!r} integer field is invalid ({ex})") from ex

        return ModelHealthRecord(
            canonical_id=raw.get("canonical_id", cid),
            provider=raw.get("provider", ""),
            model_id=raw.get("model_id", ""),
            availability=raw_avail or AvailabilityState.UNKNOWN.value,
            cost=raw_cost or CostState.UNKNOWN.value,
            confidence=raw.get("confidence", Confidence.LIKELY_TEMPORARY.value),
            cost_override=raw.get("cost_override"),
            latency_ms=latency_ms,
            status_code=status_code,
            last_error=raw.get("last_error", ""),
            reason=raw.get("reason", ""),
            last_tested_at=raw.get("last_tested_at", ""),
            last_success_at=raw.get("last_success_at"),
            success_streak=success_streak,
            counters=counters,
            previous_availability=raw.get("previous_availability"),
            delta=raw.get("delta", ""),
            note=raw.get("note", ""),
        )

    def load(self) -> CacheLoadState:
        """Load cached health records, reporting ONE explicit outcome.

        Atomic: the whole file is validated into a local mapping first, and
        only a fully valid file replaces ``records``. Malformed/truncated
        JSON, invalid record structure, invalid values, and I/O failures are
        distinguished and surfaced (never converted into a silent empty pass
        that looks like a clean cache).
        """
        with self._lock:
            self.load_state = CacheLoadState.ABSENT
            self.load_error = ""
            self.quarantine_path = None
            if not self.cache_file.exists():
                return self.load_state
            try:
                raw_bytes = self.cache_file.read_bytes()
            except OSError as ex:
                # The source is untouched and records are NOT presented as a
                # clean empty cache: the read failure is the reported state.
                self.records = {}
                self.load_state = CacheLoadState.IO_ERROR
                self.load_error = f"{type(ex).__name__}: {ex}"
                return self.load_state
            try:
                data = json.loads(raw_bytes.decode("utf-8"))
            except (UnicodeDecodeError, ValueError) as ex:
                # Malformed/truncated JSON (and undecodable bytes): the exact
                # original bytes are preserved for recovery.
                return self._corrupt(CacheLoadState.MALFORMED_JSON, ex, raw_bytes)
            if not isinstance(data, dict):
                return self._corrupt(
                    CacheLoadState.INVALID_RECORD,
                    ValueError("cache root is not a JSON object"),
                    raw_bytes,
                )
            parsed: Dict[str, ModelHealthRecord] = {}
            try:
                for cid, raw in data.items():
                    parsed[str(cid)] = self._record_from_raw(cid, raw)
            except Exception as ex:
                return self._corrupt(CacheLoadState.INVALID_RECORD, ex, raw_bytes)
            self.records = parsed
            self.load_state = CacheLoadState.LOADED
            return self.load_state

    # ------------------------------------------------------------- saving
    def _temp_path(self) -> Path:
        return self.cache_file.with_suffix(".tmp")

    def save(self) -> None:
        """Atomically persist cached health records.

        W2-005 contract: success returns None; ANY failure raises
        HealthCachePersistenceError naming the failed stage. Failures are
        never swallowed and a failed write can never look like success. A
        leftover temporary file is never treated as authoritative state.

        PERF-002: the shared memory lock is held ONLY long enough to capture a
        coherent plain-data snapshot and bump a cache generation. JSON encoding,
        temp write, fsync and the atomic replace run OUTSIDE the memory lock, so
        a slow disk/fsync can no longer park concurrent cache readers (the UI).
        A dedicated save mutex serializes writers and prevents temp-path races.
        """
        with self._lock:
            serialized: Dict[str, Dict[str, Any]] = {}
            for cid, rec in self.records.items():
                try:
                    d = asdict(rec)
                    # Include derived state for backward compatibility
                    d["state"] = rec.state
                    d["cost_status"] = rec.cost_status
                except Exception as ex:
                    raise HealthCachePersistenceError("serialize", self.cache_file, ex) from ex
                serialized[cid] = d
            captured_generation = self._record_generation

        with self._save_lock:
            try:
                payload = json.dumps(serialized, indent=2, ensure_ascii=False)
            except Exception as ex:
                raise HealthCachePersistenceError("serialize", self.cache_file, ex) from ex

            try:
                self.cache_file.parent.mkdir(parents=True, exist_ok=True)
            except OSError as ex:
                raise HealthCachePersistenceError("mkdir", self.cache_file, ex) from ex

            temp_file = self._temp_path()
            try:
                _write_temp_file(temp_file, payload)
            except HealthCachePersistenceError as error:
                # Stage already named by the writer; still clean up the temp.
                self._cleanup_temp(temp_file, error)
                raise
            except Exception as ex:
                error = HealthCachePersistenceError("write", temp_file, ex)
                self._cleanup_temp(temp_file, error)
                raise error from ex
            try:
                os.replace(temp_file, self.cache_file)
            except Exception as ex:
                error = HealthCachePersistenceError("replace", self.cache_file, ex)
                self._cleanup_temp(temp_file, error)
                raise error from ex

            # Mark the captured generation durable. If records changed while the
            # snapshot was being written, the cache stays dirty for the next save.
            with self._lock:
                if captured_generation > self._durable_generation:
                    self._durable_generation = captured_generation
                if self._record_generation == captured_generation:
                    self._dirty = False

    @staticmethod
    def _cleanup_temp(temp_file: Path, error: HealthCachePersistenceError) -> None:
        """Remove the temporary artifact; classify a cleanup failure on the
        raised error instead of hiding it or replacing the primary cause."""
        try:
            if temp_file.exists():
                temp_file.unlink()
        except Exception as cleanup_failure:  # pragma: no cover - platform dependent
            error.cleanup_error = cleanup_failure
            error.args = (
                f"{error.args[0]}; temporary-file cleanup also failed: "
                f"{type(cleanup_failure).__name__}: {cleanup_failure}",
            )

    def get(self, canonical_id: str) -> Optional[ModelHealthRecord]:
        with self._lock:
            return self.records.get(canonical_id)

    def snapshot_records(self) -> Dict[str, "ModelHealthRecord"]:
        """A coherent copy of the record mapping taken under the cache lock.

        PERF-004: callers that must iterate a stable view (e.g. Combo Tools
        Preview/Apply while a scan thread is recording) use this instead of
        touching ``records`` directly, which raised
        'dictionary changed size during iteration' when a concurrent first
        result inserted a new id."""
        with self._lock:
            return dict(self.records)

    def prune_stale_records(
        self,
        current_ids,
        referenced_ids=(),
        recent_window_seconds: float = 604800.0,
        max_records: int = 20000,
        now: Optional[datetime] = None,
    ) -> int:
        """PERF-005: bounded retention for historical cache records.

        Called ONLY after an authoritative successful inventory reconciliation.
        A record is pruned only when it is proven ABSENT from the newest
        inventory AND not referenced by a combo AND carries no manual cost
        override AND is older than the recent-history window. A hard cap
        (oldest-eligible-first) keeps the store bounded even in pathological
        churn. Never called on failed/timed-out/LOCKED discovery: absent
        current_ids is not evidence of removal.

        Returns the number of records pruned. Does not persist (the caller's
        normal save does).
        """
        current = {str(c) for c in (current_ids or ())}
        if not current:
            return 0
        referenced = {str(c) for c in (referenced_ids or ())}
        now = now or datetime.now()
        with self._lock:
            old_stale = []
            cap_eligible = []
            for cid, rec in self.records.items():
                if cid in current or cid in referenced:
                    continue
                if rec.cost_override:
                    continue
                cap_eligible.append((self._record_age_seconds(rec, now), cid))
                if self._record_age_seconds(rec, now) >= recent_window_seconds:
                    old_stale.append(cid)
            removable = set(old_stale)
            # Hard cap: after old-stale pruning, if the store still exceeds the
            # bound, prune the oldest remaining absent/unreferenced/no-override
            # records (oldest eligible first).
            remaining = len(self.records) - len(removable)
            if remaining > max_records:
                over = remaining - max_records
                cap_eligible.sort(reverse=True)  # oldest (largest age) first
                for _age, cid in cap_eligible:
                    if over <= 0:
                        break
                    if cid in removable:
                        continue
                    removable.add(cid)
                    over -= 1
            for cid in removable:
                del self.records[cid]
            if removable:
                self._record_generation += 1
            return len(removable)

    @staticmethod
    def _record_age_seconds(rec: "ModelHealthRecord", now: datetime) -> float:
        stamp = rec.last_tested_at or rec.last_success_at or ""
        if not stamp:
            return float("inf")
        try:
            return (now - datetime.fromisoformat(stamp)).total_seconds()
        except ValueError:
            return float("inf")

    def record_evidence(
        self,
        canonical_id: str,
        provider: str,
        model_id: str,
        evidence: EvidenceRecord,
        auto_save: bool = True,
    ) -> ModelHealthRecord:
        """
        Updates health record with evidence, computes delta against previous scan,
        and manages success streaks and counters.
        """
        with self._lock:
            existing = self.records.get(canonical_id)
            now_iso = datetime.now().isoformat()
            prev_avail = existing.availability if existing else None

            # Calculate success streak
            success_streak = existing.success_streak if existing else 0
            last_success_at = existing.last_success_at if existing else None

            if evidence.availability == AvailabilityState.LIVE:
                success_streak += 1
                last_success_at = now_iso
            else:
                success_streak = 0

            # Authoritative evidence counters from classification
            counters = EvidenceCounters.from_dict(evidence.counters.to_dict())

            # Delta calculation
            delta = ""
            if prev_avail and prev_avail != evidence.availability.value:
                if evidence.availability == AvailabilityState.LIVE:
                    delta = "+ recovered"
                elif prev_avail == AvailabilityState.LIVE.value:
                    delta = f"- {evidence.availability.value}"
                elif evidence.availability == AvailabilityState.BALANCE:
                    delta = "$ balance exhausted"
                elif evidence.availability == AvailabilityState.RATE_LIMIT:
                    delta = "↻ rate-limited"
                elif evidence.availability == AvailabilityState.DEAD:
                    delta = "✖ marked DEAD"
                else:
                    delta = f"~ {evidence.availability.value}"
            elif existing and existing.latency_ms > 0 and evidence.latency_ms > 0:
                diff = evidence.latency_ms - existing.latency_ms
                if diff > 1500:
                    delta = f"latency +{diff:.0f}ms"

            cost_override = existing.cost_override if existing else None

            record = ModelHealthRecord(
                canonical_id=canonical_id,
                provider=provider,
                model_id=model_id,
                availability=evidence.availability.value,
                cost=evidence.cost.value,
                confidence=evidence.confidence.value,
                cost_override=cost_override,
                latency_ms=evidence.latency_ms,
                status_code=evidence.status_code,
                last_error=evidence.raw_error,
                reason=evidence.reason,
                last_tested_at=now_iso,
                last_success_at=last_success_at,
                success_streak=success_streak,
                counters=counters,
                previous_availability=prev_avail,
                delta=delta,
                note=evidence.note,
            )
            self.records[canonical_id] = record

            # PERF-002: every record mutation advances the generation so save()
            # can tell whether the captured snapshot is still current.
            self._record_generation += 1

            if auto_save:
                self.save()

            return record

    def set_cost_override(self, canonical_id: str, cost: Optional[str]) -> Optional[ModelHealthRecord]:
        """Sets or clears a manual cost override (FREE, PAID, or None to clear). Returns updated ModelHealthRecord or None."""
        with self._lock:
            rec = self.records.get(canonical_id)
            if not rec:
                return None
            if cost is not None:
                norm = cost.upper()
                if norm not in (CostState.FREE.value, CostState.PAID.value):
                    return None
                rec.cost_override = norm
            else:
                rec.cost_override = None
            self._record_generation += 1
            self.save()
            return rec

