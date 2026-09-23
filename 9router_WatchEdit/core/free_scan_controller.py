"""
9router_WatchEdit - cost-aware provider FREE scan controller
(FREE-FALLBACK-001, milestone 5).

Separate from the per-model scanner worker: this controller schedules
PROVIDER-LEVEL work and enforces the cost guards that keep "discover what is
free" from becoming "spend money to discover what is free".

Actions
-------
    Scan Selected              respect every selected provider's scan policy/mode
    Metadata Scan Selected     catalog/metadata endpoints only -- ZERO inference
    Validate Selected          bounded official live canary, refused by default
    Stop Scan                  cooperative cancellation of the active runs

Guards (default-refusal, never a hidden bypass)
-----------------------------------------------
    * unselected or disabled providers are never touched;
    * METADATA_ONLY executes zero inference calls by construction -- the live
      canary is not even reachable from that path;
    * a live canary runs only when the adapter declares one AND the monetary
      cost class is proven safe (zero monetary / free quota) OR the operator
      explicitly enabled the per-provider billing consent;
    * POSSIBLE_BILLING, ACCOUNT_CONDITIONAL and UNKNOWN live probes are refused;
    * valid trusted-alive trust suppresses needless live probing;
    * bounded global concurrency, per-provider single-flight, cancellation-aware;
    * a source outage preserves the last-known-good FREE inventory, and only a
      successful authoritative refresh may prune scanner-owned entries;
    * a stale generation can never overwrite a newer provider result.
"""
from __future__ import annotations

import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence

from core.free_evidence import ProviderHealth, ScanMode, ScanPolicy
from core.free_provider_adapters import (
    FreeProviderAdapter,
    MetadataScanResult,
    LiveCanaryResult,
    resolve_adapter,
)
from core.free_provider_registry import FreeProviderRegistry

ACTION_METADATA = "METADATA"
ACTION_LIVE = "LIVE"
ACTION_SCAN = "SCAN"
#: Effective per-provider action for METADATA_AND_LIVE_PROBE: refresh the free
#: inventory first, then canary the route the refresh just proved.
ACTION_METADATA_THEN_LIVE = "METADATA_AND_LIVE"

ACTION_LABELS = {
    ACTION_METADATA: "Metadata Scan Selected",
    ACTION_LIVE: "Validate Selected",
    ACTION_SCAN: "Scan Selected",
}

STATUS_SCANNED = "SCANNED"
STATUS_FAILED = "FAILED"
STATUS_SKIPPED = "SKIPPED"

# Skip vocabulary (exact reasons are surfaced to the operator, never hidden).
SKIP_UNKNOWN_PROVIDER = "unknown_provider"
SKIP_DISABLED = "disabled"
SKIP_POLICY_NEVER = "policy_never"
SKIP_MODE_DISABLED = "mode_disabled"
SKIP_NOT_STALE = "not_stale"
SKIP_BACKOFF = "backoff"
SKIP_SINGLE_FLIGHT = "single_flight"
SKIP_NO_METADATA_ADAPTER = "no_metadata_adapter"
SKIP_NO_LIVE_CANARY = "no_live_canary"
SKIP_TRUSTED_ALIVE = "trusted_alive"
SKIP_REFUSED_COST_RISK = "refused_cost_risk"
SKIP_CANCELLED = "cancelled"
SKIP_RUN_REJECTED = "scan_already_running"

# Scanner evidence is durable at most every sixteen completed providers; each
# run also performs a mandatory terminal flush. Crash loss is therefore bounded
# to fifteen completed provider outcomes (or one interval when it comes first).
FREE_REGISTRY_CHECKPOINT_EVERY = 16


def _utc_now_iso(now: Optional[float] = None) -> str:
    stamp = datetime.fromtimestamp(now, tz=timezone.utc) if now else datetime.now(timezone.utc)
    return stamp.strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class ProviderScanOutcome:
    provider_id: str
    display_name: str = ""
    action: str = ""
    status: str = STATUS_SKIPPED
    reason: str = ""
    generation: int = 0
    models_found: int = 0
    strict_free: int = 0
    conditional: int = 0
    error_class: str = ""
    error_summary: str = ""
    network_calls: int = 0
    inference_calls: int = 0
    retry_after_sec: float = 0.0
    started_at: str = ""
    finished_at: str = ""
    #: Per-stage truth for composite runs: which stage reached which status.
    metadata_status: str = ""
    live_status: str = ""
    persistence_error: str = ""

    @property
    def applied(self) -> bool:
        return self.status in (STATUS_SCANNED, STATUS_FAILED)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "display_name": self.display_name,
            "action": self.action,
            "status": self.status,
            "reason": self.reason,
            "generation": self.generation,
            "models_found": self.models_found,
            "strict_free": self.strict_free,
            "conditional": self.conditional,
            "error_class": self.error_class,
            "error_summary": self.error_summary,
            "network_calls": self.network_calls,
            "inference_calls": self.inference_calls,
            "retry_after_sec": self.retry_after_sec,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "metadata_status": self.metadata_status,
            "live_status": self.live_status,
            "persistence_error": self.persistence_error,
        }


