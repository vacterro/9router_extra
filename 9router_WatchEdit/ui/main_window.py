"""
9router_WatchEdit - Main Application Window (T-33 predictable compact UI)

One top-level task workspace: Models | Combo | OpenCode. Exactly one major
task surface visible at a time; Models is selected on startup and the app
never switches tabs on its own. Compact global header, fixed-height scan
footer, explicit Model Details dialog. No background timers: nothing refreshes,
polls, reorders, or steals focus while the application is idle.
"""
from datetime import datetime
import json
import threading
from typing import Dict, List, Optional
from PySide6.QtCore import Qt, QObject, Signal, Slot
from PySide6.QtWidgets import (
    QMainWindow,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QStatusBar,
    QMessageBox,
    QDialog,
    QLabel,
    QPushButton,
    QTabWidget,
    QProgressBar,
)

from config import DEFAULT_GLOBAL_CONCURRENCY, SETTINGS_FILE
from core.router_client import RouterClient
from core.history import (
    CacheLoadState,
    HealthCache,
    HealthCachePersistenceError,
    ModelHealthRecord,
)
from core.discovery import ModelDiscovery, DiscoveredModel
from core.combo_manager import PresetManager, compute_combo_diff
from core.probe import ScanSessionExecution, ScannerWorker, ScanMode
from core.refresh_controller import (
    REFRESH_COALESCED,
    REFRESH_CLOSED,
    REFRESH_STARTED,
    DiscoverySnapshot,
    RefreshController,
)
from core.security import LiveAccessLockedError, get_default_security
from core.redaction import redact_text
from core.opencode_catalog import OpenCodeCatalogDiscovery
from core.opencode_bridge import (
    BRIDGE_OK,
    BRIDGE_UPSTREAM_REJECTED,
    OpenCodeBridge,
)
from core.ocf_registry import (
    OcfRegistry,
    SOURCE_AVAILABLE,
    SOURCE_EMPTY,
    SOURCE_INVALID,
    SOURCE_NEVER_REFRESHED,
    SOURCE_UNAVAILABLE,
    plan_direct_free_migration,
)
from core.free_provider_adapters import (
    build_default_adapters,
    register_discovered_providers,
    seed_registry,
    seeded_provider_ids,
)
from core.free_provider_registry import FreeProviderRegistry
from core.free_scan_controller import (
    ACTION_LABELS,
    ACTION_LIVE,
    ACTION_METADATA,
    FreeScanController,
    FreeScanRun,
)
from core.free_saifren_sync import commit_tail_sync, plan_tail_sync
from ui.watch_view import WatchView
from ui.combo_editor_view import ComboEditorView, DiffConfirmDialog
from ui.presets_view import PresetsView
from ui.security_ui import SecurityDialog, SecurityIndicator
from ui.inspector_panel import ModelDetailsDialog
from ui.theme import COLOR_BORDER_HIGHLIGHT, COLOR_TEXT_SECONDARY
from ui.opencode_catalog_view import OpenCodeCatalogPanel
from ui.free_fallback_view import FreeFallbackView

#: FREE fallback tab title (final main tab, after Models / Combo / OpenCode).
FREE_FALLBACK_TAB_TITLE = "FREE Fallback"

class _ScanFooter(QWidget):
    """Fixed-height compact scan footer. Space is reserved permanently so the
    layout never moves; Stop scan stays visible (disabled) while idle."""
    stop_requested = Signal()

    def __init__(self):
        super().__init__()
        self.setFixedHeight(36)
        self.setContentsMargins(4, 2, 4, 2)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        self.status_label = QLabel("Ready / Idle")
        self.status_label.setStyleSheet(f"color: {COLOR_TEXT_SECONDARY};")
        layout.addWidget(self.status_label)

        layout.addStretch()

        self.progress_bar = QProgressBar()
        self.progress_bar.setMaximumWidth(200)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setMaximumHeight(8)
        self.progress_bar.setVisible(False)
        layout.addWidget(self.progress_bar)

        self.btn_stop = QPushButton("Stop scan")
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self.stop_requested.emit)
        layout.addWidget(self.btn_stop)

    def set_scanning(self, current: int, total: int, active: int = 0):
        active_txt = f" • {active} active" if active > 0 else ""
        self.status_label.setText(f"Scanning {current} / {total}{active_txt}")
        self.progress_bar.setMaximum(max(total, 1))
        self.progress_bar.setValue(current)
        self.progress_bar.setVisible(True)
        self.btn_stop.setEnabled(True)

    def set_idle(self):
        self.status_label.setText("Ready / Idle")
        self.progress_bar.setVisible(False)
        self.btn_stop.setEnabled(False)

class ScannerSignals(QObject):
    # W2-001: every scan callback carries the owning session id FIRST so the
    # window can reject stale generations.
    probe_started = Signal(int, str)          # session_id, canonical_id
    probe_pending = Signal(int, str, float)   # session_id, canonical_id, elapsed
    probe_finished = Signal(int, str, object) # session_id, canonical_id, record
    progress = Signal(int, int, int)          # session_id, completed, total
    scan_completed = Signal(int, str)         # session_id, terminal status
    scan_failed = Signal(int, str)            # session_id, redacted error
    discovery_finished = Signal(int, object)  # generation, DiscoverySnapshot
    discovery_failed = Signal(int, str)  # generation, error message
    combos_loaded = Signal(int, list)  # generation, raw_combos (captured once)
    catalog_refreshed = Signal(dict)  # OpenCode catalog refresh result
    ocf_bridge_tested = Signal(dict)  # OCF-001 local bridge canary result
    free_scan_finished = Signal(dict)  # FREE-FALLBACK-001 provider scan summary

