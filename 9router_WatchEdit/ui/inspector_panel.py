"""
9router_WatchEdit - Diagnostic Inspector Panel
Provides transparent observability for any selected model: state, HTTP status,
exact latency, failure/success streaks, classification rationale, and raw error response.
"""
from typing import Optional
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTextEdit,
    QGroupBox,
    QFrame,
)

from core.history import ModelHealthRecord
from core.classification import HealthState, CostStatus
from ui.theme import (
    COLOR_BORDER_HIGHLIGHT,
    COLOR_BACKGROUND_SOFT,
    COLOR_TEXT_PRIMARY,
    COLOR_TEXT_SECONDARY,
    COLOR_TEXT_MUTED,
    STATE_COLORS,
)

class InspectorPanel(QWidget):
    retest_requested = Signal(str)
    add_to_combo_requested = Signal(str)
    cost_override_changed = Signal(str, object)  # canonical_id, override ("FREE", "PAID", or None)

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._current_canonical_id: Optional[str] = None
        self._current_record: Optional[ModelHealthRecord] = None
        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(8)

        # Title / Group Box
        self.group = QGroupBox("MODEL DIAGNOSTIC INSPECTOR")
        group_layout = QVBoxLayout(self.group)
        group_layout.setContentsMargins(8, 14, 8, 8)
        group_layout.setSpacing(6)

        # Model Identity Header
        self.lbl_canonical_id = QLabel("No model selected")
        self.lbl_canonical_id.setStyleSheet(f"font-size: 14px; font-weight: bold; color: {COLOR_BORDER_HIGHLIGHT};")
        self.lbl_canonical_id.setTextInteractionFlags(Qt.TextSelectableByMouse)
        group_layout.addWidget(self.lbl_canonical_id)

        self.lbl_provider = QLabel("Provider: --")
        self.lbl_provider.setStyleSheet(f"color: {COLOR_TEXT_SECONDARY};")
        group_layout.addWidget(self.lbl_provider)

        # State & Confidence Row
        state_row = QHBoxLayout()
        state_row.setSpacing(6)
        self.lbl_state_badge = QLabel("STATE: --")
        self.lbl_state_badge.setStyleSheet("padding: 2px 8px; font-weight: bold; border: 1px solid #000;")
        state_row.addWidget(self.lbl_state_badge)

        self.lbl_confidence = QLabel("Confidence: --")
        self.lbl_confidence.setStyleSheet(f"color: {COLOR_TEXT_SECONDARY};")
        state_row.addWidget(self.lbl_confidence)
        state_row.addStretch()
        group_layout.addLayout(state_row)

        # Operational Metrics Grid
        metrics_frame = QFrame()
        metrics_frame.setObjectName("beveledFrameSunken")
        metrics_layout = QVBoxLayout(metrics_frame)
        metrics_layout.setContentsMargins(6, 6, 6, 6)
        metrics_layout.setSpacing(4)

        self.lbl_http = QLabel("HTTP Status: -- | Latency: -- ms")
        self.lbl_streaks = QLabel("Success Streak: 0 | Failure Streak: 0")
        self.lbl_last_tested = QLabel("Last Tested: --")
        self.lbl_last_success = QLabel("Last Success: --")

        for lbl in (self.lbl_http, self.lbl_streaks, self.lbl_last_tested, self.lbl_last_success):
            lbl.setStyleSheet(f"color: {COLOR_TEXT_PRIMARY}; font-size: 10px;")
            lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
            metrics_layout.addWidget(lbl)

        group_layout.addWidget(metrics_frame)

        # Reason & Diagnosis
        group_layout.addWidget(QLabel("Classification Reason:"))
        self.lbl_reason = QLabel("Select an entry to view diagnosis.")
        self.lbl_reason.setWordWrap(True)
        self.lbl_reason.setStyleSheet(f"color: {COLOR_BORDER_HIGHLIGHT}; font-size: 10px;")
        self.lbl_reason.setTextInteractionFlags(Qt.TextSelectableByMouse)
        group_layout.addWidget(self.lbl_reason)

        # Raw Error Snippet
        group_layout.addWidget(QLabel("Raw Abbreviated Response (Redacted):"))
        self.txt_raw_error = QTextEdit()
        self.txt_raw_error.setReadOnly(True)
        self.txt_raw_error.setFixedHeight(80)
        self.txt_raw_error.setStyleSheet(f"background-color: {COLOR_BACKGROUND_SOFT}; font-size: 10px;")
        group_layout.addWidget(self.txt_raw_error)

        # Cost Override Controls
        cost_box = QHBoxLayout()
        cost_box.setSpacing(4)
        cost_box.addWidget(QLabel("Cost Status:"))
        self.btn_override_auto = QPushButton("Auto")
        self.btn_override_free = QPushButton("Force Free")
        self.btn_override_paid = QPushButton("Force Paid")

        self.btn_override_auto.clicked.connect(lambda: self._set_override(None))
        self.btn_override_free.clicked.connect(lambda: self._set_override("FREE"))
        self.btn_override_paid.clicked.connect(lambda: self._set_override("PAID"))

        cost_box.addWidget(self.btn_override_auto)
        cost_box.addWidget(self.btn_override_free)
        cost_box.addWidget(self.btn_override_paid)
        group_layout.addLayout(cost_box)

        # Action Buttons
        btn_layout = QHBoxLayout()
        self.btn_retest = QPushButton("Retest Model")
        self.btn_retest.setObjectName("primaryAction")
        self.btn_retest.clicked.connect(self._on_retest_clicked)

        self.btn_add_to_combo = QPushButton("Add to Active Combo")
        self.btn_add_to_combo.clicked.connect(self._on_add_to_combo_clicked)

        btn_layout.addWidget(self.btn_retest)
        btn_layout.addWidget(self.btn_add_to_combo)
        group_layout.addLayout(btn_layout)

        layout.addWidget(self.group)
        self.clear()

    def clear(self):
        self._current_canonical_id = None
        self._current_record = None
        self.lbl_canonical_id.setText("No model selected")
        self.lbl_provider.setText("Provider: --")
        self.lbl_state_badge.setText("STATE: --")
        self.lbl_state_badge.setStyleSheet("padding: 2px 8px; border: 1px solid #000;")
        self.lbl_confidence.setText("Confidence: --")
        self.lbl_http.setText("HTTP Status: -- | Latency: -- ms")
        self.lbl_streaks.setText("Success Streak: 0 | Failure Streak: 0")
        self.lbl_last_tested.setText("Last Tested: --")
        self.lbl_last_success.setText("Last Success: --")
        self.lbl_reason.setText("Select an entry to view diagnosis.")
        self.txt_raw_error.clear()
        self.btn_retest.setEnabled(False)
        self.btn_add_to_combo.setEnabled(False)

    def set_model(self, canonical_id: str, record: Optional[ModelHealthRecord]):
        self._current_canonical_id = canonical_id
        self._current_record = record

        self.btn_retest.setEnabled(True)
        self.btn_add_to_combo.setEnabled(True)
        self.lbl_canonical_id.setText(canonical_id)

        if not record:
            self.lbl_provider.setText("Provider: (Not tested yet)")
            self.lbl_state_badge.setText("STATE: UNTESTED")
            self.lbl_state_badge.setStyleSheet("background-color: #332E22; color: #9C9371; padding: 2px 8px;")
            self.lbl_confidence.setText("Confidence: --")
            self.lbl_http.setText("HTTP Status: -- | Latency: -- ms")
            self.lbl_streaks.setText("Success Streak: 0 | Failure Streak: 0")
            self.lbl_last_tested.setText("Last Tested: Never")
            self.lbl_last_success.setText("Last Success: Never")
            self.lbl_reason.setText("Model discovered but not yet probed.")
            self.txt_raw_error.clear()
            return

        self.lbl_provider.setText(f"Provider: {record.provider}")
        
        # State Badge Styling
        st_cfg = STATE_COLORS.get(record.state, {"bg": "#332E22", "fg": "#D4C89A"})
        border_css = f"border: 1px solid {st_cfg['border']};" if "border" in st_cfg else "border: 1px solid #000;"
        self.lbl_state_badge.setText(f"STATE: {record.state}")
        self.lbl_state_badge.setStyleSheet(
            f"background-color: {st_cfg['bg']}; color: {st_cfg['fg']}; padding: 2px 8px; font-weight: bold; {border_css}"
        )

        self.lbl_confidence.setText(f"Confidence: {record.confidence}")
        status_text = f"HTTP {record.status_code}" if record.status_code else "HTTP --"
        lat_text = f"{record.latency_ms:.0f} ms" if record.latency_ms else "-- ms"
        self.lbl_http.setText(f"Status: {status_text} | Latency: {lat_text}")

        self.lbl_streaks.setText(f"Success Streak: {record.success_streak} | Failure Streak: {record.failure_streak}")
        self.lbl_last_tested.setText(f"Last Tested: {record.last_tested_at or 'Never'}")
        self.lbl_last_success.setText(f"Last Success: {record.last_success_at or 'Never'}")

        reason_text = record.reason or "No diagnosis recorded."
        if record.note:
            reason_text += f" ({record.note})"
        self.lbl_reason.setText(reason_text)

        self.txt_raw_error.setPlainText(record.last_error or "(No error payload)")

    def _set_override(self, val: Optional[str]):
        if self._current_canonical_id:
            self.cost_override_changed.emit(self._current_canonical_id, val)

    def _on_retest_clicked(self):
        if self._current_canonical_id:
            self.retest_requested.emit(self._current_canonical_id)

    def _on_add_to_combo_clicked(self):
        if self._current_canonical_id:
            self.add_to_combo_requested.emit(self._current_canonical_id)