@dataclass
class FreeScanRun:
    """One operator scan action over a bounded set of providers."""

    action: str
    generation: int
    started_at: str
    provider_ids: List[str] = field(default_factory=list)
    outcomes: Dict[str, ProviderScanOutcome] = field(default_factory=dict)
    rejected_reason: str = ""
    cancelled: bool = False
    finished_at: str = ""
    cancel_event: threading.Event = field(default_factory=threading.Event)
    _futures: List[Future] = field(default_factory=list)
    _done: threading.Event = field(default_factory=threading.Event)
    _pending_publish_ids: List[str] = field(default_factory=list)
    durable: bool = True
    persistence_error: str = ""

    @property
    def finished(self) -> bool:
        return bool(self.rejected_reason) or self._done.is_set()

    def wait(self, timeout: Optional[float] = None) -> bool:
        return self._done.wait(timeout)

    def mark_finished(self, now: Optional[float] = None) -> None:
        self.finished_at = _utc_now_iso(now)
        self._done.set()

    def summary(self) -> Dict[str, Any]:
        counts: Dict[str, int] = {}
        for outcome in self.outcomes.values():
            counts[outcome.status] = counts.get(outcome.status, 0) + 1
        return {
            "action": self.action,
            "generation": self.generation,
            "providers": len(self.provider_ids),
            "cancelled": self.cancelled,
            "rejected_reason": self.rejected_reason,
            "durable": self.durable,
            "persistence_error": self.persistence_error,
            "statuses": counts,
            "outcomes": [self.outcomes[pid].as_dict() for pid in sorted(self.outcomes)],
        }


