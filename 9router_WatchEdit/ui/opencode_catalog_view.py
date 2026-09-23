"""
9router_WatchEdit - OpenCode Catalog Panel (SRC-004, T-33 third tab)

Compact catalog status + free-candidate table. Read-only; never auto-mutates
combos/providers/routing; never fetches on its own (refresh is explicit).
Catalog events live behind the explicit "View events..." surface.
"""
import time
from typing import Dict, List

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QDialog,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QListWidget,
    QListWidgetItem,
    QHeaderView,
    QAbstractItemView,
)

from core.opencode_catalog import OpenCodeCatalogDiscovery, ZenCatalogModel
from core.ocf_registry import (
    DIRECT_LOCAL_BRIDGE_REQUIRED,
    DIRECT_ROUTABLE,
    DIRECT_UNKNOWN,
)

# OCF-001: explicit operator actions only. No background mutation, no silent
# combo rewrite, no polling that changes UI state under the user.
OCF_ACTION_REFRESH = "Refresh free catalog"
OCF_ACTION_TEST_BRIDGE = "Test local OpenCode bridge"
OCF_ACTION_SYNC_TAIL = "Sync verified bridge models to SAIFREN bottom"


def _cooldown_text(cooldown_until) -> str:
    try:
        remaining = float(cooldown_until) - time.time()
    except (TypeError, ValueError):
        return "-"
    if remaining <= 0:
        return "-"
    if remaining >= 60:
        return f"{int(remaining // 60)}m"
    return f"{int(remaining)}s"


def _timestamp_text(value: str) -> str:
    return value[:16].replace("T", " ") if value else "-"


