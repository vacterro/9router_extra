"""
9router_WatchEdit - Real-Time Safe Combo Editor
Features stable model identity (provider_prefix/model_id) decoupling from widget rows,
diff preview before applying bulk changes, and atomic synchronization with 9Router.
"""
from typing import Dict, List, Optional, Set
from PySide6.QtCore import Qt, Signal, QTimer
from PySide6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QComboBox,
    QListWidget,
    QListWidgetItem,
    QLineEdit,
    QMessageBox,
    QInputDialog,
    QDialog,
    QTextEdit,
    QDialogButtonBox,
    QSplitter,
    QGroupBox,
    QAbstractItemView,
    QCheckBox,
)
from PySide6.QtGui import QColor

from core.router_client import RouterClient
from core.history import HealthCache, ModelHealthRecord
from core.discovery import DiscoveredModel
from core.combo_manager import StableCombo, compute_combo_diff, ComboDiff
from core.security import LiveAccessLockedError
from ui.theme import (
    COLOR_BORDER_HIGHLIGHT,
    COLOR_TEXT_SECONDARY,
    COLOR_TEXT_MUTED,
    COLOR_BACKGROUND_SOFT,
    COLOR_ACCENT_TEAL,
    COLOR_DANGER,
    COLOR_DANGER_TEXT,
    STATE_COLORS,
    get_app_font,
)

class ConflictDialog(QDialog):
    """
    3-way conflict dialog when 9Router combo was modified concurrently.
    Displays:
    - Baseline (state when loaded)
    - Server (current 9Router engine state)
    - Local (your unsaved changes)
    """
    OVERWRITE = 1
    KEEP_SERVER = 2
    CANCEL = 0

    def __init__(
        self,
        combo_name: str,
        baseline: List[str],
        server: List[str],
        local: List[str],
        parent: Optional[QWidget] = None,
        field_changes: Optional[List[str]] = None,
    ):
        super().__init__(parent)
        self.setWindowTitle(f"Conflict Detected: Combo '{combo_name}'")
        self.setMinimumSize(680, 420)
        self.action_choice = self.CANCEL
        self._setup_ui(baseline, server, local, field_changes or [])

    def _setup_ui(self, baseline: List[str], server: List[str], local: List[str], field_changes: List[str]):
        layout = QVBoxLayout(self)
        layout.setSpacing(8)

        warn_lbl = QLabel(
            "WARNING: CONFLICT DETECTED\n"
            "This combo was modified outside WatchEdit while you were editing.\n"
            "Review the three states below and choose how to proceed:"
        )
        warn_lbl.setStyleSheet(f"color: {COLOR_BORDER_HIGHLIGHT}; font-weight: bold; font-size: 11px;")
        layout.addWidget(warn_lbl)

        if field_changes:
            detail_lbl = QLabel("Externally changed: " + "; ".join(field_changes))
            detail_lbl.setWordWrap(True)
            detail_lbl.setStyleSheet(f"color: {COLOR_ACCENT_TEAL}; font-size: 10px;")
            layout.addWidget(detail_lbl)

        cols_layout = QHBoxLayout()
        cols_layout.setSpacing(6)

        # Baseline Box
        box_base = QGroupBox(f"1. Baseline ({len(baseline)} models)")
        l_base = QVBoxLayout(box_base)
        t_base = QTextEdit()
        t_base.setReadOnly(True)
        t_base.setPlainText("\n".join(f"{i+1}. {m}" for i, m in enumerate(baseline)) or "(Empty)")
        t_base.setStyleSheet(f"background-color: {COLOR_BACKGROUND_SOFT}; font-size: 10px;")
        l_base.addWidget(t_base)
        cols_layout.addWidget(box_base)

        # Server Box
        box_srv = QGroupBox(f"2. Current 9Router Engine ({len(server)} models)")
        l_srv = QVBoxLayout(box_srv)
        t_srv = QTextEdit()
        t_srv.setReadOnly(True)
        t_srv.setPlainText("\n".join(f"{i+1}. {m}" for i, m in enumerate(server)) or "(Empty)")
        t_srv.setStyleSheet(f"background-color: {COLOR_BACKGROUND_SOFT}; font-size: 10px; color: {COLOR_ACCENT_TEAL};")
        l_srv.addWidget(t_srv)
        cols_layout.addWidget(box_srv)

        # Local Box
        box_loc = QGroupBox(f"3. Your Local Changes ({len(local)} models)")
        l_loc = QVBoxLayout(box_loc)
        t_loc = QTextEdit()
        t_loc.setReadOnly(True)
        t_loc.setPlainText("\n".join(f"{i+1}. {m}" for i, m in enumerate(local)) or "(Empty)")
        t_loc.setStyleSheet(f"background-color: {COLOR_BACKGROUND_SOFT}; font-size: 10px; color: {COLOR_BORDER_HIGHLIGHT};")
        l_loc.addWidget(t_loc)
        cols_layout.addWidget(box_loc)

        layout.addLayout(cols_layout)

        btn_box = QHBoxLayout()
        btn_box.setSpacing(8)

        btn_overwrite = QPushButton("Overwrite 9Router (Force Local)")
        btn_overwrite.setObjectName("dangerAction")
        btn_overwrite.clicked.connect(self._on_overwrite)

        btn_keep_server = QPushButton("Keep 9Router State (Discard Local)")
        btn_keep_server.clicked.connect(self._on_keep_server)

        btn_cancel = QPushButton("Cancel")
        btn_cancel.clicked.connect(self.reject)

        btn_box.addStretch()
        btn_box.addWidget(btn_overwrite)
        btn_box.addWidget(btn_keep_server)
        btn_box.addWidget(btn_cancel)
        layout.addLayout(btn_box)

    def _on_overwrite(self):
        self.action_choice = self.OVERWRITE
        self.accept()

    def _on_keep_server(self):
        self.action_choice = self.KEEP_SERVER
        self.accept()