class FreeScanController:
    """Bounded, cost-aware provider scan scheduler."""

    def __init__(
        self,
        registry: FreeProviderRegistry,
        adapters: Mapping[str, FreeProviderAdapter],
        *,
        max_workers: int = 3,
        clock=None,
        on_publish=None,
    ):
        self.registry = registry
        self.adapters = dict(adapters)
        self.max_workers = max(1, int(max_workers))
        self._clock = clock
        self._on_publish = on_publish
        self._lock = threading.RLock()
        self._inflight: Dict[str, int] = {}
        self._latest_generation: Dict[str, int] = {}
        self._generation = 0
        self._runs: List[FreeScanRun] = []
        self._executor: Optional[ThreadPoolExecutor] = None
        self.stale_rejections: List[Dict[str, Any]] = []

    def _now(self) -> float:
        return float(self._clock()) if self._clock is not None else time.time()

    # ------------------------------------------------------------- lifecycle
    def adapter_for(self, provider_id: str) -> FreeProviderAdapter:
        return resolve_adapter(provider_id, self.adapters)

    def set_adapters(self, adapters: Mapping[str, FreeProviderAdapter]) -> None:
        with self._lock:
            self.adapters = dict(adapters)

    def _next_generation(self) -> int:
        with self._lock:
            self._generation += 1
            return self._generation

    def running_runs(self) -> List[FreeScanRun]:
        with self._lock:
            return [run for run in self._runs if not run.finished]

    def is_running(self) -> bool:
        return bool(self.running_runs())

    def running_kind(self) -> str:
        runs = self.running_runs()
        return runs[0].action if runs else ""

    def _executor_ref(self) -> ThreadPoolExecutor:
        with self._lock:
            if self._executor is None:
                self._executor = ThreadPoolExecutor(
                    max_workers=self.max_workers,
                    thread_name_prefix="free-provider-scan",
                )
            return self._executor

    def shutdown(self, wait: bool = False) -> None:
        with self._lock:
            executor = self._executor
            self._executor = None
        if executor is not None:
            executor.shutdown(wait=wait, cancel_futures=True)

    # ---------------------------------------------------------------- actions
    def metadata_scan_selected(self, provider_ids: Sequence[str]) -> FreeScanRun:
        return self._start(ACTION_METADATA, provider_ids)

    def validate_selected(self, provider_ids: Sequence[str]) -> FreeScanRun:
        return self._start(ACTION_LIVE, provider_ids)

    def scan_selected(self, provider_ids: Sequence[str]) -> FreeScanRun:
        return self._start(ACTION_SCAN, provider_ids)

    def _start(self, action: str, provider_ids: Sequence[str]) -> FreeScanRun:
        ids: List[str] = []
        for raw in provider_ids or ():
            pid = str(raw or "").strip()
            if pid and pid not in ids:
                ids.append(pid)
        generation = self._next_generation()
        run = FreeScanRun(
            action=action,
            generation=generation,
            started_at=_utc_now_iso(self._now()),
            provider_ids=ids,
        )
        with self._lock:
            self._runs.append(run)
            # Bounded history: the run log is diagnostics, not a leak.
            if len(self._runs) > 64:
                self._runs = self._runs[-32:]
        if not ids:
            run.rejected_reason = "no_providers_selected"
            run.mark_finished(self._now())
            return run
        executor = self._executor_ref()
        for pid in ids:
            run._futures.append(
                executor.submit(self._run_provider, run, pid)
            )
        # The run completes when all provider futures do; a small watcher keeps
        # that off the GUI thread without polling from the UI.
        threading.Thread(
            target=self._await_run, args=(run,), daemon=True,
            name=f"free-provider-run-{generation}",
        ).start()
        return run

    def _await_run(self, run: FreeScanRun) -> None:
        for future in list(run._futures):
            try:
                future.result()
            except Exception:
                pass
        run.mark_finished(self._now())

    def stop(self, run: Optional[FreeScanRun] = None) -> bool:
        """Cooperative cancellation. Returns True when something was signalled."""
        targets = [run] if run is not None else self.running_runs()
        signalled = False
        for target in targets:
            if target is None or target.finished:
                continue
            target.cancel_event.set()
            target.cancelled = True
            signalled = True
        return signalled

    def stop_and_wait(self, timeout: float = 5.0) -> bool:
        runs = self.running_runs()
        if not runs:
            return True
        self.stop()
        deadline = self._now() + max(0.0, float(timeout))
        for run in runs:
            remaining = max(0.0, deadline - self._now())
            if not run.wait(remaining):
                return False
        return True

    # ------------------------------------------------------------------ guards
    def _plan(self, provider_id: str, action: str) -> tuple:
        """(allowed, reason, effective_action). Pure policy evaluation, no I/O."""
        record = self.registry.get(provider_id)
        if record is None:
            return False, SKIP_UNKNOWN_PROVIDER, ""
        if not record.enabled:
            return False, SKIP_DISABLED, ""
        try:
            policy = ScanPolicy(record.scan_policy)
        except ValueError:
            policy = ScanPolicy.MANUAL
        try:
            mode = ScanMode(record.scan_mode)
        except ValueError:
            mode = ScanMode.DISABLED
        if policy == ScanPolicy.NEVER:
            return False, SKIP_POLICY_NEVER, ""
        if mode == ScanMode.DISABLED:
            return False, SKIP_MODE_DISABLED, ""
        if record.backoff_until_epoch and record.backoff_until_epoch > self._now():
            return False, SKIP_BACKOFF, ""

        adapter = self.adapter_for(provider_id)
        caps = adapter.capabilities()

        if action == ACTION_METADATA:
            if not caps.supports_metadata:
                return False, SKIP_NO_METADATA_ADAPTER, ""
            if policy == ScanPolicy.STALE_ONLY and not self.registry.is_due(provider_id):
                return False, SKIP_NOT_STALE, ""
            return True, "", ACTION_METADATA

        if action == ACTION_LIVE:
            # An explicit Validate NEVER silently degrades into something else:
            # it either runs a permitted canary or makes zero calls.
            if not caps.supports_live_canary:
                return False, SKIP_NO_LIVE_CANARY, ""
            if record.trust_valid(self._now()):
                return False, SKIP_TRUSTED_ALIVE, ""
            if not record.cost_safe_for_live():
                return False, SKIP_REFUSED_COST_RISK, ""
            return True, "", ACTION_LIVE

        # ACTION_SCAN: respect the configured mode/policy.
        if mode == ScanMode.METADATA_ONLY:
            if not caps.supports_metadata:
                return False, SKIP_NO_METADATA_ADAPTER, ""
            if policy == ScanPolicy.STALE_ONLY and not self.registry.is_due(provider_id):
                return False, SKIP_NOT_STALE, ""
            return True, "", ACTION_METADATA

        # METADATA_AND_LIVE_PROBE: metadata first, live only when permitted.
        # MANUAL is a scheduling policy, not a refusal: an explicit operator
        # action (Scan Selected IS one) runs immediately, while automatic /
        # background scheduling stays excluded by is_due(). The NEVER policy
        # and backoff were already refused above.
        due = policy == ScanPolicy.ALWAYS or self.registry.is_due(provider_id)
        if not due:
            # Not scheduled-due (fresh metadata). MANUAL is a scheduling
            # policy, not a refusal: an explicit operator action (Scan
            # Selected IS one) still runs the composite. STALE_ONLY stays
            # strict: not due means not run, even on explicit request.
            # ALWAYS never reaches this branch (it is always due).
            if policy != ScanPolicy.MANUAL:
                return False, SKIP_NOT_STALE, ""
            if not self.registry.operator_scan_requested(provider_id):
                return False, SKIP_NOT_STALE, ""
            if caps.supports_live_canary and record.trust_valid(self._now()):
                reason = SKIP_TRUSTED_ALIVE
                live_permitted = False
            elif caps.supports_live_canary and not record.cost_safe_for_live():
                reason = SKIP_REFUSED_COST_RISK
                live_permitted = False
            else:
                reason = ""
                live_permitted = caps.supports_live_canary
            if live_permitted:
                return True, reason, ACTION_METADATA_THEN_LIVE
            return True, reason, ACTION_METADATA
        if not caps.supports_metadata and not caps.supports_live_canary:
            return False, SKIP_NO_METADATA_ADAPTER, ""
        reason = ""
        live_permitted = caps.supports_live_canary
        if caps.supports_live_canary and record.trust_valid(self._now()):
            # Trust suppresses the canary; a permitted metadata refresh still runs.
            reason = SKIP_TRUSTED_ALIVE
            live_permitted = False
        elif caps.supports_live_canary and not record.cost_safe_for_live():
            # Refuse the live probe, keep the FREE metadata evidence fresh.
            reason = SKIP_REFUSED_COST_RISK
            live_permitted = False
        elif not caps.supports_live_canary:
            reason = SKIP_NO_LIVE_CANARY

        if caps.supports_metadata and live_permitted:
            return True, reason, ACTION_METADATA_THEN_LIVE
        if caps.supports_metadata:
            return True, reason, ACTION_METADATA
        if live_permitted:
            return True, reason, ACTION_LIVE
        return False, SKIP_NO_LIVE_CANARY, ""

    # --------------------------------------------------------------- execution
    def _run_provider(self, run: FreeScanRun, provider_id: str) -> None:
        outcome = ProviderScanOutcome(
            provider_id=provider_id,
            action=run.action,
            generation=run.generation,
            started_at=_utc_now_iso(self._now()),
        )
        record = self.registry.get(provider_id)
        outcome.display_name = (record.display_name if record else provider_id) or provider_id
        if run.cancel_event.is_set():
            outcome.reason = SKIP_CANCELLED
            self._finish(run, outcome)
            return
        allowed, reason, effective = self._plan(provider_id, run.action)
        outcome.reason = reason
        if not allowed:
            self._finish(run, outcome)
            return
        if not self._acquire(provider_id, run.generation):
            outcome.reason = SKIP_SINGLE_FLIGHT
            self._finish(run, outcome)
            return
        try:
            adapter = self.adapter_for(provider_id)
            if effective == ACTION_METADATA:
                self._execute_metadata(run, adapter, outcome)
                outcome.metadata_status = outcome.status
            elif effective == ACTION_METADATA_THEN_LIVE:
                self._execute_metadata(run, adapter, outcome)
                outcome.metadata_status = outcome.status
                if outcome.status == STATUS_SCANNED and not run.cancel_event.is_set():
                    self._execute_live_stage(run, adapter, outcome)
            else:
                self._execute_live(run, adapter, outcome)
                outcome.live_status = outcome.status
        except Exception as ex:  # adapter contract is total; never leak a thread
            outcome.status = STATUS_FAILED
            outcome.error_class = "controller_error"
            outcome.error_summary = f"{type(ex).__name__}"
        finally:
            self._release(provider_id)
        self._finish(run, outcome)

    def _execute_metadata(self, run: FreeScanRun, adapter: FreeProviderAdapter,
                          outcome: ProviderScanOutcome) -> None:
        """METADATA_ONLY: the live canary is unreachable from here."""
        self.registry.record_scan_started(outcome.provider_id, ACTION_METADATA)
        result: MetadataScanResult = adapter.metadata_scan()
        outcome.network_calls = int(result.network_calls)
        outcome.inference_calls = int(result.inference_calls)
        if run.cancel_event.is_set():
            # Cancelled mid-flight: the pass publishes nothing, even though the
            # adapter call itself already completed.
            outcome.status = STATUS_SKIPPED
            outcome.reason = SKIP_CANCELLED
            return
        outcome.error_class = result.error_class
        outcome.error_summary = result.error_summary
        outcome.retry_after_sec = float(result.retry_after_sec or 0.0)
        if result.inference_calls:
            # METADATA_ONLY must never execute inference. A violating adapter's
            # result is rejected instead of quietly believed.
            outcome.status = STATUS_FAILED
            outcome.error_class = "metadata_inference_violation"
            outcome.error_summary = (
                f"metadata scan reported {int(result.inference_calls)} inference call(s)"
            )
            outcome.inference_calls = 0
            return
        if result.ok:
            outcome.status = STATUS_SCANNED
            outcome.models_found = len(result.models)
            outcome.strict_free = sum(
                1 for row in result.models if row.get("free_evidence") == "STRICT_FREE"
            )
            outcome.conditional = sum(
                1 for row in result.models
                if row.get("free_evidence") in ("CONDITIONAL_FREE", "UNKNOWN_COST")
            )
        else:
            outcome.status = STATUS_FAILED
        self._apply_metadata(outcome, result)

    def _apply_metadata(self, outcome: ProviderScanOutcome,
                        result: MetadataScanResult) -> None:
        """A stale generation never reaches the registry."""
        if not self.apply_outcome(outcome):
            return
        if self.registry.get(outcome.provider_id) is None:
            return
        self.registry.record_metadata_result(
            outcome.provider_id,
            ok=bool(result.ok),
            models=result.models if result.ok else None,
            error_class=result.error_class,
            error_summary=result.error_summary,
            retry_after_sec=float(result.retry_after_sec or 0.0),
            authoritative=bool(result.authoritative),
            status=STATUS_FAILED if not result.ok else "",
            persist=False,
        )

    def _execute_live(self, run: FreeScanRun, adapter: FreeProviderAdapter,
                      outcome: ProviderScanOutcome) -> None:
        record = self.registry.get(outcome.provider_id)
        caps = adapter.capabilities()
        # Last line of defence: no code path may reach a billable inference call
        # without an adapter-declared canary AND a proven safe cost class (or an
        # explicit per-provider consent).
        if not caps.supports_live_canary:
            outcome.status = STATUS_SKIPPED
            outcome.reason = SKIP_NO_LIVE_CANARY
            return
        if record is not None and not record.cost_safe_for_live():
            outcome.status = STATUS_SKIPPED
            outcome.reason = SKIP_REFUSED_COST_RISK
            return
        # The ADAPTER owns target selection: a canary against an arbitrary first
        # row could report a provider-level failure that is really one unlucky
        # model, and that misleading health verdict would persist.
        rows = list(record.models) if record is not None else []
        target = adapter.preferred_canary_target(rows)
        if not target:
            outcome.reason = "no_free_model_to_canary"
            outcome.status = STATUS_SKIPPED
            return
        if run.cancel_event.is_set():
            outcome.reason = SKIP_CANCELLED
            outcome.status = STATUS_SKIPPED
            return
        self.registry.record_scan_started(outcome.provider_id, ACTION_LIVE)
        result: LiveCanaryResult = adapter.live_canary(target)
        outcome.network_calls = int(result.network_calls)
        outcome.inference_calls = int(result.inference_calls)
        if run.cancel_event.is_set():
            outcome.status = STATUS_SKIPPED
            outcome.reason = SKIP_CANCELLED
            return
        outcome.error_class = result.error_class or ""
        outcome.error_summary = result.error_summary or ""
        outcome.status = STATUS_SCANNED if result.ok else STATUS_FAILED
        blocking: Optional[ProviderHealth] = None
        if result.blocking_health:
            try:
                blocking = ProviderHealth(str(result.blocking_health))
            except ValueError:
                blocking = None
        if self.apply_outcome(outcome):
            self.registry.record_live_probe_result(
                outcome.provider_id,
                ok=bool(result.ok),
                state=str(result.state or ""),
                error_class=str(result.error_class or ""),
                error_summary=str(result.error_summary or ""),
                blocking_health=blocking,
                persist=False,
            )

    def _execute_live_stage(self, run: FreeScanRun, adapter: FreeProviderAdapter,
                            outcome: ProviderScanOutcome) -> None:
        """Second stage of METADATA_AND_LIVE_PROBE: canary the refreshed route.

        A failed or skipped live stage makes the **composite** operator request
        truthfully FAILED/SKIPPED. Valid metadata evidence is preserved by the
        registry and is never destroyed by a downstream live failure.
        """
        stage = ProviderScanOutcome(
            provider_id=outcome.provider_id,
            display_name=outcome.display_name,
            action=ACTION_LIVE,
            generation=run.generation,
        )
        self._execute_live(run, adapter, stage)
        outcome.network_calls += int(stage.network_calls)
        outcome.inference_calls += int(stage.inference_calls)
        outcome.live_status = stage.status
        if stage.status != STATUS_SCANNED:
            if stage.status == STATUS_FAILED:
                # A failed live stage fails the COMPOSITE operator request; the
                # metadata inventory it refreshed stays recorded downstream.
                outcome.status = STATUS_FAILED
            else:
                outcome.status = STATUS_SKIPPED
            outcome.reason = stage.reason or outcome.reason
            outcome.error_class = stage.error_class or outcome.error_class
            outcome.error_summary = stage.error_summary or outcome.error_summary

    # ----------------------------------------------------------- bookkeeping
    def _acquire(self, provider_id: str, generation: int) -> bool:
        with self._lock:
            if provider_id in self._inflight:
                return False
            self._inflight[provider_id] = generation
            return True

    def _release(self, provider_id: str) -> None:
        with self._lock:
            self._inflight.pop(provider_id, None)

    def _finish(self, run: FreeScanRun, outcome: ProviderScanOutcome) -> None:
        outcome.finished_at = _utc_now_iso(self._now())
        with self._lock:
            run.outcomes[outcome.provider_id] = outcome
            run._pending_publish_ids.append(outcome.provider_id)
            completed = len(run.outcomes)
            checkpoint_due = (
                completed % FREE_REGISTRY_CHECKPOINT_EVERY == 0
                or completed >= len(run.provider_ids)
            )
            checkpoint_ids = list(run._pending_publish_ids) if checkpoint_due else []
            if checkpoint_due:
                run._pending_publish_ids.clear()
        if not checkpoint_due:
            return

        # Publish only after the entire batch is durably committed. The
        # checkpoint runs outside the controller lock; registry.save() owns its
        # own serialization lock across snapshot, secret scan and promotion.
        if not self.registry.save():
            error = self.registry.last_save_error or "registry checkpoint failed"
            with self._lock:
                run.durable = False
                run.persistence_error = error
                for provider_id in checkpoint_ids:
                    pending = run.outcomes[provider_id]
                    pending.persistence_error = error
                    if pending.status == STATUS_SCANNED:
                        pending.status = STATUS_FAILED
                        pending.reason = "registry_persistence_failed"
                        pending.error_class = "registry_persistence_failed"
                        pending.error_summary = "FREE registry checkpoint failed"
        if self._on_publish is not None:
            for provider_id in checkpoint_ids:
                try:
                    self._on_publish(run.outcomes[provider_id])
                except Exception:
                    pass

    def inflight_providers(self) -> List[str]:
        with self._lock:
            return sorted(self._inflight)

    def apply_outcome(self, outcome: ProviderScanOutcome) -> bool:
        """Generation gate: a stale result can never overwrite a newer one."""
        with self._lock:
            latest = self._latest_generation.get(outcome.provider_id, 0)
            if outcome.generation < latest:
                self.stale_rejections.append({
                    "provider_id": outcome.provider_id,
                    "generation": outcome.generation,
                    "latest": latest,
                })
                return False
            self._latest_generation[outcome.provider_id] = outcome.generation
            return True

    def last_generation(self, provider_id: str) -> int:
        with self._lock:
            return int(self._latest_generation.get(str(provider_id), 0))
