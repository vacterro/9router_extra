"""
9router_WatchEdit - FREE Fallback tab (FREE-FALLBACK-001, milestones 6, 7, 9).

PROVIDERS are the primary control surface; models are a drilldown, not the
entry point. The view is a pure projection of data the window already owns:

  * it never performs network I/O -- opening, sorting, filtering and selecting
    only rearrange rows already in memory;
  * no timers: nothing refreshes, polls or steals focus while attention is
    elsewhere;
  * selection and check state are keyed by provider_id, so they survive every
    sort and filter;
  * actions are disabled while an incompatible scan is running, and the cost
    guards themselves live in the controller, never in the widget.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ui.theme import COLOR_TEXT_SECONDARY

# Action labels (also the operator vocabulary in the handoff).
ACTION_SCAN = "SCAN"
ACTION_METADATA = "METADATA"
ACTION_LIVE = "LIVE"

ACTION_SCAN_TEXT = "Scan Selected"
ACTION_METADATA_TEXT = "Metadata Scan Selected"
ACTION_LIVE_TEXT = "Validate Selected"
ACTION_STOP_TEXT = "Stop Scan"
ACTION_SYNC_TEXT = "Sync strict FREE to SAIFREN bottom"

PROVIDER_COLUMNS = [
    "Scan", "Provider", "Status", "Scan policy", "Scan mode", "Cost risk",
    "Trusted alive", "Last scan", "Last success", "Models found", "Strict FREE",
    "Next scan", "Error / note",
]

MODEL_COLUMNS = [
    "Route id", "Upstream model", "FREE evidence", "Routing", "Health", "Owner",
    "Last seen", "Last success", "SAIFREN", "Exclusion",
]

#: Clickable sort keys mapped to their provider-table column index.
SORT_COLUMNS = {
    1: "name",
    2: "status",
    5: "cost_risk",
    7: "last_scan",
    8: "last_success",
    10: "strict_free",
    11: "next_due",
}

TRUST_DURATIONS = (
    ("Trust 1h", "1h"),
    ("Trust 1d", "1d"),
    ("Trust 7d", "7d"),
    ("Trust 30d", "30d"),
    ("Trust until cleared", "until_cleared"),
)


def _timestamp_text(value: Any) -> str:
    text = str(value or "")
    return text[:16].replace("T", " ") if text else "-"


def _short(text: Any, limit: int = 60) -> str:
    value = str(text or "").strip()
    if len(value) <= limit:
        return value
    return value[: limit - 1] + "\u2026"


def _sort_value(row: Dict[str, Any], key: str):
    """Stable, comparable sort keys (text sorts ascending, time as ISO text)."""
    if key == "name":
        return str(row.get("display_name") or row.get("provider_id") or "").lower()
    if key == "status":
        return str(row.get("last_status") or "")
    if key == "cost_risk":
        return str(row.get("live_probe_cost_risk") or "")
    if key == "strict_free":
        return int(row.get("strict_free_count") or 0)
    if key == "next_due":
        return str(row.get("next_scan_due") or "")
    if key == "last_scan":
        return str(row.get("last_scan_at") or "")
    if key == "last_success":
        return str(row.get("last_success_at") or "")
    return str(row.get("provider_id") or "")


def provider_matches_filter(row: Dict[str, Any], filters: Dict[str, Any]) -> bool:
    """Pure filter predicate -- no side effects, no I/O."""
    search = str(filters.get("search") or "").strip().lower()
    if search:
        haystack = f"{row.get('provider_id', '')} {row.get('display_name', '')}".lower()
        if search not in haystack:
            return False
    if filters.get("enabled_only") and not row.get("enabled"):
        return False
    if filters.get("stale_only") and not row.get("metadata_stale"):
        return False
    if filters.get("errors_only"):
        has_error = bool(row.get("last_error_class") or row.get("last_error_summary"))
        if not has_error and "STALE" not in str(row.get("last_status") or ""):
            return False
    if filters.get("has_strict_free") and not row.get("has_strict_free"):
        return False
    if filters.get("cost_safe_only") and not row.get("cost_safe"):
        return False
    return True


class FreeFallbackView(QWidget):
    """Provider-first FREE fallback control surface (read-only unless acted on)."""

    scan_requested = Signal(list, str)     # provider_ids, action
    stop_requested = Signal()
    sync_requested = Signal()
    policy_changed = Signal(str, str, object)   # provider_id, field, value
    note_changed = Signal(str, str)              # provider_id, text
    provider_activated = Signal(str)             # provider_id (selection only)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._rows: List[Dict[str, Any]] = []
        self._models_by_provider: Dict[str, List[Dict[str, Any]]] = {}
        self._selected: set = set()
        self._current_provider: str = ""
        self._sort_key = "name"
        self._sort_desc = False
        self._running_kind = ""
        self._filters: Dict[str, Any] = {
            "search": "", "enabled_only": False, "stale_only": False,
            "errors_only": False, "has_strict_free": False, "cost_safe_only": False,
        }
        self._build()

    # ------------------------------------------------------------------- build
    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)

        layout.addLayout(self._build_action_bar())
        layout.addLayout(self._build_filter_bar())

        splitter = QSplitter(Qt.Vertical)
        splitter.addWidget(self._build_provider_table())
        splitter.addWidget(self._build_details_pane())
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        layout.addWidget(splitter, 1)

        self._update_action_state()

    def _build_action_bar(self):
        bar = QHBoxLayout()
        bar.setContentsMargins(0, 0, 0, 0)
        bar.setSpacing(6)
        self.btn_scan = QPushButton(ACTION_SCAN_TEXT)
        self.btn_metadata_scan = QPushButton(ACTION_METADATA_TEXT)
        self.btn_validate = QPushButton(ACTION_LIVE_TEXT)
        self.btn_stop = QPushButton(ACTION_STOP_TEXT)
        self.btn_sync = QPushButton(ACTION_SYNC_TEXT)
        self.lbl_summary = QLabel("Providers: 0 · selected: 0")
        self.lbl_summary.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.lbl_summary.setMinimumWidth(1)

        self.btn_scan.clicked.connect(lambda: self._emit_scan(ACTION_SCAN))
        self.btn_metadata_scan.clicked.connect(lambda: self._emit_scan(ACTION_METADATA))
        self.btn_validate.clicked.connect(lambda: self._emit_scan(ACTION_LIVE))
        self.btn_stop.clicked.connect(self.stop_requested.emit)
        self.btn_sync.clicked.connect(self.sync_requested.emit)

        for button in (self.btn_scan, self.btn_metadata_scan, self.btn_validate):
            bar.addWidget(button)
        bar.addWidget(self.btn_stop)
        bar.addWidget(self.btn_sync)
        bar.addStretch()
        bar.addWidget(self.lbl_summary)
        return bar

    def _build_filter_bar(self):
        bar = QHBoxLayout()
        bar.setContentsMargins(0, 0, 0, 0)
        bar.setSpacing(6)
        self.txt_search = QLineEdit()
        self.txt_search.setPlaceholderText("Search provider name or id")
        self.txt_search.setMaximumWidth(220)
        self.txt_search.textChanged.connect(self._on_filter_changed)
        self.cb_enabled = self._check("Enabled only")
        self.cb_stale = self._check("Stale only")
        self.cb_errors = self._check("Errors only")
        self.cb_has_free = self._check("Has strict FREE")
        self.cb_cost_safe = self._check("Cost-safe only")

        self.btn_select_all = QPushButton("Select All")
        self.btn_select_none = QPushButton("Select None")
        self.btn_select_enabled = QPushButton("Select Enabled")
        self.btn_select_live = QPushButton("Select LIVE/TRUSTED")
        for button in (self.btn_select_all, self.btn_select_none,
                       self.btn_select_enabled, self.btn_select_live):
            button.setMaximumWidth(150)

        bar.addWidget(QLabel("Filter:"))
        bar.addWidget(self.txt_search)
        for widget in (self.cb_enabled, self.cb_stale, self.cb_errors,
                       self.cb_has_free, self.cb_cost_safe):
            bar.addWidget(widget)
        self.btn_select_all.clicked.connect(self._on_bulk_select_all)
        self.btn_select_none.clicked.connect(self._on_bulk_select_none)
        self.btn_select_enabled.clicked.connect(self._on_bulk_select_enabled)
        self.btn_select_live.clicked.connect(self._on_bulk_select_live_trusted)

        bar.addStretch()
        bar.addWidget(self.btn_select_all)
        bar.addWidget(self.btn_select_none)
        bar.addWidget(self.btn_select_enabled)
        bar.addWidget(self.btn_select_live)
        return bar

    def _check(self, text: str):
        box = QCheckBox(text)
        box.stateChanged.connect(self._on_filter_changed)
        return box

    def _build_provider_table(self) -> QWidget:
        self.provider_table = QTableWidget(0, len(PROVIDER_COLUMNS))
        self.provider_table.setHorizontalHeaderLabels(PROVIDER_COLUMNS)
        header = self.provider_table.horizontalHeader()
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        header.setSectionResizeMode(12, QHeaderView.Stretch)
        header.setSectionsClickable(True)
        header.sectionClicked.connect(self._on_header_clicked)
        self.provider_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.provider_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.provider_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.provider_table.currentCellChanged.connect(self._on_current_cell_changed)
        self.provider_table.itemChanged.connect(self._on_item_changed)
        return self.provider_table

    def _build_details_pane(self) -> QWidget:
        pane = QWidget()
        layout = QVBoxLayout(pane)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        self.lbl_details_title = QLabel("Provider details: none selected")
        layout.addWidget(self.lbl_details_title)

        self.lbl_capabilities = QLabel("-")
        self.lbl_capabilities.setWordWrap(True)
        self.lbl_evidence = QLabel("-")
        self.lbl_evidence.setWordWrap(True)
        self.lbl_cost = QLabel("-")
        self.lbl_cost.setWordWrap(True)
        self.lbl_error = QLabel("-")
        self.lbl_error.setWordWrap(True)
        self.lbl_badges = QLabel("-")
        self.lbl_badges.setStyleSheet(f"color: {COLOR_TEXT_SECONDARY};")
        for label in (self.lbl_capabilities, self.lbl_evidence, self.lbl_cost,
                      self.lbl_error, self.lbl_badges):
            layout.addWidget(label)

        layout.addLayout(self._build_policy_bar())

        self.txt_note = QPlainTextEdit()
        self.txt_note.setPlaceholderText("Operator note for this provider")
        self.txt_note.setFixedHeight(46)
        note_bar = QHBoxLayout()
        note_bar.addWidget(self.txt_note, 1)
        self.btn_save_note = QPushButton("Save note")
        self.btn_save_note.clicked.connect(self._save_note)
        note_bar.addWidget(self.btn_save_note)
        layout.addLayout(note_bar)

        self.model_table = QTableWidget(0, len(MODEL_COLUMNS))
        self.model_table.setHorizontalHeaderLabels(MODEL_COLUMNS)
        self.model_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.model_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.model_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        layout.addWidget(self.model_table, 1)
        return pane

    def _build_policy_bar(self):
        bar = QHBoxLayout()
        bar.setContentsMargins(0, 0, 0, 0)
        bar.setSpacing(4)
        self.btn_enable = QPushButton("Enable")
        self.btn_disable = QPushButton("Disable")
        self.lbl_scan_policy = QLabel("Scan policy: -")
        self.cb_scan_policy = QComboBox()
        self.cb_scan_policy.addItems(["ALWAYS", "STALE_ONLY", "MANUAL", "NEVER"])
        self.cb_scan_policy.setMaximumWidth(160)
        self.btn_metadata_only = QPushButton("Metadata only")
        self.btn_allow_live = QPushButton("Allow safe live validation")
        self.btn_scan_now = QPushButton("Scan now")
        self.btn_clear_trust = QPushButton("Clear trusted state")
        self._trust_buttons = []
        bar.addWidget(self.btn_enable)
        bar.addWidget(self.btn_disable)
        bar.addWidget(self.lbl_scan_policy)
        bar.addWidget(self.cb_scan_policy)
        bar.addWidget(self.btn_metadata_only)
        bar.addWidget(self.btn_allow_live)
        for text, duration in TRUST_DURATIONS:
            button = QPushButton(text)
            button.clicked.connect(
                lambda _=False, value=duration: self._emit_policy("trusted_alive", value)
            )
            self._trust_buttons.append(button)
            bar.addWidget(button)
        bar.addWidget(self.btn_clear_trust)
        bar.addWidget(self.btn_scan_now)
        bar.addStretch()

        self.btn_enable.clicked.connect(lambda: self._emit_policy("enabled", True))
        self.btn_disable.clicked.connect(lambda: self._emit_policy("enabled", False))
        self.cb_scan_policy.currentTextChanged.connect(
            lambda value: self._emit_policy("scan_policy", value)
        )
        self.btn_metadata_only.clicked.connect(
            lambda: self._emit_policy("scan_mode", "METADATA_ONLY")
        )
        self.btn_allow_live.clicked.connect(
            lambda: self._emit_policy("scan_mode", "METADATA_AND_LIVE_PROBE")
        )
        self.btn_clear_trust.clicked.connect(
            lambda: self._emit_policy("trusted_alive", None)
        )
        self.btn_scan_now.clicked.connect(self._emit_scan_now)
        return bar

    # ------------------------------------------------------------------ actions
    def _emit_scan(self, action: str) -> None:
        ids = self.visible_selected_provider_ids()
        if ids:
            self.scan_requested.emit(ids, action)

    def _emit_scan_now(self) -> None:
        if self._current_provider and self.current_provider_visible():
            self.scan_requested.emit([self._current_provider], ACTION_SCAN)

    def _save_note(self) -> None:
        if self._current_provider:
            self.note_changed.emit(self._current_provider, self.txt_note.toPlainText())

    def _on_filter_changed(self, *_args) -> None:
        self._filters.update({
            "search": self.txt_search.text(),
            "enabled_only": self.cb_enabled.isChecked(),
            "stale_only": self.cb_stale.isChecked(),
            "errors_only": self.cb_errors.isChecked(),
            "has_strict_free": self.cb_has_free.isChecked(),
            "cost_safe_only": self.cb_cost_safe.isChecked(),
        })
        self.rebuild()

    def _on_header_clicked(self, section: int) -> None:
        key = SORT_COLUMNS.get(section)
        if key:
            self.sort_by(key)

    # --------------------------------------------------------------- selection
    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        if item.column() != 0:
            return
        provider_id = self._row_provider_id(item.row())
        if not provider_id:
            return
        if item.checkState() == Qt.Checked:
            self._selected.add(provider_id)
        else:
            self._selected.discard(provider_id)
        self._update_action_state()

    def _on_current_cell_changed(self, row: int, _col: int, _prev_row: int, _prev_col: int) -> None:
        provider_id = self._row_provider_id(row)
        if provider_id:
            self.select_provider(provider_id)

    def _row_provider_id(self, row: int) -> str:
        item = self.provider_table.item(row, 1)
        if item is None:
            return ""
        return str(item.data(Qt.UserRole) or "")

    def _visible_set(self) -> set:
        return {str(row["provider_id"]) for row in self.visible_rows()}

    def _on_bulk_select_all(self) -> None:
        # Visible scope only: hidden remembered selections are preserved
        # (they are still remembered, still shown in the summary, still not
        # executed while hidden); the visible set is fully selected.
        selected = self._selected - self._visible_set()
        selected |= self._visible_set()
        self._selected = selected
        self.rebuild()

    def _on_bulk_select_none(self) -> None:
        visible = self._visible_set()
        self._selected -= visible
        self.rebuild()

    def _on_bulk_select_enabled(self) -> None:
        # Visible scope only: hidden remembered selections are preserved, not
        # re-selected, and never extend the actionable set.
        selected = self._selected - self._visible_set()
        selected |= {
            str(row["provider_id"]) for row in self.visible_rows() if row.get("enabled")
        }
        self._selected = selected
        self.rebuild()

    def _on_bulk_select_live_trusted(self) -> None:
        # Visible scope only (same contract as _on_bulk_select_enabled).
        selected = self._selected - self._visible_set()
        selected |= {
            str(row["provider_id"]) for row in self.visible_rows()
            if row.get("trusted_alive") or row.get("last_status") == "OK"
        }
        self._selected = selected
        self.rebuild()

    # ------------------------------------------------------------------- reads
    def row_provider_ids(self) -> List[str]:
        """Provider ids in the CURRENT visual row order."""
        return [self._row_provider_id(row) for row in range(self.provider_table.rowCount())]

    def selected_provider_ids(self) -> List[str]:
        return sorted(pid for pid in self._selected if pid)

    def visible_selected_provider_ids(self) -> List[str]:
        """Selected providers that are currently visible under the active filter.

        Network-capable actions must operate only on the visible scope. Hidden
        remembered selections survive round-trips but are never executed while
        hidden.
        """
        visible = {row.get("provider_id") for row in self.visible_rows()}
        return sorted(pid for pid in self._selected if pid and pid in visible)

    def remembered_hidden_selected_count(self) -> int:
        """How many previously selected providers are currently hidden by the filter."""
        visible = {row.get("provider_id") for row in self.visible_rows()}
        hidden = [pid for pid in self._selected if pid and pid not in visible]
        return len(hidden)

    def current_provider_id(self) -> str:
        return self._current_provider

    def current_provider_visible(self) -> bool:
        """Poor-man's check: if the current provider is not in the visible set,
        its details pane is stale."""
        visible = {row.get("provider_id") for row in self.visible_rows()}
        return bool(self._current_provider and self._current_provider in visible)

    def sort_key(self) -> str:
        return self._sort_key

    def sort_by(self, key: str, desc: Optional[bool] = None) -> None:
        if desc is None:
            desc = (key == self._sort_key) and not self._sort_desc
        self._sort_key = key
        self._sort_desc = bool(desc)
        self.rebuild()

    def visible_rows(self) -> List[Dict[str, Any]]:
        rows = [row for row in self._rows if provider_matches_filter(row, self._filters)]
        rows.sort(key=lambda row: _sort_value(row, self._sort_key), reverse=self._sort_desc)
        return rows

    # ---------------------------------------------------------------- mutation
    def set_snapshot(self, provider_rows: Sequence[Dict[str, Any]],
                     models_by_provider: Optional[Dict[str, List[Dict[str, Any]]]] = None) -> None:
        """Install a locally-computed snapshot (no I/O, no network)."""
        self._rows = [dict(row) for row in provider_rows]
        self._models_by_provider = {
            str(pid): [dict(row) for row in rows]
            for pid, rows in (models_by_provider or {}).items()
        }
        known = {str(row["provider_id"]) for row in self._rows}
        self._selected &= known
        self.rebuild()

    def rebuild(self) -> None:
        rows = self.visible_rows()
        table = self.provider_table
        table.blockSignals(True)
        try:
            table.setRowCount(len(rows))
            for index, row in enumerate(rows):
                provider_id = str(row.get("provider_id") or "")
                check = QTableWidgetItem()
                check.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled | Qt.ItemIsSelectable)
                check.setCheckState(Qt.Checked if provider_id in self._selected else Qt.Unchecked)
                table.setItem(index, 0, check)
                name_item = QTableWidgetItem(str(row.get("display_name") or provider_id))
                name_item.setData(Qt.UserRole, provider_id)
                table.setItem(index, 1, name_item)
                table.setItem(index, 2, QTableWidgetItem(str(row.get("last_status") or "-")))
                table.setItem(index, 3, QTableWidgetItem(str(row.get("scan_policy") or "-")))
                table.setItem(index, 4, QTableWidgetItem(str(row.get("scan_mode") or "-")))
                table.setItem(index, 5, QTableWidgetItem(
                    f"{row.get('live_probe_cost_risk', '-')} / {row.get('metadata_cost_risk', '-')}"
                ))
                table.setItem(index, 6, QTableWidgetItem(str(row.get("trusted_text") or "-")))
                table.setItem(index, 7, QTableWidgetItem(_timestamp_text(row.get("last_scan_at"))))
                table.setItem(index, 8, QTableWidgetItem(
                    _timestamp_text(row.get("last_success_at"))
                ))
                table.setItem(index, 9, QTableWidgetItem(str(row.get("models_discovered", 0))))
                table.setItem(index, 10, QTableWidgetItem(str(row.get("strict_free_count", 0))))
                next_due = _timestamp_text(row.get("next_scan_due"))
                table.setItem(index, 11, QTableWidgetItem(
                    next_due if next_due != "-" else ("due" if row.get("metadata_stale")
                                                      and row.get("enabled") else "-")
                ))
                table.setItem(index, 12, QTableWidgetItem(self._note_text(row)))
        finally:
            table.blockSignals(False)
        self.lbl_summary.setText(                   f"Providers: {len(rows)} / {len(self._rows)} \u00b7 selected: "
                   f"{len(self.visible_selected_provider_ids())} visible / "
                   f"{self.remembered_hidden_selected_count()} hidden"
        )
        self._refresh_details()
        self._update_action_state()

    def _note_text(self, row: Dict[str, Any]) -> str:
        parts = list(row.get("badges") or [])
        error = str(row.get("last_error_class") or "")
        summary = str(row.get("last_error_summary") or "")
        text = " \u00b7 ".join(parts)
        if error or summary:
            text = (text + " \u00b7 " if text else "") + _short(f"{error} {summary}".strip())
        note = str(row.get("operator_note") or "")
        if note:
            text = (text + " \u00b7 " if text else "") + _short(note, 40)
        return text or "-"

    def select_provider(self, provider_id: str) -> None:
        self._current_provider = str(provider_id or "")
        self._refresh_details()
        # Per-provider policy controls are only meaningful with a current
        # provider: selection changes what is actionable, never what is allowed.
        self._update_action_state()
        if self._current_provider:
            self.provider_activated.emit(self._current_provider)

    def _selected_row(self) -> Optional[Dict[str, Any]]:
        for row in self._rows:
            if str(row.get("provider_id")) == self._current_provider:
                return row
        return None

    def _refresh_details(self) -> None:
        row = self._selected_row()
        if row is None or not self.current_provider_visible():
            self.lbl_details_title.setText("Provider details: none selected or filtered out")
            for label in (self.lbl_capabilities, self.lbl_evidence, self.lbl_cost,
                          self.lbl_error, self.lbl_badges):
                label.setText("-")
            self.model_table.setRowCount(0)
            self.txt_note.setPlainText("")
            return
        provider_id = str(row.get("provider_id"))
        self.lbl_details_title.setText(
            f"Provider details: {row.get('display_name')} [{provider_id}]"
        )
        self.lbl_capabilities.setText(
            "Adapter: {adapter} \u00b7 metadata: {meta} \u00b7 strict zero-cost evidence: "
            "{strict} \u00b7 live canary: {live} \u00b7 transport: {transport} \u00b7 source: {source}".format(
                adapter=row.get("adapter", "-"),
                meta="yes" if row.get("metadata_supported") else "no",
                strict="yes" if row.get("metadata_supported") or row.get("live_supported") else "no",
                live="yes" if row.get("live_supported") else "no",
                transport=row.get("adapter_transport") or "-",
                source=row.get("adapter_metadata_source") or "-",
            )
        )
        self.lbl_evidence.setText(
            f"Status: {row.get('last_status')} \u00b7 last scan: "
            f"{_timestamp_text(row.get('last_scan_at'))} \u00b7 last metadata success: "
            f"{_timestamp_text(row.get('last_metadata_success_at'))} \u00b7 last live probe: "
            f"{_timestamp_text(row.get('last_live_probe_success_at'))} \u00b7 trusted: "
            f"{row.get('trusted_text')}"
        )
        self.lbl_cost.setText(
            f"Cost risk \u2014 metadata: {row.get('metadata_cost_risk')} \u00b7 live probe: "
            f"{row.get('live_probe_cost_risk')} \u00b7 billing consent: "
            f"{'granted' if row.get('allow_billing_probe') else 'not granted'} \u00b7 "
            f"models {row.get('models_discovered', 0)} (strict FREE "
            f"{row.get('strict_free_count', 0)}, conditional/unknown "
            f"{row.get('conditional_count', 0)})"
        )
        self.lbl_error.setText(
            "Last error: " + _short(
                f"{row.get('last_error_class') or '-'} {row.get('last_error_summary') or ''}".strip(),
                160,
            )
        )
        self.lbl_badges.setText("Badges: " + (" ".join(row.get("badges") or []) or "-"))
        self.txt_note.setPlainText(str(row.get("operator_note") or ""))
        self._refresh_policy_controls(provider_id)
        self._fill_model_table(provider_id)

    def _fill_model_table(self, provider_id: str) -> None:
        rows = self._models_by_provider.get(provider_id, [])
        self.model_table.setRowCount(len(rows))
        for index, row in enumerate(rows):
            self.model_table.setItem(index, 0, QTableWidgetItem(
                str(row.get("canonical_id") or "")
            ))
            self.model_table.setItem(index, 1, QTableWidgetItem(
                str(row.get("upstream_model_id") or row.get("model_id") or "")
            ))
            self.model_table.setItem(index, 2, QTableWidgetItem(
                str(row.get("free_evidence") or "UNKNOWN_COST")
            ))
            self.model_table.setItem(index, 3, QTableWidgetItem(str(row.get("routing") or "-")))
            self.model_table.setItem(index, 4, QTableWidgetItem(
                str(row.get("provider_health") or "UNKNOWN")
            ))
            owner = "manual" if not row.get("scanner_managed", True) else "scanner"
            self.model_table.setItem(index, 5, QTableWidgetItem(owner))
            self.model_table.setItem(index, 6, QTableWidgetItem(
                _timestamp_text(row.get("last_seen"))
            ))
            self.model_table.setItem(index, 7, QTableWidgetItem(
                _timestamp_text(row.get("last_success_use"))
            ))
            self.model_table.setItem(index, 8, QTableWidgetItem(
                "eligible" if row.get("saifren_eligible") else "no"
            ))
            self.model_table.setItem(index, 9, QTableWidgetItem(
                _short(row.get("exclusion_reason") or "", 70)
            ))

    def _refresh_policy_controls(self, provider_id: str) -> None:
        row = self._selected_row()
        policy = str(row.get("scan_policy") or "-") if row else "-"
        self.lbl_scan_policy.setText(
            f"Scan policy: {policy}" + (" (scanning disabled)" if policy == "NEVER" else "")
        )
        # Re-projecting the control must never write policy: user-side writes
        # come from activateAsUserAction only (see cb_scan_policy wiring).
        self.cb_scan_policy.blockSignals(True)
        try:
            index = self.cb_scan_policy.findText(policy)
            self.cb_scan_policy.setCurrentIndex(index if index >= 0 else 0)
        finally:
            self.cb_scan_policy.blockSignals(False)

    def _emit_policy(self, field: str, value: Any) -> None:
        if not self._current_provider:
            return
        if field == "enabled":
            self.policy_changed.emit(self._current_provider, "enabled", bool(value))
        elif field == "scan_mode":
            self.policy_changed.emit(self._current_provider, "scan_mode", value)
        elif field == "scan_policy":
            # _refresh_policy_controls re-projections are signal-blocked, so a
            # signal that reaches here is a genuine user edit.
            self.policy_changed.emit(self._current_provider, "scan_policy", value)
        elif field == "trusted_alive":
            self.policy_changed.emit(self._current_provider, "trusted_alive", value)

    def policy_control_enabled(self) -> bool:
        # Same gate as the other per-provider policy controls: idle, with a
        # visible current provider. A NEVER policy disables SCANNING for that
        # provider, never the ability to edit the policy itself.
        idle = not self._running_kind
        return idle and bool(self._current_provider) and self.current_provider_visible()

    def set_scan_state(self, kind: Optional[str]) -> None:
        """Disable incompatible actions while a scan is running."""
        self._running_kind = str(kind or "")
        self._update_action_state()

    def scan_state(self) -> str:
        return self._running_kind

    def _update_action_state(self) -> None:
        idle = not self._running_kind
        has_selection = bool(self.visible_selected_provider_ids())
        for button in (self.btn_scan, self.btn_metadata_scan, self.btn_validate):
            button.setEnabled(idle and has_selection)
        self.btn_stop.setEnabled(not idle)
        self.btn_sync.setEnabled(idle)
        for button in (self.btn_enable, self.btn_disable, self.btn_metadata_only,
                       self.btn_allow_live, self.btn_scan_now, self.btn_clear_trust,
                       *self._trust_buttons):
            button.setEnabled(idle and bool(self._current_provider))
        self.btn_select_all.setEnabled(idle)
        self.btn_select_none.setEnabled(idle)
        self.btn_select_enabled.setEnabled(idle)
        self.btn_select_live.setEnabled(idle)
        self.cb_scan_policy.setEnabled(self.policy_control_enabled())
        self.btn_enable.setEnabled(self.policy_control_enabled())
        self.btn_disable.setEnabled(self.policy_control_enabled())
        self.btn_metadata_only.setEnabled(self.policy_control_enabled())
        self.btn_allow_live.setEnabled(self.policy_control_enabled())
        self.btn_scan_now.setEnabled(self.policy_control_enabled())
        self.btn_clear_trust.setEnabled(self.policy_control_enabled())
        for button in self._trust_buttons:
            button.setEnabled(self.policy_control_enabled())