class MainWindow(QMainWindow):
    # W2-001: bounded shutdown window for an active scanner session. Long
    # enough for terminal cache persistence, far below any hang threshold.
    _SCAN_SHUTDOWN_TIMEOUT_SEC = 5.0

    def __init__(self):
        super().__init__()
        self.setWindowTitle("9router_WatchEdit — Operational Health Scanner & Combo Controller")
        self.resize(800, 640)

        # Core Services
        self.security = get_default_security()
        self.client = RouterClient(security=self.security)
        # T-32: discovery passes can attribute their bounded provider-request
        # concurrency to this window for the stress-test evidence.
        self.client._refresh_diagnostics_owner = self
        self.cache = HealthCache()
        self.discovery = ModelDiscovery(self.client)
        self.preset_manager = PresetManager()
        self.worker = ScannerWorker(self.client, self.cache)

        self.discovered_models: List[DiscoveredModel] = []
        self._models_by_cid: Dict[str, DiscoveredModel] = {}
        self._last_scan_error: str = ""
        self._active_probes: int = 0
        self._scan_progress: tuple = (0, 0)
        # W2-001: False after a closeEvent whose bounded shutdown window
        # expired with the worker thread still alive — fail closed, later
        # callbacks must not mutate the destroyed UI.
        self._ui_published: bool = True
        self._selected_model_cid: Optional[str] = None
        self._details_dialog: Optional[ModelDetailsDialog] = None

        # Monotonically increasing refresh generation source (T-9)
        self._refresh_generation: int = 0
        self._refresh_generation_lock = threading.Lock()

        # T-32 single-flight lifecycle owner: at most one logical inventory
        # refresh at a time; repeated Refresh actions coalesce into at most
        # one newest follow-up pass; no thread is created per click.
        self._refresh_controller = RefreshController(self._next_refresh_generation)

        # T-32 diagnostics (used by the stress tests; harmless counters).
        self._refresh_diagnostics = {
            "passes_started": 0,
            "threads_spawned": 0,
            "provider_requests_in_flight": 0,
            "max_provider_requests_in_flight": 0,
        }
        self._refresh_thread_monitor_lock = threading.Lock()

        # Worker Signals Bridge
        self.signals = ScannerSignals()
        self.signals.probe_started.connect(self._on_probe_started)
        self.signals.probe_pending.connect(self._on_probe_pending)
        self.signals.probe_finished.connect(self._on_probe_finished)
        self.signals.progress.connect(self._on_progress)
        self.signals.scan_completed.connect(self._on_scan_completed)
        self.signals.scan_failed.connect(self._on_scan_failed)
        self.signals.discovery_finished.connect(self._on_discovery_finished)
        self.signals.discovery_failed.connect(self._on_discovery_failed)
        self.signals.combos_loaded.connect(self._on_combos_loaded)
        self.signals.catalog_refreshed.connect(self._on_catalog_refreshed)
        self.signals.ocf_bridge_tested.connect(self._on_ocf_bridge_tested)
        self.signals.free_scan_finished.connect(self._on_free_scan_finished)

        self.opencode_catalog = OpenCodeCatalogDiscovery()

        # FREE-FALLBACK-001: provider-level control plane. Local state only:
        # nothing here touches the network until the operator runs an action.
        self.free_provider_registry = FreeProviderRegistry()
        self._free_adapters_cache: Optional[dict] = None
        self._free_controller: Optional[FreeScanController] = None
        self._free_selected_provider: str = ""

        self.worker.on_probe_started = lambda sid, cid: self.signals.probe_started.emit(sid, cid)
        self.worker.on_probe_pending = lambda sid, cid, el: self.signals.probe_pending.emit(sid, cid, el)
        self.worker.on_probe_finished = lambda sid, cid, rec: self.signals.probe_finished.emit(sid, cid, rec)
        self.worker.on_progress = lambda sid, comp, tot: self.signals.progress.emit(sid, comp, tot)
        self.worker.on_scan_completed = lambda sid, status="COMPLETED": self.signals.scan_completed.emit(sid, status or "COMPLETED")
        self.worker.on_scan_failed = lambda sid, err: self.signals.scan_failed.emit(sid, err)

        self._setup_ui()
        self._load_settings()

        # Instant startup (<100ms) from local persisted state only; live
        # work happens strictly on explicit user actions (T-33).
        self._initial_fast_load()

    def _setup_ui(self):
        central_widget = QWidget()
        central_widget.setObjectName("centralWidget")
        self.setCentralWidget(central_widget)

        root_layout = QVBoxLayout(central_widget)
        root_layout.setContentsMargins(4, 2, 4, 2)
        root_layout.setSpacing(2)

        # Compact global header: brand + security indicator + Presets...
        header_bar = QHBoxLayout()
        header_bar.setContentsMargins(2, 1, 2, 1)
        lbl_brand = QLabel("9router_WatchEdit")
        lbl_brand.setStyleSheet(f"font-weight: bold; color: {COLOR_BORDER_HIGHLIGHT}; font-size: 12px;")
        header_bar.addWidget(lbl_brand)
        header_bar.addStretch()

        self.security_indicator = SecurityIndicator(self.security)
        self.security_indicator.clicked.connect(self._open_security_controls)
        header_bar.addWidget(self.security_indicator)

        self.btn_open_presets = QPushButton("Presets...")
        self.btn_open_presets.clicked.connect(self._open_presets_dialog)
        header_bar.addWidget(self.btn_open_presets)
        root_layout.addLayout(header_bar)

        # One top-level task workspace: Models | Combo | OpenCode
        self.main_tabs = QTabWidget()

        # Models tab (primary operational screen)
        self.models_tab = QWidget()
        models_layout = QVBoxLayout(self.models_tab)
        models_layout.setContentsMargins(2, 2, 2, 2)
        models_layout.setSpacing(2)

        self.watch_view = WatchView(self.cache)
        models_layout.addWidget(self.watch_view, stretch=1)

        self.main_tabs.addTab(self.models_tab, "Models")

        # Combo tab
        self.combo_tab = QWidget()
        combo_layout = QVBoxLayout(self.combo_tab)
        combo_layout.setContentsMargins(2, 2, 2, 2)
        combo_layout.setSpacing(2)

        self.combo_editor = ComboEditorView(self.client, self.cache)
        combo_layout.addWidget(self.combo_editor, stretch=1)

        self.main_tabs.addTab(self.combo_tab, "Combo")

        # OpenCode tab (catalog panel no longer sits under the workspace)
        self.opencode_tab = QWidget()
        opencode_layout = QVBoxLayout(self.opencode_tab)
        opencode_layout.setContentsMargins(2, 2, 2, 2)
        opencode_layout.setSpacing(2)

        self.opencode_catalog_panel = OpenCodeCatalogPanel(self.opencode_catalog)
        self.opencode_catalog_panel.refresh_requested.connect(self._refresh_opencode_catalog_async)
        # OCF-001: explicit actions only (no background mutation, no silent
        # combo rewrite, no polling that changes UI state under the user).
        self.opencode_catalog_panel.bridge_test_requested.connect(self._on_test_ocf_bridge)
        self.opencode_catalog_panel.sync_tail_requested.connect(self._on_sync_ocf_tail)
        self.opencode_catalog_panel.set_bridge_action_available(True)
        self._ocf_bridge_obj = None
        self._ocf_registry_obj = None
        self._ocf_bridge_test_running = False
        opencode_layout.addWidget(self.opencode_catalog_panel, stretch=1)

        self.main_tabs.addTab(self.opencode_tab, "OpenCode")

        # FREE Fallback tab: the FINAL main tab. Providers are the primary
        # control surface; models are a drilldown.
        self.free_tab = QWidget()
        free_layout = QVBoxLayout(self.free_tab)
        free_layout.setContentsMargins(2, 2, 2, 2)
        free_layout.setSpacing(2)

        self.free_view = FreeFallbackView()
        self.free_view.scan_requested.connect(self._on_free_scan_requested)
        self.free_view.stop_requested.connect(self._on_free_stop_requested)
        self.free_view.sync_requested.connect(self._on_free_sync_requested)
        self.free_view.policy_changed.connect(self._on_free_policy_changed)
        self.free_view.note_changed.connect(self._on_free_note_changed)
        self.free_view.provider_activated.connect(self._on_free_provider_activated)
        free_layout.addWidget(self.free_view, stretch=1)

        self.main_tabs.addTab(self.free_tab, FREE_FALLBACK_TAB_TITLE)

        # Startup always selects Models; nothing switches tabs automatically
        self.main_tabs.setCurrentIndex(0)

        root_layout.addWidget(self.main_tabs, stretch=1)

        # Fixed-height scan footer (space reserved permanently)
        self.scan_footer = _ScanFooter()
        self.scan_footer.stop_requested.connect(self.stop_scan)
        root_layout.addWidget(self.scan_footer)

        # Status Bar
        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self.status_bar.showMessage("9router_WatchEdit ready.")

        # Secondary surfaces (shown on explicit request only)
        self.presets_view = PresetsView(self.preset_manager, self.cache)
        self.presets_view.apply_preset_requested.connect(self._on_apply_preset)

        # OpenCode catalog: load persisted last-known-good snapshot; no fetch
        # until the user explicitly asks for Refresh catalog.
        self._catalog_refresh_running = False
        self._apply_catalog_state()

        # Wire Signals
        self.watch_view.scan_requested.connect(self.start_scan)
        self.watch_view.refresh_inventory_requested.connect(self.refresh_all_async)
        self.watch_view.model_selected.connect(self._on_model_selected)
        self.watch_view.details_requested.connect(self._open_model_details)

        self.combo_editor.retest_combo_requested.connect(self._retest_specific_models)
        self.combo_editor.combo_saved.connect(self._on_combo_saved)

        # FREE Fallback: projected from local state only (no network on open).
        self._refresh_free_view()

    def _initial_fast_load(self):
        """Populates UI instantly (<100ms) from genuinely local persisted
        state only: cached health records, local settings, persisted OpenCode
        snapshot. Startup performs ZERO live 9Router traffic — no discovery,
        no get_combos, no probes, no catalog HTTP. Everything live starts
        only from an explicit user action (Refresh / scan / Combo Reload /
        Refresh catalog)."""
        cached_models = []
        for cid, rec in self.cache.records.items():
            cached_models.append(
                DiscoveredModel(
                    canonical_id=cid,
                    provider_name=rec.provider,
                    provider_prefix=cid.split("/")[0] if "/" in cid else "",
                    connection_id="",
                    model_id=rec.model_id,
                    display_name=rec.model_id,
                )
            )
        if cached_models:
            self.discovered_models = cached_models
            self._models_by_cid = {m.canonical_id: m for m in self.discovered_models}
            self.watch_view.set_models(self.discovered_models)
            self.combo_editor.set_available_models(self.discovered_models)

        # W2-005: a corrupt / unreadable cache is surfaced to the operator, not
        # silently presented as a clean empty state. The recoverable bytes and
        # the exact condition come from the HealthCache load contract.
        if self.cache.corruption_detected or self.cache.load_state == CacheLoadState.IO_ERROR:
            detail = self.cache.load_error or "unknown cause"
            recovery = (
                f"recoverable copy: {self.cache.quarantine_path}"
                if self.cache.quarantine_path
                else "original file left untouched"
            )
            self.status_bar.showMessage(
                f"Health cache {self.cache.load_state.value}: {detail} ({recovery})."
            )

    def _open_security_controls(self):
        # Unlock only changes authorization state (T-33: never an implicit
        # Refresh — the user explicitly presses Refresh / scan / Reload next).
        dlg = SecurityDialog(self.security, self)
        dlg.exec()

    def _require_live_or_warn(self, what: str, silent: bool = False) -> bool:
        """Gate for live operations: warns clearly when secrets are LOCKED.

        silent=True (background/live-gated paths) only sets the status bar —
        no modal dialog."""
        if not self.security.is_locked():
            return True
        msg = (
            f"Live access requires local credential unlock. "
            f"'{what}' needs live 9Router access (use the SECRETS indicator to unlock)."
        )
        if silent:
            self.status_bar.showMessage(msg)
            return False
        QMessageBox.warning(self, "Secrets Locked", msg)
        return False

    def _next_refresh_generation(self) -> int:
        with self._refresh_generation_lock:
            self._refresh_generation += 1
            return self._refresh_generation

    def refresh_all_async(self) -> int:
        """Asynchronously discovers inventory without blocking the UI thread.

        T-32 single-flight: while one logical pass runs, repeated calls
        coalesce into at most one reserved follow-up generation. The follow-up
        thread is spawned by the finishing pass, never per click."""
        outcome, generation = self._refresh_controller.begin_refresh()
        if outcome == REFRESH_CLOSED:
            return 0
        if outcome == REFRESH_COALESCED:
            self.status_bar.showMessage(
                f"Refresh in progress — coalesced into follow-up generation {generation}."
            )
            return generation
        if not self._require_live_or_warn("Refresh inventory", silent=True):
            # Never leave single-flight state claimed on an authorization refusal.
            self._finalize_pass(generation, spawn_follow_up=False)
            return 0
        self.status_bar.showMessage("Discovering live providers and models in background...")
        t = threading.Thread(
            target=self._background_discovery_task,
            args=(generation,),
            daemon=True,
        )
        with self._refresh_thread_monitor_lock:
            self._refresh_diagnostics["threads_spawned"] += 1
            self._refresh_diagnostics["passes_started"] += 1
        t.start()
        return generation

    def _finalize_pass(self, generation: int, spawn_follow_up: bool = True) -> None:
        """Shared pass-finalization path for sync and async refreshes (T-32
        closure, Defect F).

        If a coalesced follow-up was reserved, the caller must either claim
        it (claim_follow_up) after spawning exactly one worker, or release it
        (release_unclaimed). No code path can leave controller.running=True
        without an owning execution."""
        follow_up = self._refresh_controller.finish_pass(
            generation, spawn_follow_up=spawn_follow_up
        )
        if follow_up is not None and spawn_follow_up:
            if self._refresh_controller.claim_follow_up(follow_up):
                t = threading.Thread(
                    target=self._background_discovery_task,
                    args=(follow_up,),
                    daemon=True,
                )
                with self._refresh_thread_monitor_lock:
                    self._refresh_diagnostics["threads_spawned"] += 1
                t.start()
            else:
                # Reservation was consumed/closed concurrently: keep the
                # controller idle instead of spawning an unowned worker.
                self._refresh_controller.release_unclaimed(follow_up)

    def _background_discovery_task(self, generation: Optional[int] = None):
        """One logical refresh pass (T-31/T-32).

        Uses the PURE snapshot-building path: the pass builds one detached
        DiscoverySnapshot (its own single combo capture) and emits it. No
        compatibility state is written at pass completion — publication (UI
        and compatibility) happens only through the acceptance boundary
        _on_discovery_finished (Defect D)."""
        if generation is None:
            generation = self._next_refresh_generation()
        # W2-004: consume the atomic worker-start lease BEFORE any discovery or
        # live I/O. If close() won the race after this pass was claimed/prepared
        # but before the worker started, the reservation is revoked here and the
        # worker exits without starting new provider/live work.
        if not self._refresh_controller.begin_worker(generation):
            return
        try:
            snapshot = self.discovery.build_snapshot(
                query_live=True,
                opencode_catalog=self.opencode_catalog,
            )
            if isinstance(snapshot, DiscoverySnapshot):
                pass  # already detached; generation stamped below
            else:
                # Legacy discovery test doubles returning a bare list. The
                # legacy surface carried its own single combo capture; keep
                # exactly one get_combos() per pass for that contract.
                try:
                    combos = ModelDiscovery._capture_combos_once(self.discovery.client)
                except Exception as e:
                    self.signals.discovery_failed.emit(generation, f"combo capture failed: {e}")
                    combos = None
                snapshot = DiscoverySnapshot(
                    generation=generation,
                    models=tuple(snapshot or ()),
                    combos=None if combos is None else tuple(combos),
                )
            snapshot = DiscoverySnapshot(
                generation=generation,
                models=snapshot.models,
                combos=snapshot.combos,
                live_outcomes=snapshot.live_outcomes,
                catalog_states=snapshot.catalog_states,
                catalog_model_counts=snapshot.catalog_model_counts,
                routing_excluded_connections=snapshot.routing_excluded_connections,
                opencode_source_state=snapshot.opencode_source_state,
                opencode_model_count=snapshot.opencode_model_count,
                captured_at=snapshot.captured_at,
                completed=snapshot.completed,
                error=snapshot.error,
            )
            self.signals.discovery_finished.emit(generation, snapshot)
        except Exception as e:
            self.signals.discovery_failed.emit(generation, str(e))
        finally:
            self._finalize_pass(generation)

    @Slot(int, object)
    @Slot(object)
    @Slot(int, list)
    @Slot(list)
    def _on_discovery_finished(self, a, b=None):
        """Atomic snapshot acceptance boundary.

        Only one publication per logical refresh: models AND combos AND
        per-pass discovery state are applied together. Legacy list payloads
        (older emitters/tests) are wrapped into an implicit snapshot with the
        current generation so the acceptance contract stays uniform."""
        if b is None and (isinstance(a, list) or a is None):
            generation = self._refresh_generation
            payload = a
        else:
            generation = a
            payload = b

        if isinstance(payload, DiscoverySnapshot):
            snapshot = payload
        else:
            snapshot = DiscoverySnapshot(
                generation=generation,
                models=tuple(payload or ()),
            )

        if not self._refresh_controller.may_publish(snapshot.generation):
            return

        models = list(snapshot.models)
        self.discovered_models = models
        self._models_by_cid = {m.canonical_id: m for m in models}
        # set_models preserves the current selection when it still exists
        self.watch_view.set_models(models)
        # ONE captured combo collection drives ComboEditor reconciliation —
        # the same capture classified combo membership inside the pass.
        if snapshot.has_combos:
            # Consumer isolation (T-31): ComboEditor receives DETACHED combo
            # dicts via the explicit copy helper — editor mutation can never
            # reach back into the completed snapshot (or any other consumer).
            self.combo_editor.replace_combos(
                ModelDiscovery._copy_combos(snapshot.combos)
            )
            if self.combo_editor.current_combo:
                self.presets_view.set_current_live_combo(
                    self.combo_editor.current_combo.models
                )
        self.combo_editor.set_available_models(models)
        # Compatibility state updates ONLY here, after generation acceptance
        # (T-31 closure, Defect D): an obsolete UI-managed generation can
        # never mutate live_outcomes / catalog_states / catalog_model_counts /
        # routing_excluded_connections / discovery.combos.
        self.discovery.publish_snapshot(snapshot)
        self._refresh_controller.record_published(snapshot.generation)
        self._show_discovery_status(len(models))
        # Newly discovered 9Router providers appear in the FREE provider table
        # as CONDITIONAL / NO FREE ADAPTER. Local projection only.
        try:
            self._refresh_free_view()
        except Exception:
            pass

        # PERF-005: bounded retention AFTER an authoritative successful
        # reconciliation only. current_ids proves presence; referenced ids are
        # every model of every known combo; overrides are never pruned.
        try:
            current_ids = {m.canonical_id for m in models}
            referenced = set()
            for combo in self.combo_editor.combos.values():
                referenced.update(combo.models)
            pruned = self.cache.prune_stale_records(current_ids, referenced_ids=referenced)
            if pruned:
                # Persist atomically with normal cache persistence; a failed
                # save is surfaced by the cache contract, never swallowed into
                # a false success.
                self.cache.save()
        except HealthCachePersistenceError:
            # Retention could not be persisted: the in-memory prune still
            # applies this session; the failure is not hidden.
            self.status_bar.showMessage("Health cache retention could not be persisted.")
        except Exception:
            # Retention is best-effort and never breaks a successful publish.
            pass

    @Slot(int, str)
    @Slot(str)
    def _on_discovery_failed(self, a, b=None):
        if b is None and isinstance(a, str):
            generation = self._refresh_generation
            err = a
        else:
            generation = a
            err = b

        if not self._refresh_controller.may_publish(generation, allow_equal=True):
            return
        # Mark the newest failed generation so an older success arriving later
        # can never publish over it (T-9: failure is the last word for its
        # generation; single-flight state is already released in finally).
        self._refresh_controller.record_published(generation)
        self.status_bar.showMessage(f"Discovery notice: {redact_text(err)}")

    @Slot(int, list)
    @Slot(list)
    def _on_combos_loaded(self, a, b=None):
        """Legacy single-surface combo delivery path.

        Atomic inventory refreshes publish via _on_discovery_finished with a
        DiscoverySnapshot; this slot remains only for legacy emitters/tests.
        Stale generations are rejected through the same acceptance boundary."""
        if b is None and (isinstance(a, list) or a is None):
            generation = self._refresh_generation
            raw_combos = a
        else:
            generation = a
            raw_combos = b

        if not self._refresh_controller.may_publish(generation, allow_equal=True):
            return
        self._refresh_controller.record_published(generation)

        # replace_combos keeps the selected combo and never clobbers local
        # unsaved edits with fetched data (T-33 state preservation). The
        # legacy payload is copied so editor state never aliases the emitter
        # (T-31 consumer isolation applies to legacy surfaces too).
        self.combo_editor.replace_combos(ModelDiscovery._copy_combos(raw_combos))
        if self.combo_editor.current_combo:
            self.presets_view.set_current_live_combo(self.combo_editor.current_combo.models)

    @Slot(int, str)
    def _on_scan_failed(self, session_id: int, err: str):
        """Terminal scan failure for one session: store reason; the final
        message is rendered by _on_scan_completed. Stale sessions are
        ignored."""
        if not self._scan_session_is_current(session_id):
            return
        self._last_scan_error = err or "Unknown scanner failure"
        self._active_probes = 0
        self.scan_footer.set_idle()
        self.watch_view.set_scan_active(False)

    def _scan_session_is_current(self, session_id: int) -> bool:
        """Generation gate for every scan callback (W2-001, B2).

        Acceptance uses the NEWEST claimed scanner generation, not the active
        execution lease: callbacks are emitted from a background thread and
        may be delivered to the GUI AFTER the execution lease has been
        released, so a legitimate terminal callback of the newest completed
        session must still be accepted. Claiming a newer session (G2)
        immediately stales every delayed G1 callback.

        A callback whose session id is not the newest generation must never
        mutate UI state (probe results, progress/footer, active probes,
        status bar, last-scan time). After a failed bounded shutdown the
        window is considered destroyed: nothing publishes."""
        if not getattr(self, "_ui_published", True):
            return False
        return self.worker.session_controller.is_latest_session(session_id)

    def refresh_all(self):
        """Synchronous discovery fallback (e.g. for deterministic unit testing).

        Shares the same single-flight lifecycle and snapshot publication
        contract as the async path (T-31/T-32)."""
        outcome, generation = self._refresh_controller.begin_refresh()
        if outcome == REFRESH_CLOSED:
            return
        if outcome == REFRESH_COALESCED:
            self.status_bar.showMessage(
                f"Refresh in progress — coalesced into follow-up generation {generation}."
            )
            return
        try:
            if not self._require_live_or_warn("Refresh inventory (sync)", silent=True):
                # Synchronous owner releases its reservation without
                # spawning: the controller returns to a clean idle state.
                self._finalize_pass(generation, spawn_follow_up=False)
                return
            self.status_bar.showMessage("Refreshing provider connections and models...")
            snapshot = self.discovery.build_snapshot(
                opencode_catalog=self.opencode_catalog
            )
            if not isinstance(snapshot, DiscoverySnapshot):
                # Legacy discovery test doubles returning a bare list.
                try:
                    combos = ModelDiscovery._capture_combos_once(self.discovery.client)
                except Exception as e:
                    self.signals.discovery_failed.emit(generation, f"combo capture failed: {e}")
                    combos = None
                snapshot = DiscoverySnapshot(
                    generation=generation,
                    models=tuple(snapshot or ()),
                    combos=None if combos is None else tuple(combos),
                )
            snapshot = DiscoverySnapshot(
                generation=generation,
                models=snapshot.models,
                combos=snapshot.combos,
                live_outcomes=snapshot.live_outcomes,
                catalog_states=snapshot.catalog_states,
                catalog_model_counts=snapshot.catalog_model_counts,
                routing_excluded_connections=snapshot.routing_excluded_connections,
                opencode_source_state=snapshot.opencode_source_state,
                opencode_model_count=snapshot.opencode_model_count,
                error=snapshot.error,
            )
            if self._refresh_controller.may_publish(snapshot.generation, allow_equal=True):
                # Delegate to the one acceptance boundary.
                self._on_discovery_finished(snapshot.generation, snapshot)
        finally:
            # Shared finalization: any coalesced follow-up reserved while the
            # synchronous pass owned the controller is handed to exactly one
            # spawned worker (claim_follow_up) — never left as a phantom
            # running generation (T-32 closure, Defect F).
            self._finalize_pass(generation)

    def _show_discovery_status(self, model_count: int) -> None:
        """Show empty model catalog as degraded, never a PASS result."""
        empty_count = len(self.discovery.routing_excluded_connections)
        if empty_count:
            self.status_bar.showMessage(
                f"Discovered {model_count} models across 9Router providers. "
                f"Models API: EMPTY (0 models) — {empty_count} provider(s) temporarily excluded from active routing; re-probe available."
            )
            return
        self.status_bar.showMessage(f"Discovered {model_count} models across 9Router providers.")

    # ------------------------------------------------ OpenCode catalog (SRC-004)
    def _refresh_opencode_catalog_async(self, force: bool = True):
        """Catalog fetch + CLI cross-check strictly off the GUI thread.

        Single-flight: while one refresh runs the request is dropped; the
        discovery layer additionally dedups concurrent calls internally."""
        if self._catalog_refresh_running:
            return
        self._catalog_refresh_running = True
        self.opencode_catalog_panel.set_refreshing(True)

        def _task():
            try:
                result = self.opencode_catalog.refresh(force=force)
                if result.get("fetch"):
                    result["cli"] = self.opencode_catalog.cli_cross_check()
            except Exception:
                result = {"fresh": False, "status": "API_FAILED", "changed": False,
                          "diff": None, "fetch": True, "error_class": "worker_error"}
            finally:
                self.signals.catalog_refreshed.emit(result)

        threading.Thread(target=_task, daemon=True).start()

    def _reconcile_ocf_registry(self):
        """Scanner-owned `ocf` entries vs current free evidence (OCF-001).

        A successful refresh that loses free evidence removes exactly the
        scanner-managed entry; a source outage changes nothing. Manual entries
        are never removed, and no model is live-called here.
        """
        try:
            registry = self._ocf_registry()
            return registry.reconcile(
                self.opencode_catalog.models,
                self._catalog_source_state(),
                bridge_healthy=not self._ocf_bridge().health.provider_blocked(),
            )
        except Exception:
            return None

    @Slot(dict)
    def _on_catalog_refreshed(self, result: dict):
        self._catalog_refresh_running = False
        self.opencode_catalog_panel.set_refreshing(False)
        reconcile = self._reconcile_ocf_registry()
        self._apply_catalog_state()
        if reconcile is not None and reconcile.changed:
            self.status_bar.showMessage(
                f"OCF inventory reconciled: +{len(reconcile.added)} / -{len(reconcile.removed)}"
            )
        for ev in result.get("events") or []:
            self.opencode_catalog_panel.append_events([ev])
            if ev.get("type") == "newly_free":
                self.status_bar.showMessage(ev.get("message", ""))
        if result.get("status") == "API_FAILED":
            reason = redact_text(str(result.get("error_class", "")))
            self.status_bar.showMessage(f"OpenCode catalog refresh failed: {reason}")

    def _apply_catalog_state(self):
        """Mirror the discovery snapshot into the compact panel (no network)."""
        catalog = self.opencode_catalog
        self.opencode_catalog_panel.update_state(
            catalog.models, catalog.last_success_at, catalog.ui_status()
        )
        self.opencode_catalog_panel.set_free_models(
            [m for m in catalog.models if m.free_candidate]
        )
        # OCF-001: separate evidence dimensions per free model. Local state only
        # (registry snapshot + bridge health cache): still no network here.
        self.opencode_catalog_panel.set_ocf_inventory(self._ocf_inventory_rows())
        self.opencode_catalog_panel.set_bridge_status(self._ocf_bridge_status_text())

    # ------------------------------------------------ OCF local bridge (OCF-001)
    def _ocf_bridge(self) -> OpenCodeBridge:
        if self._ocf_bridge_obj is None:
            self._ocf_bridge_obj = OpenCodeBridge()
        return self._ocf_bridge_obj

    def _ocf_registry(self) -> OcfRegistry:
        if self._ocf_registry_obj is None:
            self._ocf_registry_obj = OcfRegistry()
        return self._ocf_registry_obj

    def _direct_route_evidence(self) -> Dict[str, Dict[str, str]]:
        """Observed evidence per canonical id from the local health cache."""
        evidence: Dict[str, Dict[str, str]] = {}
        try:
            for canonical_id, record in self.cache.snapshot_records().items():
                availability = getattr(record, "availability", None)
                evidence[str(canonical_id)] = {
                    "state": str(record.state),
                    "availability": getattr(availability, "value", str(availability)),
                }
        except Exception:
            return evidence
        return evidence

    def _catalog_source_state(self) -> str:
        """Catalog source state for reconciliation (outage never deletes rows)."""
        statuses = getattr(self.opencode_catalog, "statuses", {}) or {}
        api_status = statuses.get("api")
        if api_status is None:
            return SOURCE_NEVER_REFRESHED
        if api_status == "API_FAILED":
            error_class = (getattr(self.opencode_catalog, "last_failure", None) or {}).get("class", "")
            return SOURCE_INVALID if error_class == "malformed_payload" else SOURCE_UNAVAILABLE
        return SOURCE_AVAILABLE if self.opencode_catalog.models else SOURCE_EMPTY

    def _ocf_inventory_rows(self):
        try:
            registry = self._ocf_registry()
            return registry.inventory_rows(
                self.opencode_catalog.models,
                evidence=self._direct_route_evidence(),
                health=self._ocf_bridge().health,
            )
        except Exception:
            return []

    def _ocf_bridge_status_text(self) -> str:
        try:
            bridge = self._ocf_bridge()
            provider = bridge.health.provider_status()
        except Exception:
            return "Bridge: unavailable"
        if not bridge.runtime.available:
            return "Bridge: runtime missing"
        state = str(provider.get("state") or "not tested")
        if provider.get("blocked"):
            return f"Bridge: {state} (cooldown)"
        return f"Bridge: {state}"

    @Slot()
    def _on_test_ocf_bridge(self):
        """Explicit action: one minimal official-path canary, off the GUI thread."""
        if self._ocf_bridge_test_running:
            return
        models = [str(getattr(m, "model_id", "")) for m in self.opencode_catalog.models
                  if getattr(m, "free_candidate", False)]
        if not models:
            self.status_bar.showMessage("No advertised free model to canary; refresh the free catalog first.")
            return
        self._ocf_bridge_test_running = True
        self.opencode_catalog_panel.set_test_running(True)
        target = self._ocf_canary_target(models)

        def _task():
            try:
                result = self._ocf_bridge().canary(target)
                payload = result.as_dict()
            except Exception as ex:
                payload = {"state": "BRIDGE_BROKEN", "detail": redact_text(str(ex)), "ok": False}
            self.signals.ocf_bridge_tested.emit(payload)

        threading.Thread(target=_task, daemon=True).start()

    def _ocf_canary_target(self, models: List[str]) -> str:
        """Prefer a model already verified usable on this machine (cheapest canary)."""
        registry_ids = set(self._ocf_registry().eligible_ids(self._ocf_bridge().health))
        for model_id in registry_ids:
            if model_id in models:
                return model_id
        return models[0]

    @Slot(dict)
    def _on_ocf_bridge_tested(self, payload: dict):
        self._ocf_bridge_test_running = False
        self.opencode_catalog_panel.set_test_running(False)
        state = str(payload.get("state") or "BRIDGE_BROKEN")
        detail = redact_text(str(payload.get("detail") or ""))[:200]
        version = str(payload.get("version") or "")
        if state == BRIDGE_OK:
            self.status_bar.showMessage(f"Local OpenCode bridge OK (opencode {version}).")
        elif state == BRIDGE_UPSTREAM_REJECTED:
            self.status_bar.showMessage(
                "Local OpenCode bridge UPSTREAM_BLOCKED: the official runtime itself was rejected. "
                "ocf stays out of SAIFREN."
            )
        else:
            self.status_bar.showMessage(f"Local OpenCode bridge {state}: {detail}")
        self._apply_catalog_state()

    @Slot()
    def _on_sync_ocf_tail(self):
        """Route the legacy OpenCode button through the shared FREE planner."""
        prepared = self._prepare_saifren_sync()
        if prepared is None:
            return
        combo, live_models = prepared
        ocf_registry = self._ocf_registry()
        source_state = self._catalog_source_state()
        if source_state in (SOURCE_AVAILABLE, SOURCE_EMPTY):
            if not self._mirror_ocf_inventory_to_free_registry(ocf_registry):
                self.status_bar.showMessage(
                    "OpenCode inventory could not be committed to FREE ownership: "
                    f"{self.free_provider_registry.last_save_error}"
                )
                return
        bridge_ids = self._free_bridge_eligible_ids()
        if not ocf_registry.eligible_ids(self._ocf_bridge().health):
            self.status_bar.showMessage(
                "No bridge-verified free model is eligible; run 'Test local OpenCode bridge' first."
            )
            return
        migration = {}
        if source_state in (SOURCE_AVAILABLE, SOURCE_EMPTY):
            migration = plan_direct_free_migration(
                live_models, ocf_registry, self.opencode_catalog.models
            )
        plan = plan_tail_sync(
            live_models,
            self.free_provider_registry,
            combo_name="SAIFREN",
            bridge_eligible=bridge_ids,
            direct_migration_map=migration,
        )
        self._apply_saifren_plan(
            combo,
            live_models,
            plan,
            title="Sync OpenCode routes through FREE ownership",
            prompt=(
                "Apply the shared ownership-aware FREE tail plan to SAIFREN?\n\n"
                f"{plan.summary_text()}\n\n"
                "Only verified direct routes listed as migrations will change. "
                "Manual OCF entries keep their positions."
            ),
        )

    def _find_combo_by_name(self, name: str):
        for combo in self.combo_editor.combos.values():
            if combo.name == name:
                return combo
        return None

    def _apply_combo_models_verified(self, combo, models: List[str], report: dict) -> bool:
        """Official API apply + read-back verification (fail-closed, no SQLite)."""
        try:
            res = self.client.update_combo(
                combo_id=combo.id, name=combo.name, models=list(models), kind=combo.kind
            )
        except LiveAccessLockedError as ex:
            QMessageBox.warning(self, "Secrets Locked", str(ex))
            return False
        if not res:
            QMessageBox.critical(
                self, "Apply Failed",
                "Failed to apply SAIFREN update to 9Router (fail-closed); nothing was written."
            )
            return False
        live_combos = self._read_live_combos()
        verified = next(
            (c for c in live_combos or [] if str(c.get("id")) == str(combo.id)), None
        )
        if verified is None or list(verified.get("models") or []) != list(models):
            QMessageBox.warning(
                self, "Verification Mismatch",
                "Combo save could not be verified through the live API; reload combos before retrying."
            )
            return False
        combo.models = list(models)
        combo.mark_clean(verified.get("updatedAt", ""))
        self.opencode_catalog_panel.append_events([{
            "type": "synced",
            "canonical_id": "SAIFREN",
            "message": f"SAIFREN tail synced (+{len(report.get('appended', []))} ocf)",
        }])
        self._apply_catalog_state()
        return True

    def _read_live_combos(self):
        """Return validated live API combos; never substitute an offline snapshot."""
        try:
            status, combos = self.client.get_combos_detailed()
        except LiveAccessLockedError:
            return None
        except Exception:
            return None
        if status != "OK" or not isinstance(combos, list):
            return None
        return combos

    # ------------------------------------------------ Scanner session (W2-001)
    def _begin_scan_session(self, what: str) -> Optional[ScanSessionExecution]:
        """THE one start gate for every scan entry point (W2-001).

        A session is claimed ATOMICALLY on the controller — and its
        session-local execution context (lease) is created — BEFORE any worker
        thread is spawned, so the token and its cancellation/loop/task context
        exist together from the first instant. The returned lease is OWNED by
        this UI session and released exactly once by the worker wrapper's
        finalizer (_scan_session_task). While one scan session is active,
        additional Scan/Retest requests are rejected with a clear status
        message; on any refusal the lease is returned unused so the gate never
        wedges.
        """
        lease = self.worker.claim_execution()
        if lease is None:
            if self.worker.session_controller.closing:
                self.status_bar.showMessage(f"{what} refused: application is closing.")
            else:
                self.status_bar.showMessage(
                    f"{what} refused: a scan is already running (session "
                    f"{self.worker.session_controller.active_session}). Stop it first."
                )
            return None
        if not self._require_live_or_warn(what):
            self.worker.end_execution(lease)
            return None
        return lease

    def _spawn_scan_session(self, lease: ScanSessionExecution, models, mode, target_combo_models) -> bool:
        """Spawn the ONE owned worker thread for a claimed session lease.

        The thread handle is registered on the controller (bind_thread) so
        closeEvent can join it boundedly. Returns False when the controller
        was closed concurrently; the lease is then released and no thread is
        started.
        """
        t = threading.Thread(
            target=self._scan_session_task,
            args=(lease, models, mode, target_combo_models),
            daemon=True,
            name=f"ScannerWorker-session-{lease.session_id}",
        )
        if not self.worker.session_controller.bind_thread(t):
            self.worker.end_execution(lease)
            return False
        t.start()
        return True

    def _scan_session_task(self, lease: ScanSessionExecution, models, mode, target_combo_models) -> None:
        """Worker-thread body for one UI-owned session (W2-001, B3).

        THE authoritative execution-lease release owner for UI-started scans:
        run_scan EXECUTES the work, performs the terminal cache persistence
        and emits the terminal callbacks; this wrapper then releases the lease
        exactly once in its finalizer, after run_scan has returned. Worker
        internals never release a lease they did not claim, so exactly one
        layer owns the transition.
        """
        try:
            self.worker.run_scan(
                models, mode=mode, target_combo_models=target_combo_models,
                session_id=lease.session_id, execution=lease,
            )
        finally:
            self.worker.end_execution(lease)

    def start_scan(self, mode_str: str):
        lease = self._begin_scan_session(f"Scan ({mode_str})")
        if lease is None:
            return
        session_id = lease.session_id

        mode_map = {
            "ALL": ScanMode.FULL,
            "QUICK": ScanMode.QUICK,
            "FAILED_ONLY": ScanMode.FAILED_ONLY,
        }
        mode = mode_map.get(mode_str, ScanMode.QUICK)

        target_combo_models = None
        if self.combo_editor.current_combo:
            target_combo_models = list(self.combo_editor.current_combo.models)

        candidates = self.worker._filter_candidates(self.discovered_models, mode, target_combo_models)
        self._active_probes = 0
        self._scan_progress = (0, len(candidates))
        self.scan_footer.set_scanning(0, len(candidates))
        self.watch_view.set_scan_active(True)
        self.status_bar.showMessage(f"Scan session {session_id} started ({mode.value}).")

        self._spawn_scan_session(lease, self.discovered_models, mode, target_combo_models)

    def stop_scan(self):
        controller = self.worker.session_controller
        active = controller.active_session
        if active is None:
            return
        # Cancel EXACTLY the currently owned session.
        self.worker.cancel(active)
        self.status_bar.showMessage(f"Cancelling scan session {active}...")

    def _retest_single_model(self, canonical_id: str):
        lease = self._begin_scan_session("Retest model")
        if lease is None:
            return
        m = self._models_by_cid.get(canonical_id)
        if not m:
            prefix, mid = canonical_id.split("/", 1) if "/" in canonical_id else ("", canonical_id)
            m = DiscoveredModel(
                canonical_id=canonical_id,
                provider_name=prefix,
                provider_prefix=prefix,
                connection_id="",
                model_id=mid,
                display_name=mid,
            )

        self._active_probes = 0
        self._scan_progress = (0, 1)
        self.scan_footer.set_scanning(0, 1)
        self.watch_view.set_scan_active(True)

        self._spawn_scan_session(lease, [m], ScanMode.FULL, None)

    def _retest_specific_models(self, canonical_ids: List[str]):
        lease = self._begin_scan_session("Retest combo models")
        if lease is None:
            return
        candidates = [self._models_by_cid[cid] for cid in canonical_ids if cid in self._models_by_cid]
        if not candidates:
            self.worker.end_execution(lease)
            return
        self._active_probes = 0
        self._scan_progress = (0, len(candidates))
        self.scan_footer.set_scanning(0, len(candidates))
        self.watch_view.set_scan_active(True)

        self._spawn_scan_session(lease, candidates, ScanMode.FULL, None)

    @Slot(int, str)
    def _on_probe_started(self, session_id: int, canonical_id: str):
        if not self._scan_session_is_current(session_id):
            return
        self._active_probes += 1
        self._refresh_footer_active()

    @Slot(int, str, float)
    def _on_probe_pending(self, session_id: int, canonical_id: str, elapsed_sec: float):
        if not self._scan_session_is_current(session_id):
            return
        self.watch_view.update_probe_pending(canonical_id, elapsed_sec)

    @Slot(int, str, object)
    def _on_probe_finished(self, session_id: int, canonical_id: str, record: ModelHealthRecord):
        if not self._scan_session_is_current(session_id):
            return
        self._active_probes = max(0, self._active_probes - 1)
        self._refresh_footer_active()
        self.watch_view.update_probe_result(canonical_id, record)
        self.combo_editor.update_model_health_badge(canonical_id)
        self._update_details_dialog(canonical_id, record)

    def _refresh_footer_active(self):
        """Keep the footer text in sync while a scan is running (no layout move)."""
        if self.worker.is_running():
            completed, total = self._scan_progress
            self.scan_footer.set_scanning(completed, total, self._active_probes)

    @Slot(int, int, int)
    def _on_progress(self, session_id: int, completed: int, total: int):
        if not self._scan_session_is_current(session_id):
            return
        self._scan_progress = (completed, total)
        self.scan_footer.set_scanning(completed, total, self._active_probes)

    @Slot(int, str)
    def _on_scan_completed(self, session_id: int, status: str = "COMPLETED"):
        if not self._scan_session_is_current(session_id):
            return
        self._active_probes = 0
        self.scan_footer.set_idle()
        self.watch_view.set_scan_active(False)
        now_str = datetime.now().strftime("%H:%M:%S")
        self.watch_view.set_last_scan_time(now_str)
        if status == "CANCELLED":
            self.status_bar.showMessage(f"Scan cancelled at {now_str}.")
        elif status == "COMPLETED_PERSISTENCE_FAILED":
            # W2-005: the scan itself finished but its final state is not
            # durable. The reason is surfaced once and never overwritten by a
            # generic completed message (this is the last word).
            reason = self._last_scan_error or "unknown persistence failure"
            self.status_bar.showMessage(
                f"Scan finished at {now_str} but results were NOT saved: {reason}"
            )
        elif status == "FAILED":
            # Terminal FAILED message includes the redacted reason and is never
            # overwritten by a generic completed message (this is the last word).
            reason = self._last_scan_error or "Unknown scanner failure"
            self.status_bar.showMessage(f"Scan failed at {now_str}: {reason}")
        else:
            self.status_bar.showMessage(f"Scan completed at {now_str}.")

    # ------------------------------------------- FREE Fallback (FREE-FALLBACK-001)
    def _free_adapters(self) -> Dict[str, object]:
        """Adapter set for the provider control plane (built once, lazily)."""
        if self._free_adapters_cache is None:
            self._free_adapters_cache = build_default_adapters(
                opencode_catalog=self.opencode_catalog,
                opencode_bridge=self._ocf_bridge(),
                kira_catalog_fetch=self._free_local_catalog_fetch,
            )
        return self._free_adapters_cache

    def _free_local_catalog_fetch(self):
        """Kira billing metadata from THIS installation's 9Router catalog.

        A failure raises, which the adapter reports as an unavailable metadata
        source -- an empty list would look like an authoritative "no free model"
        answer and could prune real scanner-owned tail entries.
        """
        return self.client.get_catalog_models_detailed()

    def _free_controller_obj(self) -> FreeScanController:
        if self._free_controller is None:
            self._free_controller = FreeScanController(
                self.free_provider_registry,
                self._free_adapters(),
                max_workers=DEFAULT_GLOBAL_CONCURRENCY,
            )
        return self._free_controller

    def _free_discovered_provider_ids(self) -> List[str]:
        """Provider ids observed through the normal 9Router discovery path."""
        ids: List[str] = []
        for model in self.discovered_models:
            prefix = str(getattr(model, "provider_prefix", "") or "").strip()
            if not prefix and "/" in str(getattr(model, "canonical_id", "")):
                prefix = str(model.canonical_id).split("/", 1)[0]
            if prefix and prefix not in ids:
                ids.append(prefix)
        return ids

    def _free_bridge_eligible_ids(self) -> List[str]:
        """`ocf/*` canonical ids the EXISTING bridge contract reports usable."""
        try:
            registry = self._ocf_registry()
            return list(registry.canonical_ids(self._ocf_bridge().health))
        except Exception:
            return []

    def _refresh_free_view(self) -> None:
        """Rebuild the provider table from LOCAL state only -- never network.

        Opening or re-projecting the tab performs NO write either: seeded and
        newly discovered provider rows exist in memory until an operator action
        or a scan actually changes persistent state, so merely looking at the
        surface can never mutate private storage.
        """
        registry = self.free_provider_registry
        adapters = self._free_adapters()
        seed_registry(registry, adapters)
        register_discovered_providers(
            registry, adapters,
            seeded_provider_ids() + self._free_discovered_provider_ids(),
        )
        bridge_ids = self._free_bridge_eligible_ids()
        rows = registry.provider_rows()
        models = {
            str(row["provider_id"]): registry.model_rows(
                str(row["provider_id"]), bridge_eligible=bridge_ids
            )
            for row in rows
        }
        self.free_view.set_snapshot(rows, models)

    def _free_state_saved(self) -> bool:
        """Surface a refused/failed provider-state write instead of pretending."""
        error = self.free_provider_registry.last_save_error
        if error:
            self.status_bar.showMessage(f"FREE provider state not saved: {error}")
            return False
        return True

    @Slot(list, str)
    def _on_free_scan_requested(self, provider_ids: list, action: str):
        """Operator action: only the SELECTED providers are ever touched."""
        if not provider_ids:
            self.status_bar.showMessage("Select at least one provider first.")
            return
        controller = self._free_controller_obj()
        if controller.is_running():
            self.status_bar.showMessage(
                "A FREE provider scan is already running; stop it first."
            )
            self.free_view.set_scan_state(controller.running_kind())
            return
        if action == ACTION_METADATA:
            run = controller.metadata_scan_selected(list(provider_ids))
        elif action == ACTION_LIVE:
            run = controller.validate_selected(list(provider_ids))
        else:
            run = controller.scan_selected(list(provider_ids))
        self.free_view.set_scan_state(run.action)
        label = ACTION_LABELS.get(action, action)
        self.status_bar.showMessage(
            f"{label}: {len(provider_ids)} provider(s), generation {run.generation}."
        )
        threading.Thread(
            target=self._free_scan_watch, args=(run,), daemon=True,
            name=f"free-view-scan-{run.generation}",
        ).start()

    def _free_scan_watch(self, run: FreeScanRun) -> None:
        """Wait for the bounded run, then publish one summary on the GUI thread."""
        run.wait()
        try:
            self.signals.free_scan_finished.emit(run.summary())
        except RuntimeError:
            pass  # window destroyed while the run finished

    @Slot(dict)
    def _on_free_scan_finished(self, summary: dict):
        self.free_view.set_scan_state(None)
        try:
            self._refresh_free_view()
        except Exception:
            pass
        statuses = summary.get("statuses") or {}
        detail = ", ".join(f"{key}={value}" for key, value in sorted(statuses.items()))
        reason = self._free_scan_reason_text(summary)
        self.status_bar.showMessage(
            f"FREE provider scan {summary.get('action', '')} finished: "
            f"{detail or 'no providers'}{reason}"
        )
        self._free_state_saved()

    @staticmethod
    def _free_scan_reason_text(summary: dict) -> str:
        """Report refusals/skips explicitly -- silence would look like success."""
        skipped = [
            f"{outcome.get('provider_id')}={outcome.get('reason')}"
            for outcome in (summary.get("outcomes") or [])
            if outcome.get("reason")
        ]
        if not skipped:
            return ""
        return " · " + ", ".join(skipped[:6])

    @Slot()
    def _on_free_stop_requested(self):
        controller = self._free_controller_obj()
        if controller.stop():
            self.status_bar.showMessage("Cancelling FREE provider scan...")
        else:
            self.status_bar.showMessage("No FREE provider scan is running.")

    @Slot(str, str, object)
    def _on_free_policy_changed(self, provider_id: str, field: str, value):
        registry = self.free_provider_registry
        if field == "enabled":
            registry.set_enabled(provider_id, bool(value))
        elif field == "scan_mode":
            registry.set_mode(provider_id, value)
        elif field == "scan_policy":
            registry.set_policy(provider_id, value)
        elif field == "metadata_cost_risk":
            return
        elif field == "trusted_alive":
            if value is None:
                registry.clear_trusted_alive(provider_id)
            else:
                registry.mark_trusted_alive(provider_id, str(value))
        self._refresh_free_view()
        self._free_state_saved()

    @Slot(str, str)
    def _on_free_note_changed(self, provider_id: str, text: str):
        self.free_provider_registry.set_note(provider_id, text)
        self._free_state_saved()

    @Slot(str)
    def _on_free_provider_activated(self, provider_id: str):
        """Selection only: records the current provider, performs no I/O."""
        self._free_selected_provider = provider_id

    @Slot()
    def _on_free_sync_requested(self):
        """Explicit action: append strict FREE routes at the SAIFREN bottom."""
        prepared = self._prepare_saifren_sync()
        if prepared is None:
            return
        combo, live_models = prepared
        plan = plan_tail_sync(
            live_models,
            self.free_provider_registry,
            combo_name="SAIFREN",
            bridge_eligible=self._free_bridge_eligible_ids(),
        )
        self._apply_saifren_plan(
            combo,
            live_models,
            plan,
            title="Sync strict FREE to SAIFREN bottom",
            prompt=(
                "Append strict FREE routes at the VERY BOTTOM of SAIFREN?\n\n"
                f"{plan.summary_text()}\n\n"
                "Reliable routes keep their order; manual entries are preserved."
            ),
        )

    def _prepare_saifren_sync(self):
        combo = self._find_combo_by_name("SAIFREN")
        if combo is None:
            self.status_bar.showMessage("SAIFREN combo not found; load combos first.")
            return None
        live_combos = self._read_live_combos()
        live_combo = next(
            (c for c in live_combos or [] if str(c.get("id")) == str(combo.id)), None
        )
        if live_combo is None:
            self.status_bar.showMessage(
                "SAIFREN live API read failed or combo identity changed; reload combos before syncing."
            )
            return None
        live_models = list(live_combo["models"])
        registry = self.free_provider_registry
        if registry.pending_tail_sync is not None:
            recovery = registry.reconcile_pending_tail_sync(live_models)
            if recovery not in ("CLEAN", "COMPLETED", "ROLLED_BACK"):
                self.status_bar.showMessage(
                    "SAIFREN sync recovery is unresolved; inspect the saved intent before retrying."
                )
                return None
        combo.models = live_models
        return combo, live_models

    def _apply_saifren_plan(self, combo, old_models, plan, *, title: str, prompt: str):
        if plan.unchanged:
            self.status_bar.showMessage(
                "SAIFREN already ends with the current strict FREE routes."
            )
            return False
        answer = QMessageBox.question(
            self, title, prompt,
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return False
        registry = self.free_provider_registry
        intent = {
            "combo_name": plan.combo_name,
            "old_models": list(old_models),
            "new_models": list(plan.models),
            "appended": list(plan.appended),
            "removed": list(plan.removed),
            "provider_ids": {},
        }
        if not registry.stage_tail_sync_intent(intent):
            self.status_bar.showMessage(
                f"SAIFREN sync not applied: could not persist recovery intent ({registry.last_save_error})."
            )
            return False
        if not self._apply_combo_models_verified(combo, plan.models, plan.as_dict()):
            self.status_bar.showMessage(
                "SAIFREN apply is unverified; durable recovery intent retained. Reload and sync to reconcile."
            )
            return False
        if not commit_tail_sync(registry, plan):
            self.status_bar.showMessage(
                f"SAIFREN changed, but FREE ownership commit failed; recovery intent retained ({registry.last_save_error})."
            )
            return False
        self._refresh_free_view()
        self.status_bar.showMessage(
            f"SAIFREN tail synced: +{len(plan.appended)} / -{len(plan.removed)} / "
            f"migrated {len(plan.migrated_direct)}"
        )
        return True

    def _mirror_ocf_inventory_to_free_registry(self, ocf_registry: OcfRegistry) -> bool:
        """Copy current OpenCode catalog evidence; FREE registry owns combo edits."""
        adapters = self._free_adapters()
        seed_registry(self.free_provider_registry, adapters)
        adapter = adapters.get("opencode")
        if adapter is not None:
            self.free_provider_registry.describe_adapter(
                "opencode", adapter.capabilities().as_dict()
            )
        with ocf_registry._lock:
            entries = [
                entry for entry in ocf_registry.entries.values()
                if entry.scanner_managed and not entry.withdrawn
            ]
        rows = [{
            "canonical_id": entry.canonical_id,
            "upstream_model_id": entry.model_id,
            "provider_id": "opencode",
            "free_evidence": "CLIENT_BOUND_FREE",
            "evidence_source": "opencode_catalog_current",
            "provider_health": "UNKNOWN",
            "routing": "LOCAL_BRIDGE_REQUIRED",
            "cost_risk": "FREE_QUOTA_PROBE",
            "scanner_managed": True,
            "last_seen": entry.last_seen,
        } for entry in entries]
        return self.free_provider_registry.record_metadata_result(
            "opencode", ok=True, models=rows, authoritative=True,
        )

    # ------------------------------------------------ Model details (explicit)
    def _on_model_selected(self, canonical_id: str):
        # Selection alone must never open dialogs or resize the workspace.
        self._selected_model_cid = canonical_id

    def _open_model_details(self, canonical_id: str):
        """Explicit Model Details surface (double-click / Enter)."""
        existing = self._details_dialog
        if existing is not None and getattr(existing, "_shown_cid", None) == canonical_id:
            existing.raise_()
            return
        if existing is not None:
            existing.close()
            self._details_dialog = None

        rec = self.cache.get(canonical_id)
        dlg = ModelDetailsDialog(canonical_id, rec, self)
        dlg._shown_cid = canonical_id
        dlg.inspector.retest_requested.connect(self._retest_single_model)
        dlg.inspector.add_to_combo_requested.connect(self._add_model_to_combo)
        dlg.inspector.cost_override_changed.connect(self._on_cost_override_changed)
        dlg.setAttribute(Qt.WA_DeleteOnClose)
        dlg.destroyed.connect(self._on_details_dialog_destroyed)
        self._details_dialog = dlg
        dlg.show()

    def _on_details_dialog_destroyed(self, *args):
        self._details_dialog = None

    def _update_details_dialog(self, canonical_id: str, record: ModelHealthRecord):
        dlg = self._details_dialog
        if dlg is not None and getattr(dlg, "_shown_cid", None) == canonical_id:
            dlg.inspector.set_model(canonical_id, record)

    def _add_model_to_combo(self, canonical_id: str):
        self.combo_editor.add_model_to_current(canonical_id)

    def _on_cost_override_changed(self, canonical_id: str, override: Optional[str]):
        # W2-005: persistence failure is explicit and visible, never a silent
        # "looks saved" state; the in-memory override still applies.
        try:
            rec = self.cache.set_cost_override(canonical_id, override)
        except HealthCachePersistenceError as ex:
            rec = self.cache.get(canonical_id)
            self.status_bar.showMessage(f"Cost override not saved: {redact_text(str(ex))}")
        if rec:
            self.watch_view.update_probe_result(canonical_id, rec)
            self._update_details_dialog(canonical_id, rec)

    # ------------------------------------------------ Presets (secondary dialog)
    def _open_presets_dialog(self):
        dlg = QDialog(self)
        dlg.setWindowTitle("Presets Manager")
        dlg.resize(800, 500)
        layout = QVBoxLayout(dlg)
        layout.setContentsMargins(6, 6, 6, 6)
        if self.combo_editor.current_combo:
            self.presets_view.set_current_live_combo(self.combo_editor.current_combo.models)
        layout.addWidget(self.presets_view)
        dlg.exec()

    def _on_apply_preset(self, preset_name: str, preset_models: List[str]):
        if not self.combo_editor.current_combo:
            QMessageBox.warning(self, "No Combo", "Please select a combo first in the Combo Editor.")
            return

        old_models = list(self.combo_editor.current_combo.models)
        diff = compute_combo_diff(old_models, preset_models)

        dlg = DiffConfirmDialog(diff, self)
        if dlg.exec() == QDialog.Accepted:
            self.combo_editor.current_combo.models = list(preset_models)
            self.combo_editor._refresh_combo_models_list()
            self.combo_editor._refresh_available_list()
            self.combo_editor._update_dirty_state()

    def _on_combo_saved(self, combo_name: str):
        self.status_bar.showMessage(f"Combo '{combo_name}' successfully saved to 9Router.")
        if self.combo_editor.current_combo:
            self.presets_view.set_current_live_combo(self.combo_editor.current_combo.models)

    def _load_settings(self):
        if SETTINGS_FILE.exists():
            try:
                data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
                geom = data.get("geometry")
                if geom:
                    self.setGeometry(*geom)
            except Exception:
                pass

    def closeEvent(self, event):
        # FREE scan evidence is checkpointed in bounded batches. Closing owns a
        # final bounded cancellation/flush; refuse this close attempt if the
        # worker or durable write cannot finish, so the operator sees the
        # recovery state and can retry.
        if self._free_controller is not None:
            controller = self._free_controller
            if not controller.stop_and_wait(timeout=5.0):
                self.status_bar.showMessage(
                    "FREE scan is still active; close again after it stops."
                )
                event.ignore()
                return
            if (self.free_provider_registry.scan_evidence_dirty
                    and not self.free_provider_registry.save()):
                self.status_bar.showMessage(
                    "FREE scan evidence is not durable; fix storage and retry close: "
                    f"{self.free_provider_registry.last_save_error}"
                )
                event.ignore()
                return
            controller.shutdown(wait=True)

        # T-32 close semantics: no new refresh may start, the pending
        # follow-up is discarded, and late workers cannot publish to a
        # destroyed UI. The refresh thread itself stays daemon/bounded; we do
        # not hang the GUI waiting for network work.
        self._refresh_controller.close()

        # W2-001 close semantics: block new scan sessions, request
        # cancellation of the current session exactly once, retain the owned
        # worker thread handle and wait BOUNDEDLY for it so the terminal cache
        # persistence can finish — never an indefinite hang on provider work.
        scan_controller = self.worker.session_controller
        scan_controller.close()          # no new scan/retest may start
        self.worker.cancel()             # cancel the CURRENT owned session once
        self._ui_published = False       # late callbacks never reach the UI
        thread = scan_controller.owned_thread()
        if thread is not None and thread.is_alive():
            thread.join(timeout=self._SCAN_SHUTDOWN_TIMEOUT_SEC)
        if thread is None or not thread.is_alive():
            # The lease owner (the worker wrapper's finalizer) has already
            # released. This is only the fail-closed sweep for a thread that
            # died outside its own finalizer: it is not a competing release
            # owner (idempotent, token-scoped, and it marks any abandoned
            # context dead so it cannot mutate a future session).
            self.worker.abandon_active_execution()
        # If the timeout expired the thread is still alive: fail closed —
        # release is skipped, and the closed controller plus _ui_published
        # suppress any later UI publication.

        if self._details_dialog is not None:
            self._details_dialog.close()
        # Vault/grant session ends with the application: lock secrets on exit.
        self.security.lock()
        try:
            rect = self.geometry()
            data = {"geometry": [rect.x(), rect.y(), rect.width(), rect.height()]}
            SETTINGS_FILE.write_text(json.dumps(data), encoding="utf-8")
        except Exception:
            pass
        super().closeEvent(event)
