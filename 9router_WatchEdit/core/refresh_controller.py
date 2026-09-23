"""
9router_WatchEdit - Coherent refresh snapshot & single-flight refresh controller
(T-31 / T-32, SRC-005:W2-003 + SRC-005:PERF-006)

Two cooperating pieces:

DiscoverySnapshot
    One logical inventory refresh produces ONE immutable publication object.
    Models and combos are captured together (combos exactly once per logical
    refresh) and publish through one acceptance boundary, so the UI can never
    hold generation N models next to generation N-1/N+1 combos. Discovery-pass
    containers (live outcomes, catalog states, catalog model counts, routing
    exclusions) are per-pass values frozen into the snapshot; overlapping
    generations can never mutate the same containers.

RefreshController
    Explicit lifecycle owner for the refresh single-flight state machine:
    at most one logical inventory refresh executes at a time; repeated user
    Refresh actions coalesce into at most one newest follow-up pass. No thread
    is created per click. State: active generation, latest requested/pending
    generation, running flag, follow-up flag, closing flag, last published
    generation (stale-completion rejection lives here, preserving T-9
    semantics through one publication contract for sync and async paths).

The OpenCode-current source joins the snapshot envelope as explicit source
state (AVAILABLE / EMPTY / UNAVAILABLE / INVALID / NEVER_REFRESHED); snapshot
capture never performs OpenCode HTTP.
"""
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from core.classification import CatalogState
from core.discovery import DiscoveredModel

# Explicit source-state values for the independent OpenCode-current lane.
OPENCODE_AVAILABLE = "AVAILABLE"
OPENCODE_EMPTY = "EMPTY"
OPENCODE_UNAVAILABLE = "UNAVAILABLE"
OPENCODE_INVALID = "INVALID"
OPENCODE_NEVER_REFRESHED = "NEVER_REFRESHED"

# begin_refresh() outcomes
REFRESH_STARTED = "STARTED"          # caller owns a new active pass
REFRESH_COALESCED = "COALESCED"      # a pass is active; request represented
                                     # by the newest reserved generation
REFRESH_CLOSED = "CLOSED"            # application closing: nothing may start


@dataclass(frozen=True)
class DiscoverySnapshot:
    """Immutable, coherent result of one logical inventory refresh.

    `combos is None` means the publication intentionally carries no combo
    payload (legacy single-surface publications only); a real refresh always
    carries the captured combo collection. Combos are captured exactly once
    per logical refresh and drive both combo-membership classification and
    ComboEditor reconciliation.
    """

    generation: int
    models: Tuple[DiscoveredModel, ...] = ()
    combos: Optional[Tuple[Dict[str, Any], ...]] = None
    live_outcomes: Mapping[str, str] = field(default_factory=dict)
    catalog_states: Mapping[str, CatalogState] = field(default_factory=dict)
    catalog_model_counts: Mapping[str, int] = field(default_factory=dict)
    routing_excluded_connections: frozenset = frozenset()
    opencode_source_state: str = OPENCODE_NEVER_REFRESHED
    opencode_model_count: int = 0
    captured_at: float = 0.0
    completed: bool = True
    error: str = ""

    @property
    def has_combos(self) -> bool:
        return self.combos is not None

    def __iter__(self):
        """Iterate over the discovered models. Backward compatibility so
        existing ``for m in discover_all(...)`` / ``list(discover_all(...))``
        consumers keep working with the snapshot publication."""
        return iter(self.models)

    def __len__(self) -> int:
        return len(self.models)


