"""
9router_WatchEdit - Main Application Window
Coordinates unified single-screen productivity view:
- Top-Left: Watch View (model health table & filters) (55% width)
- Top-Right: Active Combo Editor (45% width)
- Bottom-Left: Activity Panel (log stream) (60% width)
- Bottom-Right: Diagnostic Inspector (40% width)
- Presets: Secondary modal dialog for on-demand comparison and application
Features instant startup (<100ms) from local cache and asynchronous background discovery.
"""
from datetime import datetime
import json
from pathlib import Path
import threading
from typing import Dict, List, Optional
from PySide6.QtCore import Qt, QObject, QTimer, Signal, Slot
from PySide6.QtWidgets import (
    QMainWindow,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QSplitter,
    QStatusBar,
    QMessageBox,
    QDialog,
    QLabel,
    QPushButton,
    QTabWidget,
)

from config import SETTINGS_FILE
from core.router_client import RouterClient
from core.history import HealthCache, ModelHealthRecord
from core.discovery import ModelDiscovery, DiscoveredModel
from core.combo_manager import PresetManager, compute_combo_diff, StableCombo
from core.probe import ScannerWorker, ScanMode
from core.security import get_default_security
from core.redaction import redact_text
from core.opencode_catalog import OpenCodeCatalogDiscovery
from ui.watch_view import WatchView
from ui.combo_editor_view import ComboEditorView, DiffConfirmDialog
from ui.presets_view import PresetsView
from ui.inspector_panel import InspectorPanel
from ui.activity_panel import ActivityPanel
from ui.security_ui import SecurityDialog, SecurityIndicator
from ui.theme import COLOR_BORDER_HIGHLIGHT
from ui.opencode_catalog_view import OpenCodeCatalogPanel

