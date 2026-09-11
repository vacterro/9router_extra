"""
9router_WatchEdit - Watch Dashboard View
Displays real-time provider/model health table, quick operational filter pills,
search filter, and instant diagnostic selection.
"""
from datetime import datetime
from typing import Dict, List, Optional, Set
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QLineEdit,
    QTableWidget,
    QTableWidgetItem,
    QHeaderView,
    QAbstractItemView,
)
from PySide6.QtGui import QColor

from core.discovery import DiscoveredModel
from core.history import HealthCache, ModelHealthRecord
from core.classification import HealthState
from ui.theme import (
    COLOR_BORDER_HIGHLIGHT,
    COLOR_TEXT_PRIMARY,
    COLOR_TEXT_SECONDARY,
    COLOR_TEXT_MUTED,
    COLOR_BACKGROUND_SOFT,
    COLOR_SURFACE,
    STATE_COLORS,
    get_app_font,
)

class WatchView(QWidget):
    scan_requested = Signal(str)  # "ALL", "QUICK", "FAILED_ONLY"
    refresh_inventory_requested = Signal()
    model_selected = Signal(str)  # canonical_id

    def __init__(self, cache: HealthCache, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.cache = cache
        self.models: List[DiscoveredModel] = []
        self._current_filter_state: Optional[str] = None
        self._in_flight_pending: Dict[str, float] = {}  # canonical_id -> elapsed_sec
        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 2, 4, 2)
        layout.setSpacing(3)

        # 1. Action Toolbar
        toolbar = QHBoxLayout()
        toolbar.setSpacing(4)

        self.btn_quick_scan = QPushButton("QUICK SCAN")
        self.btn_quick_scan.setObjectName("primaryAction")
        self.btn_quick_scan.clicked.connect(lambda: self.scan_requested.emit("QUICK"))

        self.btn_scan_all = QPushButton("SCAN ALL")
        self.btn_scan_all.clicked.connect(lambda: self.scan_requested.emit("ALL"))

        self.btn_failed_only = QPushButton("FAILED ONLY")
        self.btn_failed_only.clicked.connect(lambda: self.scan_requested.emit("FAILED_ONLY"))

        self.btn_refresh = QPushButton("REFRESH INVENTORY")
        self.btn_refresh.clicked.connect(self.refresh_inventory_requested.emit)

        self.lbl_last_scan = QLabel("Last scan: Never")
        self.lbl_last_scan.setStyleSheet(f"color: {COLOR_TEXT_SECONDARY}; font-size: 10px;")

        toolbar.addWidget(self.btn_quick_scan)
        toolbar.addWidget(self.btn_scan_all)
        toolbar.addWidget(self.btn_failed_only)
        toolbar.addWidget(self.btn_refresh)
        toolbar.addStretch()
        toolbar.addWidget(self.lbl_last_scan)

        layout.addLayout(toolbar)

        # 2. Quick Filter Pills Bar
        pills_layout = QHBoxLayout()
        pills_layout.setSpacing(4)

        self.filter_buttons: Dict[Optional[str], QPushButton] = {}
        pill_defs = [
            (None, "ALL"),
            ("USE", "USE"),
            ("FREE", "FREE"),
            ("PAID", "PAID"),
            ("ATTENTION", "ATTENTION"),
            ("PENDING", "PENDING"),
            ("DEAD", "DEAD"),
        ]

        for state_val, label in pill_defs:
            btn = QPushButton(label)
            btn.setFixedHeight(18)
            btn.setFont(get_app_font(10))
            btn.clicked.connect(lambda checked=False, s=state_val: self._on_filter_pill_clicked(s))
            pills_layout.addWidget(btn)
            self.filter_buttons[state_val] = btn

        pills_layout.addStretch()

        # Search box
        self.txt_search = QLineEdit()
        self.txt_search.setPlaceholderText("Search...")
        self.txt_search.setMaximumWidth(200)
        self.txt_search.textChanged.connect(self._apply_filter)
        pills_layout.addWidget(self.txt_search)

        layout.addLayout(pills_layout)

        # 3. Main Models Table
        self.table = QTableWidget()
        self.table.setFont(get_app_font(11))
        self.table.setColumnCount(6)
        self.table.setHorizontalHeaderLabels([
            "Provider", "Model", "State", "Latency", "Reason / Detail", "Last Success"
        ])
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setDragEnabled(True)
        self.table.setDragDropMode(QAbstractItemView.DragOnly)
        self.table.setSortingEnabled(True)

        header = self.table.horizontalHeader()
        header.setFont(get_app_font(11, bold=True))
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.Stretch)
        header.setSectionResizeMode(5, QHeaderView.ResizeToContents)

        self.table.itemSelectionChanged.connect(self._on_row_selected)
        layout.addWidget(self.table)

    def set_models(self, models: List[DiscoveredModel]):
        self.models = models
        self.refresh_table()

    def set_last_scan_time(self, timestamp_str: str):
        self.lbl_last_scan.setText(f"Last scan: {timestamp_str}")

    def update_probe_pending(self, canonical_id: str, elapsed_sec: float):
        self._in_flight_pending[canonical_id] = elapsed_sec
        self._update_row_state_cell(canonical_id, f"PENDING ({elapsed_sec:.0f}s)", HealthState.PENDING.value)
        self._update_filter_pill_counts()

    def update_probe_result(self, canonical_id: str, record: ModelHealthRecord):
        if canonical_id in self._in_flight_pending:
            del self._in_flight_pending[canonical_id]
        self._update_table_row(canonical_id, record)
        self._update_filter_pill_counts()

    def _on_filter_pill_clicked(self, state: Optional[str]):
        self._current_filter_state = state
        for s_val, btn in self.filter_buttons.items():
            if s_val == state:
                btn.setStyleSheet(f"background-color: {COLOR_BORDER_HIGHLIGHT}; color: #000000; font-weight: bold;")
            else:
                btn.setStyleSheet("")
        self._apply_filter()

    def set_scan_active(self, active: bool):
        """Freezes sorting while live scan updates rows to prevent flicker/jumps."""
        self.table.setSortingEnabled(not active)

    def _update_filter_pill_counts(self):
        counts = {
            "USE": 0,
            "FREE": 0,
            "PAID": 0,
            "ATTENTION": 0,
            "PENDING": len(self._in_flight_pending),
            "DEAD": 0,
        }
        total = len(self.models)
        for m in self.models:
            cid = m.canonical_id
            rec = self.cache.get(cid)
            if rec:
                if rec.is_dead() or rec.state == "DEAD":
                    counts["DEAD"] += 1
                elif rec.is_healthy() or rec.state in ("FREE/USE", "PAID", "USE/?"):
                    counts["USE"] += 1
                    if rec.cost_status == "FREE" or rec.state == "FREE/USE":
                        counts["FREE"] += 1
                    elif rec.cost_status == "PAID" or rec.state == "PAID":
                        counts["PAID"] += 1
                else:
                    counts["ATTENTION"] += 1
            else:
                counts["ATTENTION"] += 1

        self.filter_buttons[None].setText(f"ALL ({total})")
        self.filter_buttons["USE"].setText(f"USE ({counts['USE']})")
        self.filter_buttons["FREE"].setText(f"FREE ({counts['FREE']})")
        self.filter_buttons["PAID"].setText(f"PAID ({counts['PAID']})")
        self.filter_buttons["ATTENTION"].setText(f"ATT ({counts['ATTENTION']})")
        self.filter_buttons["PENDING"].setText(f"PEND ({counts['PENDING']})")
        self.filter_buttons["DEAD"].setText(f"DEAD ({counts['DEAD']})")

    def refresh_table(self):
        self.table.setSortingEnabled(False)
        self.table.setRowCount(len(self.models))

        for row, m in enumerate(self.models):
            rec = self.cache.get(m.canonical_id)
            
            # Col 0: Provider
            item_prov = QTableWidgetItem(m.provider_name or m.provider_prefix)
            item_prov.setData(Qt.UserRole, m.canonical_id)
            
            # Col 1: Model
            item_model = QTableWidgetItem(m.model_id)
            item_model.setData(Qt.UserRole, m.canonical_id)
            if m.is_combo_member:
                item_model.setForeground(QColor(COLOR_BORDER_HIGHLIGHT))

            # Col 2: State
            state_str = rec.state if rec else HealthState.UNKNOWN.value
            if m.canonical_id in self._in_flight_pending:
                elapsed = self._in_flight_pending[m.canonical_id]
                state_str = f"PENDING ({elapsed:.0f}s)"
            item_state = QTableWidgetItem(state_str)
            self._style_state_item(item_state, rec.state if rec else HealthState.UNKNOWN.value)

            # Col 3: Latency
            lat_str = f"{rec.latency_ms:.0f} ms" if rec and rec.latency_ms else "--"
            item_lat = QTableWidgetItem(lat_str)
            item_lat.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)

            # Col 4: Reason / Detail
            reason_str = rec.reason if rec else "(Untested)"
            item_reason = QTableWidgetItem(reason_str)

            # Col 5: Last Success
            succ_str = rec.last_success_at[:19].replace("T", " ") if rec and rec.last_success_at else "--"
            item_succ = QTableWidgetItem(succ_str)

            self.table.setItem(row, 0, item_prov)
            self.table.setItem(row, 1, item_model)
            self.table.setItem(row, 2, item_state)
            self.table.setItem(row, 3, item_lat)
            self.table.setItem(row, 4, item_reason)
            self.table.setItem(row, 5, item_succ)

        self.table.setSortingEnabled(True)
        self._update_filter_pill_counts()
        self._apply_filter()

    def _style_state_item(self, item: QTableWidgetItem, state_val: str):
        cfg = STATE_COLORS.get(state_val, {"bg": COLOR_SURFACE, "fg": COLOR_TEXT_PRIMARY})
        item.setBackground(QColor(cfg["bg"]))
        item.setForeground(QColor(cfg["fg"]))
        item.setTextAlignment(Qt.AlignCenter)

    def _eval_row_visibility(self, row: int, cid: Optional[str] = None, rec: Optional[ModelHealthRecord] = None):
        if cid is None:
            item_cid = self.table.item(row, 0)
            if not item_cid:
                return
            cid = item_cid.data(Qt.UserRole) or ""
        if rec is None:
            rec = self.cache.get(cid)

        filter_state = self._current_filter_state
        query = self.txt_search.text().strip().lower()

        # Match state filter
        if filter_state is not None:
            matches = False
            if filter_state == "PENDING":
                matches = (cid in self._in_flight_pending)
            elif filter_state == "DEAD":
                matches = bool(rec and (rec.is_dead() or rec.state == "DEAD"))
            elif filter_state == "USE":
                matches = bool(rec and (rec.is_healthy() or rec.state in ("FREE/USE", "PAID", "USE/?")))
            elif filter_state == "FREE":
                matches = bool(rec and (rec.cost_status == "FREE" or rec.state == "FREE/USE"))
            elif filter_state == "PAID":
                matches = bool(rec and (rec.cost_status == "PAID" or rec.state == "PAID"))
            elif filter_state == "ATTENTION":
                matches = (rec is None) or (not rec.is_healthy() and not rec.is_dead())

            if not matches:
                self.table.setRowHidden(row, True)
                return

        # Match text query
        if query:
            row_text = " ".join(
                (self.table.item(row, c).text() if self.table.item(row, c) else "")
                for c in range(6)
            ).lower() + f" {cid.lower()}"
            if query not in row_text:
                self.table.setRowHidden(row, True)
                return

        self.table.setRowHidden(row, False)

    def _update_row_state_cell(self, canonical_id: str, display_text: str, state_val: str):
        for row in range(self.table.rowCount()):
            item_id = self.table.item(row, 0)
            if item_id and item_id.data(Qt.UserRole) == canonical_id:
                item_state = self.table.item(row, 2)
                if item_state:
                    item_state.setText(display_text)
                    self._style_state_item(item_state, state_val)
                self._eval_row_visibility(row, cid=canonical_id)
                break

    def _update_table_row(self, canonical_id: str, rec: ModelHealthRecord):
        for row in range(self.table.rowCount()):
            item_id = self.table.item(row, 0)
            if item_id and item_id.data(Qt.UserRole) == canonical_id:
                # Update State
                item_state = self.table.item(row, 2)
                if item_state:
                    item_state.setText(rec.state)
                    self._style_state_item(item_state, rec.state)
                # Update Latency
                item_lat = self.table.item(row, 3)
                if item_lat:
                    item_lat.setText(f"{rec.latency_ms:.0f} ms" if rec.latency_ms else "--")
                # Update Reason
                item_reason = self.table.item(row, 4)
                if item_reason:
                    item_reason.setText(rec.reason)
                # Update Last Success
                item_succ = self.table.item(row, 5)
                if item_succ:
                    succ_str = rec.last_success_at[:19].replace("T", " ") if rec.last_success_at else "--"
                    item_succ.setText(succ_str)

                self._eval_row_visibility(row, cid=canonical_id, rec=rec)
                break

    def _apply_filter(self):
        for row in range(self.table.rowCount()):
            self._eval_row_visibility(row)

    def _on_row_selected(self):
        selected_rows = self.table.selectionModel().selectedRows()
        if selected_rows:
            row = selected_rows[0].row()
            item = self.table.item(row, 0)
            if item:
                cid = item.data(Qt.UserRole)
                if cid:
                    self.model_selected.emit(cid)