class OpenCodeCatalogPanel(QWidget):
    refresh_requested = Signal(bool)
    # OCF-001 explicit actions (wired by MainWindow; disabled when unwired).
    bridge_test_requested = Signal()
    sync_tail_requested = Signal()

    def __init__(self, discovery: OpenCodeCatalogDiscovery, parent=None):
        super().__init__(parent)
        self.discovery = discovery
        self._bridge_label = "Bridge: not tested"
        self._build()

    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(2)

        # Status strip: compact, one line, never reflows (fixed role per label)
        status = QHBoxLayout()
        status.setContentsMargins(0, 0, 0, 0)
        status.setSpacing(6)
        self.lbl_models = QLabel("Models: 0")
        self.lbl_free = QLabel("Free: 0")
        self.lbl_last = QLabel("Updated: -")
        # Timestamp text varies in length; it must never pin the panel's
        # minimum width (640x480 must fit) nor reflow neighbors on refresh.
        self.lbl_last.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.lbl_last.setMinimumWidth(1)
        self.lbl_api_status = QLabel("Status: STALE")
        self.lbl_bridge = QLabel(self._bridge_label)
        self.btn_events = QPushButton("View events...")
        self.btn_refresh = QPushButton(OCF_ACTION_REFRESH)
        self.btn_test_bridge = QPushButton(OCF_ACTION_TEST_BRIDGE)
        self.btn_sync_tail = QPushButton(OCF_ACTION_SYNC_TAIL)
        status.addWidget(self.lbl_models)
        status.addWidget(self.lbl_free)
        status.addWidget(self.lbl_last)
        status.addWidget(self.lbl_api_status)
        status.addWidget(self.lbl_bridge)
        status.addStretch()
        status.addWidget(self.btn_events)
        status.addWidget(self.btn_refresh)
        status.addWidget(self.btn_test_bridge)
        status.addWidget(self.btn_sync_tail)
        layout.addLayout(status)

        # Free-candidate table: the useful part, kept prominent. Columns 0-4
        # keep the original catalog view; 5-9 add the OCF evidence dimensions
        # (free evidence / direct route / local bridge / SAIFREN eligibility /
        # last successful bridge use / cooldown).
        self.free_table = QTableWidget(0, 10)
        self.free_table.setHorizontalHeaderLabels([
            "Canonical ID", "Status", "Configured", "Routing", "Source",
            "Free evidence", "Direct route", "Local bridge", "SAIFREN",
            "Last bridge OK / cooldown",
        ])
        self.free_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.free_table.horizontalHeader().setStretchLastSection(True)
        self.free_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.free_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        layout.addWidget(self.free_table, 1)

        # Events: read-only history in an explicit secondary surface
        self.events_list = QListWidget()
        self._events_dialog = None
        self.btn_events.clicked.connect(self._show_events)

        self.btn_refresh.clicked.connect(lambda: self.refresh_requested.emit(True))
        self.btn_test_bridge.clicked.connect(self.bridge_test_requested.emit)
        self.btn_sync_tail.clicked.connect(self.sync_tail_requested.emit)
        self.set_bridge_action_available(False)

    def set_bridge_action_available(self, available: bool, reason: str = "") -> None:
        """Explicit actions are only usable when the host wired a handler."""
        for button in (self.btn_test_bridge, self.btn_sync_tail):
            button.setEnabled(bool(available))
            button.setToolTip(reason or "")

    def set_bridge_status(self, text: str) -> None:
        self._bridge_label = text or "Bridge: not tested"
        self.lbl_bridge.setText(self._bridge_label)

    def set_test_running(self, busy: bool) -> None:
        self.btn_test_bridge.setEnabled(not busy)
        self.btn_test_bridge.setText(
            "Testing bridge..." if busy else OCF_ACTION_TEST_BRIDGE
        )

    def _show_events(self) -> None:
        if self._events_dialog is None:
            dlg = QDialog(self)
            dlg.setWindowTitle("OpenCode Catalog Events")
            dlg.setMinimumSize(480, 320)
            dlg_layout = QVBoxLayout(dlg)
            dlg_layout.setContentsMargins(8, 8, 8, 8)
            dlg_layout.addWidget(QLabel("Catalog events (read-only, newest last):"))
            dlg_layout.addWidget(self.events_list, 1)
            self._events_dialog = dlg
        self._events_dialog.show()
        self._events_dialog.raise_()

    def update_state(self, models: List[ZenCatalogModel], last_success: str, status: str) -> None:
        self.lbl_models.setText(f"Models: {len(models)}")
        self.lbl_free.setText(f"Free: {sum(1 for m in models if m.free_candidate)}")
        # Bounded display form: 2026-09-11 10:08 (never a full ISO string)
        shown = last_success[:16].replace("T", " ") if last_success else "-"
        self.lbl_last.setText(f"Updated: {shown}")
        # Discovery layer reports FAILED; the UI rollup is OK / STALE / ERROR.
        display = "ERROR" if status == "FAILED" else status
        self.lbl_api_status.setText(f"Status: {display}")

    def set_free_models(self, models: List[ZenCatalogModel]) -> None:
        self.free_table.setRowCount(len(models))
        for row, model in enumerate(models):
            self.free_table.setItem(row, 0, QTableWidgetItem(model.canonical_id))
            self.free_table.setItem(row, 1, QTableWidgetItem(model.cost_state))
            self.free_table.setItem(row, 2, QTableWidgetItem("Yes" if model.configured else "No"))
            self.free_table.setItem(row, 3, QTableWidgetItem("Yes" if model.routing_eligible else "No"))
            self.free_table.setItem(row, 4, QTableWidgetItem(model.source))

    def set_ocf_inventory(self, rows: List[Dict[str, object]]) -> None:
        """Render the OCF evidence table (one row per advertised free model).

        A client-bound model is shown as LOCAL BRIDGE REQUIRED -- never as a
        generic red/dead state: the catalog model itself is valid and free.
        """
        self.free_table.setRowCount(len(rows))
        for index, row in enumerate(rows):
            direct = str(row.get("direct_state") or DIRECT_UNKNOWN)
            if direct == DIRECT_LOCAL_BRIDGE_REQUIRED:
                direct_display = DIRECT_LOCAL_BRIDGE_REQUIRED
            elif direct == DIRECT_ROUTABLE:
                direct_display = "Routable"
            else:
                direct_display = "Unknown"
            bridge_state = str(row.get("bridge_state") or "")
            if bool(row.get("bridge_blocked")):
                bridge_display = f"{bridge_state or 'BLOCKED'} (cooldown)"
            else:
                bridge_display = bridge_state or "Not tested"
            cells = [
                str(row.get("upstream_canonical_id") or ""),
                "FREE_CANDIDATE" if row.get("free_evidence") else "UNKNOWN",
                "Yes" if row.get("manual") else "scanner" if row.get("scanner_managed") else "No",
                "Yes" if row.get("saifren_eligible") else "No",
                str(row.get("free_reason") or ""),
                "Yes" if row.get("free_evidence") else "No",
                direct_display,
                bridge_display,
                str(row.get("canonical_id") or ""),
                f"{_timestamp_text(str(row.get('last_bridge_ok_at') or ''))} / "
                f"{_cooldown_text(row.get('cooldown_until'))}",
            ]
            for column, value in enumerate(cells):
                self.free_table.setItem(index, column, QTableWidgetItem(value))

    def append_events(self, events: List[Dict[str, str]]) -> None:
        for ev in events:
            msg = ev.get("message") or f"{ev['type']}: {ev['canonical_id']}"
            QListWidgetItem(msg, self.events_list)
        while self.events_list.count() > 50:
            self.events_list.takeItem(0)

    def set_refreshing(self, busy: bool) -> None:
        self.btn_refresh.setEnabled(not busy)
        self.btn_refresh.setText("Refreshing..." if busy else "Refresh catalog")
