"""
9router_WatchEdit - Real-Time Safe Combo Editor
Features stable model identity (provider_prefix/model_id) decoupling from widget rows,
diff preview before applying bulk changes, and atomic synchronization with 9Router.
"""
from typing import Dict, List, Optional, Set
from dataclasses import replace
from PySide6.QtCore import Qt, Signal, QTimer
from PySide6.QtWidgets import (
    QWidget,
    QApplication,
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

from core.router_client import ComboWriteError, RouterClient
from core.history import HealthCache
from core.discovery import DiscoveredModel
from core.combo_manager import StableCombo, compute_combo_diff, ComboDiff
from core.combo_failover import failover_report
from core.upstream_attribution import attribute_upstream, owner_summary
from core.activation import (
    ActivationStore,
    ComboActivation,
    ReconcileStatus,
    reconcile,
)
from core.model_explorer import (
    ExplorerQuery,
    ModelExplorer,
    ModelRow,
    ProviderEntry,
    build_row,
    provider_entries,
    summarize,
)
from core.security import LiveAccessLockedError
from ui.model_explorer_controls import _ExplorerPanelControls
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

#: The engine has no combo-level suspension. This is the exact, measured basis
#: for the disclosure shown wherever a combo ON/OFF switch would otherwise be.
COMBO_SUSPEND_UNSUPPORTED = (
    "9Router has no combo enable/disable state to switch.\n\n"
    "Measured on the installed engine (9router 0.5.91-extra): the combos table "
    "is id, name, kind, models, createdAt, updatedAt -- there is no enabled "
    "column and no comboEnabled field; the combo PUT route persists only "
    "name/kind/models, and the routing path reads that models array verbatim.\n\n"
    "The only lever the engine offers is emptying models, which would delete "
    "the combo's configuration rather than suspend it, so this control stays "
    "off instead of showing a switch that does nothing.\n\n"
    "Per-model suspension IS enforced: a model switched OFF inside a combo is "
    "published as absent from that combo's routing list."
)

#: Combo selector filters. Only states the operator's own data can actually
#: support -- no combo-level "disabled" filter, because no such state exists.
COMBO_FILTER_ALL = "all"
COMBO_FILTER_WITH_ROUTABLE = "routable"
COMBO_FILTER_NO_ROUTABLE = "no-routable"
COMBO_FILTER_WITH_DISABLED = "has-disabled"
COMBO_FILTER_OPTIONS = (
    (COMBO_FILTER_ALL, "All combos"),
    (COMBO_FILTER_WITH_ROUTABLE, "Combos with routable models"),
    (COMBO_FILTER_NO_ROUTABLE, "Combos with no routable models"),
    (COMBO_FILTER_WITH_DISABLED, "Combos with a disabled model"),
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

class _ComboModelRef:
    """A discovered-model shape built from a bare canonical id.

    A combo entry is just a string, so the presentation model needs a minimal
    adapter for it. Everything it can state derives from the canonical id
    itself; nothing is invented.
    """

    __slots__ = ("canonical_id", "model_id", "display_name", "provider_name",
                 "provider_prefix", "connection_id", "routing_eligible", "cost_hint")

    def __init__(self, canonical_id: str):
        self.canonical_id = canonical_id
        prefix, _, model_id = canonical_id.partition("/")
        self.provider_prefix = prefix
        self.provider_name = prefix
        self.model_id = model_id or canonical_id
        self.display_name = ""
        self.connection_id = ""
        self.routing_eligible = True
        self.cost_hint = None


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
        # Multi-selection: the activation bulk actions (ON/OFF selected,
        # Toggle selected) need more than one row at a time.
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)

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
        activation_store: Optional[ActivationStore] = None,
    ):
        super().__init__(parent)
        self.client = client
        self.cache = cache
        self.combos: Dict[str, StableCombo] = {}  # combo_id -> StableCombo
        self.current_combo: Optional[StableCombo] = None
        # Durable per-combo activation (configured sequence + manual ON/OFF).
        # Constructed lazily so a test can redirect the file first.
        self._activation_store = activation_store
        self._building_combo_list = False
        #: Result of the last engine-vs-local activation reconciliation.
        self._last_reconcile = None
        #: Combo ids in engine order, so the selector can be re-filtered without
        #: re-reading the engine.
        self._combo_order: List[str] = []
        self.available_models: List[DiscoveredModel] = []
        self._routing_blocked_model_ids: Set[str] = set()
        # W2-001: ids of dirty combos whose target disappeared from an accepted
        # server snapshot. They stay selectable (tombstoned) so the local draft
        # is recoverable and visibly conflicted until the operator resolves it.
        self._missing_dirty_ids: Set[str] = set()
        #: Combo ids whose engine list no longer matches the local activation
        #: record. Kept visible and flagged; never silently overwritten.
        self._drifted_combo_ids: Set[str] = set()
        # PERF-003: canonical_id -> QListWidgetItem indices, rebuilt with each
        # list rebuild so update_model_health_badge is O(1) rather than a full
        # QList scan per probe completion.
        self._combo_item_by_cid: Dict[str, QListWidgetItem] = {}
        self._combo_row_by_cid: Dict[str, int] = {}
        self._available_item_by_cid: Dict[str, QListWidgetItem] = {}
        self._available_row_by_cid: Dict[str, int] = {}
        # Two independent presentation models: the left panel never inherits
        # the right panel's query, and neither may rewrite combo routing order.
        self._combo_explorer = ModelExplorer()
        self._available_explorer = ModelExplorer()
        self._available_model_by_cid: Dict[str, DiscoveredModel] = {}
        self._discovered_model_by_cid: Dict[str, DiscoveredModel] = {}
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

        # Combo state. The engine has no combo suspension, so this is a
        # disclosure, not a switch: it never renders as an operable control.
        self.lbl_combo_state = QLabel("State: ACTIVE")
        self.lbl_combo_state.setToolTip(COMBO_SUSPEND_UNSUPPORTED)
        top_row1.addWidget(self.lbl_combo_state)

        self.btn_combo_suspend = QPushButton("Combo OFF")
        self.btn_combo_suspend.setEnabled(False)
        self.btn_combo_suspend.setToolTip(COMBO_SUSPEND_UNSUPPORTED)
        top_row1.addWidget(self.btn_combo_suspend)

        self.btn_combo_suspend_why = QPushButton("?")
        self.btn_combo_suspend_why.setFixedWidth(20)
        self.btn_combo_suspend_why.setToolTip("Why combo ON/OFF is unavailable.")
        self.btn_combo_suspend_why.clicked.connect(self._explain_combo_suspend)
        top_row1.addWidget(self.btn_combo_suspend_why)

        self.cb_combo_filter = QComboBox()
        for value, label in COMBO_FILTER_OPTIONS:
            self.cb_combo_filter.addItem(label, value)
        self.cb_combo_filter.setToolTip(
            "Filter the combo selector by real, engine-verifiable state."
        )
        self.cb_combo_filter.currentIndexChanged.connect(self._on_combo_filter_changed)
        top_row1.addWidget(self.cb_combo_filter)

        self.lbl_combo_model_count = QLabel("0 models")
        self.lbl_combo_model_count.setStyleSheet(f"color: {COLOR_TEXT_SECONDARY};")
        top_row1.addWidget(self.lbl_combo_model_count)

        # Combo-pool counts. The engine has no combo-level enabled state, so the
        # honest counters are "fully enabled" versus "holds disabled members".
        self.lbl_combo_pool = QLabel("0 combos")
        self.lbl_combo_pool.setToolTip(
            "Engine combos: how many are fully enabled and how many hold "
            "manually disabled members."
        )
        self.lbl_combo_pool.setStyleSheet(f"color: {COLOR_TEXT_SECONDARY};")
        top_row1.addWidget(self.lbl_combo_pool)

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

        self.btn_upstream = QPushButton("Upstream...")
        self.btn_upstream.setToolTip(
            "Upstream attribution (which engine answers, which process owns it) "
            "and the predicted failover chain of the published member list."
        )
        self.btn_upstream.clicked.connect(self._show_upstream_diagnostics)

        for btn in (self.btn_new_combo, self.btn_rename, self.btn_duplicate,
                    self.btn_revert, self.btn_reload, self.btn_manage, self.btn_tools,
                    self.btn_upstream):
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

        # Failover summary of the PUBLISHED member list. It predicts what the
        # upstream engine is expected to do; it never claims a request outcome.
        self.lbl_failover = QLabel("Failover: no combo selected")
        self.lbl_failover.setStyleSheet(f"color: {COLOR_TEXT_SECONDARY};")
        self.lbl_failover.setToolTip(
            "Predicted upstream failover chain for the member list the engine "
            "holds. Open Upstream... for the full attribution and per-member "
            "expectations."
        )
        left_layout.addWidget(self.lbl_failover)

        # Left panel search/provider/health/cost. Sorting is deliberately absent:
        # the left order IS routing priority, so it is never re-ordered here.
        self.combo_controls = _ExplorerPanelControls(
            "in this combo", allow_sort=False
        )
        self.combo_controls.query_changed.connect(self._on_combo_query_changed)
        self.combo_controls.focus_by_id_requested.connect(self._focus_combo_model_by_id)
        left_layout.addWidget(self.combo_controls)

        self.list_combo_models = ReorderableComboListWidget()
        self.list_combo_models.setFont(get_app_font(11))
        self.list_combo_models.setToolTip(
            "This list is the combo's routing priority, top to bottom.\n"
            "Hiding a row with a filter is a view change only: the model stays "
            "in the combo until you remove it explicitly."
        )
        self.list_combo_models.order_changed.connect(self._on_drag_order_changed)
        self.list_combo_models.model_dropped_from_picker.connect(self._on_model_dropped_from_picker)
        self.list_combo_models.setContextMenuPolicy(Qt.CustomContextMenu)
        self.list_combo_models.customContextMenuRequested.connect(self._on_combo_list_context_menu)
        self.list_combo_models.itemChanged.connect(self._on_combo_item_changed)
        left_layout.addWidget(self.list_combo_models)

        # Combo Reorder Controls
        reorder_bar = QHBoxLayout()
        reorder_bar.setSpacing(4)

        self.btn_move_up = QPushButton("▲ Up")
        self.btn_move_up.clicked.connect(self._move_up)

        self.btn_move_down = QPushButton("▼ Down")
        self.btn_move_down.clicked.connect(self._move_down)

        # Shift+arrow the same span in one jump: the modifier is what the
        # operator already reaches for, so the buttons stay the plain span.
        self.btn_move_top = QPushButton("⇧↑ Top")
        self.btn_move_top.clicked.connect(self._move_to_top)

        self.btn_move_bottom = QPushButton("⇩↓ Bottom")
        self.btn_move_bottom.clicked.connect(self._move_to_bottom)

        self.btn_remove = QPushButton("✖ Remove")
        self.btn_remove.clicked.connect(self._remove_selected)

        reorder_bar.addWidget(self.btn_move_up)
        reorder_bar.addWidget(self.btn_move_down)
        reorder_bar.addWidget(self.btn_move_top)
        reorder_bar.addWidget(self.btn_move_bottom)
        reorder_bar.addWidget(self.btn_remove)
        left_layout.addLayout(reorder_bar)

        bulk_bar = QHBoxLayout()
        bulk_bar.setSpacing(4)

        self.btn_combo_select_visible = QPushButton("Select visible")
        self.btn_combo_select_visible.setToolTip("Select every combo model the current filter leaves visible.")
        self.btn_combo_select_visible.clicked.connect(self._select_visible_in_combo)

        self.btn_combo_clear_selection = QPushButton("Clear selection")
        self.btn_combo_clear_selection.clicked.connect(self.list_combo_models.clearSelection)

        self.btn_combo_remove_visible = QPushButton("Remove selected visible")
        self.btn_combo_remove_visible.setToolTip(
            "Remove the selected models that are currently visible. Asks first."
        )
        self.btn_combo_remove_visible.clicked.connect(self._remove_selected_visible)

        self.btn_combo_copy_ids = QPushButton("Copy IDs")
        self.btn_combo_copy_ids.setToolTip("Copy the selected canonical model ids to the clipboard.")
        self.btn_combo_copy_ids.clicked.connect(self._copy_combo_selected_ids)

        for btn in (self.btn_combo_select_visible, self.btn_combo_clear_selection,
                    self.btn_combo_remove_visible, self.btn_combo_copy_ids):
            bulk_bar.addWidget(btn)
        left_layout.addLayout(bulk_bar)

        activation_bar = QHBoxLayout()
        activation_bar.setSpacing(4)

        self.btn_enable_selected = QPushButton("ON selected")
        self.btn_enable_selected.setToolTip(
            "Switch the selected models ON in this combo (local draft, then Save)."
        )
        self.btn_enable_selected.clicked.connect(lambda: self._set_activation_selected(True))

        self.btn_disable_selected = QPushButton("OFF selected")
        self.btn_disable_selected.setToolTip(
            "Switch the selected models OFF in this combo. The models stay "
            "configured and keep their routing position; they are only excluded "
            "from what the router is given. Local draft until you Save."
        )
        self.btn_disable_selected.clicked.connect(lambda: self._set_activation_selected(False))

        self.btn_enable_visible = QPushButton("ON visible")
        self.btn_enable_visible.setToolTip(
            "Switch every model the current filter leaves VISIBLE ON. Hidden rows "
            "are never included."
        )
        self.btn_enable_visible.clicked.connect(lambda: self._set_activation_visible(True))

        self.btn_disable_visible = QPushButton("OFF visible")
        self.btn_disable_visible.setToolTip(
            "Switch every model the current filter leaves VISIBLE OFF. Hidden rows "
            "are never included, and the exact scope is shown before applying."
        )
        self.btn_disable_visible.clicked.connect(lambda: self._set_activation_visible(False))

        self.btn_toggle_selected = QPushButton("Toggle selected")
        self.btn_toggle_selected.setToolTip(
            "Flip the ON/OFF state of the selected models in this combo."
        )
        self.btn_toggle_selected.clicked.connect(self._toggle_activation_selected)

        self.btn_enable_provider = QPushButton("ON provider")
        self.btn_enable_provider.setToolTip(
            "Switch ON every member of this combo that belongs to a provider "
            "selected in the panel's provider filter. Scope is previewed first."
        )
        self.btn_enable_provider.clicked.connect(lambda: self._set_activation_provider(True))

        self.btn_disable_provider = QPushButton("OFF provider")
        self.btn_disable_provider.setToolTip(
            "Switch OFF every member of this combo that belongs to a provider "
            "selected in the panel's provider filter. Scope is previewed first."
        )
        self.btn_disable_provider.clicked.connect(lambda: self._set_activation_provider(False))

        self.btn_reset_activation = QPushButton("Reset activation")
        self.btn_reset_activation.setToolTip(
            "Re-enable every model in this combo (activation only; membership is untouched)."
        )
        self.btn_reset_activation.clicked.connect(lambda _checked=False: self._reset_activation())

        for btn in (self.btn_enable_selected, self.btn_disable_selected,
                    self.btn_enable_visible, self.btn_disable_visible,
                    self.btn_toggle_selected, self.btn_enable_provider,
                    self.btn_disable_provider,
                    self.btn_reset_activation):
            activation_bar.addWidget(btn)
        activation_bar.addStretch()
        left_layout.addLayout(activation_bar)

        splitter.addWidget(left_widget)

        # --- RIGHT PANE: AVAILABLE MODELS PICKER ---
        right_widget = QWidget()
        right_layout = QVBoxLayout(right_widget)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(4)

        right_header = QHBoxLayout()
        right_header.addWidget(QLabel("Available Models:"))
        right_header.addStretch()
        right_layout.addLayout(right_header)

        # Right panel search/provider/health/cost plus sort and grouping.
        self.available_controls = _ExplorerPanelControls(
            "available models", allow_sort=True
        )
        self.available_controls.query_changed.connect(self._on_available_query_changed)
        self.available_controls.focus_by_id_requested.connect(self._focus_available_model_by_id)
        right_layout.addWidget(self.available_controls)

        # Retained attribute names: existing callers and tests address the picker
        # search field and its health dropdown directly.
        self.txt_picker_search = self.available_controls.search.edit
        self.cb_picker_filter = self.available_controls.cb_health

        self.list_available_models = QListWidget()
        self.list_available_models.setFont(get_app_font(11))
        self.list_available_models.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.list_available_models.setDragEnabled(True)
        self.list_available_models.setDragDropMode(QAbstractItemView.DragOnly)
        self.list_available_models.itemDoubleClicked.connect(self._on_picker_double_clicked)
        self.list_available_models.setContextMenuPolicy(Qt.CustomContextMenu)
        self.list_available_models.customContextMenuRequested.connect(
            self._on_available_list_context_menu
        )
        right_layout.addWidget(self.list_available_models)

        # Picker action bar
        picker_bar = QHBoxLayout()
        self.btn_add_to_combo = QPushButton("Add selected")
        self.btn_add_to_combo.setObjectName("primaryAction")
        self.btn_add_to_combo.clicked.connect(self._add_selected_from_picker)

        self.btn_add_visible = QPushButton("Add selected visible")
        self.btn_add_visible.setToolTip(
            "Add only the selected models the current filter leaves visible."
        )
        self.btn_add_visible.clicked.connect(self._add_selected_visible)

        self.btn_copy_ids = QPushButton("Copy IDs")
        self.btn_copy_ids.setToolTip("Copy the selected canonical model ids to the clipboard.")
        self.btn_copy_ids.clicked.connect(self._copy_available_selected_ids)

        picker_bar.addWidget(self.btn_add_to_combo)
        picker_bar.addWidget(self.btn_add_visible)
        picker_bar.addWidget(self.btn_copy_ids)
        right_layout.addLayout(picker_bar)

        splitter.addWidget(right_widget)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        # Sensible minimum panel widths so neither side collapses below usable.
        splitter.setCollapsible(0, False)
        splitter.setCollapsible(1, False)
        left_widget.setMinimumWidth(280)
        right_widget.setMinimumWidth(320)

        main_layout.addWidget(splitter)

    def keyPressEvent(self, event):
        """Ctrl+F focuses the model search of the panel that has focus.

        The keystroke is only claimed when focus is not already inside another
        editing control, so an active text field keeps its own Ctrl+F.
        """
        if event.key() == Qt.Key_F and event.modifiers() & Qt.ControlModifier:
            focus_owner = QApplication.focusWidget()
            if focus_owner is None or not self._is_editing_control(focus_owner):
                search = self._search_for_focused_panel()
                if search is not None:
                    search.take_focus()
                    event.accept()
                    return
        if self._handle_shift_arrow(event):
            return
        super().keyPressEvent(event)

    def _handle_shift_arrow(self, event) -> bool:
        """Shift+arrow moves the selected combo model to the span's edge.

        Only consumed when the combo list owns focus: that list's order IS
        routing priority, so the shortcut targets it unambiguously. The picker
        keeps its own arrow navigation. Disabled while a filter is active, so
        a hidden row can never be silently dragged to an edge.
        """
        if not (event.modifiers() & Qt.ShiftModifier):
            return False
        key = event.key()
        if key not in (Qt.Key_Up, Qt.Key_Down):
            return False
        if QApplication.focusWidget() is not self.list_combo_models:
            return False
        if self._combo_explorer.query.is_filtering():
            return False
        if key == Qt.Key_Up:
            self._move_to_top()
        else:
            self._move_to_bottom()
        event.accept()
        return True

    def _is_editing_control(self, widget: QWidget) -> bool:
        """True when the focused widget is itself a text editor."""
        return isinstance(widget, (QLineEdit, QTextEdit))

    def _search_for_focused_panel(self):
        """The search field of whichever panel currently owns focus."""
        focus_owner = QApplication.focusWidget()
        panels = (self.combo_controls, self.available_controls)
        lists = (self.list_combo_models, self.list_available_models)
        for controls, model_list in zip(panels, lists):
            if focus_owner is model_list or model_list.isAncestorOf(focus_owner):
                return controls.search
        # No list focused: fall back to the picker, the usual find target.
        return self.available_controls.search

    def resizeEvent(self, event):
        """Narrow windows drop into the panels' compact filtering mode."""
        super().resizeEvent(event)
        narrow = self.width() < 900
        self.combo_controls.set_compact(narrow)
        self.available_controls.set_compact(narrow)

    def export_display_prefs(self) -> Dict[str, Dict]:
        """JSON-safe form of both panels' view (search, filters, sort)."""
        return {
            "combo": self._combo_explorer.query.to_dict(),
            "available": self._available_explorer.query.to_dict(),
        }

    def apply_display_prefs(self, data: Optional[Dict]) -> None:
        """Restore remembered display preferences without touching combo state."""
        data = data if isinstance(data, dict) else {}
        self.combo_controls.apply_query(ExplorerQuery.from_dict(data.get("combo")))
        self.available_controls.apply_query(ExplorerQuery.from_dict(data.get("available")))
        self._on_combo_query_changed()
        self._on_available_query_changed()


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
        self._drifted_combo_ids.clear()
        self._combo_order = []
        store = self.activation_store

        for c in raw_combos:
            cid = c.get("id")
            server_models = list(c.get("models", []))
            combo = StableCombo(
                combo_id=cid,
                name=c.get("name"),
                models=server_models,
                kind=c.get("kind"),
                updated_at=c.get("updatedAt", ""),
            )
            self._hydrate_activation(combo, server_models, c.get("updatedAt", ""), store)
            self.combos[cid] = combo
            self._combo_order.append(cid)

        # Drop activation records for combos the engine no longer lists, so a
        # deleted combo can never leave state that affects another one.
        present = set(self._combo_order)
        gone = store.prune_absent(present)
        if gone:
            self._persist_activation_store()

        self._repopulate_combo_selector(current_id=prev_id)

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
            if idx < 0 and self.cb_combo_selector.count():
                # The active combo-filter excludes the previously selected
                # combo; fall back to the first combo that does match.
                idx = 0
                target_id = self.cb_combo_selector.itemData(0)
            if idx >= 0:
                # setCurrentIndex is a no-op when the index did not change
                # (cleared combos reset to 0), so select explicitly too.
                self.cb_combo_selector.setCurrentIndex(idx)
                self._select_combo_by_index(idx)
        else:
            self.current_combo = None
            self._refresh_combo_models_list()
            self._refresh_available_list()

    def _repopulate_combo_selector(self, current_id: Optional[str] = None):
        """Rebuild the selector from the current combo map + combo filter."""
        self.cb_combo_selector.blockSignals(True)
        self.cb_combo_selector.clear()
        for cid in self._combo_order:
            combo = self.combos.get(cid)
            if combo is None or not self._combo_matches_filter(combo):
                continue
            self.cb_combo_selector.addItem(self._combo_selector_label(combo), cid)
        self.cb_combo_selector.blockSignals(False)

    def _hydrate_activation(self, combo: StableCombo, server_models: List[str],
                            server_updated_at: str, store: ActivationStore) -> None:
        """Apply the durable activation record to a freshly read combo.

        The engine's models array is the projection, not the configured
        sequence. The local record holds the full sequence (including disabled
        members), so it is authoritative for membership and the engine is
        reconciled against it -- an engine list that is shorter than the local
        projection is reported as drift, never accepted as deletion.
        """
        record = store.get(combo.id)
        if record is None:
            # First sighting: the engine IS the configured truth.
            store.ensure(combo.id, combo.name, server_models)
            combo.models = list(server_models)
            combo.disabled = set()
            combo.baseline_models = list(server_models)
            combo.baseline_disabled = set()
            combo.baseline_updated_at = server_updated_at
            return

        record.combo_name = combo.name
        result = reconcile(record, server_models, server_updated_at)
        self._last_reconcile = result
        combo.models = list(record.configured)
        combo.disabled = set(record.disabled)
        combo.baseline_models = list(record.configured)
        combo.baseline_disabled = set(record.disabled)
        combo.baseline_updated_at = server_updated_at
        if result.is_conflict:
            # Engine and local record disagree. The local configured sequence
            # stays visible and the divergence is flagged; an externally
            # shortened list is never read as an intentional deletion.
            self._drifted_combo_ids.add(combo.id)
            # Make the projection the operator sees differ from the engine's,
            # so the change is an explicit, reviewable diff on the next Save.
            combo.baseline_disabled = set()

    @property
    def activation_store(self) -> ActivationStore:
        if self._activation_store is None:
            self._activation_store = ActivationStore()
        return self._activation_store

    def _persist_activation_store(self):
        """Persist the activation record; a failure is surfaced, never silent."""
        try:
            self.activation_store.save()
        except Exception as ex:  # noqa: BLE001 - surfaced to the operator
            QMessageBox.warning(
                self, "Activation State Not Saved",
                "The per-combo activation record could not be written:\n"
                f"{ex}\n\nRouting was not changed.",
            )
            return False
        return True

    def set_available_models(self, models: List[DiscoveredModel]):
        # Empty live catalogues are authoritative negative routing evidence,
        # but their inventory rows remain known for audit and re-probing.
        self._routing_blocked_model_ids = {
            m.canonical_id for m in models if not m.routing_eligible
        }
        # Keep the FULL discovery snapshot so a combo member that is not
        # currently addable (because it is already in the combo) still has its
        # metadata available for blocking evidence. The addable subset is derived
        # below and never replaces this record.
        self._discovered_model_by_cid = {m.canonical_id: m for m in models}
        self.available_models = [m for m in models if m.routing_eligible]
        self._available_model_by_cid = {m.canonical_id: m for m in self.available_models}
        self._refresh_combo_models_list()
        self._refresh_available_list()

    # ------------------------------------------------------------------
    # Presentation: both panels are driven by one model_explorer.ModelExplorer.
    # Nothing in this section mutates a combo, issues a request or writes a
    # cache: search/filter/sort are view operations over the existing snapshots.
    # ------------------------------------------------------------------
    @staticmethod
    def _row_text(row: ModelRow, position: Optional[int]) -> str:
        """One readable line: activation, position, identity, provider, badges."""
        identity = row.display_name or row.canonical_id
        prefix = f"#{position:02d}  " if position is not None else ""
        described = [f"{prefix}{identity}"]
        for extra in (row.provider_label, row.canonical_id):
            if extra and extra != identity and extra not in described:
                described.append(extra)
        badges = [f"[{row.health_badge}]"]
        if row.cost_badge != "UNKNOWN":
            badges.append(f"[{row.cost_badge}]")
        elif row.health_badge != "UNTESTED":
            badges.append("[cost ?]")
        if row.latency_ms > 0:
            badges.append(f"{row.latency_ms:.0f}ms")
        # Activation is stated on combo rows only: a model in the picker is not
        # part of the combo, so it has no per-combo activation to report. A
        # member switched ON but not routable is labelled BLOCKED rather than
        # left looking healthy.
        if row.within_combo:
            if not row.manual_enabled:
                state = "[OFF]"
            elif row.effective_routable:
                state = "[ON]"
            else:
                state = "[ON/BLOCKED]"
            if row.pending_change:
                state += "[unsaved]"
            described.insert(0, state)
        return "  ".join([" · ".join(described)] + badges)

    @staticmethod
    def _row_tooltip(row: ModelRow, position: Optional[int]) -> str:
        latency = f"{row.latency_ms:.0f} ms" if row.latency_ms > 0 else "not measured"
        lines = [
            f"Model: {row.display_name or row.canonical_id}",
            f"Canonical id: {row.canonical_id}",
            f"Provider: {row.provider_label} ({row.provider_key})",
            f"Health: {row.health_badge}",
            f"Cost: {row.cost_badge}",
            f"Latency: {latency}",
            f"Last tested: {row.last_tested_at or 'never'}",
            f"Routing eligible: {'yes' if row.routing_eligible else 'no'}",
        ]
        if position is not None:
            lines.insert(1, f"Combo position: #{position} (routing priority)")
        if row.within_combo:
            manual = "ON" if row.manual_enabled else "OFF"
            effective = "routable" if row.effective_routable else "NOT routable"
            lines.append(f"Manual (this combo): {manual}")
            lines.append(f"Effective: {effective}")
            if row.effective_reason and not row.effective_routable:
                lines.append(f"Reason: {row.effective_reason}")
            if row.pending_change:
                lines.append("Pending change: not applied to the engine yet")
        return "\n".join(lines)

    @staticmethod
    def _row_foreground(row: ModelRow) -> QColor:
        if row.health_badge in STATE_COLORS:
            return QColor(STATE_COLORS[row.health_badge]["fg"])
        return QColor(COLOR_TEXT_MUTED)

    def _combo_rows(self) -> List[ModelRow]:
        """Rows for the current combo, in canonical (routing) order.

        A combo entry is a bare canonical id; when discovery knows the model we
        use its metadata, otherwise the id alone is all that is claimed.
        """
        if not self.current_combo:
            return []
        combo = self.current_combo
        changed = (
            combo.disabled != combo.baseline_disabled
        )
        pending_ids = (
            combo.disabled ^ combo.baseline_disabled if changed else set()
        )
        rows: List[ModelRow] = []
        for idx, m in enumerate(combo.models):
            model = self._discovered_model_by_cid.get(m)
            if model is None:
                model = self._available_model_by_cid.get(m)
            blocked, reason = self._blocking_reason(m, model)
            row = build_row(
                model if model is not None else _ComboModelRef(m),
                self.cache.get(m),
                idx,
                within_combo=True,
            )
            rows.append(replace(
                row,
                manual_enabled=combo.is_model_enabled(m),
                blocked=blocked,
                blocked_reason=reason,
                pending_change=m in pending_ids,
            ))
        return rows

    def _blocking_reason(self, canonical_id: str, model: Optional[DiscoveredModel]):
        """Why the engine would not select this member, if it would not.

        Only evidence we actually hold is allowed to block a row: a catalog
        exclusion, a model missing from the live catalog, or a DEAD probe. A
        temporary provider failure is NOT a routing block and stays a health
        badge, because the engine will still attempt it.
        """
        if canonical_id in self._routing_blocked_model_ids:
            return True, "excluded from the discovery catalogue (routing_eligible = false)"
        if model is None:
            return True, "not present in the current live catalogue"
        record = self.cache.get(canonical_id)
        if record is not None and str(getattr(record, "availability", "")).upper() == "DEAD":
            return True, "last probe reported DEAD"
        return False, ""

    def _available_rows(self) -> List[ModelRow]:
        """Rows for every model that can still be added to the current combo."""
        current_set = set(self.current_combo.models) if self.current_combo else set()
        return [
            build_row(m, self.cache.get(m.canonical_id), idx)
            for idx, m in enumerate(self.available_models)
            if m.canonical_id not in current_set
        ]

    def _refresh_provider_catalog(self):
        """Rebuild both provider selectors, preserving active selections."""
        combo_entries = provider_entries(self._combo_rows())
        previous = dict(self.available_controls.provider._entries)
        available_entries = provider_entries(self._available_rows(), previous)
        self.combo_controls.set_provider_entries(combo_entries)
        self.available_controls.set_provider_entries(available_entries)

    # ------------------------------------------------------------ LEFT panel
    def _on_combo_query_changed(self):
        self._combo_explorer.set_query(self.combo_controls.query())
        self._apply_combo_visibility()

    def _refresh_combo_models_list(self):
        """Rebuild the left list: always the full membership, canonical order.

        Filtering hides rows; it never removes them, so a hidden model can
        never be dropped from the combo as a side effect of a view operation.
        """
        self._combo_explorer.set_rows(self._combo_rows())
        self._building_combo_list = True
        self.list_combo_models.clear()
        self._combo_item_by_cid.clear()
        self._combo_row_by_cid.clear()

        if not self.current_combo:
            self._building_combo_list = False
            self.lbl_combo_count.setText("Combo Models (0):")
            self.lbl_combo_model_count.setText("0 models")
            self.lbl_combo_state.setText("State: (no combo)")
            self.lbl_failover.setText("Failover: no combo selected")
            self.combo_controls.set_status(0, 0, "", empty_catalog=False)
            self._update_reorder_affordances()
            return

        self.lbl_combo_state.setText("State: ACTIVE")

        for row in self._combo_explorer.rows:
            position = row.discovery_index + 1
            item = QListWidgetItem(self._row_text(row, position))
            item.setData(Qt.UserRole, row.canonical_id)
            item.setData(Qt.UserRole + 1, row)
            item.setToolTip(self._row_tooltip(row, position))
            item.setForeground(self._row_foreground(row))
            if row.canonical_id in self._routing_blocked_model_ids:
                item.setForeground(QColor(COLOR_DANGER))
            # The per-row ON/OFF switch. A disabled member is shown, never
            # hidden, so it can always be switched back on.
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked if row.manual_enabled else Qt.Unchecked)
            self.list_combo_models.addItem(item)
            # PERF-003: maintain the keyed index as we build.
            self._combo_item_by_cid[row.canonical_id] = item
            self._combo_row_by_cid[row.canonical_id] = self.list_combo_models.count() - 1

        self._building_combo_list = False
        self._refresh_activation_counts()
        self._refresh_failover_summary()
        self._apply_combo_visibility()
        self._refresh_provider_catalog()
        self._update_reorder_affordances()

    def _refresh_activation_counts(self):
        """The counts line: configured / enabled / routable / disabled.

        Configured counts every member the combo still holds; enabled is the
        operator's manual ON; routable additionally excludes blocked members;
        disabled is configured minus enabled. They are deliberately separate
        numbers, so a global-eligibility reading can never be confused with a
        per-combo activation reading.
        """
        self._refresh_combo_pool_counts()
        combo = self.current_combo
        if combo is None:
            return
        routable_ids = {
            m.canonical_id for m in self._combo_explorer.rows if m.effective_routable
        }
        counts = combo.activation_counts(routable_ids)
        self.lbl_combo_count.setText(f"{combo.name} ({counts['configured']} configured):")
        self.lbl_combo_model_count.setText(
            f"{counts['configured']} configured | {counts['enabled']} enabled | "
            f"{counts['routable']} routable | {counts['disabled']} disabled"
        )

    # ------------------------------------------------------- failover chain
    def _published_members(self) -> List[str]:
        """The member list the ENGINE holds for the current combo.

        The engine routes what it was given, so the failover prediction is
        built from the last verified engine sequence, not from an unsaved
        draft. A draft-only change therefore never shows up as a live route.
        """
        combo = self.current_combo
        if combo is None:
            return []
        return combo.effective_baseline()

    def _health_snapshot(self, members: List[str]) -> Dict[str, object]:
        """Health records for the given members, read from the local cache."""
        snapshot: Dict[str, object] = {}
        for cid in members:
            record = self.cache.get(cid) if self.cache is not None else None
            if record is not None:
                snapshot[cid] = record
        return snapshot

    def _failover_report(self) -> dict:
        """Failover diagnostics for the current combo (engine member list)."""
        combo = self.current_combo
        members = self._published_members()
        disabled = combo.disabled_models() if combo is not None else []
        return failover_report(
            members,
            self._health_snapshot(members),
            disabled=disabled,
            combo_name=combo.name if combo is not None else "",
        )

    def _refresh_failover_summary(self):
        """Show the predicted upstream failover chain of the published list.

        This is a PREDICTION from the local health history, attributed to the
        upstream engine's own failover rules. It never claims that a request
        was served or failed.
        """
        if self.current_combo is None:
            self.lbl_failover.setText("Failover: no combo selected")
            self.lbl_failover.setToolTip("")
            return
        report = self._failover_report()
        text = report["summary"]
        draft_note = ""
        if self.current_combo.has_unsaved_changes():
            # The chain above is the ENGINE's list. An unsaved draft is named
            # separately, so the label can never read as "already published".
            draft = self.current_combo.effective_models()
            draft_note = f" | draft {len(draft)} enabled (unsaved)"
            text = text + draft_note
        self.lbl_failover.setText(text)
        lines = [
            f"Published members (the engine's routing list): {report['member_count']}",
            f"Serving (predicted): {report['serving'] or 'none proven routable'}",
            f"Failover: {report['failover_from'] or 'none'} -> "
            f"{report['failover_to'] or 'no member left'}",
            "",
            "Upstream rules:",
        ]
        lines.extend(f"- {rule}" for rule in report["upstream_rules"])
        if draft_note:
            lines.insert(
                0,
                "Unsaved draft: the published list above is what the engine "
                "holds right now; the draft is not published yet.",
            )
        if report["disabled"]:
            lines.append("")
            lines.append(
                "Configured but disabled (not published): "
                + ", ".join(report["disabled"])
            )
        self.lbl_failover.setToolTip("\n".join(lines))

    def _refresh_combo_pool_counts(self):
        """Combo-pool counters.

        The engine has no combo-level enabled state, so the counters describe
        what the operator's own activation state really is: how many combos
        are fully enabled, and how many hold manually disabled members. They
        never claim a combo is "disabled" in the engine.
        """
        combos = [self.combos.get(cid) for cid in self._combo_order]
        combos = [c for c in combos if c is not None]
        total = len(combos)
        with_disabled = sum(1 for c in combos if c.disabled)
        no_route = sum(1 for c in combos if c.has_no_routable_models())
        self.lbl_combo_pool.setText(
            f"{total} combos | {total - with_disabled} fully enabled | "
            f"{with_disabled} with disabled models"
        )
        self.lbl_combo_pool.setToolTip(
            f"{total} combo(s) known to the engine. {total - with_disabled} have "
            f"every member enabled, {with_disabled} hold manually disabled "
            f"members, {no_route} have no enabled member left."
        )
        self._update_dirty_state()

    def _apply_combo_visibility(self):
        """Hide the left rows the current query excludes. Nothing is removed."""
        query = self._combo_explorer.query
        visible = {row.canonical_id: row for row in self._combo_explorer.visible}
        total = self._combo_explorer.total
        for cid, item in self._combo_item_by_cid.items():
            self.list_combo_models.setRowHidden(
                self._combo_row_by_cid[cid], cid not in visible
            )
        self.combo_controls.set_status(
            len(visible), total, summarize(query, len(visible), total), empty_catalog=not total
        )
        # A filter typed after the list was built must also re-gate drag/Up/Down.
        self._update_reorder_affordances()

    def _update_reorder_affordances(self):
        """Reorder is only safe on the canonical view: disable it under a filter.

        Reordering a filtered list would silently rewrite combos whose rows the
        operator cannot see, so drag, Up and Down stay unavailable until the
        view is restored. Removing is always available.
        """
        filtering = self._combo_explorer.query.is_filtering()
        self.list_combo_models.setDragEnabled(not filtering)
        self.btn_move_up.setEnabled(not filtering)
        self.btn_move_down.setEnabled(not filtering)
        self.btn_move_top.setEnabled(not filtering)
        self.btn_move_bottom.setEnabled(not filtering)
        if filtering:
            for btn in (self.btn_move_up, self.btn_move_down,
                        self.btn_move_top, self.btn_move_bottom):
                btn.setToolTip("Restore the combo view to reorder (Reset filters).")
        else:
            self.btn_move_up.setToolTip("Move the selected model up one routing position.")
            self.btn_move_down.setToolTip("Move the selected model down one routing position.")
            self.btn_move_top.setToolTip("Move the selected model to the top of the combo.")
            self.btn_move_bottom.setToolTip("Move the selected model to the bottom of the combo.")

    def _select_visible_in_combo(self):
        for cid, item in self._combo_item_by_cid.items():
            if not self.list_combo_models.isRowHidden(self._combo_row_by_cid[cid]):
                item.setSelected(True)

    def _copy_combo_selected_ids(self):
        cids = [it.data(Qt.UserRole) for it in self.list_combo_models.selectedItems()]
        self._copy_cids(cids)

    def _remove_selected_visible(self):
        """Remove the selected models that are visible right now, with a prompt."""
        if not self.current_combo:
            return
        targets = [
            it.data(Qt.UserRole) for it in self.list_combo_models.selectedItems()
            if not self.list_combo_models.isRowHidden(self.list_combo_models.row(it))
        ]
        if not targets:
            return
        if not self._confirm_bulk_removal(len(targets)):
            return
        self.current_combo.remove_models(targets)
        self._refresh_combo_models_list()
        self._refresh_available_list()

    def _confirm_bulk_removal(self, count: int) -> bool:
        reply = QMessageBox.question(
            self, "Confirm Removal",
            f"Remove {count} selected model{'s' if count != 1 else ''} from this combo?",
            QMessageBox.Yes | QMessageBox.No,
        )
        return reply == QMessageBox.Yes

    # ------------------------------------------------------------------
    # Per-combo model activation (ON/OFF).
    #
    # Activation is a LOCAL DRAFT: flipping a switch never writes to the
    # router. The change participates in dirty tracking and is published only
    # through Save, which reads back and verifies before claiming success.
    # ------------------------------------------------------------------
    def _on_combo_item_changed(self, item: QListWidgetItem):
        """One row's ON/OFF checkbox toggled by the operator."""
        if self._building_combo_list or not self.current_combo:
            return
        cid = item.data(Qt.UserRole)
        if not cid:
            return
        wanted = item.checkState() == Qt.Checked
        if not self.current_combo.set_model_enabled(cid, wanted):
            return
        self._after_activation_change()

    def _set_activation_selected(self, enabled: bool) -> List[str]:
        """Switch the selected combo models ON or OFF. Returns the changed ids."""
        if not self.current_combo:
            return []
        targets = [it.data(Qt.UserRole) for it in self.list_combo_models.selectedItems()]
        changed = self.current_combo.set_models_enabled([c for c in targets if c], enabled)
        if changed:
            self._guard_last_routable(changed, enabled)
            self._after_activation_change()
        return changed

    def _set_activation_visible(self, enabled: bool) -> List[str]:
        """Bulk activation limited to rows the current filter leaves VISIBLE.

        The exact scope is previewed before anything changes, and a row hidden
        by a filter is never touched, so a broad selection under a narrow filter
        does exactly what its label says.
        """
        if not self.current_combo:
            return []
        visible = [row.canonical_id for row in self._combo_explorer.visible]
        targets = [c for c in visible if self.current_combo.is_model_enabled(c) != enabled]
        if not targets:
            QMessageBox.information(
                self, "Nothing to change",
                f"Every visible model is already {'ON' if enabled else 'OFF'}.",
            )
            return []
        hidden_total = self._combo_explorer.total - len(visible)
        if not self._confirm_bulk_activation(targets, enabled, hidden_total):
            return []
        changed = self.current_combo.set_models_enabled(targets, enabled)
        self._guard_last_routable(changed, enabled)
        self._after_activation_change()
        return changed

    def _confirm_bulk_activation(self, targets: List[str], enabled: bool,
                                 hidden_total: int,
                                 scope_label: str = "visible model(s)") -> bool:
        scope = "\n".join(f"  • {cid}" for cid in targets[:20])
        if len(targets) > 20:
            scope += f"\n  ... and {len(targets) - 20} more"
        hidden_note = (
            f"\n\n{hidden_total} row(s) are hidden by the current filter and are "
            "NOT part of this operation."
            if hidden_total else ""
        )
        reply = QMessageBox.question(
            self, "Confirm Bulk Activation",
            f"Switch {len(targets)} {scope_label} "
            f"{'ON' if enabled else 'OFF'} in this combo?\n\n"
            f"Exact scope:\n{scope}{hidden_note}\n\n"
            "Nothing is sent to the router until you Save.",
            QMessageBox.Yes | QMessageBox.No,
        )
        return reply == QMessageBox.Yes

    def _toggle_activation_selected(self) -> List[str]:
        """Flip the activation of the selected models in this combo."""
        if not self.current_combo:
            return []
        targets = [it.data(Qt.UserRole) for it in self.list_combo_models.selectedItems()]
        changed = self.current_combo.toggle_models([c for c in targets if c])
        if changed:
            self._after_activation_change()
        return changed

    def _set_activation_for_provider(self, provider_key: str, enabled: bool) -> List[str]:
        """Bulk activation scoped to one provider, regardless of the filter."""
        return self._set_activation_provider_keys({provider_key}, enabled)

    def _set_activation_provider_key(self, provider_key: str, enabled: bool) -> List[str]:
        """Alias for the per-provider activation action."""
        return self._set_activation_for_provider(provider_key, enabled)

    def _set_activation_for_provider_key(self, provider_key: str, enabled: bool) -> List[str]:
        return self._set_activation_for_provider(provider_key, enabled)

    def _toggle_activation_provider(self, provider_key: str) -> List[str]:
        by_cid = {row.canonical_id: row for row in self._combo_rows()}
        configured = [
            cid for cid in self.current_combo.models
            if by_cid.get(cid) is not None and by_cid[cid].provider_key == provider_key
        ] if self.current_combo else []
        if not configured:
            return []
        enabled = all(self.current_combo.is_model_enabled(cid) for cid in configured)
        return self._set_activation_for_provider(provider_key, not enabled)

    def _set_activation_provider_keys(self, provider_keys, enabled: bool) -> List[str]:
        if not self.current_combo:
            return []
        provider_keys = set(provider_keys or ())
        if not provider_keys:
            QMessageBox.information(
                self, "No Provider Selected",
                "Select at least one provider in this panel's provider filter first.",
            )
            return []
        by_cid = {row.canonical_id: row for row in self._combo_rows()}
        targets = []
        for cid in self.current_combo.models:
            row = by_cid.get(cid)
            if row is None or row.provider_key not in provider_keys:
                continue
            if self.current_combo.is_model_enabled(cid) != enabled:
                targets.append(cid)
        if not targets:
            QMessageBox.information(
                self, "Nothing to change",
                "Every member of the selected provider(s) is already "
                f"{'ON' if enabled else 'OFF'}.",
            )
            return []
        if not self._confirm_bulk_activation(
            targets, enabled, 0, scope_label="model(s) of the selected provider(s)"
        ):
            return []
        changed = self.current_combo.set_models_enabled(targets, enabled)
        self._guard_last_routable(changed, enabled)
        self._after_activation_change()
        return changed

    def _set_activation_provider(self, enabled: bool, provider_keys=None) -> List[str]:
        """Bulk activation scoped to the providers selected in the left panel.

        The scope is the PROVIDER selection, not the visible rows: a member of
        a selected provider is included even when a search or health filter
        currently hides it, so the preview always names the exact list.
        """
        if provider_keys is None:
            provider_keys = set(self.combo_controls.provider.selected())
        return self._set_activation_provider_keys(provider_keys, enabled)

    def _guard_last_routable(self, changed: List[str], enabled: bool):
        """Warn when an edit leaves the combo with nothing to route.

        The engine resolves an empty models array to no combo at all, so this
        is the one activation state worth a second look. The operator's intent
        is still honoured; it is the consequence that is stated.
        """
        if enabled or not changed or not self.current_combo:
            return
        if not self.current_combo.has_no_routable_models():
            return
        QMessageBox.warning(
            self, "Combo Would Have No Routable Model",
            f"\"{self.current_combo.name}\" would have no enabled model left.\n\n"
            "9Router cannot route an empty combo: a request naming it would not "
            "resolve. The members stay configured and you can switch one back ON "
            "at any time; Save is still required to publish this state.",
        )

    def _reset_activation(self, notify: bool = True) -> List[str]:
        """Re-enable every model in the combo. Membership is never touched."""
        if not self.current_combo:
            return []
        if not self.current_combo.disabled:
            if notify:
                QMessageBox.information(self, "Nothing to reset", "Every model is already ON.")
            return []
        restored = self.current_combo.reset_activation()
        self._after_activation_change()
        if notify:
            QMessageBox.information(
                self, "Activation Reset",
                f"{len(restored)} model(s) switched back ON at their original routing "
                "positions. Save to publish.",
            )
        return restored

    def _after_activation_change(self):
        """Rebuild the view after an activation edit: draft only, no I/O."""
        self._rebuild_activation_view()

    def _rebuild_activation_view(self):
        """Repaint activation state without discarding the draft."""
        self._refresh_combo_models_list()
        self._refresh_available_list()
        self._update_dirty_state()

    # -------------------------------------------------- combo-level disclosure
    def _explain_combo_suspend(self):
        QMessageBox.information(self, "Combo ON/OFF Is Not Available", COMBO_SUSPEND_UNSUPPORTED)

    def _on_combo_filter_changed(self, _index: int = 0):
        self._apply_combo_filter()

    def _combo_matches_filter(self, combo: Optional[StableCombo]) -> bool:
        if combo is None:
            return True
        selected = self.cb_combo_filter.currentData()
        if selected in (None, COMBO_FILTER_ALL):
            return True
        routable = False
        for cid in combo.effective_models():
            blocked, _reason = self._blocking_reason(cid, self._available_model_by_cid.get(cid))
            if not blocked:
                routable = True
                break
        if selected == COMBO_FILTER_WITH_ROUTABLE:
            return routable
        if selected == COMBO_FILTER_NO_ROUTABLE:
            return not routable
        if selected == COMBO_FILTER_WITH_DISABLED:
            return bool(combo.disabled)
        return True

    def _apply_combo_filter(self):
        """Re-populate the combo selector from the filter, keeping the selection
        whenever the selected combo still matches."""
        if not self._combo_order:
            return
        current_id = self.current_combo.id if self.current_combo else None
        self.cb_combo_selector.blockSignals(True)
        self.cb_combo_selector.clear()
        for cid in self._combo_order:
            combo = self.combos.get(cid)
            if combo is None or not self._combo_matches_filter(combo):
                continue
            self.cb_combo_selector.addItem(self._combo_selector_label(combo), cid)
        self.cb_combo_selector.blockSignals(False)
        if self.cb_combo_selector.count() == 0:
            self.current_combo = None
            self._refresh_combo_models_list()
            self._refresh_available_list()
            return
        idx = self.cb_combo_selector.findData(current_id)
        if idx < 0:
            idx = 0
        self.cb_combo_selector.setCurrentIndex(idx)
        self._select_combo_by_index(idx)

    def _combo_selector_label(self, combo: StableCombo) -> str:
        counts = combo.activation_counts()
        if counts["disabled"]:
            return (f"{combo.name} ({counts['configured']} models, "
                    f"{counts['enabled']} enabled)")
        return f"{combo.name} ({counts['configured']} models)"

    # ------------------------------------------------------------- context menus
    def _on_combo_list_context_menu(self, pos):
        if not self.current_combo:
            return
        item = self.list_combo_models.itemAt(pos)
        if item is None:
            return
        from PySide6.QtWidgets import QMenu

        cid = item.data(Qt.UserRole)
        row = item.data(Qt.UserRole + 1)
        is_on = self.current_combo.is_model_enabled(cid)
        menu = QMenu(self)
        if is_on:
            menu.addAction("Disable in this combo", lambda: self._menu_set_enabled([cid], False))
        else:
            menu.addAction("Enable in this combo", lambda: self._menu_set_enabled([cid], True))
        if row is not None:
            menu.addSeparator()
            menu.addAction(
                f"Enable all {row.provider_label} models in this combo",
                lambda: self._set_activation_for_provider(row.provider_key, True),
            )
            menu.addAction(
                f"Disable all {row.provider_label} models in this combo",
                lambda: self._set_activation_for_provider(row.provider_key, False),
            )
        menu.addSeparator()
        act_on_sel = menu.addAction("Enable selected", lambda: self._menu_set_enabled(None, True))
        act_off_sel = menu.addAction("Disable selected", lambda: self._menu_set_enabled(None, False))
        selected = [it.data(Qt.UserRole) for it in self.list_combo_models.selectedItems()]
        act_on_sel.setEnabled(bool(selected))
        act_off_sel.setEnabled(bool(selected))
        menu.addSeparator()
        menu.addAction("Move up", self._move_up)
        menu.addAction("Move down", self._move_down)
        menu.addAction("Show model details", lambda: self._show_model_details(cid))
        menu.addAction("Copy canonical id", lambda: self._copy_cids([cid]))
        menu.addAction("Retest", lambda: self.retest_combo_requested.emit([cid]))
        menu.addSeparator()
        # Remove stays a separate, destructive action -- never merged with OFF.
        menu.addAction("Remove from combo...", self._remove_selected)
        menu.exec(self.list_combo_models.mapToGlobal(pos))

    def _menu_set_enabled(self, cids: Optional[List[str]], enabled: bool):
        if not self.current_combo:
            return
        if cids is None:
            cids = [it.data(Qt.UserRole) for it in self.list_combo_models.selectedItems()]
        cids = [c for c in cids if c]
        if not cids:
            return
        changed = self.current_combo.set_models_enabled(cids, enabled)
        if changed:
            self._guard_last_routable(changed, enabled)
            self._after_activation_change()

    def _show_model_details(self, canonical_id: str):
        combo = self.current_combo
        enabled = combo.is_model_enabled(canonical_id) if combo else True
        blocked, reason = self._blocking_reason(
            canonical_id, self._available_model_by_cid.get(canonical_id)
        )
        record = self.cache.get(canonical_id)
        lines = [
            f"Canonical id: {canonical_id}",
            f"Manual (this combo): {'ON' if enabled else 'OFF'}",
            f"Position in combo: #{combo.models.index(canonical_id) + 1}" if combo and canonical_id in combo.models else "Position in combo: not a member",
            f"Effective: {'BLOCKED' if blocked else 'routable'}",
        ]
        if blocked:
            lines.append(f"Reason: {reason}")
        if record is not None:
            lines.append(f"Health: {getattr(record, 'availability', '') or 'unknown'}")
            lines.append(f"Last tested: {getattr(record, 'last_tested_at', '') or 'never'}")
        QMessageBox.information(self, "Model Details", "\n".join(lines))

    def _on_available_list_context_menu(self, pos):
        item = self.list_available_models.itemAt(pos)
        if item is None:
            return
        from PySide6.QtWidgets import QMenu

        cid = item.data(Qt.UserRole)
        menu = QMenu(self)
        menu.addAction("Add to combo", lambda: self.add_model_to_current(cid))
        menu.addAction("Show model details", lambda: self._show_model_details(cid))
        menu.addAction("Copy canonical id", lambda: self._copy_cids([cid]))
        menu.addAction("Retest", lambda: self.retest_combo_requested.emit([cid]))
        menu.exec(self.list_available_models.mapToGlobal(pos))

    def _focus_combo_model_by_id(self, canonical_id: str):
        self._focus_row_in_list(
            self.list_combo_models, canonical_id, self._combo_item_by_cid,
            self._combo_row_by_cid,
        )

    # ----------------------------------------------------------- RIGHT panel
    def _on_available_query_changed(self):
        self._available_explorer.set_query(self.available_controls.query())
        self._apply_available_visibility()

    def _refresh_available_list(self):
        """Rebuild the right list in the current presentation order.

        Rows are every model that can still be added; the query only decides
        which of them are visible, so a filter can never hide a model from the
        catalogue and then have it vanish outright.
        """
        self._available_explorer.set_rows(self._available_rows())
        self.list_available_models.clear()
        self._available_item_by_cid.clear()
        self._available_row_by_cid.clear()

        # Presentation order (sort + optional provider grouping), not membership.
        for row in self._available_explorer.ordered_all():
            item = QListWidgetItem(self._row_text(row, None))
            item.setData(Qt.UserRole, row.canonical_id)
            item.setData(Qt.UserRole + 1, row)
            item.setToolTip(self._row_tooltip(row, None))
            item.setForeground(self._row_foreground(row))
            if row.canonical_id in self._routing_blocked_model_ids:
                item.setForeground(QColor(COLOR_DANGER))
            self.list_available_models.addItem(item)
            self._available_item_by_cid[row.canonical_id] = item
            self._available_row_by_cid[row.canonical_id] = self.list_available_models.count() - 1

        self._apply_available_visibility()
        self._refresh_provider_catalog()

    def _apply_available_visibility(self):
        """Hide the right rows the current query excludes. Nothing is removed."""
        query = self._available_explorer.query
        visible = {row.canonical_id for row in self._available_explorer.visible}
        total = self._available_explorer.total
        for cid, item in self._available_item_by_cid.items():
            self.list_available_models.setRowHidden(
                self._available_row_by_cid[cid], cid not in visible
            )
        self.available_controls.set_status(
            len(visible), total, summarize(query, len(visible), total),
            empty_catalog=not total,
        )

    def _copy_available_selected_ids(self):
        cids = [it.data(Qt.UserRole) for it in self.list_available_models.selectedItems()]
        self._copy_cids(cids)

    def _copy_cids(self, cids: List[str]):
        cids = [c for c in cids if c]
        if not cids:
            return
        from PySide6.QtWidgets import QApplication

        QApplication.clipboard().setText("\n".join(cids))

    def _focus_available_model_by_id(self, canonical_id: str):
        self._focus_row_in_list(
            self.list_available_models, canonical_id, self._available_item_by_cid,
            self._available_row_by_cid,
        )

    def _focus_row_in_list(self, list_widget: QListWidget, canonical_id: str,
                           items_by_cid: Dict[str, QListWidgetItem],
                           rows_by_cid: Dict[str, int]):
        """Select and scroll to an exact canonical id without touching the combo."""
        item = items_by_cid.get(canonical_id)
        if item is None:
            return
        list_widget.setCurrentItem(item)
        list_widget.scrollToItem(item)

    def _visible_selected_cids(self, list_widget: QListWidget) -> List[str]:
        """Canonical ids of selected rows that are not hidden by a filter."""
        return [
            it.data(Qt.UserRole) for it in list_widget.selectedItems()
            if not list_widget.isRowHidden(list_widget.row(it))
        ]

    # ----------------------------------------------------- health badge update
    def update_model_health_badge(self, canonical_id: str):
        """Targeted in-place update of items in both lists without full rebuilds.

        PERF-003: both list indices are maintained at rebuild time, so this is
        O(1) in the number of models instead of two full QList scans per probe
        completion (the audited ~674k item inspections on the scale fixture)."""
        rec = self.cache.get(canonical_id)
        cfg = STATE_COLORS.get(rec.state) if rec else None
        fg_color = QColor(cfg["fg"]) if cfg else QColor(COLOR_TEXT_MUTED)

        # 1. Update in combo models list via keyed index.
        combo_item = self._combo_item_by_cid.get(canonical_id)
        if combo_item is not None:
            row = self._combo_row_by_cid.get(canonical_id, 0)
            combo_item.setData(
                Qt.UserRole + 1,
                build_row(_ComboModelRef(canonical_id), rec, row, within_combo=True),
            )
            model_row = combo_item.data(Qt.UserRole + 1)
            combo_item.setText(self._row_text(model_row, row + 1))
            combo_item.setToolTip(self._row_tooltip(model_row, row + 1))
            combo_item.setForeground(fg_color)

        # 2. Update in available models picker list via keyed index.
        avail_item = self._available_item_by_cid.get(canonical_id)
        if avail_item is not None:
            row_index = self._available_row_by_cid.get(canonical_id)
            model_row = build_row(
                self._model_for_cid(canonical_id), rec, row_index or 0
            )
            avail_item.setData(Qt.UserRole + 1, model_row)
            avail_item.setText(self._row_text(model_row, None))
            avail_item.setToolTip(self._row_tooltip(model_row, None))
            avail_item.setForeground(fg_color)
            if row_index is not None:
                # A badge change must respect the active filter immediately, so
                # a model that just died disappears from a "Usable" view.
                visible = canonical_id in {
                    r.canonical_id for r in self._available_explorer.visible
                }
                self.list_available_models.setRowHidden(row_index, not visible)

    def _model_for_cid(self, canonical_id: str) -> "_ComboModelRef":
        """Discovery metadata for a picker row, for the O(1) badge refresh."""
        model = self._available_model_by_cid.get(canonical_id)
        return model if model is not None else _ComboModelRef(canonical_id)

    def _select_combo_by_index(self, idx: int):
        cid = self.cb_combo_selector.itemData(idx)
        if cid and cid in self.combos:
            self.current_combo = self.combos[cid]
            self._refresh_combo_models_list()
            # Membership decides what the right panel offers, so switching
            # combos rebuilds it (a stale right list offered removed models).
            self._refresh_available_list()

    def _on_combo_selection_changed(self, idx: int):
        self._select_combo_by_index(idx)


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

    def _move_to_top(self):
        if not self.current_combo:
            return
        selected = self.list_combo_models.selectedItems()
        if not selected:
            return
        cid = selected[0].data(Qt.UserRole)
        if self.current_combo.move_to_top(cid):
            self._refresh_combo_models_list()
            self._select_in_combo_list(cid)

    def _move_to_bottom(self):
        if not self.current_combo:
            return
        selected = self.list_combo_models.selectedItems()
        if not selected:
            return
        cid = selected[0].data(Qt.UserRole)
        if self.current_combo.move_to_bottom(cid):
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

    def _add_selected_visible(self):
        """Add only the picker rows the current filter leaves visible.

        A row hidden by a filter is never pulled in by accident, so a broad
        selection under a narrow filter adds exactly what the operator sees.
        """
        if not self.current_combo:
            return
        targets = self._visible_selected_cids(self.list_available_models)
        if not targets:
            return
        for cid in targets:
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

        # A combo with nothing enabled cannot be routed by the engine at all.
        # Refuse rather than publish an empty list and call it success.
        if self.current_combo.has_no_routable_models() and self.current_combo.models:
            QMessageBox.warning(
                self, "No Enabled Model",
                f"\"{self.current_combo.name}\" has every model switched OFF.\n\n"
                "9Router cannot route an empty combo, so this state is not "
                "published. Switch at least one model ON (the members and their "
                "positions are untouched) and save again.",
            )
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
                baseline=self.current_combo.effective_baseline(),
                server=server_models,
                local=self.current_combo.effective_models(),
                parent=self,
                field_changes=self.current_combo.describe_server_divergence(
                    server_name, server_kind, server_models, server_updated
                ),
            )
            if dlg.exec() != QDialog.Accepted:
                return
            if dlg.action_choice == ConflictDialog.KEEP_SERVER:
                # The engine becomes the configured truth: its list is adopted
                # whole, so no disabled member survives that the engine dropped.
                self.current_combo.name = server_name
                self.current_combo.kind = server_kind
                self.current_combo.models = list(server_models)
                self.current_combo.disabled = set()
                self.current_combo.mark_clean(server_updated)
                self._drifted_combo_ids.discard(self.current_combo.id)
                self._sync_activation_record(self.current_combo, server_models)
                self._refresh_combo_models_list()
                self._refresh_available_list()
                return
            # If OVERWRITE: continue saving local changes

        # 2. DIFF against server state. What the engine is asked to store is the
        # effective projection, so that is what the diff must describe.
        publish_models = self.current_combo.effective_models()
        diff = compute_combo_diff(server_models, publish_models)
        if not diff.is_empty():
            diff_dlg = DiffConfirmDialog(diff, self)
            if diff_dlg.exec() != QDialog.Accepted:
                return

        # 3. APPLY via 9Router API (Fail-Closed)
        self._set_apply_state("Applying…", "info")
        try:
            res = self.client.update_combo(
                combo_id=self.current_combo.id,
                name=self.current_combo.name,
                models=publish_models,
                kind=self.current_combo.kind,
            )
        except ComboWriteError as exc:
            self._set_apply_state("Failed: not applied", "error")
            QMessageBox.critical(self, "Apply Failed", exc.message_for_ui())
            return

        if res is None or res is False:
            self._set_apply_state("Failed: not applied", "error")
            QMessageBox.critical(self, "Apply Failed", "9Router did not confirm the write. Local edits are preserved.")
            return

        # 4. VERIFY: Read-back confirmation directly from engine.
        # CORE-004: verify the COMPLETE mutable state (name, kind, ordered
        # models) before mark_clean. Models-only verification turned a partial
        # backend write/normalization into false success and recorded unverified
        # local name/kind as the clean baseline.
        verified_combos = self.client.get_combos()
        verified_match = next((c for c in verified_combos if c.get("id") == self.current_combo.id), None)
        if verified_match is None:
            self._set_apply_state("Failed: verification read-back", "error")
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
            self._drifted_combo_ids.discard(self.current_combo.id)
            self._sync_activation_record(self.current_combo, verified_match.get("models", []))
            self._set_apply_state("Verified", "ok")
            QMessageBox.information(
                self, "Verified & Saved",
                f"Combo '{self.current_combo.name}' "
                f"({len(publish_models)} routable model"
                f"{'s' if len(publish_models) != 1 else ''}) successfully applied "
                "and verified in 9Router."
            )
            self.combo_saved.emit(self.current_combo.name)
        else:
            # Do NOT mark_clean: the mismatching fields stay dirty so the
            # editor baseline never adopts unverified local state.
            self._set_apply_state("Failed: verification mismatch", "error")
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
        try:
            res = self.client.rename_combo(
                combo_id=cid,
                new_name=target_name,
                current_models=list(server_models),
                kind=server_kind,
            )
        except ComboWriteError as exc:
            QMessageBox.critical(self, "Rename Failed", exc.message_for_ui())
            return

        if res is None or res is False:
            QMessageBox.critical(self, "Rename Failed", "9Router did not confirm the rename. Local edits are preserved.")
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

    def _show_upstream_diagnostics(self):
        """Upstream attribution + predicted failover chain (T-110).

        Read-only. It attributes the observed engine to a base URL, a version
        and an owning process, and predicts -- from the published member list
        and the local health history -- what the upstream failover chain will
        do. It never writes and never reports a request outcome.
        """
        combo = self.current_combo
        if combo is None:
            QMessageBox.information(self, "No Combo", "Select a combo first.")
            return
        attribution = attribute_upstream(self.client)
        report = self._failover_report()
        dlg = _UpstreamDiagnosticsDialog(attribution, report, self)
        dlg.exec()

    def _sync_activation_record(self, combo: StableCombo, verified_models: List[str]):
        """Persist the verified activation record for one combo.

        Called only after a read-back confirms the engine holds the published
        projection, so the durable record can never claim a state the engine
        did not accept. A write failure is reported and leaves the in-memory
        draft intact.
        """
        record = self.activation_store.ensure(combo.id, combo.name, verified_models)
        record.configured = list(combo.models)
        record.disabled = set(combo.disabled)
        record.combo_name = combo.name
        record.engine_baseline = list(verified_models)
        record.engine_updated_at = combo.baseline_updated_at
        self._persist_activation_store()

    def _set_apply_state(self, text: str, tone: str = "info"):
        """Show an explicit apply state: Applying / Verified / Failed.

        The state is never inferred from a checkbox: it reports what the
        engine read-back actually confirmed.
        """
        color = {
            "info": COLOR_TEXT_SECONDARY,
            "ok": COLOR_ACCENT_TEAL,
            "error": COLOR_DANGER_TEXT,
        }.get(tone, COLOR_TEXT_SECONDARY)
        self.lbl_dirty_state.setText(text)
        self.lbl_dirty_state.setStyleSheet(f"color: {color}; font-weight: bold;")

    def _update_dirty_state(self):
        """Update dirty state label and save button."""
        if self.current_combo and self.current_combo.id in self._missing_dirty_ids:
            self.lbl_dirty_state.setText("Unsaved changes (missing on server)")
            self.lbl_dirty_state.setStyleSheet(f"color: {COLOR_DANGER_TEXT}; font-weight: bold;")
            self.btn_save.setEnabled(True)
        elif self.current_combo and self.current_combo.id in self._drifted_combo_ids:
            self.lbl_dirty_state.setText("Conflict: engine list differs")
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
            except ComboWriteError as ex:
                QMessageBox.critical(self, "Create Failed", ex.message_for_ui())
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
        source = self.current_combo
        try:
            new_name = f"{source.name}_copy"
            # The duplicate carries the FULL configured sequence and starts with
            # every member enabled: activation is routing policy, and a copy
            # silently inheriting another combo's suspensions would surprise.
            res = self.client.create_combo(name=new_name, models=list(source.models))
        except LiveAccessLockedError as ex:
            QMessageBox.warning(self, "Secrets Locked", str(ex))
            return
        except ComboWriteError as ex:
            QMessageBox.critical(self, "Duplicate Failed", ex.message_for_ui())
            return
        if res:
            new_id = res.get("id") if isinstance(res, dict) else None
            if new_id:
                self.activation_store.records[new_id] = ComboActivation(
                    combo_id=new_id,
                    combo_name=new_name,
                    configured=list(source.models),
                    disabled=set(),
                    engine_baseline=list(source.models),
                )
                self._persist_activation_store()
            self.load_combos_from_9router()

    def _delete_combo(self):
        if not self.current_combo:
            return
        name = self.current_combo.name
        deleted_id = self.current_combo.id
        reply = QMessageBox.question(
            self, "Delete Combo",
            f'Delete combo "{name}" from 9Router?',
            QMessageBox.Yes | QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return
        try:
            deleted = self.client.delete_combo(deleted_id)
        except LiveAccessLockedError as ex:
            QMessageBox.warning(self, "Secrets Locked", str(ex))
            return
        if deleted:
            # Drop the activation record with the combo, so no orphaned local
            # state can later apply to a different combo that reuses the name.
            self.activation_store.forget(deleted_id)
            self._drifted_combo_ids.discard(deleted_id)
            self._missing_dirty_ids.discard(deleted_id)
            self._persist_activation_store()
            self.current_combo = None
            self.load_combos_from_9router()


class _ComboManageDialog(QDialog):
    """Combo management surface: rare/destructive operations live here,
    away from the permanent editing strip (T-33).

    It also carries the combo-level activation actions the addendum asks
    for, with the engine's real capability stated: there is no combo ON/OFF,
    so that control is shown DISABLED with its measured reason instead of
    pretending to work.
    """

    def __init__(self, combo: StableCombo, parent=None):
        super().__init__(parent)
        self.combo = combo
        self.view = parent if isinstance(parent, ComboEditorView) else None
        self.delete_requested = False
        self.setWindowTitle(f"Manage Combo: {combo.name}")
        self.setMinimumWidth(420)

        layout = QVBoxLayout(self)
        layout.setSpacing(8)

        counts = combo.activation_counts()
        layout.addWidget(QLabel(f'Combo: "{combo.name}" ({len(combo.models)} models)'))
        layout.addWidget(QLabel(
            f"{counts['configured']} configured | {counts['enabled']} enabled | "
            f"{counts['routable']} routable | {counts['disabled']} disabled"
        ))

        # Inspect: every member with its manual state, in routing order.
        self.inspect_text = QTextEdit()
        self.inspect_text.setReadOnly(True)
        self.inspect_text.setMaximumHeight(160)
        self.inspect_text.setStyleSheet("font-family: monospace; font-size: 10px;")
        self._fill_inspect()
        layout.addWidget(QLabel("Members (routing order):"))
        layout.addWidget(self.inspect_text)

        act_row = QHBoxLayout()
        act_row.setSpacing(4)

        self.btn_reset = QPushButton("Reset activation changes")
        self.btn_reset.setToolTip(
            "Switch every member of this combo back ON. Membership and routing "
            "positions are untouched."
        )
        self.btn_reset.setEnabled(bool(combo.disabled))
        self.btn_reset.clicked.connect(self._on_reset_clicked)
        act_row.addWidget(self.btn_reset)

        self.btn_show_disabled = QPushButton("Show disabled models")
        self.btn_show_disabled.setEnabled(bool(combo.disabled))
        self.btn_show_disabled.clicked.connect(self._on_show_disabled)
        act_row.addWidget(self.btn_show_disabled)

        # Combo ON/OFF: the engine has no such state. Disabled control, real
        # reason -- never a switch that cannot take effect.
        self.btn_combo_off = QPushButton("Combo OFF")
        self.btn_combo_off.setEnabled(False)
        self.btn_combo_off.setToolTip(COMBO_SUSPEND_UNSUPPORTED)
        act_row.addWidget(self.btn_combo_off)

        self.btn_why = QPushButton("Why?")
        self.btn_why.setFixedWidth(34)
        self.btn_why.setToolTip("Why combo ON/OFF is unavailable.")
        self.btn_why.clicked.connect(self._on_why_clicked)
        act_row.addWidget(self.btn_why)

        layout.addLayout(act_row)

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

    def _fill_inspect(self):
        lines = []
        for index, cid in enumerate(self.combo.models, start=1):
            state = "OFF " if not self.combo.is_model_enabled(cid) else "ON  "
            lines.append(f"#{index:02d} [{state}] {cid}")
        self.inspect_text.setPlainText("\n".join(lines) or "(no members)")

    def _on_reset_clicked(self):
        if self.view is None:
            return
        restored = self.view._reset_activation(notify=False)
        self._fill_inspect()
        self.btn_reset.setEnabled(bool(self.combo.disabled))
        self.btn_show_disabled.setEnabled(bool(self.combo.disabled))

    def _on_show_disabled(self):
        disabled = self.combo.disabled_models()
        QMessageBox.information(
            self, "Disabled Models",
            "\n".join(disabled) if disabled else "No model is switched OFF.",
        )

    def _on_why_clicked(self):
        QMessageBox.information(self, "Combo ON/OFF Is Not Available", COMBO_SUSPEND_UNSUPPORTED)

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


class _UpstreamDiagnosticsDialog(QDialog):
    """Upstream attribution + predicted combo failover chain (T-110).

    Read-only diagnostic. It answers two questions with evidence:

    1. WHICH upstream answers -- the loopback base URL, whether it answered,
       the version it reports and which local process owns the port;
    2. WHAT the upstream failover chain will do with the published member
       list -- per member the expectation derived from the local health
       history, plus the upstream rules the expectation is attributed to.

    The expectations are PREDICTIONS from the health cache. The upstream
    still tries every member in its own order; the dialog never claims that a
    request was served, failed or cancelled.
    """

    def __init__(self, attribution: Dict, report: Dict, parent=None):
        super().__init__(parent)
        self.attribution = dict(attribution or {})
        self.report = dict(report or {})
        self.setWindowTitle("Upstream & Failover Diagnostics")
        self.setMinimumSize(720, 520)
        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(6)

        header = QLabel(owner_summary(self.attribution))
        header.setStyleSheet("font-weight: bold;")
        header.setWordWrap(True)
        layout.addWidget(header)

        notes = self.attribution.get("notes") or []
        if notes:
            note_lbl = QLabel("Notes: " + "; ".join(str(n) for n in notes))
            note_lbl.setStyleSheet(f"color: {COLOR_TEXT_SECONDARY};")
            note_lbl.setWordWrap(True)
            layout.addWidget(note_lbl)

        layout.addWidget(QLabel("Published member list (the engine's routing order):"))
        self.members_text = QTextEdit()
        self.members_text.setReadOnly(True)
        self.members_text.setStyleSheet("font-family: monospace; font-size: 10px;")
        layout.addWidget(self.members_text, 1)

        rules_lbl = QLabel(
            "Upstream rules this prediction is attributed to:\n"
            + "\n".join(f"- {rule}" for rule in self.report.get("upstream_rules") or [])
        )
        rules_lbl.setStyleSheet(f"color: {COLOR_TEXT_SECONDARY};")
        rules_lbl.setWordWrap(True)
        layout.addWidget(rules_lbl)

        attribution_lbl = QLabel(
            "This report predicts upstream behaviour from the published member "
            "list and the local health history. It is not a request log."
        )
        attribution_lbl.setStyleSheet(f"color: {COLOR_TEXT_MUTED};")
        attribution_lbl.setWordWrap(True)
        layout.addWidget(attribution_lbl)

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)

        self._fill_members()

    def _fill_members(self):
        """Render the per-member expectations."""
        report = self.report
        lines = [
            f"Combo: {report.get('combo_name') or '(unnamed)'}",
            f"Published members: {report.get('member_count', 0)} | "
            f"predicted routable: {report.get('routable_count', 0)}",
            f"Serving (predicted): {report.get('serving') or 'none proven routable'}",
            f"Failover: {report.get('failover_from') or 'none'} -> "
            f"{report.get('failover_to') or 'no member left'}",
            "",
            f"{'#':>2}  {'member':<40} {'availability':<16} expectation",
        ]
        for row in report.get("members") or []:
            status = row.get("status_code") or 0
            suffix = f" (http {status})" if status else ""
            lines.append(
                f"{row.get('position', 0):>2}  "
                f"{str(row.get('canonical_id'))[:40]:<40} "
                f"{str(row.get('availability'))[:16]:<16} "
                f"{row.get('expectation_text', '')}{suffix}"
            )
            reason = str(row.get("reason") or "")
            if reason:
                lines.append(f"      reason: {reason}")
        disabled = report.get("disabled") or []
        if disabled:
            lines.append("")
            lines.append("Configured but disabled (not published to the engine):")
            lines.extend(f"  - {cid}" for cid in disabled)
        untested = report.get("untested") or []
        if untested:
            lines.append("")
            lines.append(
                f"Untested members ({len(untested)}): the upstream may still "
                "serve them; there is no local evidence either way."
            )
        self.members_text.setPlainText("\n".join(lines))
