"""
9router_WatchEdit - Asynchronous Two-Stage Health Probe & Scanner Engine
Executes cancellable async tests with httpx.AsyncClient, strict per-connection concurrency,
account-level circuit breaking, Retry-After backoff, and two-stage (Fast -> Slow) probing.
"""
import asyncio
from collections import deque
from enum import Enum
import re
import threading
import time
from typing import Callable, Dict, List, Optional, Set, Tuple, Any
import httpx

from config import (
    DEFAULT_GLOBAL_CONCURRENCY,
    DEFAULT_PER_PROVIDER_CONCURRENCY,
    DEFAULT_FAST_TIMEOUT_SEC,
    DEFAULT_SLOW_TIMEOUT_SEC,
    PENDING_THRESHOLD_SEC,
    redact_secrets,
)
from core.router_client import RouterClient
from core.discovery import DiscoveredModel
from core.history import HealthCache, ModelHealthRecord
from core.classification import (
    AvailabilityState,
    CostState,
    Confidence,
    classify_probe_result,
    EvidenceRecord,
    EvidenceCounters,
)

class ScanMode(str, Enum):
    QUICK = "QUICK"
    FULL = "FULL"
    FAILED_ONLY = "FAILED_ONLY"
    COMBO = "COMBO"

class ProviderCircuitBreaker:
    """Tracks connection/provider-level outages (AUTH, BALANCE, severe rate limits) to skip redundant probes."""
    def __init__(self):
        self._tripped: Dict[str, Tuple[AvailabilityState, str]] = {}

    def trip(self, identity: str, state: AvailabilityState, reason: str = ""):
        self._tripped[identity.lower()] = (state, reason)

    def record_result(self, identity: str, state: AvailabilityState, reason: str = "", is_account_level: bool = True):
        if state == AvailabilityState.AUTH:
            self.trip(identity, state, reason or state.value)
        elif state == AvailabilityState.BALANCE and is_account_level:
            self.trip(identity, state, reason or state.value)
        elif state == AvailabilityState.RATE_LIMIT:
            self.trip(identity, state, reason or "Severe rate limiting")

    def is_tripped(self, identity: str) -> bool:
        return identity.lower() in self._tripped

    def get_inherited_state(self, identity: str) -> Optional[AvailabilityState]:
        match = self._tripped.get(identity.lower())
        return match[0] if match else None

    def get_inherited_reason(self, identity: str) -> str:
        match = self._tripped.get(identity.lower())
        return match[1] if match else ""

    def reset(self):
        self._tripped.clear()

