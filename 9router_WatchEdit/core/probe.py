"""
9router_WatchEdit - Asynchronous Two-Stage Health Probe & Scanner Engine
Executes cancellable async tests with httpx.AsyncClient, strict per-connection concurrency,
account-level circuit breaking, Retry-After backoff, and two-stage (Fast -> Slow) probing.
"""
import asyncio
from collections import deque
import datetime
import email.utils
from enum import Enum
import heapq
import itertools
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
    REASONING_MODEL_TIMEOUT_SEC,
    REASONING_MODEL_PATTERNS,
    PENDING_THRESHOLD_SEC,
    redact_secrets,
)
from core.router_client import RouterClient
from core.security import LOCKED, LiveAccessLockedError
from core.discovery import DiscoveredModel
from core.history import HealthCache, HealthCachePersistenceError, ModelHealthRecord
from core.classification import (
    AvailabilityState,
    CostState,
    Confidence,
    classify_probe_result,
    EvidenceRecord,
    EvidenceCounters,
)


# ---------------------------------------------------------------------------
# PERF-001 (T-2 / SRC-001:R0005): provider Retry-After deadlines
# ---------------------------------------------------------------------------
# The server's own deadline is authoritative and is honored IN FULL: no local
# ceiling may shorten it. The audited defect was a 30s cap on a 2-30s server
# range plus a `min(delay, 5.0)` sleep that fired the request anyway 5s later.
MIN_RETRY_AFTER_SEC = 2.0
DEFAULT_RETRY_AFTER_SEC = 8.0
#: Bounded re-check tick while waiting on the delay heap. It exists only to
#: keep cancellation and the live-access lock responsive; the heap owns the
#: actual resume ordering, so this tick never shortens a provider deadline.
RETRY_RESUME_TICK_SEC = 0.25

#: PERF-004 (SRC-001:R0013): at most this many MID-SCAN cache checkpoints are
#: written per scan, regardless of model count. Each checkpoint rewrites the
#: whole cache (O(N) bytes), so capping the COUNT makes total persistence
#: volume O(N) instead of the audited O(N^2). The terminal save is separate.
#: 20 preserves the old every-10 cadence for scans up to 200 models.
MAX_MIDSCAN_CHECKPOINTS = 20

#: Sentinel returned by an already-scheduled task that discovers its connection
#: entered backoff before transport: the scheduler parks that model on the delay
#: heap instead of issuing a request. Deliberately NOT evidence of any kind --
#: it is neither a completed probe nor a cancellation.
_DEFERRED = object()


def parse_retry_after(value: Optional[str]) -> Optional[float]:
    """Parse a Retry-After header into seconds, honoring the full deadline.

    Accepts delay-seconds (the common case) and an HTTP-date, as the RFC allows.
    Returns None when the header is absent or unparseable so the caller keeps
    its documented default. A value below MIN_RETRY_AFTER_SEC is raised to it;
    there is deliberately NO upper clamp.
    """
    text = (value or "").strip()
    if not text:
        return None
    seconds: Optional[float] = None
    try:
        seconds = float(text)
    except ValueError:
        seconds = None
    if seconds is None:
        try:
            when = email.utils.parsedate_to_datetime(text)
        except Exception:
            return None
        if when is None:
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=datetime.timezone.utc)
        seconds = (when - datetime.datetime.now(datetime.timezone.utc)).total_seconds()
    if seconds != seconds or seconds in (float("inf"), float("-inf")):
        return None
    return max(seconds, MIN_RETRY_AFTER_SEC)


async def _sleep(seconds: float) -> None:
    """Single sleep seam for the delay-heap wait (PERF-001).

    Tests drive a virtual clock through this one function, so a 30s server
    deadline is provable without spending 30s of wall time.
    """
    await asyncio.sleep(seconds)


class ScanMode(str, Enum):
    QUICK = "QUICK"
    FULL = "FULL"
    FAILED_ONLY = "FAILED_ONLY"
    COMBO = "COMBO"

def is_reasoning_model(model_id: str) -> bool:
    """Check if model is a reasoning model that needs extended timeout."""
    for pattern in REASONING_MODEL_PATTERNS:
        if pattern.search(model_id):
            return True
    return False

class ScanSessionExecution:
    """W2-001 (B1): ONE session's execution context — a session-local lease
    that is the sole owner of that session's mutable execution state.

    The state that used to live in ScannerWorker-wide slots lives HERE, per
    session: the cancellation event, the asyncio loop that runs the session,
    and the set of asyncio tasks belonging to it.

    Why this matters (the forced handover race): a cancel request resolves
    the context object of the session it was accepted for and mutates ONLY
    that object. A G1 cancel that resumes after G2 has claimed the scanner
    therefore holds G1's own event/loop/task set — it is *physically
    incapable* of touching G2's cancellation state, loop, or tasks, with no
    post-mutation re-check needed.
    """

    __slots__ = (
        "session_id",
        "_cancel_event",
        "_loop",
        "_tasks",
        "_released",
        "_lock",
    )

    def __init__(self, session_id: int):
        self.session_id = session_id
        self._cancel_event = threading.Event()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._tasks: Set[asyncio.Task] = set()
        self._released = False
        self._lock = threading.Lock()

    # ------------------------------------------------------ session cancel
    def request_cancel(self) -> None:
        """Mark THIS session cancelled (accepted cancels only)."""
        self._cancel_event.set()

    @property
    def cancelled(self) -> bool:
        return self._cancel_event.is_set()

    # ------------------------------------------------- loop/task ownership
    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        with self._lock:
            self._loop = loop

    def clear_loop(self) -> None:
        with self._lock:
            self._loop = None

    def owned_loop(self) -> Optional[asyncio.AbstractEventLoop]:
        with self._lock:
            return self._loop

    def task_set(self) -> Set[asyncio.Task]:
        """The live task set OF THIS SESSION (read/introspection view)."""
        return self._tasks

    def register_task(self, task: asyncio.Task) -> None:
        self._tasks.add(task)

    def bind_tasks(self, tasks: Set[asyncio.Task]) -> None:
        """Bind the concrete task set the owning scheduler work loop owns.

        The set object belongs to THIS session: nothing outside this context
        ever reads or cancels it, so a stale session cannot touch a newer
        session's in-flight tasks.
        """
        self._tasks = tasks

    def discard_task(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)

    def cancel_tasks_on_loop(self) -> None:
        """Runs ON this session's own loop thread: cancel its tasks."""
        for t in list(self._tasks):
            if not t.done():
                t.cancel()

    def cancel_tasks(self) -> None:
        """Thread-safe cancellation addressed to THIS session's own loop.

        A stale/old context can only ever reach its own loop: a newer
        session's loop is unreachable from here by construction.
        """
        loop = self.owned_loop()
        if loop is None or not loop.is_running():
            return
        try:
            loop.call_soon_threadsafe(self.cancel_tasks_on_loop)
        except RuntimeError:
            pass

    # --------------------------------------------------------------- lease
    @property
    def released(self) -> bool:
        with self._lock:
            return self._released

    def release(self) -> bool:
        """Terminal lease release: exactly once per session."""
        with self._lock:
            if self._released:
                return False
            self._released = True
            self._loop = None
        self._tasks.clear()
        return True


