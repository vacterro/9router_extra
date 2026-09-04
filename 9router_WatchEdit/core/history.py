"""
9router_WatchEdit - Health Cache & Operational History
Maintains persistent local health cache, latency records, evidence-specific streaks,
scan deltas, and manual cost overrides across sessions with thread-safe persistence.
"""
from dataclasses import dataclass, asdict, field
from datetime import datetime
import json
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

class HealthCache:
    def __init__(self, cache_file: Path = HEALTH_CACHE_FILE):
        self.cache_file = cache_file
        self._lock = threading.RLock()
        self.records: Dict[str, ModelHealthRecord] = {}
        self.last_scan_time: Optional[str] = None
        self.load()

    def load(self):
        """Loads cached health records from JSON file with thread safety."""
        with self._lock:
            if not self.cache_file.exists():
                return
            try:
                data = json.loads(self.cache_file.read_text(encoding="utf-8"))
                for cid, raw in data.items():
                    raw_counters = raw.get("counters") or {}
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

                    self.records[cid] = ModelHealthRecord(
                        canonical_id=raw.get("canonical_id", cid),
                        provider=raw.get("provider", ""),
                        model_id=raw.get("model_id", ""),
                        availability=raw_avail or AvailabilityState.UNKNOWN.value,
                        cost=raw_cost or CostState.UNKNOWN.value,
                        confidence=raw.get("confidence", Confidence.LIKELY_TEMPORARY.value),
                        cost_override=raw.get("cost_override"),
                        latency_ms=float(raw.get("latency_ms", 0.0)),
                        status_code=int(raw.get("status_code", 0)),
                        last_error=raw.get("last_error", ""),
                        reason=raw.get("reason", ""),
                        last_tested_at=raw.get("last_tested_at", ""),
                        last_success_at=raw.get("last_success_at"),
                        success_streak=int(raw.get("success_streak", 0)),
                        counters=counters,
                        previous_availability=raw.get("previous_availability"),
                        delta=raw.get("delta", ""),
                        note=raw.get("note", ""),
                    )
            except Exception:
                pass

    def save(self):
        """Atomically saves cached health records to JSON file under file lock."""
        with self._lock:
            self.cache_file.parent.mkdir(parents=True, exist_ok=True)
            temp_file = self.cache_file.with_suffix(".tmp")
            try:
                data = {}
                for cid, rec in self.records.items():
                    d = asdict(rec)
                    # Include derived state for backward compatibility
                    d["state"] = rec.state
                    d["cost_status"] = rec.cost_status
                    data[cid] = d
                temp_file.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
                temp_file.replace(self.cache_file)
            except Exception:
                if temp_file.exists():
                    try:
                        temp_file.unlink()
                    except Exception:
                        pass

    def get(self, canonical_id: str) -> Optional[ModelHealthRecord]:
        with self._lock:
            return self.records.get(canonical_id)

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
            self.save()
            return rec