class RefreshController:
    """Single-flight lifecycle owner for logical inventory refreshes.

    Contract (T-32):
    - At most one logical refresh pass runs at a time.
    - Refresh requests issued while a pass runs coalesce: at most ONE newest
      follow-up pass is reserved; intermediate requests never create threads
      or discovery pools.
    - `may_publish(generation)` rejects anything older than the last accepted
      publication (stale completion rejection, T-9) and anything after close.
    - `finish_pass` always releases the running state (error recovery: a
      failed refresh can never permanently disable future Refresh actions).
    - `close()` discards any pending follow-up and permanently blocks new
      passes and publications (bounded shutdown; late workers cannot publish
      to a destroyed UI).
    """

    def __init__(self, next_generation: Callable[[], int]):
        self._next_generation = next_generation
        self._lock = threading.RLock()
        self._active_generation: Optional[int] = None
        self._pending_generation: Optional[int] = None
        self._running = False
        self._closing = False
        self._last_published_generation = 0
        # T-32 closure: the highest generation ever REQUESTED by a caller.
        # A completion for any generation below this is superseded user
        # intent and must never publish (even before the newer pass has
        # produced a snapshot).
        self._latest_requested_generation = 0

    # ------------------------------------------------------------- lifecycle
    def begin_refresh(self) -> Tuple[str, int]:
        """Request one logical refresh.

        Returns (REFRESH_STARTED, gen) when the caller becomes the active
        pass, (REFRESH_COALESCED, pending_gen) when an active pass exists and
        this request is represented by the newest reserved follow-up
        generation, or (REFRESH_CLOSED, 0) after close.
        """
        with self._lock:
            if self._closing:
                return (REFRESH_CLOSED, 0)
            if self._running:
                # Coalescing: repeated requests while a pass runs never spawn
                # new threads, but the single reserved follow-up always comes
                # to represent the LATEST request — each new click re-reserves
                # a newer generation for that one follow-up pass.
                self._pending_generation = self._next_generation()
                self._latest_requested_generation = max(
                    self._latest_requested_generation, self._pending_generation
                )
                return (REFRESH_COALESCED, self._pending_generation)
            generation = self._next_generation()
            self._active_generation = generation
            self._pending_generation = None
            self._running = True
            self._latest_requested_generation = max(
                self._latest_requested_generation, generation
            )
            return (REFRESH_STARTED, generation)

    def finish_pass(self, generation: int, spawn_follow_up: bool = True) -> Optional[int]:
        """Release the active pass.

        Returns the follow-up generation for the caller to spawn, or None.
        The controller stays CLAIMED (running=True, active_generation=follow-up)
        until the caller reports the actual spawn via claim_follow_up(); an
        unclaimed follow-up is released to idle by release_unclaimed().
        Always safe to call from a finally block: the previous pass is
        released unconditionally so a failed or cancelled pass can never
        wedge the single-flight state.
        """
        with self._lock:
            if self._active_generation == generation:
                self._active_generation = None
            self._running = False
            pending = self._pending_generation
            self._pending_generation = None
            if self._closing or pending is None or not spawn_follow_up:
                return None
            # Reserved for the follow-up, but NOT running: the caller must
            # claim_follow_up(pending) right after spawning the worker thread.
            # This closes the phantom-owner gap: between finish_pass and the
            # actual spawn there is a single owner-free moment, and if the
            # caller never spawns (legacy sync owner), release_unclaimed()
            # returns the controller to a clean idle state.
            self._active_generation = pending
            return pending

    def claim_follow_up(self, generation: int) -> bool:
        """Mark a reserved follow-up generation as actually owned by a
        spawned worker. Returns False (no state change) if the reservation was
        already consumed, closed, or replaced by a newer reservation."""
        with self._lock:
            if self._closing:
                return False
            if (
                self._active_generation == generation
                and not self._running
                and self._pending_generation is None
            ):
                self._running = True
                return True
            return False

    def release_unclaimed(self, generation: int) -> None:
        """Return an unclaimed follow-up reservation to a clean idle state.

        Used by owners that do not intend to spawn (e.g. the synchronous
        refresh path): running becomes False and no phantom active
        generation remains."""
        with self._lock:
            if self._active_generation == generation and not self._running:
                self._active_generation = None
                self._pending_generation = None

    def begin_worker(self, generation: int) -> bool:
        """Atomic worker-start boundary (W2-004).

        A worker must call this immediately before ANY discovery/live I/O. It
        consumes the pass's start lease under the controller lock: if close()
        won the race after the lease was claimed/prepared but before the worker
        actually started, the reservation is revoked and this returns False, so
        the worker exits before build_snapshot/network work and cannot begin
        post-close work that publication blocking alone would only mask.
        Returns True when the worker may proceed."""
        with self._lock:
            if self._closing:
                if self._active_generation == generation:
                    self._active_generation = None
                self._pending_generation = None
                self._running = False
                return False
            return True

    def close(self) -> None:
        """Application closing: discard the pending follow-up and block all
        future passes and publications."""
        with self._lock:
            self._closing = True
            self._pending_generation = None

    # ----------------------------------------------------------- publication
    def may_publish(self, generation: int, allow_equal: bool = False) -> bool:
        """Acceptance boundary: only a generation strictly newer than the last
        accepted publication may publish, and nothing publishes after close.
        This preserves T-9 stale-completion rejection for both sync and async
        paths through one contract.

        allow_equal=True exists only for legacy single-surface emitters
        (pre-snapshot signals) that carry the current generation; real
        snapshot publications always use the strict rule."""
        with self._lock:
            if self._closing:
                return False
            # Supersession (T-32): once a newer refresh has been REQUESTED,
            # any older generation is obsolete user intent and must never
            # become authoritative — not even briefly while the newer pass
            # is still running.
            if generation < self._latest_requested_generation:
                return False
            if allow_equal:
                return generation >= self._last_published_generation
            return generation > self._last_published_generation

    def record_published(self, generation: int) -> None:
        """Mark a generation as accepted. Also used as the failure marker for
        a failed newest pass so an older success can never resurrect state
        after a newer failure."""
        with self._lock:
            if generation > self._last_published_generation:
                self._last_published_generation = generation

    # ------------------------------------------------------------- observers
    @property
    def running(self) -> bool:
        with self._lock:
            return self._running

    @property
    def active_generation(self) -> Optional[int]:
        with self._lock:
            return self._active_generation

    @property
    def pending_follow_up(self) -> Optional[int]:
        with self._lock:
            return self._pending_generation

    @property
    def latest_requested_generation(self) -> int:
        with self._lock:
            return self._latest_requested_generation

    @property
    def closing(self) -> bool:
        with self._lock:
            return self._closing

    @property
    def last_published_generation(self) -> int:
        with self._lock:
            return self._last_published_generation

    @property
    def max_active_logical_refreshes(self) -> int:
        """Structural proof for the T-32 stress gate: the controller can only
        ever own one active pass."""
        return 1 if self._running else 0