class ScannerWorker:
    def __init__(
        self,
        client: RouterClient,
        cache: HealthCache,
        global_concurrency: int = DEFAULT_GLOBAL_CONCURRENCY,
        per_provider_concurrency: int = DEFAULT_PER_PROVIDER_CONCURRENCY,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ):
        self.client = client
        self.cache = cache
        self.global_concurrency = global_concurrency
        self.per_provider_concurrency = per_provider_concurrency
        self.transport = transport
        self.fast_timeout = DEFAULT_FAST_TIMEOUT_SEC
        self.slow_timeout = DEFAULT_SLOW_TIMEOUT_SEC

        self._cancelled = False
        self._is_running = False
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self.circuit_breaker = ProviderCircuitBreaker()
        self._tripped_providers = self.circuit_breaker._tripped
        self._provider_backoffs: Dict[str, float] = {}  # identity -> resume_timestamp
        self._active_tasks: Set[asyncio.Task] = set()

        # Callbacks
        self.on_probe_started: Optional[Callable[[str], None]] = None
        self.on_probe_pending: Optional[Callable[[str, float], None]] = None
        self.on_probe_finished: Optional[Callable[[str, ModelHealthRecord], None]] = None
        self.on_progress: Optional[Callable[[int, int], None]] = None
        self.on_scan_completed: Optional[Callable[[str], None]] = None
        self.on_scan_failed: Optional[Callable[[str], None]] = None

    def cancel(self):
        """Thread-safe cooperative cancellation."""
        self._cancelled = True
        loop = self._loop
        if loop and loop.is_running():
            try:
                loop.call_soon_threadsafe(self._cancel_active_tasks_threadsafe)
            except RuntimeError:
                pass

    def _cancel_active_tasks_threadsafe(self):
        """Cancels tasks from within the loop thread."""
        for t in list(self._active_tasks):
            if not t.done():
                t.cancel()

    def is_cancelled(self) -> bool:
        return self._cancelled

    def is_running(self) -> bool:
        return self._is_running

    def run_scan(
        self,
        models: List[DiscoveredModel],
        mode: ScanMode = ScanMode.QUICK,
        target_combo_models: Optional[List[str]] = None,
    ):
        """Entry point executed in worker thread: runs the asyncio scan loop."""
        self._cancelled = False
        self._is_running = True
        self.circuit_breaker.reset()
        self._provider_backoffs.clear()
        self._active_tasks.clear()
        terminal_status = "COMPLETED"
        failure_error = None

        try:
            asyncio.run(self._run_async(models, mode, target_combo_models))
            if self._cancelled:
                terminal_status = "CANCELLED"
            else:
                terminal_status = "COMPLETED"
        except asyncio.CancelledError:
            terminal_status = "CANCELLED"
        except Exception as ex:
            terminal_status = "FAILED"
            # Unexpected internal scanner failure: redact before surfacing to UI/log
            failure_error = redact_secrets(str(ex))
        finally:
            self._is_running = False
            self.cache.save()
            if terminal_status == "FAILED" and self.on_scan_failed:
                self.on_scan_failed(failure_error or "Unknown scanner failure")
            if self.on_scan_completed:
                try:
                    self.on_scan_completed(terminal_status)
                except TypeError:
                    self.on_scan_completed()

    async def _run_async(
        self,
        models: List[DiscoveredModel],
        mode: ScanMode,
        target_combo_models: Optional[List[str]],
    ):
        target_models = self._filter_scan_targets(models, mode, target_combo_models)
        total = len(target_models)
        if total == 0:
            return

        self._loop = asyncio.get_running_loop()
        try:
            completed_count = 0
            global_sem = asyncio.Semaphore(self.global_concurrency)
            provider_sems: Dict[str, asyncio.Semaphore] = {}

            def get_prov_sem(identity: str) -> asyncio.Semaphore:
                norm = identity.lower()
                if norm not in provider_sems:
                    provider_sems[norm] = asyncio.Semaphore(self.per_provider_concurrency)
                return provider_sems[norm]

            headers = self.client._get_headers()
            cli_token = self.client.get_cli_token()
            headers["x-9r-cli-token"] = cli_token

            limits = httpx.Limits(max_keepalive_connections=10, max_connections=20)
            client_kwargs: Dict[str, Any] = {
                "timeout": DEFAULT_SLOW_TIMEOUT_SEC,
                "limits": limits,
                # Local 9router credentials must never cross a redirect to
                # another host; never rely on httpx library defaults here.
                "follow_redirects": False,
                # A remote environment proxy must not receive local tokens.
                "trust_env": False,
            }
            if self.transport is not None:
                client_kwargs["transport"] = self.transport
            async with httpx.AsyncClient(**client_kwargs) as http_client:
                active_tasks: Set[asyncio.Task] = set()
                self._active_tasks = active_tasks

                async def probe_worker(m: DiscoveredModel):
                    nonlocal completed_count
                    if self._cancelled:
                        return None

                    conn_key = m.connection_id or m.provider_prefix.lower()

                    # 1. Circuit Breaker check
                    if self.circuit_breaker.is_tripped(conn_key):
                        tripped_state, tripped_reason = self.circuit_breaker._tripped[conn_key.lower()]
                        existing_rec = self.cache.get(m.canonical_id)
                        counters = existing_rec.counters if existing_rec else EvidenceCounters()
                        
                        conf = Confidence.CONFIG_ERROR if tripped_state in (AvailabilityState.AUTH, AvailabilityState.BALANCE) else Confidence.LIKELY_TEMPORARY
                        evidence = EvidenceRecord(
                            availability=tripped_state,
                            cost=CostState.UNKNOWN,
                            confidence=conf,
                            status_code=0,
                            latency_ms=0.0,
                            error_code="CIRCUIT_BREAKER",
                            reason=f"Provider circuit breaker tripped: {tripped_reason} (inherited)",
                            raw_error="Inherited from tripped provider breaker.",
                            counters=counters,
                            note="circuit breaker skipped",
                        )
                        rec = self.cache.record_evidence(m.canonical_id, m.provider_name, m.model_id, evidence, auto_save=False)
                        completed_count += 1
                        if self.on_probe_finished:
                            self.on_probe_finished(m.canonical_id, rec)
                        if self.on_progress:
                            self.on_progress(completed_count, total)
                        return rec

                    # 2. Check Provider Backoff (Retry-After)
                    resume_time = self._provider_backoffs.get(conn_key.lower(), 0.0)
                    delay = resume_time - time.time()
                    if delay > 0:
                        await asyncio.sleep(min(delay, 5.0))
                        if self._cancelled:
                            return None

                    # 3. Two-Stage Probing with concurrency limits
                    p_sem = get_prov_sem(conn_key)
                    async with global_sem:
                        if self._cancelled:
                            return None
                        async with p_sem:
                            if self._cancelled:
                                return None
                            rec = await self._probe_single_model(http_client, m, mode)
                            completed_count += 1
                            if self.on_probe_finished:
                                self.on_probe_finished(m.canonical_id, rec)
                            if self.on_progress:
                                self.on_progress(completed_count, total)
                            if completed_count % 10 == 0:
                                self.cache.save()
                            return rec

                # Organize models into per-connection queues for fair scheduling
                conn_queues: Dict[str, deque[DiscoveredModel]] = {}
                for m in target_models:
                    c_key = (m.connection_id or m.provider_prefix).lower()
                    conn_queues.setdefault(c_key, deque()).append(m)

                in_flight_per_conn: Dict[str, int] = {}
                active_conns = list(conn_queues.keys())
                conn_rr_idx = 0

                async def probe_worker_wrapper(m: DiscoveredModel, c_key: str):
                    try:
                        return await probe_worker(m)
                    finally:
                        in_flight_per_conn[c_key] = max(0, in_flight_per_conn.get(c_key, 1) - 1)

                # Bounded fair task scheduling loop: enforces global_concurrency and prevents head-of-line blocking
                while True:
                    if self._cancelled:
                        self._cancel_active_tasks_threadsafe()
                        break

                    # Schedule round-robin across available connections
                    while len(active_tasks) < self.global_concurrency and active_conns:
                        scheduled = False
                        num_conns = len(active_conns)
                        for _ in range(num_conns):
                            if conn_rr_idx >= len(active_conns):
                                conn_rr_idx = 0
                            c_key = active_conns[conn_rr_idx]
                            conn_rr_idx = (conn_rr_idx + 1) % len(active_conns)

                            cur_inflight = in_flight_per_conn.get(c_key, 0)
                            if cur_inflight < self.per_provider_concurrency:
                                q = conn_queues[c_key]
                                m = q.popleft()
                                if not q:
                                    del conn_queues[c_key]
                                    active_conns.remove(c_key)
                                    if active_conns:
                                        conn_rr_idx = conn_rr_idx % len(active_conns)

                                in_flight_per_conn[c_key] = cur_inflight + 1
                                task = asyncio.create_task(probe_worker_wrapper(m, c_key))
                                active_tasks.add(task)
                                scheduled = True
                                break

                        if not scheduled:
                            # All active connections are saturated or no models can be scheduled
                            break

                    if not active_tasks:
                        break

                    done, pending = await asyncio.wait(active_tasks, return_when=asyncio.FIRST_COMPLETED)
                    active_tasks.difference_update(done)
                    for t in done:
                        try:
                            t.result()
                        except asyncio.CancelledError:
                            pass
                        except Exception:
                            # Unexpected internal worker failure: cancel remaining active tasks and propagate
                            self._cancel_active_tasks_threadsafe()
                            raise
        finally:
            self._loop = None

    async def _probe_single_model(
        self,
        http_client: httpx.AsyncClient,
        m: DiscoveredModel,
        mode: ScanMode,
    ) -> ModelHealthRecord:
        cid = m.canonical_id
        if self.on_probe_started:
            self.on_probe_started(cid)

        existing = self.cache.get(cid)
        prev_counters = existing.counters if existing else EvidenceCounters()
        cost_override = existing.cost_override if existing else None
        cost_hint = getattr(m, "cost_hint", None)

        url = f"{self.client.base_url}/api/models/test"
        headers = self.client._get_headers()
        headers["x-9r-cli-token"] = self.client.get_cli_token()
        payload = {"model": cid, "kind": "llm"}

        start_t = time.time()

        # -------------------------------------------------------------
        # STAGE 1: FAST PASS (Budget: DEFAULT_FAST_TIMEOUT_SEC)
        # -------------------------------------------------------------
        fast_budget = DEFAULT_FAST_TIMEOUT_SEC
        slow_needed = False
        res = None
        raw_text = ""
        parsed_json = None
        status_code = 0
        is_timeout = False

        try:
            res = await asyncio.wait_for(
                http_client.post(url, headers=headers, json=payload),
                timeout=fast_budget,
            )
            status_code = res.status_code
            raw_text = res.text
            try:
                parsed_json = res.json()
            except Exception:
                parsed_json = None
        except (asyncio.TimeoutError, TimeoutError, httpx.TimeoutException):
            if mode in (ScanMode.FULL, ScanMode.COMBO) or m.is_combo_member:
                slow_needed = True
            else:
                is_timeout = True
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.TransportError, httpx.NetworkError) as net_err:
            status_code = 503
            raw_text = f"Network transport error: {net_err}"
            parsed_json = {"error": {"code": "network_error", "message": str(net_err)}}
        except asyncio.CancelledError:
            raise
        except Exception as ex:
            status_code = 500
            raw_text = str(ex)
            parsed_json = {"error": {"code": "probe_exception", "message": str(ex)}}

        # -------------------------------------------------------------
        # STAGE 2: SLOW PASS (Budget: DEFAULT_SLOW_TIMEOUT_SEC)
        # -------------------------------------------------------------
        if slow_needed and not self._cancelled:
            elapsed_so_far = time.time() - start_t
            if self.on_probe_pending:
                self.on_probe_pending(cid, elapsed_so_far)

            remaining_budget = max(DEFAULT_SLOW_TIMEOUT_SEC - elapsed_so_far, 4.0)
            try:
                res = await asyncio.wait_for(
                    http_client.post(url, headers=headers, json=payload),
                    timeout=remaining_budget,
                )
                status_code = res.status_code
                raw_text = res.text
                try:
                    parsed_json = res.json()
                except Exception:
                    parsed_json = None
            except (asyncio.TimeoutError, TimeoutError, httpx.TimeoutException):
                is_timeout = True
            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.TransportError, httpx.NetworkError) as net_err:
                status_code = 503
                raw_text = f"Network transport error: {net_err}"
                parsed_json = {"error": {"code": "network_error", "message": str(net_err)}}
            except asyncio.CancelledError:
                raise
            except Exception as ex:
                status_code = 500
                raw_text = str(ex)
                parsed_json = {"error": {"code": "probe_exception", "message": str(ex)}}

        elapsed_ms = (time.time() - start_t) * 1000.0
        response_headers = dict(res.headers) if res is not None else {}

        # Parse Retry-After on 429
        conn_key = m.connection_id or m.provider_prefix.lower()
        if res is not None and res.status_code == 429:
            retry_after_hdr = res.headers.get("retry-after")
            backoff_sec = 8.0
            if retry_after_hdr:
                try:
                    backoff_sec = min(max(float(retry_after_hdr), 2.0), 30.0)
                except Exception:
                    pass
            self._provider_backoffs[conn_key.lower()] = time.time() + backoff_sec

        evidence = classify_probe_result(
            status_code=status_code,
            latency_ms=elapsed_ms,
            raw_body=raw_text,
            parsed_json=parsed_json,
            provider_prefix=m.provider_prefix,
            model_id=m.model_id,
            previous_counters=prev_counters,
            cost_override=cost_override,
            cost_hint=cost_hint,
            is_timeout=is_timeout,
            response_headers=response_headers,
        )

        # Provider / Connection Circuit Breaker Trip condition
        if evidence.availability == AvailabilityState.AUTH and evidence.counters.consecutive_auth >= 2:
            self.circuit_breaker.trip(conn_key, AvailabilityState.AUTH, "Repeated invalid API key / authentication rejection")
        elif evidence.availability == AvailabilityState.BALANCE:
            combined_err = f"{evidence.error_code} {evidence.reason} {evidence.raw_error}".lower()
            is_model_specific = bool(re.search(r'\b(model\s+quota|model\s+rate|per\s+model|rpm|tpm)\b', combined_err))
            if not is_model_specific:
                self.circuit_breaker.trip(conn_key, AvailabilityState.BALANCE, "Account quota or balance exhaustion")
        elif evidence.availability == AvailabilityState.RATE_LIMIT and evidence.counters.consecutive_rate_limit >= 3:
            self.circuit_breaker.trip(conn_key, AvailabilityState.RATE_LIMIT, "Severe consecutive rate limiting")

        return self.cache.record_evidence(cid, m.provider_name, m.model_id, evidence, auto_save=False)

    def _filter_scan_targets(
        self,
        models: List[DiscoveredModel],
        mode: ScanMode,
        target_combo_models: Optional[List[str]],
    ) -> List[DiscoveredModel]:
        """Prioritizes active combo models and filters targets based on mode and TTL."""
        if mode == ScanMode.COMBO and target_combo_models:
            combo_set = set(target_combo_models)
            return [m for m in models if m.canonical_id in combo_set]

        if mode == ScanMode.FAILED_ONLY:
            filtered = []
            for m in models:
                rec = self.cache.get(m.canonical_id)
                if not rec or not rec.is_healthy():
                    filtered.append(m)
            return filtered

        if mode == ScanMode.QUICK:
            filtered = []
            for m in models:
                rec = self.cache.get(m.canonical_id)
                if m.is_combo_member:
                    filtered.append(m)
                elif not rec or not rec.is_healthy() or not rec.is_fresh(ttl_seconds=300.0):
                    filtered.append(m)
            filtered.sort(key=lambda m: (not m.is_combo_member, m.provider_prefix, m.model_id))
            return filtered

        sorted_all = list(models)
        sorted_all.sort(key=lambda m: (not m.is_combo_member, m.provider_prefix, m.model_id))
        return sorted_all

    _filter_candidates = _filter_scan_targets
