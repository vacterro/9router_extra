"""
9router_WatchEdit - Live Activity Panel & Progress Bar
Displays in-flight probes with live ticking seconds, scan progress, and status logs.
"""
import time
from typing import Dict, Optional
from PySide6.QtCore import Qt, Signal, QTimer
from PySide6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QFrame,
)

from config import PENDING_THRESHOLD_SEC
from ui.theme import (
    COLOR_BORDER_HIGHLIGHT,
    COLOR_TEXT_PRIMARY,
    COLOR_TEXT_SECONDARY,
    COLOR_TEXT_MUTED,
    COLOR_ACCENT_TEAL,
    COLOR_SURFACE,
    COLOR_BACKGROUND_SOFT,
)

class ActivityPanel(QWidget):
    stop_requested = Signal()

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._probe_start_times: Dict[str, float] = {}
        self._in_flight_probes: Dict[str, float] = {}  # canonical_id -> elapsed_sec
        self._ticker_timer = QTimer(self)
        self._ticker_timer.setInterval(250)
        self._ticker_timer.timeout.connect(self._on_tick)
        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 2, 4, 2)
        layout.setSpacing(2)

        frame = QFrame()
        frame.setObjectName("beveledFrameSunken")
        frame_layout = QVBoxLayout(frame)
        frame_layout.setContentsMargins(4, 2, 4, 2)
        frame_layout.setSpacing(2)

        # Top line: Status text, In-flight ticker, and Stop Button
        top_row = QHBoxLayout()
        top_row.setSpacing(8)

        self.lbl_status = QLabel("Ready / Idle")
        self.lbl_status.setStyleSheet(f"color: {COLOR_TEXT_PRIMARY}; font-weight: bold; font-size: 11px;")
        top_row.addWidget(self.lbl_status)

        self.lbl_inflight = QLabel("")
        self.lbl_inflight.setStyleSheet(f"color: {COLOR_BORDER_HIGHLIGHT}; font-size: 10px;")
        top_row.addWidget(self.lbl_inflight, stretch=1)

        self.btn_stop = QPushButton("STOP")
        self.btn_stop.setObjectName("dangerAction")
        self.btn_stop.setFixedHeight(22)
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self.stop_requested.emit)
        top_row.addWidget(self.btn_stop)

        frame_layout.addLayout(top_row)

        # Progress bar
        self.progress_bar = QProgressBar()
        self.progress_bar.setFixedHeight(10)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(True)
        self.progress_bar.setFormat("%v / %m (%p%)")
        frame_layout.addWidget(self.progress_bar)

        layout.addWidget(frame)

    def set_scanning(self, is_scanning: bool, total_models: int = 0):
        self.btn_stop.setEnabled(is_scanning)
        if is_scanning:
            self.lbl_status.setText(f"SCANNING ({total_models} models)...")
            self.progress_bar.setMaximum(max(total_models, 1))
            self.progress_bar.setValue(0)
            self._ticker_timer.start()
        else:
            self.lbl_status.setText("Scan completed / Idle.")
            self._ticker_timer.stop()
            self._probe_start_times.clear()
            self._in_flight_probes.clear()
            self._update_inflight_text()

    def update_progress(self, completed: int, total: int):
        self.progress_bar.setMaximum(max(total, 1))
        self.progress_bar.setValue(completed)
        self.lbl_status.setText(f"Scanning {completed} / {total}...")

    def set_probe_started(self, canonical_id: str):
        self._probe_start_times[canonical_id] = time.monotonic()
        self._in_flight_probes[canonical_id] = 0.0
        self._update_inflight_text()

    def set_probe_pending(self, canonical_id: str, elapsed_sec: float):
        self._in_flight_probes[canonical_id] = elapsed_sec
        self._update_inflight_text()

    def set_probe_finished(self, canonical_id: str):
        self._probe_start_times.pop(canonical_id, None)
        self._in_flight_probes.pop(canonical_id, None)
        self._update_inflight_text()

    def _on_tick(self):
        now = time.monotonic()
        for cid, t0 in list(self._probe_start_times.items()):
            self._in_flight_probes[cid] = now - t0
        self._update_inflight_text()

    def _update_inflight_text(self):
        if not self._in_flight_probes:
            self.lbl_inflight.setText("")
            return

        items = []
        for cid, elapsed in list(self._in_flight_probes.items())[:3]:
            if elapsed >= PENDING_THRESHOLD_SEC:
                items.append(f"{cid} [PENDING {elapsed:.1f}s]")
            else:
                items.append(f"{cid} [{elapsed:.1f}s]")

        extra = len(self._in_flight_probes) - 3
        if extra > 0:
            items.append(f"+{extra} more")

        self.lbl_inflight.setText(" | In-flight: " + ", ".join(items))
