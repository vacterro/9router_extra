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
)
from PySide6.QtGui import QColor

from core.router_client import RouterClient
from core.history import HealthCache, ModelHealthRecord
from core.discovery import DiscoveredModel
from core.combo_manager import StableCombo, compute_combo_diff, ComboDiff
from core.classification import HealthState
from core.security import LiveAccessLockedError
from ui.theme import (
    COLOR_BORDER_HIGHLIGHT,
    COLOR_TEXT_PRIMARY,
    COLOR_TEXT_SECONDARY,
    COLOR_TEXT_MUTED,
    COLOR_BACKGROUND_SOFT,
    COLOR_SURFACE,
    COLOR_ACCENT_TEAL,
    COLOR_DANGER,
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
        self._setup_ui()

    def _setup_ui(self):
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(4, 2, 4, 2)
        main_layout.setSpacing(3)

        # External Change Banner
        self.banner_external_change = QWidget()
        self.banner_external_change.setStyleSheet(f"background-color: {COLOR_SURFACE}; border: 1px solid {COLOR_BORDER_HIGHLIGHT};")
        b_layout = QHBoxLayout(self.banner_external_change)
        b_layout.setContentsMargins(4, 2, 4, 2)
        b_layout.setSpacing(4)

        lbl_b = QLabel("External change detected!")
        lbl_b.setStyleSheet(f"color: {COLOR_BORDER_HIGHLIGHT}; font-weight: bold; font-size: 10px;")
        b_layout.addWidget(lbl_b)
        b_layout.addStretch()

        btn_b_reload = QPushButton("Reload")
        btn_b_reload.clicked.connect(self._reload_current_from_server)
        b_layout.addWidget(btn_b_reload)

        btn_b_dismiss = QPushButton("Dismiss")
        btn_b_dismiss.clicked.connect(lambda: self.banner_external_change.setVisible(False))
        b_layout.addWidget(btn_b_dismiss)

        self.banner_external_change.setVisible(False)
        main_layout.addWidget(self.banner_external_change)

        # Top Bar Row 1: Combo Selector + Save
        top_row1 = QHBoxLayout()
        top_row1.setSpacing(4)

        top_row1.addWidget(QLabel("Combo:"))
        self.cb_combo_selector = QComboBox()
        self.cb_combo_selector.setMinimumWidth(120)
        self.cb_combo_selector.currentIndexChanged.connect(self._on_combo_selection_changed)
        top_row1.addWidget(self.cb_combo_selector, stretch=1)

        self.btn_save = QPushButton("SAVE TO 9ROUTER")
        self.btn_save.setObjectName("primaryAction")
        self.btn_save.setFixedHeight(22)
        self.btn_save.clicked.connect(self.save_current_combo)
        top_row1.addWidget(self.btn_save)

        main_layout.addLayout(top_row1)

        # Top Bar Row 2: Action buttons
        top_row2 = QHBoxLayout()
        top_row2.setSpacing(3)

        self.btn_new_combo = QPushButton("New")
        self.btn_new_combo.clicked.connect(self._create_new_combo)

        self.btn_rename = QPushButton("Rename")
        self.btn_rename.clicked.connect(self._rename_combo)

        self.btn_duplicate = QPushButton("Dup")
        self.btn_duplicate.clicked.connect(self._duplicate_combo)

        self.btn_revert = QPushButton("Revert")
        self.btn_revert.clicked.connect(self._revert_unsaved)

        self.btn_reload = QPushButton("Reload")
        self.btn_reload.clicked.connect(self.load_combos_from_9router)

        self.btn_delete = QPushButton("Delete")
        self.btn_delete.setObjectName("dangerAction")
        self.btn_delete.clicked.connect(self._delete_combo)

        for btn in (self.btn_new_combo, self.btn_rename, self.btn_duplicate,
                    self.btn_revert, self.btn_reload, self.btn_delete):
            top_row2.addWidget(btn)
        top_row2.addStretch()

        main_layout.addLayout(top_row2)

        # Passive External Change Detection Timer
        self.change_detection_timer = QTimer(self)
        self.change_detection_timer.setInterval(5000)
        self.change_detection_timer.timeout.connect(self._check_external_changes)
        self.change_detection_timer.start()

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

        # Smart Actions Bar
        smart_bar = QHBoxLayout()
        smart_bar.setSpacing(4)

        self.btn_smart_top = QPushButton("Healthy↑")
        self.btn_smart_top.clicked.connect(self._smart_move_healthy_to_top)

        self.btn_smart_purge = QPushButton("Purge DEAD")
        self.btn_smart_purge.clicked.connect(self._smart_remove_dead)

        self.btn_smart_add_free = QPushButton("+FREE/USE")
        self.btn_smart_add_free.clicked.connect(self._smart_add_all_free)

        smart_bar.addWidget(self.btn_smart_top)
        smart_bar.addWidget(self.btn_smart_purge)
        smart_bar.addWidget(self.btn_smart_add_free)
        left_layout.addLayout(smart_bar)

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
        right_header.addWidget(self.txt_picker_search)

        right_layout.addLayout(right_header)

        # Quick Filter Pills for Picker (Default: USE, hides untested models)
        picker_pills_layout = QHBoxLayout()
        picker_pills_layout.setSpacing(3)
        self.picker_filter_buttons: Dict[str, QPushButton] = {}
        picker_pill_defs = [
            ("ALL", "ALL"),
            ("USE", "USE"),
            ("FREE", "FREE"),
            ("PAID", "PAID"),
            ("UNTESTED", "UNTESTED"),
            ("ATTENTION", "ATTENTION"),
            ("DEAD", "DEAD"),
        ]
        self._picker_filter_state = "USE"
        for p_state, p_label in picker_pill_defs:
            btn = QPushButton(p_label)
            btn.setFixedHeight(16)
            btn.setFont(get_app_font(10))
            if p_state == "USE":
                btn.setStyleSheet(f"background-color: {COLOR_BORDER_HIGHLIGHT}; color: #000000; font-weight: bold;")
            btn.clicked.connect(lambda checked=False, s=p_state: self._on_picker_filter_pill_clicked(s))
            picker_pills_layout.addWidget(btn)
            self.picker_filter_buttons[p_state] = btn
        picker_pills_layout.addStretch()
        right_layout.addLayout(picker_pills_layout)

        self.list_available_models = QListWidget()
        self.list_available_models.setFont(get_app_font(11))
        self.list_available_models.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.list_available_models.setDragEnabled(True)
        self.list_available_models.setDragDropMode(QAbstractItemView.DragOnly)
        right_layout.addWidget(self.list_available_models)

        # Picker action bar
        picker_bar = QHBoxLayout()
        self.btn_add_to_combo = QPushButton("+ Add to Combo")
        self.btn_add_to_combo.setObjectName("primaryAction")
        self.btn_add_to_combo.clicked.connect(self._add_selected_from_picker)
        picker_bar.addWidget(self.btn_add_to_combo)
        right_layout.addLayout(picker_bar)

        splitter.addWidget(right_widget)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)

        main_layout.addWidget(splitter)

    def load_combos_from_9router(self):
        """Loads combos from 9Router and updates views."""
        try:
            self._load_combos_from_9router_locked()
        except LiveAccessLockedError as ex:
            QMessageBox.warning(self, "Secrets Locked", str(ex))

    def _load_combos_from_9router_locked(self):
        raw_combos = self.client.get_combos()
        self.combos.clear()
        self.cb_combo_selector.blockSignals(True)
        self.cb_combo_selector.clear()

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
            self.combos[cid] = combo
            self.cb_combo_selector.addItem(f"{name} ({len(models)} models)", cid)

        self.cb_combo_selector.blockSignals(False)

        if self.cb_combo_selector.count() > 0:
            self._select_combo_by_index(0)

    def set_available_models(self, models: List[DiscoveredModel]):
        # Empty live catalogues are authoritative negative routing evidence,
        # but their inventory rows remain known for audit and re-probing.
        self._routing_blocked_model_ids = {
            m.canonical_id for m in models if not m.routing_eligible
        }
        self.available_models = [m for m in models if m.routing_eligible]
        self._refresh_combo_models_list()
        self._refresh_available_list()

    def _on_picker_filter_pill_clicked(self, state: str):
        self._picker_filter_state = state
        for s_val, btn in self.picker_filter_buttons.items():
            if s_val == state:
                btn.setStyleSheet(f"background-color: {COLOR_BORDER_HIGHLIGHT}; color: #000000; font-weight: bold;")
            else:
                btn.setStyleSheet("")
        self._apply_picker_filter()

    def _matches_picker_filter(self, cid: str, rec: Optional[ModelHealthRecord]) -> bool:
        state = self._picker_filter_state
        if state == "ALL":
            return True
        if state == "USE":
            return bool(rec and (rec.is_healthy() or rec.state in ("FREE/USE", "PAID", "USE/?")))
        if state == "FREE":
            return bool(rec and (rec.cost_status == "FREE" or rec.state == "FREE/USE"))
        if state == "PAID":
            return bool(rec and (rec.cost_status == "PAID" or rec.state == "PAID"))
        if state == "UNTESTED":
            return rec is None or rec.state == HealthState.UNKNOWN.value
        if state == "ATTENTION":
            return bool(rec and not rec.is_healthy() and not rec.is_dead() and rec.state != HealthState.UNKNOWN.value)
        if state == "DEAD":
            return bool(rec and (rec.is_dead() or rec.state == "DEAD"))
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
        """Targeted in-place update of items in both lists without full rebuilds."""
        rec = self.cache.get(canonical_id)
        state_text = f"[{rec.state}]" if rec else "[UNTESTED]"
        lat_text = f"{rec.latency_ms:.0f}ms" if rec and rec.latency_ms else "--"
        cfg = STATE_COLORS.get(rec.state) if rec else None
        fg_color = QColor(cfg["fg"]) if cfg else QColor(COLOR_TEXT_MUTED)

        # 1. Update in combo models list
        for i in range(self.list_combo_models.count()):
            it = self.list_combo_models.item(i)
            if it.data(Qt.UserRole) == canonical_id:
                it.setText(f"#{i + 1:02d}  {canonical_id}   {state_text} ({lat_text})")
                it.setForeground(fg_color)
                break

        # 2. Update in available models picker list
        query = self.txt_picker_search.text().strip().lower()
        for i in range(self.list_available_models.count()):
            it = self.list_available_models.item(i)
            if it.data(Qt.UserRole) == canonical_id:
                lat_str = f"{rec.latency_ms:.0f}ms" if rec and rec.latency_ms else ""
                it.setText(f"{canonical_id}  {state_text} {lat_str}")
                it.setForeground(fg_color)

                matches_filter = self._matches_picker_filter(canonical_id, rec)
                matches_query = (not query) or (query in canonical_id.lower())
                self.list_available_models.setRowHidden(i, not (matches_filter and matches_query))
                break

    def _select_combo_by_index(self, idx: int):
        cid = self.cb_combo_selector.itemData(idx)
        if cid and cid in self.combos:
            self.current_combo = self.combos[cid]
            self._refresh_combo_models_list()

    def _on_combo_selection_changed(self, idx: int):
        self._select_combo_by_index(idx)

    def _refresh_combo_models_list(self):
        self.list_combo_models.clear()
        if not self.current_combo:
            self.lbl_combo_count.setText("Combo Models (0):")
            return

        models = self.current_combo.models
        self.lbl_combo_count.setText(f"{self.current_combo.name} ({len(models)} models):")

        for idx, m in enumerate(models):
            rec = self.cache.get(m)
            state_text = f"[{rec.state}]" if rec else "[UNTESTED]"
            lat_text = f"{rec.latency_ms:.0f}ms" if rec and rec.latency_ms else "--"

            routing_text = " [CATALOG EMPTY - ROUTING BLOCKED]" if m in self._routing_blocked_model_ids else ""
            text = f"#{idx + 1:02d}  {m}   {state_text} ({lat_text}){routing_text}"
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

    def _refresh_available_list(self):
        self.list_available_models.clear()
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

    def add_model_to_current(self, canonical_id: str):
        """Adds a single model directly (e.g. from Inspector)."""
        if self.current_combo:
            if canonical_id in self._routing_blocked_model_ids:
                return
            if self.current_combo.add_model(canonical_id):
                self._refresh_combo_models_list()
                self._refresh_available_list()

    def _smart_move_healthy_to_top(self):
        if not self.current_combo:
            return
        old_models = list(self.current_combo.models)
        diff = self.current_combo.move_healthy_to_top(self.cache)
        if diff.is_empty():
            QMessageBox.information(self, "No Change", "Healthy models are already at the top.")
            return

        dlg = DiffConfirmDialog(diff, self)
        if dlg.exec() == QDialog.Accepted:
            self._refresh_combo_models_list()
        else:
            self.current_combo.models = old_models

    def _smart_remove_dead(self):
        if not self.current_combo:
            return
        old_models = list(self.current_combo.models)
        diff = self.current_combo.remove_dead_models(self.cache)
        if diff.is_empty():
            QMessageBox.information(self, "No Dead Models", "No DEAD models detected in this combo.")
            return

        dlg = DiffConfirmDialog(diff, self)
        if dlg.exec() == QDialog.Accepted:
            self._refresh_combo_models_list()
            self._refresh_available_list()
        else:
            self.current_combo.models = old_models

    def _smart_add_all_free(self):
        if not self.current_combo:
            return
        old_models = list(self.current_combo.models)
        free_models = []
        for m in self.available_models:
            rec = self.cache.get(m.canonical_id)
            if rec and rec.state == HealthState.FREE_USE.value:
                if m.canonical_id not in self.current_combo.models:
                    free_models.append(m.canonical_id)

        if not free_models:
            QMessageBox.information(self, "No Models", "No additional verified FREE/USE models found.")
            return

        for cid in free_models:
            self.current_combo.add_model(cid)

        diff = compute_combo_diff(old_models, self.current_combo.models)
        dlg = DiffConfirmDialog(diff, self)
        if dlg.exec() == QDialog.Accepted:
            self._refresh_combo_models_list()
            self._refresh_available_list()
        else:
            self.current_combo.models = old_models

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
                self.banner_external_change.setVisible(False)
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

        # 4. VERIFY: Read-back confirmation directly from engine
        verified_combos = self.client.get_combos()
        verified_match = next((c for c in verified_combos if c.get("id") == self.current_combo.id), None)
        if verified_match and verified_match.get("models") == self.current_combo.models:
            self.current_combo.mark_clean(verified_match.get("updatedAt", ""))
            self.banner_external_change.setVisible(False)
            QMessageBox.information(
                self, "Verified & Saved",
                f"Combo '{self.current_combo.name}' ({len(self.current_combo.models)} models) successfully applied and verified in 9Router."
            )
            self.combo_saved.emit(self.current_combo.name)
        else:
            QMessageBox.warning(
                self, "Verification Mismatch",
                "Combo was saved, but backend read-back returned different state. Please refresh."
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

        # Concurrency Conflict Check: Has 9Router combo changed externally since baseline?
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
                self.banner_external_change.setVisible(False)
                self.load_combos_from_9router()
                return

        # 2. APPLY via 9Router API (Fail-Closed) passing models + kind
        res = self.client.rename_combo(
            combo_id=cid,
            new_name=target_name,
            current_models=self.current_combo.models,
            kind=self.current_combo.kind,
        )
        if not res:
            QMessageBox.critical(self, "Rename Failed", "Failed to rename combo in 9Router (fail-closed).")
            return

        # 3. VERIFY: Read-back confirmation directly from engine
        verified_combos = self.client.get_combos()
        verified_match = next((c for c in verified_combos if c.get("id") == cid), None)
        if verified_match and verified_match.get("name") == target_name:
            self.current_combo.name = target_name
            self.current_combo.mark_clean(verified_match.get("updatedAt", ""))
            self.banner_external_change.setVisible(False)
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

    def _check_external_changes(self):
        if not self.current_combo:
            return
        try:
            combos = self.client.get_combos()
            match = next((c for c in combos if c.get("id") == self.current_combo.id), None)
            if not match:
                return
            server_models = match.get("models", [])
            server_name = match.get("name", "")
            server_kind = match.get("kind")
            server_updated = match.get("updatedAt", "")
            if self.current_combo.check_server_conflict(server_name, server_kind, server_models, server_updated):
                self.banner_external_change.setVisible(True)
                self.external_change_detected.emit()
        except Exception:
            pass

    def _reload_current_from_server(self):
        if not self.current_combo:
            return
        current_id = self.current_combo.id
        self.load_combos_from_9router()
        for i in range(self.cb_combo_selector.count()):
            if self.cb_combo_selector.itemData(i) == current_id:
                self.cb_combo_selector.setCurrentIndex(i)
                break
        self.banner_external_change.setVisible(False)

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
        reply = QMessageBox.question(
            self, "Delete Combo",
            f"Are you sure you want to delete combo '{self.current_combo.name}' from 9Router?",
            QMessageBox.Yes | QMessageBox.No,
        )
        if reply == QMessageBox.Yes:
            try:
                deleted = self.client.delete_combo(self.current_combo.id)
            except LiveAccessLockedError as ex:
                QMessageBox.warning(self, "Secrets Locked", str(ex))
                return
            if deleted:
                self.load_combos_from_9router()
