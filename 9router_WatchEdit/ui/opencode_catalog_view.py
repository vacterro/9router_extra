"""
9router_WatchEdit - OpenCode Catalog Panel (SRC-004)

Compact catalog status + OpenCode Free view.
Never auto-mutates combos/providers/routing; events are read-only surface.
"""
from typing import Dict, List

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QListWidget,
    QListWidgetItem,
    QAbstractItemView,
)

from core.opencode_catalog import OpenCodeCatalogDiscovery, ZenCatalogModel


class OpenCodeCatalogPanel(QWidget):
    refresh_requested = Signal(bool)

    def __init__(self, discovery: OpenCodeCatalogDiscovery, parent=None):
        super().__init__(parent)
        self.discovery = discovery
        self._build()

    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(2)

        # Status strip
        status = QHBoxLayout()
        status.setContentsMargins(0, 0, 0, 0)
        status.setSpacing(6)
        self.lbl_models = QLabel("Models: 0")
        self.lbl_free = QLabel("Free: 0")
        self.lbl_last = QLabel("Last refresh: -")
        self.lbl_api_status = QLabel("Status: STALE")
        self.btn_refresh = QPushButton("Refresh OpenCode Catalog")
        status.addWidget(self.lbl_models)
        status.addWidget(self.lbl_free)
        status.addWidget(self.lbl_last)
        status.addWidget(self.lbl_api_status)
        status.addStretch()
        status.addWidget(self.btn_refresh)
        layout.addLayout(status)

        # Compact OpenCode Free view
        self.free_table = QTableWidget(0, 5)
        self.free_table.setHorizontalHeaderLabels([
            "Canonical ID", "Status", "Configured", "Routing", "Source"
        ])
        self.free_table.horizontalHeader().setStretchLastSection(True)
        self.free_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.free_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        layout.addWidget(self.free_table)

        # Events surface
        layout.addWidget(QLabel("Catalog events:"))
        self.events_list = QListWidget()
        layout.addWidget(self.events_list, 1)

        self.btn_refresh.clicked.connect(lambda: self.refresh_requested.emit(True))

    def update_state(self, models: List[ZenCatalogModel], last_success: str, status: str) -> None:
        self.lbl_models.setText(f"Models: {len(models)}")
        self.lbl_free.setText(f"Free: {sum(1 for m in models if m.free_candidate)}")
        self.lbl_last.setText(f"Last refresh: {last_success or '-'}")
        self.lbl_api_status.setText(f"Status: {status}")

    def set_free_models(self, models: List[ZenCatalogModel]) -> None:
        self.free_table.setRowCount(len(models))
        for row, model in enumerate(models):
            self.free_table.setItem(row, 0, QTableWidgetItem(model.canonical_id))
            self.free_table.setItem(row, 1, QTableWidgetItem(model.cost_state))
            self.free_table.setItem(row, 2, QTableWidgetItem("Yes" if model.configured else "No"))
            self.free_table.setItem(row, 3, QTableWidgetItem("Yes" if model.routing_eligible else "No"))
            self.free_table.setItem(row, 4, QTableWidgetItem(model.source))

    def append_events(self, events: List[Dict[str, str]]) -> None:
        for ev in events:
            msg = ev.get("message") or f"{ev['type']}: {ev['canonical_id']}"
            QListWidgetItem(msg, self.events_list)
        while self.events_list.count() > 50:
            self.events_list.takeItem(0)

    def set_refreshing(self, busy: bool) -> None:
        self.btn_refresh.setEnabled(not busy)
        self.btn_refresh.setText("Refreshing..." if busy else "Refresh OpenCode Catalog")