class DiffConfirmDialog(QDialog):
    def __init__(self, diff: ComboDiff, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setWindowTitle("Review Proposed Combo Changes")
        self.setFixedSize(480, 360)
        self._setup_ui(diff)

    def _setup_ui(self, diff: ComboDiff):
        layout = QVBoxLayout(self)
        layout.setSpacing(8)

        lbl = QLabel("The following changes will be made to the combo:")
        lbl.setStyleSheet(f"font-weight: bold; color: {COLOR_BORDER_HIGHLIGHT};")
        layout.addWidget(lbl)

        txt = QTextEdit()
        txt.setReadOnly(True)
        txt.setPlainText(diff.format_text())
        txt.setStyleSheet(f"background-color: {COLOR_BACKGROUND_SOFT}; font-size: 11px;")
        layout.addWidget(txt)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("Apply Changes")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

class ReorderableComboListWidget(QListWidget):
    order_changed = Signal()
    model_dropped_from_picker = Signal(str, int)  # canonical_id, target_index

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setDragDropMode(QAbstractItemView.InternalMove)
        self.setDefaultDropAction(Qt.MoveAction)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(True)
        self.setSelectionMode(QAbstractItemView.SingleSelection)

    def dragEnterEvent(self, event):
        if event.source() is not None:
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dragMoveEvent(self, event):
        if event.source() is not None:
            event.acceptProposedAction()
        else:
            super().dragMoveEvent(event)

    def dropEvent(self, event):
        source = event.source()
        if source is not None and source != self:
            pos = event.position().toPoint() if hasattr(event, "position") else event.pos()
            target_idx = self.indexAt(pos).row()
            if target_idx < 0:
                target_idx = self.count()
            seen_cids = set()
            for it in source.selectedItems():
                cid = it.data(Qt.UserRole)
                if cid and cid not in seen_cids:
                    seen_cids.add(cid)
                    self.model_dropped_from_picker.emit(cid, target_idx)
                    target_idx += 1
            event.accept()
            return

        super().dropEvent(event)
        QTimer.singleShot(0, self.order_changed.emit)


class ComboEditorView(QWidget):
    combo_saved = Signal(str)  # combo_name
    retest_combo_requested = Signal(list)  # list of canonical_ids
    external_change_detected = Signal()

    def __init__(
        self,
        client: RouterClient,
        cache: HealthCache,
        parent: Optional[QWidget] = None,
    ):
        super().__init__(parent)
        self.client = client
        self.cache = cache
        self.combos: Dict[str, StableCombo] = {}  # combo_id -> StableCombo
        self.current_combo: Optional[StableCombo] = None
        self.available_models: List[DiscoveredModel] = []
        self._routing_blocked_model_ids: Set[str] = set()
        # W2-001: ids of dirty combos whose target disappeared from an accepted
        # server snapshot. They stay selectable (tombstoned) so the local draft
        # is recoverable and visibly conflicted until the operator resolves it.
        self._missing_dirty_ids: Set[str] = set()
        # PERF-003: canonical_id -> QListWidgetItem indices, rebuilt with each
        # list rebuild so update_model_health_badge is O(1) rather than a full
        # QList scan per probe completion.
        self._combo_item_by_cid: Dict[str, QListWidgetItem] = {}
        self._combo_row_by_cid: Dict[str, int] = {}
        self._available_item_by_cid: Dict[str, QListWidgetItem] = {}
        self._available_row_by_cid: Dict[str, int] = {}
        self._setup_ui()

    def _setup_ui(self):
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(4, 2, 4, 2)
        main_layout.setSpacing(3)

        # Top Bar Row 1: Combo Selector + Count + Dirty State + Save
        top_row1 = QHBoxLayout()
        top_row1.setSpacing(4)

        top_row1.addWidget(QLabel("Combo:"))
        self.cb_combo_selector = QComboBox()
        self.cb_combo_selector.setMinimumWidth(120)
        self.cb_combo_selector.currentIndexChanged.connect(self._on_combo_selection_changed)
        top_row1.addWidget(self.cb_combo_selector, stretch=1)

        self.lbl_combo_model_count = QLabel("0 models")
        self.lbl_combo_model_count.setStyleSheet(f"color: {COLOR_TEXT_SECONDARY};")
        top_row1.addWidget(self.lbl_combo_model_count)

        self.lbl_dirty_state = QLabel("Saved")
        self.lbl_dirty_state.setStyleSheet(f"color: {COLOR_TEXT_SECONDARY};")
        top_row1.addWidget(self.lbl_dirty_state)

        self.btn_save = QPushButton("Save changes")
        self.btn_save.setObjectName("primaryAction")
        self.btn_save.setFixedHeight(22)
        self.btn_save.setEnabled(False)
        self.btn_save.clicked.connect(self.save_current_combo)
        top_row1.addWidget(self.btn_save)

        main_layout.addLayout(top_row1)

        # Top Bar Row 2: Action buttons (simplified)
        top_row2 = QHBoxLayout()
        top_row2.setSpacing(3)

        self.btn_new_combo = QPushButton("New")
        self.btn_new_combo.clicked.connect(self._create_new_combo)

        self.btn_rename = QPushButton("Rename")
        self.btn_rename.clicked.connect(self._rename_combo)

        self.btn_duplicate = QPushButton("Duplicate")
        self.btn_duplicate.clicked.connect(self._duplicate_combo)

        self.btn_revert = QPushButton("Revert")
        self.btn_revert.clicked.connect(self._revert_unsaved)

        self.btn_reload = QPushButton("Reload")
        self.btn_reload.clicked.connect(self._reload_current_from_server)

        self.btn_manage = QPushButton("Manage...")
        self.btn_manage.clicked.connect(self._show_manage_dialog)

        self.btn_tools = QPushButton("Tools...")
        self.btn_tools.clicked.connect(self._show_tools_dialog)

        for btn in (self.btn_new_combo, self.btn_rename, self.btn_duplicate,
                    self.btn_revert, self.btn_reload, self.btn_manage, self.btn_tools):
            top_row2.addWidget(btn)
        top_row2.addStretch()

        main_layout.addLayout(top_row2)

        # Splitter: Left (Current Combo Models) | Right (Available Models Picker)
        splitter = QSplitter(Qt.Horizontal)

        # --- LEFT PANE: COMBO ORDERED LIST ---
        left_widget = QWidget()
        left_layout = QVBoxLayout(left_widget)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(4)

        left_header = QHBoxLayout()
        self.lbl_combo_count = QLabel("Combo Models (0):")
        self.lbl_combo_count.setStyleSheet("font-weight: bold;")
        left_header.addWidget(self.lbl_combo_count)
        left_header.addStretch()

        self.btn_retest_combo = QPushButton("Retest This Combo")
        self.btn_retest_combo.clicked.connect(self._retest_combo)
        left_header.addWidget(self.btn_retest_combo)

        left_layout.addLayout(left_header)

        self.list_combo_models = ReorderableComboListWidget()
        self.list_combo_models.setFont(get_app_font(11))
        self.list_combo_models.order_changed.connect(self._on_drag_order_changed)
        self.list_combo_models.model_dropped_from_picker.connect(self._on_model_dropped_from_picker)
        left_layout.addWidget(self.list_combo_models)

        # Combo Reorder Controls
        reorder_bar = QHBoxLayout()
        reorder_bar.setSpacing(4)

        self.btn_move_up = QPushButton("▲ Up")
        self.btn_move_up.clicked.connect(self._move_up)

        self.btn_move_down = QPushButton("▼ Down")
        self.btn_move_down.clicked.connect(self._move_down)

        self.btn_remove = QPushButton("✖ Remove")
        self.btn_remove.clicked.connect(self._remove_selected)

        reorder_bar.addWidget(self.btn_move_up)
        reorder_bar.addWidget(self.btn_move_down)
        reorder_bar.addWidget(self.btn_remove)
        left_layout.addLayout(reorder_bar)

        splitter.addWidget(left_widget)

        # --- RIGHT PANE: AVAILABLE MODELS PICKER ---
        right_widget = QWidget()
        right_layout = QVBoxLayout(right_widget)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(4)

        right_header = QHBoxLayout()
        right_header.addWidget(QLabel("Available Models:"))
        right_header.addStretch()

        self.txt_picker_search = QLineEdit()
        self.txt_picker_search.setPlaceholderText("Search...")
        self.txt_picker_search.setMaximumWidth(120)
        self.txt_picker_search.textChanged.connect(self._filter_available_models)
        right_header.addWidget(QLabel("Search:"))
        right_header.addWidget(self.txt_picker_search)

        right_layout.addLayout(right_header)

        # Single filter dropdown instead of multiple pills
        filter_row = QHBoxLayout()
        filter_row.addWidget(QLabel("Show:"))
        self.cb_picker_filter = QComboBox()
        self.cb_picker_filter.addItems(["Usable", "Free", "Paid", "Untested", "Attention", "Dead", "All"])
        self.cb_picker_filter.setCurrentIndex(0)  # Default to Usable
        self.cb_picker_filter.currentIndexChanged.connect(self._filter_available_models)
        filter_row.addWidget(self.cb_picker_filter)
        filter_row.addStretch()
        right_layout.addLayout(filter_row)

        self.list_available_models = QListWidget()
        self.list_available_models.setFont(get_app_font(11))
        self.list_available_models.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.list_available_models.setDragEnabled(True)
        self.list_available_models.setDragDropMode(QAbstractItemView.DragOnly)
        self.list_available_models.itemDoubleClicked.connect(self._on_picker_double_clicked)
        right_layout.addWidget(self.list_available_models)

        # Picker action bar
        picker_bar = QHBoxLayout()
        self.btn_add_to_combo = QPushButton("Add selected")
        self.btn_add_to_combo.setObjectName("primaryAction")
        self.btn_add_to_combo.clicked.connect(self._add_selected_from_picker)
        picker_bar.addWidget(self.btn_add_to_combo)
        right_layout.addLayout(picker_bar)

        splitter.addWidget(right_widget)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)

        main_layout.addWidget(splitter)

    def load_combos_from_9router(self, discard_dirty: bool = False):
        """Loads combos from 9Router and updates views."""
        try:
            self._load_combos_from_9router_locked(discard_dirty=discard_dirty)
        except LiveAccessLockedError as ex:
            QMessageBox.warning(self, "Secrets Locked", str(ex))

    def _load_combos_from_9router_locked(self, discard_dirty: bool = False):
        self.replace_combos(self.client.get_combos(), preserve_dirty=not discard_dirty)

    def replace_combos(self, raw_combos: List[dict], preserve_dirty: bool = True) -> None:
        """Rebuild the combo map from server data.

        State preservation (T-33): the current selection survives the reload,
        and (unless the caller explicitly confirmed a discard) a combo with
        unsaved local edits keeps its local state instead of being silently
        overwritten by the fetched copy."""
        prev_id = self.current_combo.id if self.current_combo else None
        keep_dirty = (
            preserve_dirty
            and self.current_combo is not None
            and self.current_combo.has_unsaved_changes()
        )

        # W2-001: a dirty combo that disappears from an accepted snapshot must
        # not be silently dropped. Re-add it as an explicit missing/conflicted
        # entry so its draft stays selectable until an explicit resolution.
        re_add_missing = bool(keep_dirty and prev_id and prev_id not in {c.get("id") for c in raw_combos})

        self.combos.clear()
        self.cb_combo_selector.blockSignals(True)
        self.cb_combo_selector.clear()
        self._missing_dirty_ids.clear()

        for c in raw_combos:
            cid = c.get("id")
            combo = StableCombo(
                combo_id=cid,
                name=c.get("name"),
                models=c.get("models", []),
                kind=c.get("kind"),
                updated_at=c.get("updatedAt", ""),
            )
            self.combos[cid] = combo
            self.cb_combo_selector.addItem(f"{combo.name} ({len(combo.models)} models)", cid)

        if keep_dirty and prev_id in self.combos:
            self.combos[prev_id] = self.current_combo
        elif re_add_missing:
            # Keep the local draft, labelled as missing from the server.
            self._missing_dirty_ids.add(prev_id)
            self.combos[prev_id] = self.current_combo
            self.cb_combo_selector.addItem(
                f"{self.current_combo.name} (missing on server; unsaved changes)", prev_id
            )

        self.cb_combo_selector.blockSignals(False)

        target_id = prev_id if prev_id in self.combos else (
            next(iter(self.combos)) if self.combos else None
        )
        if target_id is not None:
            idx = self.cb_combo_selector.findData(target_id)
            if idx >= 0:
                # setCurrentIndex is a no-op when the index did not change
                # (cleared combos reset to 0), so select explicitly too.
                self.cb_combo_selector.setCurrentIndex(idx)
                self._select_combo_by_index(idx)
        else:
            self.current_combo = None
            self._refresh_combo_models_list()
            self._refresh_available_list()

    def set_available_models(self, models: List[DiscoveredModel]):
        # Empty live catalogues are authoritative negative routing evidence,
        # but their inventory rows remain known for audit and re-probing.
        self._routing_blocked_model_ids = {
            m.canonical_id for m in models if not m.routing_eligible
        }
        self.available_models = [m for m in models if m.routing_eligible]
        self._refresh_combo_models_list()
        self._refresh_available_list()

    def _matches_picker_filter(self, cid: str, rec: Optional[ModelHealthRecord]) -> bool:
        filter_text = self.cb_picker_filter.currentText()
        if filter_text == "All":
            return True
        if filter_text == "Usable":
            return bool(rec and (rec.is_healthy() or rec.state in ("FREE/USE", "PAID", "USE/?")))
        if filter_text == "Free":
            return bool(rec and rec.cost_status == "FREE")
        if filter_text == "Paid":
            return bool(rec and rec.cost_status == "PAID")
        if filter_text == "Untested":
            return rec is None
        if filter_text == "Attention":
            # CORE-003: canonical non-LIVE states that need operator attention,
            # including the distinct timeout/route/model-invalid taxonomy.
            return bool(rec and rec.state in (
                "AUTH_REJECTED", "ACCESS_FORBIDDEN", "BALANCE_REQUIRED",
                "RATE_LIMITED", "CONNECT_TIMEOUT", "PROVIDER_ERROR",
                "ENDPOINT_OR_MODEL_INVALID", "MODEL_INVALID", "MODEL_MISSING",
                "ROUTE_ERROR", "MODEL_GONE", "ROUTER_DEGRADED", "DNS_FAILURE",
                "NON_API_HTML_RESPONSE", "WAF_BLOCKED", "BROWSER_CHALLENGE",
                "MODEL_DISCOVERY_UNAVAILABLE",
            ))
        if filter_text == "Dead":
            return bool(rec and rec.state == "DEAD")
        return True

    def _apply_picker_filter(self):
        query = self.txt_picker_search.text().strip().lower()
        for i in range(self.list_available_models.count()):
            it = self.list_available_models.item(i)
            cid = it.data(Qt.UserRole)
            if not cid:
                continue
            rec = self.cache.get(cid)
            matches_filter = self._matches_picker_filter(cid, rec)
            matches_query = (not query) or (query in cid.lower())
            self.list_available_models.setRowHidden(i, not (matches_filter and matches_query))

    def update_model_health_badge(self, canonical_id: str):
        """Targeted in-place update of items in both lists without full rebuilds.

        PERF-003: both list indices are maintained at rebuild time, so this is
        O(1) in the number of models instead of two full QList scans per probe
        completion (the audited ~674k item inspections on the scale fixture)."""
        rec = self.cache.get(canonical_id)
        state_text = f"[{rec.state}]" if rec else "[UNTESTED]"
        lat_text = f"{rec.latency_ms:.0f}ms" if rec and rec.latency_ms else "--"
        cfg = STATE_COLORS.get(rec.state) if rec else None
        fg_color = QColor(cfg["fg"]) if cfg else QColor(COLOR_TEXT_MUTED)

        # 1. Update in combo models list via keyed index.
        combo_item = self._combo_item_by_cid.get(canonical_id)
        if combo_item is not None:
            row = self._combo_row_by_cid.get(canonical_id, 0)
            combo_item.setText(f"#{row + 1:02d}  {canonical_id}   {state_text} ({lat_text})")
            combo_item.setForeground(fg_color)

        # 2. Update in available models picker list via keyed index.
        avail_item = self._available_item_by_cid.get(canonical_id)
        if avail_item is not None:
            row = self._available_row_by_cid.get(canonical_id)
            lat_str = f"{rec.latency_ms:.0f}ms" if rec and rec.latency_ms else ""
            avail_item.setText(f"{canonical_id}  {state_text} {lat_str}")
            avail_item.setForeground(fg_color)
            if row is not None:
                query = self.txt_picker_search.text().strip().lower()
                matches_filter = self._matches_picker_filter(canonical_id, rec)
                matches_query = (not query) or (query in canonical_id.lower())
                self.list_available_models.setRowHidden(row, not (matches_filter and matches_query))

    def _select_combo_by_index(self, idx: int):
        cid = self.cb_combo_selector.itemData(idx)
        if cid and cid in self.combos:
            self.current_combo = self.combos[cid]
            self._refresh_combo_models_list()

    def _on_combo_selection_changed(self, idx: int):
        self._select_combo_by_index(idx)

    def _refresh_combo_models_list(self):
        self.list_combo_models.clear()
        self._combo_item_by_cid.clear()
        self._combo_row_by_cid.clear()
        if not self.current_combo:
            self.lbl_combo_count.setText("Combo Models (0):")
            self.lbl_combo_model_count.setText("0 models")
            return

        models = self.current_combo.models
        self.lbl_combo_count.setText(f"{self.current_combo.name} ({len(models)} models):")
        self.lbl_combo_model_count.setText(f"{len(models)} models")
        self._update_dirty_state()

        for idx, m in enumerate(models):
            rec = self.cache.get(m)
            state_text = f"[{rec.state}]" if rec else "[UNTESTED]"
            lat_text = f"{rec.latency_ms:.0f}ms" if rec and rec.latency_ms else "--"

            text = f"#{idx + 1:02d}  {m}   {state_text} ({lat_text})"
            item = QListWidgetItem(text)
            item.setData(Qt.UserRole, m)

            # Style item per health
            if rec:
                cfg = STATE_COLORS.get(rec.state)
                if cfg:
                    item.setForeground(QColor(cfg["fg"]))
            else:
                item.setForeground(QColor(COLOR_TEXT_MUTED))
            if m in self._routing_blocked_model_ids:
                item.setForeground(QColor(COLOR_DANGER))

            self.list_combo_models.addItem(item)
            # PERF-003: maintain the keyed index as we build.
            self._combo_item_by_cid[m] = item
            self._combo_row_by_cid[m] = self.list_combo_models.count() - 1

    def _refresh_available_list(self):
        self.list_available_models.clear()
        self._available_item_by_cid.clear()
        self._available_row_by_cid.clear()
        current_set = set(self.current_combo.models) if self.current_combo else set()
        query = self.txt_picker_search.text().strip().lower()

        for m in self.available_models:
            cid = m.canonical_id
            if cid in current_set:
                continue

            rec = self.cache.get(cid)
            state_text = f"[{rec.state}]" if rec else "[UNTESTED]"
            lat_text = f"{rec.latency_ms:.0f}ms" if rec and rec.latency_ms else ""

            text = f"{cid}  {state_text} {lat_text}"
            item = QListWidgetItem(text)
            item.setData(Qt.UserRole, cid)

            if rec:
                cfg = STATE_COLORS.get(rec.state)
                if cfg:
                    item.setForeground(QColor(cfg["fg"]))
            else:
                item.setForeground(QColor(COLOR_TEXT_MUTED))

            self.list_available_models.addItem(item)

            row = self.list_available_models.count() - 1
            # PERF-003: maintain the keyed index as we build.
            self._available_item_by_cid[cid] = item
            self._available_row_by_cid[cid] = row
            matches_filter = self._matches_picker_filter(cid, rec)
            matches_query = (not query) or (query in cid.lower() or query in m.provider_name.lower())
            self.list_available_models.setRowHidden(row, not (matches_filter and matches_query))

    def _filter_available_models(self):
        self._apply_picker_filter()

    def _move_up(self):
        if not self.current_combo:
            return
        selected = self.list_combo_models.selectedItems()
        if not selected:
            return
        cid = selected[0].data(Qt.UserRole)
        if self.current_combo.move_up(cid):
            self._refresh_combo_models_list()
            self._select_in_combo_list(cid)

    def _move_down(self):
        if not self.current_combo:
            return
        selected = self.list_combo_models.selectedItems()
        if not selected:
            return
        cid = selected[0].data(Qt.UserRole)
        if self.current_combo.move_down(cid):
            self._refresh_combo_models_list()
            self._select_in_combo_list(cid)

    def _remove_selected(self):
        if not self.current_combo:
            return
        selected = self.list_combo_models.selectedItems()
        if not selected:
            return
        cids = [it.data(Qt.UserRole) for it in selected]
        self.current_combo.remove_models(cids)
        self._refresh_combo_models_list()
        self._refresh_available_list()

    def _on_drag_order_changed(self):
        if not self.current_combo:
            return
        new_order = []
        for i in range(self.list_combo_models.count()):
            cid = self.list_combo_models.item(i).data(Qt.UserRole)
            if cid:
                new_order.append(cid)
        if new_order:
            self.current_combo.models = new_order
            self._refresh_combo_models_list()

    def _on_model_dropped_from_picker(self, canonical_id: str, target_idx: int):
        if not self.current_combo:
            return
        if canonical_id in self._routing_blocked_model_ids:
            return
        if canonical_id in self.current_combo.models:
            self.current_combo.reorder_model(canonical_id, target_idx)
        else:
            self.current_combo.add_model(canonical_id, target_idx)
        self._refresh_combo_models_list()
        self._refresh_available_list()


    def _add_selected_from_picker(self):
        if not self.current_combo:
            return
        selected = self.list_available_models.selectedItems()
        if not selected:
            return
        for it in selected:
            cid = it.data(Qt.UserRole)
            self.current_combo.add_model(cid)
        self._refresh_combo_models_list()
        self._refresh_available_list()

    def _on_picker_double_clicked(self, item):
        """Double-click adds exactly the clicked model; selection is untouched."""
        if not self.current_combo:
            return
        cid = item.data(Qt.UserRole)
        if not cid:
            return
        if self.current_combo.add_model(cid):
            self._refresh_combo_models_list()
            self._refresh_available_list()

    def add_model_to_current(self, canonical_id: str):
        """Adds a single model directly (e.g. from Inspector)."""
        if self.current_combo:
            if canonical_id in self._routing_blocked_model_ids:
                return
            if self.current_combo.add_model(canonical_id):
                self._refresh_combo_models_list()
                self._refresh_available_list()

    def _retest_combo(self):
        if self.current_combo and self.current_combo.models:
            self.retest_combo_requested.emit(list(self.current_combo.models))

    def _select_in_combo_list(self, canonical_id: str):
        for i in range(self.list_combo_models.count()):
            it = self.list_combo_models.item(i)
            if it.data(Qt.UserRole) == canonical_id:
                self.list_combo_models.setCurrentItem(it)
                break

    def save_current_combo(self):
        """
        Saves current combo changes to 9Router following:
        READ (server state) -> MODEL (stable identity) -> DIFF (review) -> APPLY (API) -> VERIFY (read-back).
        Includes optimistic concurrency conflict detection against baseline.
        """
        if not self.current_combo:
            return

        try:
            # 1. READ current engine state
            server_combos = self.client.get_combos()
            server_match = next((c for c in server_combos if c.get("id") == self.current_combo.id), None)
            server_models = server_match.get("models", []) if server_match else []
            server_name = server_match.get("name", "") if server_match else ""
            server_kind = server_match.get("kind") if server_match else None
            server_updated = server_match.get("updatedAt", "") if server_match else ""
        except LiveAccessLockedError as ex:
            QMessageBox.warning(self, "Secrets Locked", str(ex))
            return

        # Concurrency Conflict Check: Has 9Router changed since we loaded this combo?
        if server_match and self.current_combo.check_server_conflict(server_name, server_kind, server_models, server_updated):
            dlg = ConflictDialog(
                combo_name=self.current_combo.name,
                baseline=self.current_combo.baseline_models,
                server=server_models,
                local=self.current_combo.models,
                parent=self,
                field_changes=self.current_combo.describe_server_divergence(
                    server_name, server_kind, server_models, server_updated
                ),
            )
            if dlg.exec() != QDialog.Accepted:
                return
            if dlg.action_choice == ConflictDialog.KEEP_SERVER:
                self.current_combo.name = server_name
                self.current_combo.kind = server_kind
                self.current_combo.models = list(server_models)
                self.current_combo.mark_clean(server_updated)
                self._refresh_combo_models_list()
                self._refresh_available_list()
                return
            # If OVERWRITE: continue saving local changes

        # 2. DIFF against server state
        diff = compute_combo_diff(server_models, self.current_combo.models)
        if not diff.is_empty():
            diff_dlg = DiffConfirmDialog(diff, self)
            if diff_dlg.exec() != QDialog.Accepted:
                return

        # 3. APPLY via 9Router API (Fail-Closed)
        res = self.client.update_combo(
            combo_id=self.current_combo.id,
            name=self.current_combo.name,
            models=self.current_combo.models,
            kind=self.current_combo.kind,
        )
        if not res:
            QMessageBox.critical(self, "Apply Failed", "Failed to apply combo update to 9Router backend (fail-closed).")
            return

        # 4. VERIFY: Read-back confirmation directly from engine.
        # CORE-004: verify the COMPLETE mutable state (name, kind, ordered
        # models) before mark_clean. Models-only verification turned a partial
        # backend write/normalization into false success and recorded unverified
        # local name/kind as the clean baseline.
        verified_combos = self.client.get_combos()
        verified_match = next((c for c in verified_combos if c.get("id") == self.current_combo.id), None)
        if verified_match is None:
            QMessageBox.warning(
                self, "Verification Mismatch",
                "Combo was saved, but backend read-back could not find it. Please refresh."
            )
            return
        mismatches = self.current_combo.server_field_mismatches(
            verified_match.get("name"),
            verified_match.get("kind"),
            verified_match.get("models", []),
        )
        if not mismatches:
            self.current_combo.mark_clean(verified_match.get("updatedAt", ""))
            QMessageBox.information(
                self, "Verified & Saved",
                f"Combo '{self.current_combo.name}' ({len(self.current_combo.models)} models) successfully applied and verified in 9Router."
            )
            self.combo_saved.emit(self.current_combo.name)
        else:
            # Do NOT mark_clean: the mismatching fields stay dirty so the
            # editor baseline never adopts unverified local state.
            QMessageBox.warning(
                self, "Verification Mismatch",
                "Combo was saved, but backend read-back returned different "
                f"state for: {', '.join(mismatches)}. Local edits are preserved; please refresh."
            )

    def _rename_combo(self):
        if not self.current_combo:
            return
        new_name, ok = QInputDialog.getText(
            self, "Rename Combo", "Enter new combo name:", text=self.current_combo.name
        )
        if not ok or not new_name.strip() or new_name.strip() == self.current_combo.name:
            return

        target_name = new_name.strip()
        cid = self.current_combo.id

        try:
            # 1. READ current engine state
            server_combos = self.client.get_combos()
            server_match = next((c for c in server_combos if c.get("id") == cid), None)
            server_models = server_match.get("models", []) if server_match else []
            server_name = server_match.get("name", "") if server_match else ""
            server_kind = server_match.get("kind") if server_match else None
            server_updated = server_match.get("updatedAt", "") if server_match else ""
        except LiveAccessLockedError as ex:
            QMessageBox.warning(self, "Secrets Locked", str(ex))
            return

        # W2-001: absence is NOT authoritative empty state. A rename requires a
        # positively matched server combo; without one, synthesize no mutation
        # (previously server_models=[]/server_kind=None would erase models/kind
        # if a transient read missed the target but the later PUT succeeded).
        if server_match is None:
            QMessageBox.warning(
                self, "Combo Not Found",
                "The selected combo could not be read from 9Router; rename was "
                "not sent. Local state is unchanged — refresh and retry."
            )
            return

        # Concurrency Conflict Check: Has 9Router combo changed externally since baseline?
        if self.current_combo.check_server_conflict(server_name, server_kind, server_models, server_updated):
            dlg = ConflictDialog(
                combo_name=self.current_combo.name,
                baseline=self.current_combo.baseline_models,
                server=server_models,
                local=self.current_combo.models,
                parent=self,
                field_changes=self.current_combo.describe_server_divergence(
                    server_name, server_kind, server_models, server_updated
                ),
            )
            if dlg.exec() != QDialog.Accepted:
                return
            if dlg.action_choice == ConflictDialog.KEEP_SERVER:
                self.current_combo.name = server_name
                self.current_combo.kind = server_kind
                self.current_combo.models = list(server_models)
                self.current_combo.mark_clean(server_updated)
                self.load_combos_from_9router()
                return

        # 2. APPLY via 9Router API (Fail-Closed). CORE-002: rename is NAME-ONLY.
        # The mutation must carry the authoritative server models/kind read in
        # step 1, never the dirty local values -- otherwise a rename silently
        # persists unsaved model edits. A combined name+models mutation is the
        # Save path's job, with its own full diff confirmation.
        res = self.client.rename_combo(
            combo_id=cid,
            new_name=target_name,
            current_models=list(server_models),
            kind=server_kind,
        )
        if not res:
            QMessageBox.critical(self, "Rename Failed", "Failed to rename combo in 9Router (fail-closed).")
            return

        # 3. VERIFY: Read-back confirmation directly from engine
        verified_combos = self.client.get_combos()
        verified_match = next((c for c in verified_combos if c.get("id") == cid), None)
        if verified_match and verified_match.get("name") == target_name:
            # CORE-002: the rename changes the NAME only. Updating the baseline
            # name/updatedAt records the server-side outcome while any local
            # model/kind edits stay dirty and unsaved (mark_clean() would adopt
            # them as saved state, which is the defect T-8 exists to close).
            self.current_combo.name = target_name
            self.current_combo.baseline_name = target_name
            verified_updated = verified_match.get("updatedAt", "")
            if verified_updated:
                self.current_combo.baseline_updated_at = verified_updated
            self.load_combos_from_9router()
            for i in range(self.cb_combo_selector.count()):
                if self.cb_combo_selector.itemData(i) == cid:
                    self.cb_combo_selector.setCurrentIndex(i)
                    break
            self.combo_saved.emit(target_name)
            QMessageBox.information(
                self, "Verified & Renamed",
                f"Combo successfully renamed to '{target_name}' in 9Router."
            )
        else:
            QMessageBox.warning(
                self, "Verification Mismatch",
                "Combo rename was applied, but backend read-back returned mismatched name. Please refresh."
            )

    def _revert_unsaved(self):
        if not self.current_combo:
            return
        if not self.current_combo.has_unsaved_changes():
            QMessageBox.information(self, "No Changes", "No unsaved changes in current combo.")
            return
        self.current_combo.revert_unsaved_changes()
        self._refresh_combo_models_list()
        self._refresh_available_list()
        self._update_dirty_state()

    def _show_manage_dialog(self):
        """Secondary surface for rare/destructive combo management (T-33)."""
        if not self.current_combo:
            QMessageBox.information(self, "No Combo", "Select a combo first.")
            return
        dlg = _ComboManageDialog(self.current_combo, self)
        dlg.exec()
        if dlg.delete_requested:
            self._delete_combo()

    def _show_tools_dialog(self):
        """Secondary surface for mass combo operations with preview (T-33)."""
        if not self.current_combo:
            QMessageBox.information(self, "No Combo", "Select a combo first.")
            return

        dlg = _ComboToolsDialog(self.current_combo, self.cache, self)
        if dlg.exec() == QDialog.Accepted:
            if dlg.changes_made:
                self._refresh_combo_models_list()
                self._refresh_available_list()
                self._update_dirty_state()

    def _update_dirty_state(self):
        """Update dirty state label and save button."""
        if self.current_combo and self.current_combo.id in self._missing_dirty_ids:
            self.lbl_dirty_state.setText("Unsaved changes (missing on server)")
            self.lbl_dirty_state.setStyleSheet(f"color: {COLOR_DANGER_TEXT}; font-weight: bold;")
            self.btn_save.setEnabled(True)
        elif self.current_combo and self.current_combo.has_unsaved_changes():
            self.lbl_dirty_state.setText("Unsaved changes")
            self.lbl_dirty_state.setStyleSheet(f"color: {COLOR_DANGER_TEXT}; font-weight: bold;")
            self.btn_save.setEnabled(True)
        else:
            self.lbl_dirty_state.setText("Saved")
            self.lbl_dirty_state.setStyleSheet(f"color: {COLOR_TEXT_SECONDARY};")
            self.btn_save.setEnabled(False)

    def _reload_current_from_server(self):
        """Explicit reload from the server (single get_combos call).

        First load (T-33 closure): with no current combo yet — e.g. right
        after a clean startup — Reload performs exactly one get_combos call,
        populates the selector and deterministically selects the first server
        combo. It never starts discovery, never triggers another hidden
        refresh and never switches tabs. An empty server list is a stable
        "No combos" state, not an error; New stays usable.

        Never runs silently with local edits: unsaved changes are confirmed
        away first (existing dirty-state semantics)."""
        if not self.current_combo:
            self.load_combos_from_9router()
            if not self.combos:
                self.lbl_combo_count.setText("No combos")
            return
        discard_dirty = False
        if self.current_combo.has_unsaved_changes():
            reply = QMessageBox.question(
                self, "Discard Unsaved Changes",
                f'Discard unsaved changes in combo "{self.current_combo.name}" and reload from 9Router?',
                QMessageBox.Yes | QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                return
            discard_dirty = True
        current_id = self.current_combo.id
        self.load_combos_from_9router(discard_dirty=discard_dirty)
        # replace_combos() already reselects current_id when it still exists;
        # fall back to an explicit selection if the signal path was blocked.
        if self.current_combo is None or self.current_combo.id != current_id:
            for i in range(self.cb_combo_selector.count()):
                if self.cb_combo_selector.itemData(i) == current_id:
                    self.cb_combo_selector.setCurrentIndex(i)
                    break

    def _create_new_combo(self):
        name, ok = QInputDialog.getText(self, "New Combo", "Enter new combo name (alphanumeric, -, _):")
        if ok and name.strip():
            try:
                cname = name.strip()
                res = self.client.create_combo(name=cname, models=[])
            except LiveAccessLockedError as ex:
                QMessageBox.warning(self, "Secrets Locked", str(ex))
                return
            if res:
                self.load_combos_from_9router()
                for i in range(self.cb_combo_selector.count()):
                    if self.cb_combo_selector.itemText(i).startswith(cname):
                        self.cb_combo_selector.setCurrentIndex(i)
                        break

    def _duplicate_combo(self):
        if not self.current_combo:
            return
        try:
            new_name = f"{self.current_combo.name}_copy"
            res = self.client.create_combo(name=new_name, models=list(self.current_combo.models))
        except LiveAccessLockedError as ex:
            QMessageBox.warning(self, "Secrets Locked", str(ex))
            return
        if res:
            self.load_combos_from_9router()

    def _delete_combo(self):
        if not self.current_combo:
            return
        name = self.current_combo.name
        reply = QMessageBox.question(
            self, "Delete Combo",
            f'Delete combo "{name}" from 9Router?',
            QMessageBox.Yes | QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return
        try:
            deleted = self.client.delete_combo(self.current_combo.id)
        except LiveAccessLockedError as ex:
            QMessageBox.warning(self, "Secrets Locked", str(ex))
            return
        if deleted:
            self.current_combo = None
            self.load_combos_from_9router()


class _ComboManageDialog(QDialog):
    """Combo management surface: rare/destructive operations live here,
    away from the permanent editing strip (T-33)."""

    def __init__(self, combo: StableCombo, parent=None):
        super().__init__(parent)
        self.combo = combo
        self.delete_requested = False
        self.setWindowTitle(f"Manage Combo: {combo.name}")
        self.setMinimumWidth(340)

        layout = QVBoxLayout(self)
        layout.setSpacing(8)

        layout.addWidget(QLabel(f'Combo: "{combo.name}" ({len(combo.models)} models)'))

        btn_delete = QPushButton("Delete combo...")
        btn_delete.setObjectName("dangerAction")
        btn_delete.clicked.connect(self._on_delete_clicked)
        layout.addWidget(btn_delete)

        layout.addStretch()

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        btn_close = QPushButton("Close")
        btn_close.clicked.connect(self.reject)
        btn_row.addWidget(btn_close)
        layout.addLayout(btn_row)

    def _on_delete_clicked(self):
        self.delete_requested = True
        self.accept()


class _ComboToolsDialog(QDialog):
    """Mass operations dialog with preview (T-33 Tools... surface)."""

    def __init__(self, combo: StableCombo, cache: HealthCache, parent=None):
        super().__init__(parent)
        self.combo = combo
        self.cache = cache
        self.changes_made = False
        self.setWindowTitle(f"Combo Tools: {combo.name}")
        self.setMinimumSize(500, 400)
        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(8)

        # Healthy first
        self.chk_healthy_first = QCheckBox("Move healthy models to top")
        layout.addWidget(self.chk_healthy_first)

        # Purge DEAD
        self.chk_purge_dead = QCheckBox("Remove DEAD models")
        layout.addWidget(self.chk_purge_dead)

        # Add all FREE/USE
        self.chk_add_free = QCheckBox("Add all FREE/USE models")
        layout.addWidget(self.chk_add_free)

        # Preview section
        self.preview_text = QTextEdit()
        self.preview_text.setReadOnly(True)
        self.preview_text.setMaximumHeight(150)
        self.preview_text.setStyleSheet("font-family: monospace; font-size: 10px;")
        layout.addWidget(QLabel("Preview of changes:"))
        layout.addWidget(self.preview_text)

        # Update preview when checkboxes change
        self.chk_healthy_first.stateChanged.connect(self._update_preview)
        self.chk_purge_dead.stateChanged.connect(self._update_preview)
        self.chk_add_free.stateChanged.connect(self._update_preview)

        layout.addStretch()

        # Buttons
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("Apply Changes")
        buttons.accepted.connect(self._apply_changes)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._update_preview()

    def _update_preview(self):
        """Generate preview of proposed changes."""
        preview_lines = []
        preview_lines.append(f"Current combo ({len(self.combo.models)} models):")
        preview_lines.extend(f"  {m}" for m in self.combo.models)
        preview_lines.append("")

        changes = []

        if self.chk_healthy_first.isChecked():
            healthy = []
            unhealthy = []
            for m in self.combo.models:
                rec = self.cache.get(m)
                if rec and rec.is_healthy():
                    healthy.append(m)
                else:
                    unhealthy.append(m)
            if healthy != self.combo.models[:len(healthy)]:
                changes.append(f"Move {len(healthy)} healthy models to top")

        if self.chk_purge_dead.isChecked():
            dead_count = sum(1 for m in self.combo.models
                            if self.cache.get(m) and self.cache.get(m).is_dead())
            if dead_count > 0:
                changes.append(f"Remove {dead_count} DEAD models")

        if self.chk_add_free.isChecked():
            # PERF-004: one coherent locked snapshot instead of iterating
            # cache.records (which races concurrent record_evidence inserts).
            snap = self.cache.snapshot_records()
            free_models = [cid for cid, rec in snap.items()
                          if rec.is_healthy() and rec.cost_status == "FREE"]
            combo_set = set(self.combo.models)
            not_in_combo = [m for m in free_models if m not in combo_set]
            if not_in_combo:
                changes.append(f"Add {len(not_in_combo)} FREE/USE models")

        if changes:
            preview_lines.append("Proposed changes:")
            preview_lines.extend(f"  - {c}" for c in changes)
        else:
            preview_lines.append("No changes will be made.")

        self.preview_text.setPlainText("\n".join(preview_lines))

    def _apply_changes(self):
        """Apply the selected mass operations."""
        if self.chk_healthy_first.isChecked():
            healthy = []
            unhealthy = []
            for m in self.combo.models:
                rec = self.cache.get(m)
                if rec and rec.is_healthy():
                    healthy.append(m)
                else:
                    unhealthy.append(m)
            self.combo.models = healthy + unhealthy

        if self.chk_purge_dead.isChecked():
            self.combo.models = [m for m in self.combo.models
                               if not (self.cache.get(m) and self.cache.get(m).is_dead())]

        if self.chk_add_free.isChecked():
            # PERF-004: one coherent locked snapshot; O(F+K) membership via a set.
            snap = self.cache.snapshot_records()
            free_models = [cid for cid, rec in snap.items()
                          if rec.is_healthy() and rec.cost_status == "FREE"]
            combo_set = set(self.combo.models)
            for m in free_models:
                if m not in combo_set:
                    self.combo.models.append(m)
                    combo_set.add(m)

        self.changes_made = True
        self.accept()