class ScanSessionController:
    """W2-001: explicit lifecycle owner for scanner sessions.

    Owns: the monotonically increasing session id source, the ACTIVE session
    id (execution ownership, only while work really executes), the LATEST
    claimed session id (publication generation, which survives execution
    release), the closing flag, the owned worker thread handle, the
    per-session cancellation flag, and the registry of live session
    execution contexts.

    Contract:
    - A session is claimed ATOMICALLY (try_claim) BEFORE its worker thread is
      spawned. There is no check-then-spawn window: at most one ScannerWorker
      session can ever be active (max active sessions == 1).
    - Ownership releases exactly once, and only after the worker session has
      really terminated (release_session is idempotent; only the session
      holding the lease can release it).
    - Cancellation is session-specific: cancel() targets exactly the session
      id it is given, and mutates exactly that session's context. A stale
      cancel for session N cannot cancel N+1.
    - Execution ownership (active_session) and publication generation
      (latest_session) are DIFFERENT lifetimes: releasing the execution lease
      does not invalidate the newest generation's queued callbacks, and
      claiming a newer session stales them immediately.
    - close() blocks new sessions permanently and exposes the owned thread
      handle for a bounded join.
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._session_counter = 0
        self._active_session: Optional[int] = None
        # Newest session ever successfully claimed. NEVER cleared by a
        # normal release (B2): queued callbacks of the newest completed
        # session stay acceptable after its execution lease is gone.
        self._latest_session: Optional[int] = None
        self._cancel_requested: bool = False
        self._worker_thread: Optional[threading.Thread] = None
        self._closing: bool = False
        # Live per-session execution contexts, keyed by session id. Entries
        # exist only while a session executes; a stale session has no
        # reachable context, so an old cancel cannot address a new session.
        self._executions: Dict[int, ScanSessionExecution] = {}

    # ------------------------------------------------------------- claiming
    def try_claim(self) -> Optional[int]:
        """Atomically claim a new session id.

        Returns the new session id, or None when a session is already active
        or the controller is closing. Called BEFORE the worker thread exists:
        the ownership check and the ownership claim are one atomic step.
        """
        with self._lock:
            if self._closing or self._active_session is not None:
                return None
            self._session_counter += 1
            self._active_session = self._session_counter
            # The newly claimed session is immediately the publication
            # generation: any queued callback of an older session is stale
            # from this instant onward.
            self._latest_session = self._active_session
            self._cancel_requested = False
            return self._active_session

    # -------------------------------------------------- execution contexts
    def register_execution(self, execution: "ScanSessionExecution") -> bool:
        """Bind a session's execution context at the START of its run.

        Returns False (no state change) when the controller is closing or the
        given session no longer owns the scanner: a run that lost its lease
        must not execute.
        """
        with self._lock:
            if self._closing or self._active_session != execution.session_id:
                return False
            self._executions[execution.session_id] = execution
            return True

    def execution_for(self, session_id: int) -> Optional["ScanSessionExecution"]:
        with self._lock:
            return self._executions.get(session_id)

    def accept_cancel(self, session_id: Optional[int]) -> Tuple[bool, Optional["ScanSessionExecution"]]:
        """Atomically accept a cancel request for ONE session and resolve
        that session's own execution context.

        Returns (accepted, context). The context is the object the caller may
        mutate; the caller must never touch worker-wide state afterwards.
        """
        with self._lock:
            active = self._active_session
            if active is None:
                return (False, None)
            if session_id is not None and session_id != active:
                return (False, None)
            self._cancel_requested = True
            return (True, self._executions.get(active))

    # ---------------------------------------------------------- ownership
    def bind_thread(self, thread: threading.Thread) -> bool:
        """Register the spawned worker thread for the active session.

        Must be called by the claiming owner immediately after Thread creation
        and before/just after start(). Returns False (no state change) if the
        controller was closed concurrently; the caller must then not start
        the thread.
        """
        with self._lock:
            if self._closing:
                return False
            self._worker_thread = thread
            return True

    def release_session(self, session_id: int) -> bool:
        """Release ownership for a terminated session — exactly once.

        Only the session currently holding the lease can release it; a stale
        release from an older session never clears a newer session's state.
        Returns True when this call actually released the lease.
        """
        with self._lock:
            if self._active_session != session_id:
                return False
            self._active_session = None
            self._cancel_requested = False
            self._worker_thread = None
            # The session's execution context becomes unreachable AND dead at
            # the same instant: no later cancel can address it, and a lease
            # abandoned without its own finalizer is marked released so it can
            # never mutate anything afterwards. The publication generation
            # (latest_session) deliberately does NOT change here.
            execution = self._executions.pop(session_id, None)
            if execution is not None:
                execution.release()
            return True

    # ------------------------------------------------------- cancellation
    def request_cancel(self, session_id: int) -> bool:
        """Request cancellation of EXACTLY one session.

        Returns True when the request was accepted (the given session is the
        currently owned one). A stale cancel for any other session id is a
        no-op and returns False.
        """
        with self._lock:
            if self._active_session != session_id:
                return False
            self._cancel_requested = True
            return True

    def is_cancel_requested(self, session_id: int) -> bool:
        with self._lock:
            return self._active_session == session_id and self._cancel_requested

    # ------------------------------------------------------------ closing
    def close(self) -> None:
        """Enter the closing state: no new sessions may be claimed."""
        with self._lock:
            self._closing = True

    def owned_thread(self) -> Optional[threading.Thread]:
        with self._lock:
            return self._worker_thread

    # ---------------------------------------------------------- observers
    @property
    def running(self) -> bool:
        with self._lock:
            return self._active_session is not None

    @property
    def active_session(self) -> Optional[int]:
        with self._lock:
            return self._active_session

    @property
    def latest_session(self) -> Optional[int]:
        """Newest session ever successfully claimed (publication generation).

        Survives execution release by design: an execution lifetime and a
        publication-generation lifetime are different things.
        """
        with self._lock:
            return self._latest_session

    def is_latest_session(self, session_id: Optional[int]) -> bool:
        """Callback-acceptance gate: is this the newest claimed generation?"""
        with self._lock:
            return session_id is not None and session_id == self._latest_session

    @property
    def session_counter(self) -> int:
        """Monotonic session-id source (test evidence: ids never repeat)."""
        with self._lock:
            return self._session_counter

    @property
    def closing(self) -> bool:
        with self._lock:
            return self._closing

    @property
    def max_active_sessions(self) -> int:
        """Structural proof for the W2-001 stress gate."""
        return 1 if self._active_session is not None else 0


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
        # W2-001: maximum latency added to cancellation by the scheduler
        # loop's bounded wait (see _wait_or_cancel in _run_async).
        self.CANCEL_POLL_SEC = 0.1

        # W2-001 (B1) session ownership: the controller owns the single-session
        # lifecycle and the registry of live execution contexts. This worker
        # holds ONE reference to the context of the session it is currently
        # executing (None when idle) — never session-global cancellation/loop/
        # task slots. All mutable execution state is session-local by
        # construction, so a stale cancel cannot reach a newer session.
        self.session_controller = ScanSessionController()
        self._execution: Optional[ScanSessionExecution] = None
        # Terminal outcome view of the LAST terminated session (observability
        # only: written once at release, never used to decide anything).
        self._last_terminated_cancelled = False
        # W2-005 pending persistence failure of the CURRENT run (mid-scan
        # saves are non-fatal but must block a plain COMPLETED at the end).
        self._pending_persistence_failure: Optional[str] = None
        self.circuit_breaker = ProviderCircuitBreaker()
        self._tripped_providers = self.circuit_breaker._tripped
        self._provider_backoffs: Dict[str, float] = {}  # identity -> resume_timestamp
        self.client.security.on_state_changed(self._on_security_state_changed)

        # Callbacks. W2-001: every callback that can mutate UI is attributed
        # to a session id so stale generations can be rejected by consumers.
        self.on_probe_started: Optional[Callable[[int, str], None]] = None
        self.on_probe_pending: Optional[Callable[[int, str, float], None]] = None
        self.on_probe_finished: Optional[Callable[[int, str, ModelHealthRecord], None]] = None
        self.on_progress: Optional[Callable[[int, int, int], None]] = None
        self.on_scan_completed: Optional[Callable[[int, str], None]] = None
        self.on_scan_failed: Optional[Callable[[int, str], None]] = None

    # ------------------------------------------- session-local state views
    # Read-only views over the CURRENT execution context. They exist for
    # introspection/back-compat; the authoritative state lives in the context
    # object so no session can observe or mutate another session's state.
    @property
    def _session(self) -> Optional[int]:
        ex = self._execution
        return ex.session_id if ex is not None else None

    @property
    def _cancelled(self) -> bool:
        ex = self._execution
        return ex.cancelled if ex is not None else self._last_terminated_cancelled

    @property
    def _loop(self) -> Optional[asyncio.AbstractEventLoop]:
        ex = self._execution
        return ex.owned_loop() if ex is not None else None

    @property
    def _active_tasks(self) -> Set[asyncio.Task]:
        ex = self._execution
        return ex.task_set() if ex is not None else set()

    # ------------------------------------------------- session lifecycle
    def try_start_session(self) -> Optional[int]:
        """Atomically reserve the single scanner session BEFORE any thread is
        spawned. Returns the session token, or None when a session is active
        or the controller is closing."""
        return self.session_controller.try_claim()

    # ---------------------------------------------- session-stamped emitters
    # Every UI-mutable callback is attributed to the owning session id so
    # consumers can reject stale generations (W2-001). Legacy 2-arg/1-arg
    # callbacks (engine tests, CLI) keep working through the TypeError
    # fallback, which strips the session id.
    def _emit_probe_started(self, canonical_id: str) -> None:
        cb = self.on_probe_started
        if not cb:
            return
        try:
            cb(self._session, canonical_id)
        except TypeError:
            cb(canonical_id)

    def _emit_probe_pending(self, canonical_id: str, elapsed_sec: float) -> None:
        cb = self.on_probe_pending
        if not cb:
            return
        try:
            cb(self._session, canonical_id, elapsed_sec)
        except TypeError:
            cb(canonical_id, elapsed_sec)

    def _emit_probe_finished(self, canonical_id: str, record) -> None:
        cb = self.on_probe_finished
        if not cb:
            return
        try:
            cb(self._session, canonical_id, record)
        except TypeError:
            cb(canonical_id, record)

    def _emit_progress(self, completed: int, total: int) -> None:
        cb = self.on_progress
        if not cb:
            return
        try:
            cb(self._session, completed, total)
        except TypeError:
            cb(completed, total)

    def _session_is_active(self) -> bool:
        """True only while THIS worker's execution context still owns the
        scanner (emission gate)."""
        ex = self._execution
        if ex is None:
            return False
        return self.session_controller.active_session == ex.session_id

    def _on_security_state_changed(self, state: str) -> None:
        """Lock Now immediately cancels the active authorized session."""
        if state == LOCKED:
            self.cancel()

    def cancel(self, session_id: Optional[int] = None, wait_tasks: bool = True) -> bool:
        """Thread-safe, SESSION-SPECIFIC cooperative cancellation.

        With an explicit session_id the request is accepted only when that
        session is the currently owned one: a stale cancel for session N
        cannot cancel N+1. With no argument (Lock Now, Stop scan) the
        CURRENTLY owned session is cancelled. Returns True when a live
        session received the cancellation.

        The controller accepts the request AND resolves the target session's
        own execution context in one atomic step. Everything after that lock
        release mutates ONLY the captured context: no worker-wide cancel
        flag, loop, or task set is touched, so a G1 cancel resuming after G2
        has claimed the scanner cannot reach G2 in any way.
        """
        accepted, execution = self.session_controller.accept_cancel(session_id)
        if not accepted:
            return False
        if execution is not None:
            execution.request_cancel()
            execution.cancel_tasks()
        return True

    def is_cancelled(self) -> bool:
        """Cancellation state of the session in view: the live state of the
        ACTIVE execution while one runs, otherwise the terminal outcome of the
        last terminated session. Never another session's live state."""
        return self._cancelled

    def is_running(self) -> bool:
        """True only while a session owns the scanner (controller-backed)."""
        return self.session_controller.running

    def run_scan(
        self,
        models: List[DiscoveredModel],
        mode: ScanMode = ScanMode.QUICK,
        target_combo_models: Optional[List[str]] = None,
        session_id: Optional[int] = None,
        execution: Optional[ScanSessionExecution] = None,
    ) -> Optional[str]:
        """Entry point executed in the owned worker thread for exactly ONE
        claimed session: EXECUTE, then return the terminal status.

        W2-001 (B3) — ONE authoritative release owner per lease:
        * This method EXECUTES: it runs the asyncio scan loop, performs the
          terminal cache persistence, emits the terminal callbacks and
          returns the terminal result. It never releases a lease it did not
          claim.
        * A caller that owns a lease (MainWindow's worker wrapper, via
          ``claim_execution()``) is the authoritative releaser: it must call
          ``end_execution(lease)`` exactly once after this method returns.
        * A bare call with no token and no lease claims the lease itself, so
          this call is the authoritative releaser for that run (CLI / direct
          engine callers).
        * A bare ``session_id`` (legacy direct caller that claimed the token
          itself) is the same rule: the claimer owns the release, and
          ``run_scan_owned`` is the explicit claim/run/release wrapper for
          callers that simply want to run a scan.
        * A failed terminal cache save (W2-005) can never prevent terminal
          status delivery or the owner's ownership cleanup.
        """
        lease = execution
        owns_lease = False
        try:
            if lease is None:
                lease = self._begin_execution(session_id)
                if lease is None:
                    # Stale token, session already owned, or closing: no state
                    # reset, no callbacks, no run.
                    return None
                # This call claimed the lease, so this call releases it.
                owns_lease = session_id is None
            elif not self._bind_claim(lease, session_id):
                # Lost ownership before the run started: nothing is executed
                # and no state is touched; the lease owner still releases it.
                return None
            return self._execute_session(lease, models, mode, target_combo_models)
        finally:
            if owns_lease and lease is not None:
                self.end_execution(lease)

    # -------------------------------------------------- execution lifecycle
    def claim_execution(self) -> Optional[ScanSessionExecution]:
        """ATOMICALLY claim the single scanner session and create its
        session-local execution context (the lease).

        Called BEFORE any worker thread is spawned, so the claim, the session
        token and the session's own cancellation/loop/task context all exist
        atomically — a cancel can address the right context from the first
        instant. The returned lease is OWNED by the caller: it must be passed
        to run_scan and released exactly once through ``end_execution``.
        Returns None when a session is already active or the controller is
        closing.
        """
        controller = self.session_controller
        session_id = controller.try_claim()
        if session_id is None:
            return None
        lease = ScanSessionExecution(session_id)
        if not controller.register_execution(lease):
            # The controller closed between claim and registration: give the
            # claim back so the start gate never wedges.
            controller.release_session(session_id)
            return None
        return lease

    def end_execution(self, lease: ScanSessionExecution) -> bool:
        """THE single authoritative release transition for ONE lease.

        Called exactly once by the lease OWNER after run_scan has returned
        (terminal persistence and terminal callback emission included).
        Idempotent by construction (the context owns a released flag) and
        scoped to the session that actually owns the lease: release_session
        refuses a token that no longer matches the active session.
        """
        if not lease.release():
            return False
        # Observability-only terminal view of the terminated session.
        self._last_terminated_cancelled = lease.cancelled
        self._pending_persistence_failure = None
        if self._execution is lease:
            self._execution = None
        return self.session_controller.release_session(lease.session_id)

    def abandon_active_execution(self) -> bool:
        """Fail-closed sweep for the rare case where an owned thread died
        outside its own finalizer: release whatever lease the controller still
        considers active.

        NOT a competing release owner — it is never used on the normal path,
        it is token-scoped and idempotent, and it marks the abandoned context
        dead so it can never mutate a future session.
        """
        controller = self.session_controller
        active = controller.active_session
        if active is None:
            return False
        if self._execution is not None and self._execution.session_id == active:
            self._execution = None
        # release_session marks the context dead as well.
        return controller.release_session(active)

    def _bind_claim(
        self,
        lease: ScanSessionExecution,
        session_id: Optional[int],
    ) -> bool:
        """Bind a lease created by claim_execution() to this run.

        Returns False (no state change) when the token no longer owns the
        scanner, e.g. an accepted close released it first. The lease owner
        still releases its own lease in that case.
        """
        controller = self.session_controller
        if session_id is not None and session_id != lease.session_id:
            return False
        if controller.active_session != lease.session_id:
            return False
        # Start state comes from the session-scoped cancellation view: a lock
        # that happened before the run, or an accepted cancel for THIS session.
        if (
            not self.client.security.is_live_allowed()
            or controller.is_cancel_requested(lease.session_id)
        ):
            lease.request_cancel()
        self._execution = lease
        return True

    def _begin_execution(self, session_id: Optional[int]) -> Optional[ScanSessionExecution]:
        """Legacy/self-owned path: claim (when needed), register and bind the
        execution context of THIS session."""
        controller = self.session_controller
        if session_id is None:
            session_id = controller.try_claim()
            if session_id is None:
                # Another session owns the scanner: no state reset, no run.
                return None
        lease = ScanSessionExecution(session_id)
        if (
            not self.client.security.is_live_allowed()
            or controller.is_cancel_requested(session_id)
        ):
            lease.request_cancel()
        if not controller.register_execution(lease):
            # Stale/unknown token, or the controller closed concurrently: no
            # state reset, no callbacks, no run.
            return None
        self._execution = lease
        return lease

    def run_scan_owned(
        self,
        models: List[DiscoveredModel],
        mode: ScanMode = ScanMode.QUICK,
        target_combo_models: Optional[List[str]] = None,
    ) -> Optional[int]:
        """Explicit claim/run/release wrapper for legacy/direct callers that
        do not hold a lease: claims the single session ATOMICALLY, runs the
        scan, and releases that lease exactly once after run_scan returns.
        Returns the session id used, or None when the scanner is already owned
        / closing."""
        lease = self.claim_execution()
        if lease is None:
            return None
        try:
            self.run_scan(
                models, mode=mode, target_combo_models=target_combo_models,
                session_id=lease.session_id, execution=lease,
            )
        finally:
            self.end_execution(lease)
        return lease.session_id

    # ---------------------------------------- W2-005 cache persistence
    TERMINAL_STATUS_PERSISTENCE_FAILED = "COMPLETED_PERSISTENCE_FAILED"

    def _persist_cache(self) -> Optional[str]:
        """Exactly one explicit cache persistence attempt.

        Returns a redacted failure reason, or None when persistence verifiably
        succeeded. Any save-implementation failure counts, including a plain
        exception raised by a save() double: persistence is never assumed.
        """
        try:
            self.cache.save()
        except HealthCachePersistenceError as ex:
            return redact_secrets(str(ex))
        except Exception as ex:
            return redact_secrets(f"{type(ex).__name__}: {ex}")
        return None

    def _periodic_persist(self) -> None:
        """Mid-scan persistence policy (documented + tested): NON-FATAL.

        A periodic save failure must not throw away an otherwise good scan,
        but it IS recorded as a pending persistence failure so the final
        terminal status can never claim plain COMPLETED unless a later
        VERIFIED save succeeds and clears it.
        """
        self._pending_persistence_failure = self._persist_cache()

    def _execute_session(
        self,
        execution: ScanSessionExecution,
        models: List[DiscoveredModel],
        mode: ScanMode,
        target_combo_models: Optional[List[str]],
    ) -> str:
        """One owned session's body: scan, terminal persistence, terminal
        callbacks. Returns the terminal status (also delivered via callback).

        Terminal cache persistence is part of terminal scan correctness: a
        scan whose final state is not durable must never advertise a plain
        COMPLETED, and the failure must not be overwritten afterwards.
        """
        session_id = execution.session_id
        self.circuit_breaker.reset()
        self._provider_backoffs.clear()
        execution.task_set().clear()
        self._pending_persistence_failure = None
        terminal_status = "COMPLETED"
        failure_error = None

        try:
            asyncio.run(self._run_async(execution, models, mode, target_combo_models))
            if execution.cancelled:
                terminal_status = "LOCKED" if self.client.security.is_locked() else "CANCELLED"
            else:
                terminal_status = "COMPLETED"
        except asyncio.CancelledError:
            terminal_status = "LOCKED" if self.client.security.is_locked() else "CANCELLED"
        except Exception as ex:
            terminal_status = "FAILED"
            # Unexpected internal scanner failure: redact before surfacing to UI/log
            failure_error = redact_secrets(str(ex))
        finally:
            # Terminal cache persistence: attempted exactly once BEFORE the
            # terminal callbacks and BEFORE the execution-lease release.
            persistence_failure = self._persist_cache()
            if persistence_failure is None:
                # A VERIFIED save supersedes any earlier pending failure: the
                # whole final state is durable, so COMPLETED is honest.
                pending_failure = None
            else:
                pending_failure = self._pending_persistence_failure
            self._pending_persistence_failure = None
            if terminal_status == "COMPLETED" and (
                persistence_failure is not None or pending_failure is not None
            ):
                # W2-005 terminal semantics: the scan succeeded but its final
                # state is not durable — never a plain COMPLETED.
                terminal_status = self.TERMINAL_STATUS_PERSISTENCE_FAILED
                failure_error = (
                    "Scan results were NOT persisted: "
                    + (persistence_failure or pending_failure or "unknown persistence failure")
                )
            elif persistence_failure is not None and terminal_status == "FAILED":
                # Already terminal FAILED: keep it, and make the additional
                # persistence failure visible in the reason.
                failure_error = (failure_error or "Unknown scanner failure") + (
                    f" (terminal cache persistence also failed: {persistence_failure})"
                )
            if terminal_status in (
                "FAILED", self.TERMINAL_STATUS_PERSISTENCE_FAILED
            ) and self.on_scan_failed:
                reason = failure_error or "Unknown scanner failure"
                try:
                    self.on_scan_failed(session_id, reason)
                except TypeError:
                    self.on_scan_failed(reason)
            if self.on_scan_completed:
                try:
                    try:
                        self.on_scan_completed(session_id, terminal_status)
                    except TypeError:
                        self.on_scan_completed(terminal_status)
                except TypeError:
                    self.on_scan_completed()
        return terminal_status

    async def _run_async(
        self,
        execution: ScanSessionExecution,
        models: List[DiscoveredModel],
        mode: ScanMode,
        target_combo_models: Optional[List[str]],
    ):
        target_models = self._filter_scan_targets(models, mode, target_combo_models)
        total = len(target_models)
        if total == 0:
            return
        if execution.cancelled:
            # LOCKED/CANCELLED start: never build auth material, never connect.
            return

        execution.bind_loop(asyncio.get_running_loop())
        # PERF-004 (SRC-001:R0013): bounded mid-scan checkpoint count. The
        # audited defect saved every 10 completions, so a full O(N) rewrite
        # happened N/10 times => O(N^2) bytes written per scan. The interval is
        # the LARGER of the small-scan floor (10, preserving the documented
        # policy for small scans) and the batch size that caps a scan to at
        # most MAX_MIDSCAN_CHECKPOINTS mid-scan saves, so total persistence
        # volume is O(N) for every N. The terminal save is always separate.
        _checkpoint_interval = max(
            10, (total + MAX_MIDSCAN_CHECKPOINTS - 1) // MAX_MIDSCAN_CHECKPOINTS
        )
        try:
            completed_count = 0
            global_sem = asyncio.Semaphore(self.global_concurrency)
            provider_sems: Dict[str, asyncio.Semaphore] = {}

            def get_prov_sem(identity: str) -> asyncio.Semaphore:
                norm = identity.lower()
                if norm not in provider_sems:
                    provider_sems[norm] = asyncio.Semaphore(self.per_provider_concurrency)
                return provider_sems[norm]

            headers = None
            try:
                headers = self.client._get_headers()
                headers["x-9r-cli-token"] = self.client.get_cli_token()
            except LiveAccessLockedError:
                # Locked between scan start and auth material build: zero
                # transport requests, terminal LOCKED/CANCELLED, no evidence.
                execution.request_cancel()
                return

            # W2-001 cancellation responsiveness: the scheduler loop below
            # parks in asyncio.wait for up to CANCEL_POLL_SEC while in-flight
            # probes sit in blocking transport calls; a cancel must be seen
            # within that bound so Stop scan terminates promptly.
            async def _wait_or_cancel(tasks: Set[asyncio.Task]) -> None:
                done, pending = await asyncio.wait(
                    tasks, timeout=self.CANCEL_POLL_SEC,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if execution.cancelled:
                    for p in pending:
                        p.cancel()

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
                execution.bind_tasks(active_tasks)

                async def probe_worker(m: DiscoveredModel):
                    nonlocal completed_count
                    if execution.cancelled:
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
                        if self._session_is_active():
                            self._emit_probe_finished(m.canonical_id, rec)
                            self._emit_progress(completed_count, total)
                        return rec

                    # 2. Provider backoff (Retry-After) is enforced by the
                    # SCHEDULER, not by sleeping here: a connection inside its
                    # server deadline never receives a task at all, so waiting
                    # costs no scheduler capacity (PERF-001). This task therefore
                    # only ever runs for a connection that is ready to be probed.

                    # 3. Two-Stage Probing with concurrency limits
                    p_sem = get_prov_sem(conn_key)
                    async with global_sem:
                        if execution.cancelled:
                            return None
                        async with p_sem:
                            if execution.cancelled:
                                return None
                            # PERF-001: the connection may have entered backoff
                            # while this task waited for its slot (possible when
                            # per-provider concurrency exceeds 1). Do NOT issue a
                            # request, and do NOT wait here either -- report the
                            # model back to the scheduler's delay heap, which
                            # resumes it exactly at the provider's deadline.
                            if self._provider_resume_at(conn_key) > time.time():
                                return _DEFERRED
                            # Authoritative request boundary: every queued task
                            # re-checks live access immediately before transport.
                            # A late lock turns the post into no-evidence cancellation.
                            try:
                                self.client.security.require_live("probe_model_queued")
                            except LiveAccessLockedError:
                                execution.request_cancel()
                                return None
                            try:
                                rec = await self._probe_single_model(
                                    http_client, m, mode, headers, execution
                                )
                            except LiveAccessLockedError:
                                execution.request_cancel()
                                return None
                            if rec is None:
                                # Lock/cancel boundary: no evidence, no counting.
                                return None
                            completed_count += 1
                            if self._session_is_active():
                                self._emit_probe_finished(m.canonical_id, rec)
                                self._emit_progress(completed_count, total)
                            if completed_count % _checkpoint_interval == 0:
                                # W2-005: periodic persistence, non-fatal (the
                                # policy lives in one documented helper).
                                self._periodic_persist()
                            return rec

                # Organize models into per-connection queues for fair scheduling
                conn_queues: Dict[str, deque[DiscoveredModel]] = {}
                for m in target_models:
                    c_key = (m.connection_id or m.provider_prefix).lower()
                    conn_queues.setdefault(c_key, deque()).append(m)

                # PERF-001 (T-2 / SRC-001:R0005): ready vs delayed connection
                # queues. A connection the server told us to wait on is never
                # scheduled at all -- its models park in a min-heap keyed by that
                # connection's own resume time, so backoff waiting consumes NO
                # global and NO per-provider scheduler slot.
                delayed_heap: List[Tuple[float, int, str, DiscoveredModel]] = []
                delay_seq = itertools.count()

                in_flight_per_conn: Dict[str, int] = {}
                active_conns = list(conn_queues.keys())
                conn_rr_idx = 0

                async def probe_worker_wrapper(m: DiscoveredModel, c_key: str):
                    deferred = False
                    try:
                        result = await probe_worker(m)
                        deferred = result is _DEFERRED
                        return result
                    finally:
                        in_flight_per_conn[c_key] = max(0, in_flight_per_conn.get(c_key, 1) - 1)
                        if deferred:
                            self._park_deferred(
                                delayed_heap, delay_seq, c_key, m,
                                self._provider_resume_at(c_key),
                            )

                # Bounded fair task scheduling loop: enforces global_concurrency and prevents head-of-line blocking
                while True:
                    if execution.cancelled:
                        execution.cancel_tasks_on_loop()
                        break

                    # Resume every connection whose server deadline has passed.
                    self._resume_delayed(delayed_heap, conn_queues, active_conns)

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

                                if self._defer_for_backoff(delayed_heap, delay_seq, c_key, m):
                                    # Cooling down: park the model, hold no slot.
                                    continue

                                in_flight_per_conn[c_key] = cur_inflight + 1
                                task = asyncio.create_task(probe_worker_wrapper(m, c_key))
                                active_tasks.add(task)
                                scheduled = True
                                break

                        if not scheduled:
                            # All active connections are saturated or no models can be scheduled
                            break

                    if not active_tasks:
                        if not delayed_heap:
                            break
                        # Nothing runnable: wait on the heap's earliest resume time
                        # instead of failing the scan or parking a task asleep.
                        wait = delayed_heap[0][0] - time.time()
                        await _sleep(max(0.0, min(wait, RETRY_RESUME_TICK_SEC)))
                        if execution.cancelled:
                            execution.cancel_tasks_on_loop()
                            break
                        continue

                    await _wait_or_cancel(active_tasks)
                    if execution.cancelled:
                        execution.cancel_tasks_on_loop()
                        break
                    done = {t for t in active_tasks if t.done()}
                    active_tasks.difference_update(done)
                    for t in done:
                        try:
                            t.result()
                        except asyncio.CancelledError:
                            pass
                        except Exception:
                            # Unexpected internal worker failure: cancel remaining active tasks and propagate
                            execution.cancel_tasks_on_loop()
                            raise
        finally:
            execution.clear_loop()

    # ------------------------------------------------------------------
    # PERF-001 (T-2 / SRC-001:R0005): provider backoff is a scheduling state
    # ------------------------------------------------------------------
    def _provider_resume_at(self, conn_key: str) -> float:
        """Epoch timestamp this connection may be probed again (0.0 = ready)."""
        return float(self._provider_backoffs.get((conn_key or "").lower(), 0.0))

    def _defer_for_backoff(
        self,
        delayed_heap: List[Tuple[float, int, str, DiscoveredModel]],
        delay_seq,
        conn_key: str,
        model: DiscoveredModel,
    ) -> bool:
        """Park a model on the delay heap while its connection is cooling down.

        Returns True when the model was deferred instead of scheduled. This is
        what makes backoff free: a deferred model never becomes a task, so it
        holds neither a global nor a per-provider scheduler slot (PERF-001).
        """
        resume_at = self._provider_resume_at(conn_key)
        if resume_at <= time.time():
            return False
        heapq.heappush(delayed_heap, (resume_at, next(delay_seq), conn_key, model))
        return True

    @staticmethod
    def _park_deferred(
        delayed_heap: List[Tuple[float, int, str, DiscoveredModel]],
        delay_seq,
        conn_key: str,
        model: DiscoveredModel,
        resume_at: float,
    ) -> None:
        """Park a model whose already-scheduled task declined to send it.

        The deadline is never shortened to `now`: if it already passed, the next
        scheduler pass resumes the model immediately (resume_at <= now).
        """
        heapq.heappush(
            delayed_heap,
            (max(resume_at, time.time()), next(delay_seq), conn_key, model),
        )

    @staticmethod
    def _resume_delayed(
        delayed_heap: List[Tuple[float, int, str, DiscoveredModel]],
        conn_queues: Dict[str, deque],
        active_conns: List[str],
    ) -> None:
        """Move every model whose server deadline has passed back to its queue."""
        now = time.time()
        while delayed_heap and delayed_heap[0][0] <= now:
            _resume_at, _seq, conn_key, model = heapq.heappop(delayed_heap)
            conn_queues.setdefault(conn_key, deque()).append(model)
            if conn_key not in active_conns:
                active_conns.append(conn_key)

    async def _probe_single_model(
        self,
        http_client: httpx.AsyncClient,
        m: DiscoveredModel,
        mode: ScanMode,
        headers: Optional[Dict[str, str]] = None,
        execution: Optional[ScanSessionExecution] = None,
    ) -> ModelHealthRecord:
        # The session context is passed explicitly so this probe can only ever
        # observe/mutate the session it belongs to (B1). A direct caller that
        # omits it falls back to the worker's current context, if any.
        if execution is None:
            execution = self._execution
        if execution is None:
            # No owned session: no transport, no evidence, no state change.
            return None
        cid = m.canonical_id
        if self._session_is_active():
            self._emit_probe_started(cid)

        existing = self.cache.get(cid)
        prev_counters = existing.counters if existing else EvidenceCounters()
        cost_override = existing.cost_override if existing else None
        cost_hint = getattr(m, "cost_hint", None)

        url = f"{self.client.base_url}/api/models/test"
        payload = {"model": cid, "kind": "llm"}

        if headers is None:
            try:
                headers = self.client._get_headers()
                headers["x-9r-cli-token"] = self.client.get_cli_token()
            except LiveAccessLockedError:
                # LOCKED start of this probe: no transport, no evidence record.
                execution.request_cancel()
                return None

        start_t = time.time()

        # -------------------------------------------------------------
        # SINGLE-POST TWO-THRESHOLD PROBE (PERF-003 / SRC-001:R0012)
        # -------------------------------------------------------------
        # ONE POST task per model, observed at the fast threshold and, for an
        # extended model (FULL/COMBO or combo member), again at the slow
        # threshold. The task is NEVER cancelled and resubmitted: asyncio.wait
        # observes without cancelling, so a slow model issues exactly one POST
        # and emits PENDING at the fast cutoff while the SAME request stays in
        # flight. A QUICK non-combo model is cut at the fast cutoff and the one
        # task is cancelled. This resolves the audited defect where the fast
        # stage cancelled its request and the slow stage issued a second POST.
        if is_reasoning_model(m.canonical_id):
            fast_budget = min(DEFAULT_FAST_TIMEOUT_SEC * 2, REASONING_MODEL_TIMEOUT_SEC / 2)
            slow_budget = REASONING_MODEL_TIMEOUT_SEC
        else:
            fast_budget = DEFAULT_FAST_TIMEOUT_SEC
            slow_budget = DEFAULT_SLOW_TIMEOUT_SEC
        extended = mode in (ScanMode.FULL, ScanMode.COMBO) or m.is_combo_member

        res = None
        raw_text = ""
        parsed_json = None
        status_code = 0
        is_timeout = False
        request_task: Optional["asyncio.Future"] = None

        def _adopt(task) -> None:
            nonlocal res, raw_text, parsed_json, status_code
            res = task.result()
            status_code = res.status_code
            raw_text = res.text
            try:
                parsed_json = res.json()
            except Exception:
                parsed_json = None

        async def _cut(task) -> None:
            """Cancel the single in-flight task and consume its terminal state
            so no 'exception was never retrieved' noise escapes."""
            task.cancel()
            try:
                await task
            except BaseException:
                pass

        try:
            # Authoritative request boundary: re-verified immediately before
            # the single authenticated transport call (semaphore race safe).
            self.client.security.require_live("probe_model_fast")
            request_task = asyncio.ensure_future(
                http_client.post(url, headers=headers, json=payload)
            )
            done, _pending = await asyncio.wait({request_task}, timeout=fast_budget)
            if request_task in done:
                _adopt(request_task)
            elif not extended:
                # QUICK non-combo: cut at the fast cutoff, no second POST.
                await _cut(request_task)
                is_timeout = True
            elif execution.cancelled:
                # Session already cancelled at the fast cutoff: stop the one
                # in-flight request, no evidence, no second POST.
                await _cut(request_task)
                return None
            else:
                # FAST THRESHOLD crossed: the SAME task keeps running. Emit
                # PENDING, then continue awaiting the identical request.
                elapsed_so_far = time.time() - start_t
                if self._session_is_active():
                    self._emit_probe_pending(cid, elapsed_so_far)
                # A late lock stops the in-flight request instead of letting
                # it finish: no second transport boundary, no evidence.
                self.client.security.require_live("probe_model_slow")
                remaining_budget = max(slow_budget - elapsed_so_far, 4.0)
                done2, _p2 = await asyncio.wait({request_task}, timeout=remaining_budget)
                if request_task in done2:
                    _adopt(request_task)
                else:
                    await _cut(request_task)
                    is_timeout = True
        except (asyncio.TimeoutError, TimeoutError, httpx.TimeoutException):
            is_timeout = True
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.TransportError, httpx.NetworkError) as net_err:
            status_code = 503
            raw_text = f"Network transport error: {net_err}"
            parsed_json = {"error": {"code": "network_error", "message": str(net_err)}}
        except asyncio.CancelledError:
            raise
        except LiveAccessLockedError:
            if request_task is not None and not request_task.done():
                await _cut(request_task)
            execution.request_cancel()
            return None
        except Exception as ex:
            status_code = 500
            raw_text = str(ex)
            parsed_json = {"error": {"code": "probe_exception", "message": str(ex)}}

        if execution.cancelled:
            # Cancellation boundary (W2-001/T-30): a probe whose transport
            # call completed only after the session was cancelled produces
            # NO evidence — cancellation never invents model state.
            return None

        elapsed_ms = (time.time() - start_t) * 1000.0
        response_headers = dict(res.headers) if res is not None else {}

        # Parse Retry-After on 429
        conn_key = m.connection_id or m.provider_prefix.lower()
        if res is not None and res.status_code == 429:
            parsed_backoff = parse_retry_after(res.headers.get("retry-after"))
            backoff_sec = parsed_backoff if parsed_backoff is not None else DEFAULT_RETRY_AFTER_SEC
            # PERF-001: the server's deadline is stored IN FULL (no local clamp).
            # It is enforced by the scheduler's delay heap -- the next attempt for
            # this connection is not even scheduled before it expires.
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
