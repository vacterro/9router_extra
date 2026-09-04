"""
9router_WatchEdit - Presets Management View
Maintains local routing presets, independent of active 9Router configuration,
annotates health states and compares against active combo.
"""
from typing import Dict, List, Optional
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QListWidget,
    QListWidgetItem,
    QTableWidget,
    QTableWidgetItem,
    QHeaderView,
    QMessageBox,
    QInputDialog,
    QSplitter,
    QAbstractItemView,
)
from PySide6.QtGui import QColor

from core.combo_manager import PresetManager, Preset
from core.history import HealthCache
from ui.theme import (
    COLOR_BORDER_HIGHLIGHT,
    COLOR_TEXT_PRIMARY,
    COLOR_TEXT_SECONDARY,
    COLOR_TEXT_MUTED,
    COLOR_BACKGROUND_SOFT,
    COLOR_SURFACE,
    STATE_COLORS,
)

class PresetsView(QWidget):
    apply_preset_requested = Signal(str, list)  # preset_name, models_list

    def __init__(
        self,
        preset_manager: PresetManager,
        cache: HealthCache,
        parent: Optional[QWidget] = None,
    ):
        super().__init__(parent)
        self.preset_manager = preset_manager
        self.cache = cache
        self.current_live_models: List[str] = []
        self._selected_preset_name: Optional[str] = None
        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(6)

        # Header / Action Toolbar
        toolbar = QHBoxLayout()
        toolbar.setSpacing(6)

        self.btn_apply = QPushButton("Apply Selected Preset to Active Combo")
        self.btn_apply.setObjectName("primaryAction")
        self.btn_apply.clicked.connect(self._apply_selected_preset)

        self.btn_save_current = QPushButton("Save Active Combo as New Preset")
        self.btn_save_current.clicked.connect(self._save_active_as_preset)

        self.btn_delete_preset = QPushButton("Delete Preset")
        self.btn_delete_preset.setObjectName("dangerAction")
        self.btn_delete_preset.clicked.connect(self._delete_preset)

        toolbar.addWidget(self.btn_apply)
        toolbar.addWidget(self.btn_save_current)
        toolbar.addWidget(self.btn_delete_preset)
        toolbar.addStretch()

        layout.addLayout(toolbar)

        # Splitter: Left (Presets List) | Right (Preset Models & Comparison Table)
        splitter = QSplitter(Qt.Horizontal)

        # Left: Presets list
        left_widget = QWidget()
        left_layout = QVBoxLayout(left_widget)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.addWidget(QLabel("Available Presets:"))
        self.list_presets = QListWidget()
        self.list_presets.currentItemChanged.connect(self._on_preset_selected)
        left_layout.addWidget(self.list_presets)
        splitter.addWidget(left_widget)

        # Right: Preset Inspection & Comparison
        right_widget = QWidget()
        right_layout = QVBoxLayout(right_widget)
        right_layout.setContentsMargins(0, 0, 0, 0)

        self.lbl_preset_title = QLabel("Preset Details & Live Status:")
        self.lbl_preset_title.setStyleSheet(f"font-weight: bold; color: {COLOR_BORDER_HIGHLIGHT};")
        right_layout.addWidget(self.lbl_preset_title)

        self.table_preset_models = QTableWidget()
        self.table_preset_models.setColumnCount(4)
        self.table_preset_models.setHorizontalHeaderLabels([
            "Index", "Model Canonical ID", "Health State", "In Active Combo?"
        ])
        self.table_preset_models.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table_preset_models.setEditTriggers(QAbstractItemView.NoEditTriggers)
        header = self.table_preset_models.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeToContents)

        right_layout.addWidget(self.table_preset_models)
        splitter.addWidget(right_widget)

        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 2)
        layout.addWidget(splitter)

        self.refresh_presets_list()

    def set_current_live_combo(self, models: List[str]):
        self.current_live_models = list(models)
        self._refresh_comparison_table()

    def refresh_presets_list(self):
        self.list_presets.clear()
        for name, preset in self.preset_manager.presets.items():
            it = QListWidgetItem(f"{name} ({len(preset.models)} models)")
            it.setData(Qt.UserRole, name)
            self.list_presets.addItem(it)

        if self.list_presets.count() > 0:
            self.list_presets.setCurrentRow(0)

    def _on_preset_selected(self, current: Optional[QListWidgetItem], previous=None):
        if not current:
            self._selected_preset_name = None
            self.table_preset_models.setRowCount(0)
            return
        name = current.data(Qt.UserRole)
        self._selected_preset_name = name
        self._refresh_comparison_table()

    def _refresh_comparison_table(self):
        if not self._selected_preset_name:
            self.table_preset_models.setRowCount(0)
            return

        preset = self.preset_manager.presets.get(self._selected_preset_name)
        if not preset:
            return

        self.lbl_preset_title.setText(f"Preset: {preset.name} ({len(preset.models)} models) - {preset.description}")
        comparison = self.preset_manager.compare_with_combo(
            self._selected_preset_name,
            self.current_live_models,
            self.cache,
        )

        self.table_preset_models.setRowCount(len(comparison))
        for row, item in enumerate(comparison):
            model_id = item["model"]
            in_combo = item["in_combo"]
            state = item["state"]
            is_dead = item["is_dead"]

            # Col 0: Index
            it_idx = QTableWidgetItem(f"#{row + 1:02d}")
            it_idx.setTextAlignment(Qt.AlignCenter)

            # Col 1: Model
            it_model = QTableWidgetItem(model_id)
            if is_dead:
                it_model.setForeground(QColor(COLOR_TEXT_MUTED))

            # Col 2: Health State
            it_state = QTableWidgetItem(state)
            cfg = STATE_COLORS.get(state)
            if cfg:
                it_state.setBackground(QColor(cfg["bg"]))
                it_state.setForeground(QColor(cfg["fg"]))
            it_state.setTextAlignment(Qt.AlignCenter)

            # Col 3: In Active Combo?
            combo_str = "YES" if in_combo else "NO (Missing)"
            it_combo = QTableWidgetItem(combo_str)
            if in_combo:
                it_combo.setForeground(QColor(COLOR_BORDER_HIGHLIGHT))
            else:
                it_combo.setForeground(QColor(COLOR_TEXT_MUTED))
            it_combo.setTextAlignment(Qt.AlignCenter)

            self.table_preset_models.setItem(row, 0, it_idx)
            self.table_preset_models.setItem(row, 1, it_model)
            self.table_preset_models.setItem(row, 2, it_state)
            self.table_preset_models.setItem(row, 3, it_combo)

    def _apply_selected_preset(self):
        if not self._selected_preset_name:
            return
        preset = self.preset_manager.presets.get(self._selected_preset_name)
        if not preset:
            return
        self.apply_preset_requested.emit(preset.name, list(preset.models))

    def _save_active_as_preset(self):
        if not self.current_live_models:
            QMessageBox.warning(self, "Empty Combo", "Active combo has no models to save.")
            return

        name, ok = QInputDialog.getText(self, "Save Preset", "Enter name for new preset:")
        if ok and name.strip():
            pname = name.strip()
            self.preset_manager.save_preset(pname, self.current_live_models, "User saved preset")
            self.refresh_presets_list()

    def _delete_preset(self):
        if not self._selected_preset_name:
            return
        reply = QMessageBox.question(
            self, "Delete Preset",
            f"Are you sure you want to delete preset '{self._selected_preset_name}'?",
            QMessageBox.Yes | QMessageBox.No,
        )
        if reply == QMessageBox.Yes:
            self.preset_manager.delete_preset(self._selected_preset_name)
            self.refresh_presets_list()