class ScannerSignals(QObject):
    probe_started = Signal(str)
    probe_pending = Signal(str, float)
    probe_finished = Signal(str, object)  # canonical_id, ModelHealthRecord
    progress = Signal(int, int)
    scan_completed = Signal(str)  # terminal status: COMPLETED, CANCELLED, FAILED
    scan_failed = Signal(str)
    discovery_finished = Signal(list)  # List[DiscoveredModel]
    discovery_failed = Signal(str)
    combos_loaded = Signal(list)
    catalog_refreshed = Signal(dict)  # OpenCode catalog refresh result

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("9router_WatchEdit — Operational Health Scanner & Combo Controller")
        self.resize(800, 640)

        # Core Services
        self.security = get_default_security()
        self.client = RouterClient(security=self.security)
        self.cache = HealthCache()
        self.discovery = ModelDiscovery(self.client)
        self.preset_manager = PresetManager()
        self.worker = ScannerWorker(self.client, self.cache)

        self.discovered_models: List[DiscoveredModel] = []
        self._models_by_cid: Dict[str, DiscoveredModel] = {}
        self._last_scan_error: str = ""

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

        self.opencode_catalog = OpenCodeCatalogDiscovery()

        self.worker.on_probe_started = lambda cid: self.signals.probe_started.emit(cid)
        self.worker.on_probe_pending = lambda cid, el: self.signals.probe_pending.emit(cid, el)
        self.worker.on_probe_finished = lambda cid, rec: self.signals.probe_finished.emit(cid, rec)
        self.worker.on_progress = lambda comp, tot: self.signals.progress.emit(comp, tot)
        self.worker.on_scan_completed = lambda status="COMPLETED": self.signals.scan_completed.emit(status or "COMPLETED")
        self.worker.on_scan_failed = lambda err: self.signals.scan_failed.emit(err)

        self._setup_ui()
        self._load_settings()

        # Instant startup (<100ms) from local cache, then background discovery
        self._initial_fast_load()

    def _setup_ui(self):
        central_widget = QWidget()
        central_widget.setObjectName("centralWidget")
        self.setCentralWidget(central_widget)

        root_layout = QVBoxLayout(central_widget)
        root_layout.setContentsMargins(4, 2, 4, 2)
        root_layout.setSpacing(2)

        # Header / Global Toolbar
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

        # Vertical Splitter: WatchView (top) | Tabs (bottom)
        self.vertical_splitter = QSplitter(Qt.Vertical)

        # Top: Watch View (main health table)
        self.watch_view = WatchView(self.cache)
        self.vertical_splitter.addWidget(self.watch_view)

        # Bottom container: Activity strip + Tabs
        bottom_widget = QWidget()
        bottom_layout = QVBoxLayout(bottom_widget)
        bottom_layout.setContentsMargins(0, 0, 0, 0)
        bottom_layout.setSpacing(2)

        # Activity Panel (compact strip)
        self.activity = ActivityPanel()
        self.activity.setMaximumHeight(52)
        bottom_layout.addWidget(self.activity)

        # Tab widget: Combo Editor + Inspector + OpenCode Catalog
        self.bottom_tabs = QTabWidget()
        self.combo_editor = ComboEditorView(self.client, self.cache)
        self.inspector = InspectorPanel()
        self.presets_view = PresetsView(self.preset_manager, self.cache)

        self.bottom_tabs.addTab(self.combo_editor, "Combo Editor")
        self.bottom_tabs.addTab(self.inspector, "Inspector")
        bottom_layout.addWidget(self.bottom_tabs, stretch=1)

        # Compact OpenCode Catalog section (SRC-004): separate panel below tabs,
        # never alters the existing tab count.
        self.opencode_catalog_panel = OpenCodeCatalogPanel(self.opencode_catalog)
        self.opencode_catalog_panel.refresh_requested.connect(self._refresh_opencode_catalog_async)
        self.opencode_catalog_panel.setMaximumHeight(180)
        bottom_layout.addWidget(self.opencode_catalog_panel)

        self.vertical_splitter.addWidget(bottom_widget)

        self.vertical_splitter.addWidget(bottom_widget)
        self.vertical_splitter.setStretchFactor(0, 55)
        self.vertical_splitter.setStretchFactor(1, 45)
        root_layout.addWidget(self.vertical_splitter, stretch=1)

        # Status Bar
        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self.status_bar.showMessage("9router_WatchEdit ready.")

        # OpenCode catalog: automatic TTL-gated refresh; button forces.
        # Do NOT startup-fetch here; reflect persisted LKG snapshot instantly.
        from core.opencode_catalog import CATALOG_TTL_SECONDS
        self._catalog_refresh_running = False
        self._apply_catalog_state()
        self._catalog_timer = QTimer(self)
        self._catalog_timer.setInterval(max(60_000, int(CATALOG_TTL_SECONDS * 1000 / 3)))
        self._catalog_timer.timeout.connect(lambda: self._refresh_opencode_catalog_async(force=False))
        self._catalog_timer.start()

        # Wire Signals
        self.watch_view.scan_requested.connect(self.start_scan)
        self.watch_view.refresh_inventory_requested.connect(self.refresh_all_async)
        self.watch_view.model_selected.connect(self._on_model_selected)

        self.combo_editor.retest_combo_requested.connect(self._retest_specific_models)
        self.combo_editor.combo_saved.connect(self._on_combo_saved)
        self.combo_editor.list_combo_models.currentItemChanged.connect(self._on_combo_item_selected)

        self.presets_view.apply_preset_requested.connect(self._on_apply_preset)

        self.inspector.retest_requested.connect(self._retest_single_model)
        self.inspector.add_to_combo_requested.connect(self._add_model_to_combo)
        self.inspector.cost_override_changed.connect(self._on_cost_override_changed)

        self.activity.stop_requested.connect(self.stop_scan)

    def _initial_fast_load(self):
        """Populates UI instantly (<100ms) from cached records and initial combos."""
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

        # Trigger background authoritative discovery and combo loading (NO synchronous network calls on UI thread)
        self.refresh_all_async()

    def _open_security_controls(self):
        dlg = SecurityDialog(self.security, self, on_unlock_callback=self.refresh_all_async)
        dlg.exec()

    def _require_live_or_warn(self, what: str, silent: bool = False) -> bool:
        """Gate for live operations: warns clearly when secrets are LOCKED.

        silent=True (startup background refresh) only sets the status bar —
        no modal dialog before the window is even shown."""
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

    def refresh_all_async(self):
        """Asynchronously discovers inventory without blocking the UI thread."""
        if not self._require_live_or_warn("Refresh inventory", silent=True):
            return
        self.status_bar.showMessage("Discovering live providers and models in background...")
        t = threading.Thread(target=self._background_discovery_task, daemon=True)
        t.start()

    def _background_discovery_task(self):
        try:
            models = self.discovery.discover_all(query_live=True)
            self.signals.discovery_finished.emit(models)
        except Exception as e:
            self.signals.discovery_failed.emit(str(e))

        try:
            raw_combos = self.client.get_combos()
            self.signals.combos_loaded.emit(raw_combos)
        except Exception:
            pass

    @Slot(list)
    def _on_discovery_finished(self, models: List[DiscoveredModel]):
        self.discovered_models = models
        self._models_by_cid = {m.canonical_id: m for m in self.discovered_models}
        self.watch_view.set_models(self.discovered_models)
        self.combo_editor.set_available_models(self.discovered_models)
        self._show_discovery_status(len(self.discovered_models))

    @Slot(str)
    def _on_discovery_failed(self, err: str):
        self.status_bar.showMessage(f"Discovery notice: {redact_text(err)}")

    @Slot(list)
    def _on_combos_loaded(self, raw_combos: list):
        self.combo_editor.combos.clear()
        self.combo_editor.cb_combo_selector.blockSignals(True)
        self.combo_editor.cb_combo_selector.clear()
        for c in raw_combos:
            cid = c.get("id")
            name = c.get("name")
            models = c.get("models", [])
            combo = StableCombo(
                combo_id=cid,
                name=name,
                models=models,
                kind=c.get("kind"),
                updated_at=c.get("updatedAt", ""),
            )
            self.combo_editor.combos[cid] = combo
            self.combo_editor.cb_combo_selector.addItem(f"{name} ({len(models)} models)", cid)
        self.combo_editor.cb_combo_selector.blockSignals(False)
        if self.combo_editor.cb_combo_selector.count() > 0:
            self.combo_editor._select_combo_by_index(0)
            if self.combo_editor.current_combo:
                self.presets_view.set_current_live_combo(self.combo_editor.current_combo.models)

    @Slot(str)
    def _on_scan_failed(self, err: str):
        """Terminal scan failure: store reason; final message is rendered by _on_scan_completed."""
        self._last_scan_error = err or "Unknown scanner failure"
        self.activity.set_scanning(False)
        self.watch_view.set_scan_active(False)

    def refresh_all(self):
        """Synchronous discovery fallback (e.g. for deterministic unit testing)."""
        if not self._require_live_or_warn("Refresh inventory (sync)", silent=True):
            return
        self.status_bar.showMessage("Refreshing provider connections and models...")
        self.discovered_models = self.discovery.discover_all()
        self._models_by_cid = {m.canonical_id: m for m in self.discovered_models}
        self.watch_view.set_models(self.discovered_models)
        self.combo_editor.load_combos_from_9router()
        self.combo_editor.set_available_models(self.discovered_models)
        if self.combo_editor.current_combo:
            self.presets_view.set_current_live_combo(self.combo_editor.current_combo.models)
        self._show_discovery_status(len(self.discovered_models))

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

    @Slot(dict)
    def _on_catalog_refreshed(self, result: dict):
        self._catalog_refresh_running = False
        self.opencode_catalog_panel.set_refreshing(False)
        self._apply_catalog_state()
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

    def start_scan(self, mode_str: str):
        if self.worker.is_running():
            return
        if not self._require_live_or_warn(f"Scan ({mode_str})"):
            return

        mode_map = {
            "ALL": ScanMode.FULL,
            "QUICK": ScanMode.QUICK,
            "FAILED_ONLY": ScanMode.FAILED_ONLY,
        }
        mode = mode_map.get(mode_str, ScanMode.QUICK)

        target_combo_models = None
        if self.combo_editor.current_combo:
            target_combo_models = self.combo_editor.current_combo.models

        candidates = self.worker._filter_candidates(self.discovered_models, mode, target_combo_models)
        self.activity.set_scanning(True, len(candidates))
        self.watch_view.set_scan_active(True)

        t = threading.Thread(
            target=self.worker.run_scan,
            args=(self.discovered_models, mode, target_combo_models),
            daemon=True,
        )
        t.start()

    def stop_scan(self):
        self.worker.cancel()
        self.status_bar.showMessage("Cancelling scan...")

    def _retest_single_model(self, canonical_id: str):
        if not self._require_live_or_warn("Retest model"):
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

        self.activity.set_scanning(True, 1)
        self.watch_view.set_scan_active(True)
        t = threading.Thread(
            target=self.worker.run_scan,
            args=([m], ScanMode.FULL, None),
            daemon=True,
        )
        t.start()

    def _retest_specific_models(self, canonical_ids: List[str]):
        if not self._require_live_or_warn("Retest combo models"):
            return
        candidates = [self._models_by_cid[cid] for cid in canonical_ids if cid in self._models_by_cid]
        if not candidates:
            return
        self.activity.set_scanning(True, len(candidates))
        self.watch_view.set_scan_active(True)
        t = threading.Thread(
            target=self.worker.run_scan,
            args=(candidates, ScanMode.FULL, None),
            daemon=True,
        )
        t.start()

    @Slot(str)
    def _on_probe_started(self, canonical_id: str):
        self.activity.set_probe_started(canonical_id)

    @Slot(str, float)
    def _on_probe_pending(self, canonical_id: str, elapsed_sec: float):
        self.activity.set_probe_pending(canonical_id, elapsed_sec)
        self.watch_view.update_probe_pending(canonical_id, elapsed_sec)

    @Slot(str, object)
    def _on_probe_finished(self, canonical_id: str, record: ModelHealthRecord):
        self.activity.set_probe_finished(canonical_id)
        self.watch_view.update_probe_result(canonical_id, record)
        self.combo_editor.update_model_health_badge(canonical_id)
        if self.inspector._current_canonical_id == canonical_id:
            self.inspector.set_model(canonical_id, record)

    @Slot(int, int)
    def _on_progress(self, completed: int, total: int):
        self.activity.update_progress(completed, total)

    @Slot(str)
    def _on_scan_completed(self, status: str = "COMPLETED"):
        self.activity.set_scanning(False)
        self.watch_view.set_scan_active(False)
        now_str = datetime.now().strftime("%H:%M:%S")
        self.watch_view.set_last_scan_time(now_str)
        if status == "CANCELLED":
            self.status_bar.showMessage(f"Scan cancelled at {now_str}.")
        elif status == "FAILED":
            # Terminal FAILED message includes the redacted reason and is never
            # overwritten by a generic completed message (this is the last word).
            reason = self._last_scan_error or "Unknown scanner failure"
            self.status_bar.showMessage(f"Scan failed at {now_str}: {reason}")
        else:
            self.status_bar.showMessage(f"Scan completed at {now_str}.")

    def _on_model_selected(self, canonical_id: str):
        rec = self.cache.get(canonical_id)
        self.inspector.set_model(canonical_id, rec)

    def _on_combo_item_selected(self, current, previous=None):
        if current:
            cid = current.data(Qt.UserRole)
            if cid:
                self._on_model_selected(cid)

    def _add_model_to_combo(self, canonical_id: str):
        self.combo_editor.add_model_to_current(canonical_id)

    def _on_cost_override_changed(self, canonical_id: str, override: Optional[str]):
        rec = self.cache.set_cost_override(canonical_id, override)
        if rec:
            self.watch_view.update_probe_result(canonical_id, rec)
            self.inspector.set_model(canonical_id, rec)

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
        self.worker.cancel()
        # Vault/grant session ends with the application: lock secrets on exit.
        self.security.lock()
        try:
            rect = self.geometry()
            data = {"geometry": [rect.x(), rect.y(), rect.width(), rect.height()]}
            SETTINGS_FILE.write_text(json.dumps(data), encoding="utf-8")
        except Exception:
            pass
        super().closeEvent(event)
