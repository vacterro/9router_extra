"""
9router_WatchEdit - Models View (primary operational screen)
Compact model health table with one-click state filters, search, and scan
actions. Detailed diagnostics open explicitly (double-click or Enter), never
on selection change.
"""
from typing import Dict, List, Optional
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
    COLOR_SURFACE,
    COLOR_TEXT_PRIMARY,
    STATE_COLORS,
    get_app_font,
)

# Direct one-click filters (T-33: no permanent Pending/Paid primary pills;
# pending is a temporary in-scan state shown in the State column).
FILTER_ALL = None
FILTER_USABLE = "USABLE"
FILTER_FREE = "FREE"
FILTER_ATTENTION = "ATTENTION"
FILTER_DEAD = "DEAD"


class _ModelTable(QTableWidget):
    """Model table; Enter opens details for the current row explicitly."""

    details_requested = Signal(str)

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key_Return, Qt.Key_Enter):
            item = self.item(self.currentRow(), 0)
            cid = item.data(Qt.UserRole) if item else None
            if cid:
                self.details_requested.emit(cid)
                return
        super().keyPressEvent(event)


class WatchView(QWidget):
    scan_requested = Signal(str)  # "QUICK", "ALL", "FAILED_ONLY"
    refresh_inventory_requested = Signal()
    model_selected = Signal(str)  # canonical_id; selection alone opens nothing
    details_requested = Signal(str)  # canonical_id; explicit double-click / Enter

    COLUMN_MODEL = 0
    COLUMN_STATE = 1
    COLUMN_LATENCY = 2
    COLUMN_LAST_SUCCESS = 3

    def __init__(self, cache: HealthCache, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.cache = cache
        self.models: List[DiscoveredModel] = []
        self._current_filter_state: Optional[str] = None
        self._in_flight_pending: Dict[str, float] = {}  # canonical_id -> elapsed_sec
        # PERF-005 (SRC-001:R0014): canonical_id -> row index. A per-completion
        # update looks its row up in O(1) instead of scanning every row (the
        # audited defect was a linear row scan per completion => O(N^2) per
        # scan). Rebuilt once per inventory change, never per probe.
        self._row_by_cid: Dict[str, int] = {}
        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 2, 4, 2)
        layout.setSpacing(3)

        # 1. Action row: clear verbs, Quick scan is the primary action
        toolbar = QHBoxLayout()
        toolbar.setSpacing(4)

        self.btn_quick_scan = QPushButton("Quick scan")
        self.btn_quick_scan.setObjectName("primaryAction")
        self.btn_quick_scan.clicked.connect(lambda: self.scan_requested.emit("QUICK"))

        self.btn_scan_all = QPushButton("Full scan")
        self.btn_scan_all.clicked.connect(lambda: self.scan_requested.emit("ALL"))

        self.btn_retry_attention = QPushButton("Retry attention")
        self.btn_retry_attention.clicked.connect(lambda: self.scan_requested.emit("FAILED_ONLY"))

        self.btn_refresh = QPushButton("Refresh")
        self.btn_refresh.clicked.connect(self.refresh_inventory_requested.emit)

        toolbar.addWidget(self.btn_quick_scan)
        toolbar.addWidget(self.btn_scan_all)
        toolbar.addWidget(self.btn_retry_attention)
        toolbar.addWidget(self.btn_refresh)
        toolbar.addStretch()

        layout.addLayout(toolbar)

        # 2. Filter row: one-click state filters + labeled search + last scan status
        pills_layout = QHBoxLayout()
        pills_layout.setSpacing(4)

        self.filter_buttons: Dict[Optional[str], QPushButton] = {}
        pill_defs = [
            (FILTER_ALL, "All"),
            (FILTER_USABLE, "Usable"),
            (FILTER_FREE, "Free"),
            (FILTER_ATTENTION, "Attention"),
            (FILTER_DEAD, "Dead"),
        ]

        for state_val, label in pill_defs:
            btn = QPushButton(label)
            btn.setFixedHeight(18)
            btn.setFont(get_app_font(10))
            btn.clicked.connect(lambda checked=False, s=state_val: self._on_filter_pill_clicked(s))
            pills_layout.addWidget(btn)
            self.filter_buttons[state_val] = btn

        pills_layout.addStretch()

        lbl_search = QLabel("Search:")
        pills_layout.addWidget(lbl_search)

        self.txt_search = QLineEdit()
        self.txt_search.setPlaceholderText("provider/model...")
        self.txt_search.setMaximumWidth(160)
        self.txt_search.textChanged.connect(self._apply_filter)
        pills_layout.addWidget(self.txt_search)

        self.lbl_last_scan = QLabel("Last scan: Never")
        self.lbl_last_scan.setStyleSheet(f"font-size: 10px; color: {COLOR_BORDER_HIGHLIGHT};")
        pills_layout.addWidget(self.lbl_last_scan)

        layout.addLayout(pills_layout)

        # 3. Model table: only what is needed for scanning; details live in the
        #    explicit Model Details dialog, not in a wide Reason/Detail column.
        self.table = _ModelTable()
        self.table.setFont(get_app_font(11))
        self.table.setColumnCount(4)
        self.table.setHorizontalHeaderLabels(["Model", "State", "Latency", "Last success"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSortingEnabled(True)
        self.table.verticalHeader().setVisible(False)
        self.table.setShowGrid(False)

        header = self.table.horizontalHeader()
        header.setFont(get_app_font(11, bold=True))
        header.setStretchLastSection(False)
        header.setSectionResizeMode(self.COLUMN_MODEL, QHeaderView.Stretch)
        for col, width in (
            (self.COLUMN_STATE, 110),
            (self.COLUMN_LATENCY, 80),
            (self.COLUMN_LAST_SUCCESS, 130),
        ):
            header.setSectionResizeMode(col, QHeaderView.Fixed)
            self.table.setColumnWidth(col, width)

        self.table.itemSelectionChanged.connect(self._on_row_selected)
        self.table.cellDoubleClicked.connect(self._on_row_double_clicked)
        self.table.details_requested.connect(self.details_requested.emit)
        layout.addWidget(self.table)

    # ------------------------------------------------------------ public api
    def set_models(self, models: List[DiscoveredModel]):
        """Replace inventory; keeps the current selection when it still exists."""
        selected_cid = self._selected_cid()
        self.models = models
        self.refresh_table()
        if selected_cid:
            self.select_model(selected_cid)

    def select_model(self, canonical_id: str) -> bool:
        row = self._row_by_cid.get(canonical_id, -1)
        if row < 0:
            return False
        item = self.table.item(row, self.COLUMN_MODEL)
        if not item or item.data(Qt.UserRole) != canonical_id:
            return False
        self.table.selectRow(row)
        return True

    def set_last_scan_time(self, timestamp_str: str):
        self.lbl_last_scan.setText(f"Last scan: {timestamp_str}")

    def set_scan_active(self, active: bool):
        """Freezes sorting while live scan updates rows to prevent jumps."""
        self.table.setSortingEnabled(not active)
        if not active:
            # Re-enabling sorting reorders rows: the keyed index must follow.
            self._rebuild_row_index()

    def update_probe_pending(self, canonical_id: str, elapsed_sec: float):
        self._in_flight_pending[canonical_id] = elapsed_sec
        self._update_row_state_cell(canonical_id, f"PENDING ({elapsed_sec:.0f}s)", HealthState.PENDING.value)

    def update_probe_result(self, canonical_id: str, record: ModelHealthRecord):
        if canonical_id in self._in_flight_pending:
            del self._in_flight_pending[canonical_id]
        self._update_table_row(canonical_id, record)

    # ------------------------------------------------------------ table build
    def refresh_table(self):
        self.table.setSortingEnabled(False)
        selected_cid = self._selected_cid()
        self.table.setRowCount(len(self.models))

        for row, m in enumerate(self.models):
            rec = self.cache.get(m.canonical_id)

            # Col 0: Model (canonical identity, provider prefix included)
            item_model = QTableWidgetItem(m.canonical_id)
            item_model.setData(Qt.UserRole, m.canonical_id)
            if m.is_combo_member:
                item_model.setForeground(QColor(COLOR_BORDER_HIGHLIGHT))

            # Col 1: State
            state_str = rec.state if rec else HealthState.UNKNOWN.value
            if m.canonical_id in self._in_flight_pending:
                elapsed = self._in_flight_pending[m.canonical_id]
                state_str = f"PENDING ({elapsed:.0f}s)"
            item_state = QTableWidgetItem(state_str)
            self._style_state_item(item_state, rec.state if rec else HealthState.UNKNOWN.value)

            # Col 2: Latency
            lat_str = f"{rec.latency_ms:.0f} ms" if rec and rec.latency_ms else "--"
            item_lat = QTableWidgetItem(lat_str)
            item_lat.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)

            # Col 3: Last success
            succ_str = rec.last_success_at[:19].replace("T", " ") if rec and rec.last_success_at else "--"
            item_succ = QTableWidgetItem(succ_str)

            self.table.setItem(row, self.COLUMN_MODEL, item_model)
            self.table.setItem(row, self.COLUMN_STATE, item_state)
            self.table.setItem(row, self.COLUMN_LATENCY, item_lat)
            self.table.setItem(row, self.COLUMN_LAST_SUCCESS, item_succ)

        self.table.setSortingEnabled(True)
        self._rebuild_row_index()
        if selected_cid:
            self.select_model(selected_cid)
        self._apply_filter()

    def _rebuild_row_index(self):
        """Rebuild the O(1) canonical_id -> row map after any reorder/rebuild."""
        index: Dict[str, int] = {}
        for row in range(self.table.rowCount()):
            item = self.table.item(row, self.COLUMN_MODEL)
            if item is not None:
                cid = item.data(Qt.UserRole)
                if cid is not None:
                    index[cid] = row
        self._row_by_cid = index

    def _style_state_item(self, item: QTableWidgetItem, state_val: str):
        cfg = STATE_COLORS.get(state_val, {"bg": COLOR_SURFACE, "fg": COLOR_TEXT_PRIMARY})
        item.setBackground(QColor(cfg["bg"]))
        item.setForeground(QColor(cfg["fg"]))
        item.setTextAlignment(Qt.AlignCenter)

    # ------------------------------------------------------------ filtering
    def _matches_filter(self, cid: str, rec: Optional[ModelHealthRecord], filter_state: Optional[str]) -> bool:
        if filter_state is None:
            return True
        if filter_state == FILTER_DEAD:
            return bool(rec and (rec.is_dead() or rec.state == "DEAD"))
        if filter_state == FILTER_USABLE:
            return bool(rec and (rec.is_healthy() or rec.state in ("FREE/USE", "PAID", "USE/?")))
        if filter_state == FILTER_FREE:
            return bool(rec and (rec.cost_status == "FREE" or rec.state == "FREE/USE"))
        if filter_state == FILTER_ATTENTION:
            return (rec is None) or (not rec.is_healthy() and not rec.is_dead())
        return True

    def _eval_row_visibility(self, row: int, cid: Optional[str] = None, rec: Optional[ModelHealthRecord] = None):
        if cid is None:
            item_cid = self.table.item(row, self.COLUMN_MODEL)
            if not item_cid:
                return
            cid = item_cid.data(Qt.UserRole) or ""
        if rec is None:
            rec = self.cache.get(cid)

        if not self._matches_filter(cid, rec, self._current_filter_state):
            self.table.setRowHidden(row, True)
            return

        query = self.txt_search.text().strip().lower()
        if query:
            row_text = " ".join(
                (self.table.item(row, c).text() if self.table.item(row, c) else "")
                for c in range(4)
            ).lower() + f" {cid.lower()}"
            if query not in row_text:
                self.table.setRowHidden(row, True)
                return

        self.table.setRowHidden(row, False)

    def _on_filter_pill_clicked(self, state: Optional[str]):
        self._current_filter_state = state
        for s_val, btn in self.filter_buttons.items():
            if s_val == state:
                btn.setStyleSheet(
                    f"background-color: {COLOR_BORDER_HIGHLIGHT}; color: #000000; font-weight: bold;"
                )
            else:
                btn.setStyleSheet("")
        self._apply_filter()

    def _apply_filter(self):
        for row in range(self.table.rowCount()):
            self._eval_row_visibility(row)

    # ------------------------------------------------------------ row updates
    def _find_row(self, canonical_id: str) -> int:
        """O(1) keyed lookup (PERF-005). Falls back to a linear scan only when
        the index is stale, so correctness is preserved if a row was mutated
        outside this view's helpers."""
        row = self._row_by_cid.get(canonical_id, -1)
        if row >= 0:
            item = self.table.item(row, self.COLUMN_MODEL)
            if item is not None and item.data(Qt.UserRole) == canonical_id:
                return row
        return self._find_row_linear(canonical_id)

    def _find_row_linear(self, canonical_id: str) -> int:
        for row in range(self.table.rowCount()):
            item = self.table.item(row, self.COLUMN_MODEL)
            if item is not None and item.data(Qt.UserRole) == canonical_id:
                self._row_by_cid[canonical_id] = row
                return row
        return -1

    def _update_row_state_cell(self, canonical_id: str, display_text: str, state_val: str):
        row = self._find_row(canonical_id)
        if row < 0:
            return
        item_state = self.table.item(row, self.COLUMN_STATE)
        if item_state:
            item_state.setText(display_text)
            self._style_state_item(item_state, state_val)
        self._eval_row_visibility(row, cid=canonical_id)

    def _update_table_row(self, canonical_id: str, rec: ModelHealthRecord):
        row = self._find_row(canonical_id)
        if row < 0:
            return
        item_state = self.table.item(row, self.COLUMN_STATE)
        if item_state:
            item_state.setText(rec.state)
            self._style_state_item(item_state, rec.state)
        item_lat = self.table.item(row, self.COLUMN_LATENCY)
        if item_lat:
            item_lat.setText(f"{rec.latency_ms:.0f} ms" if rec.latency_ms else "--")
        item_succ = self.table.item(row, self.COLUMN_LAST_SUCCESS)
        if item_succ:
            succ_str = rec.last_success_at[:19].replace("T", " ") if rec.last_success_at else "--"
            item_succ.setText(succ_str)
        self._eval_row_visibility(row, cid=canonical_id, rec=rec)

    # ------------------------------------------------------------ selection
    def _selected_cid(self) -> Optional[str]:
        rows = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        if rows:
            item = self.table.item(rows[0].row(), self.COLUMN_MODEL)
            if item:
                return item.data(Qt.UserRole)
        return None

    def _on_row_selected(self):
        cid = self._selected_cid()
        if cid:
            self.model_selected.emit(cid)

    def _on_row_double_clicked(self, row: int, column: int):
        item = self.table.item(row, self.COLUMN_MODEL)
        cid = item.data(Qt.UserRole) if item else None
        if cid:
            self.details_requested.emit(cid)
