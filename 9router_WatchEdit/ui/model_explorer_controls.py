"""Reusable search / filter controls for the Combo model explorer.

Two of these are instantiated -- one for the current-combo panel, one for the
available-models panel -- and each owns its own independent state, so the two
search areas never share a query.

Every control here writes to a plain ``ExplorerQuery``; none of them touches a
combo, a widget row or the network. The presentation model
(``core.model_explorer``) does the actual matching, so a filter cannot behave
differently depending on which callback triggered it.
"""
from __future__ import annotations

from typing import Dict, Iterable, Optional, Sequence, Set

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QWidget,
    QHBoxLayout,
    QVBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QToolButton,
    QComboBox,
    QCheckBox,
    QListWidget,
    QListWidgetItem,
    QFrame,
)

from core.model_explorer import (
    COST_ALL,
    COST_FREE,
    COST_PAID,
    COST_UNKNOWN,
    FILTER_SCOPE_COMBINED,
    FILTER_SCOPE_MODELS,
    FILTER_SCOPE_OPTIONS,
    FILTER_SCOPE_PROVIDERS,
    FILTER_SCOPES,
    HEALTH_ALL,
    HEALTH_ATTENTION,
    HEALTH_DEAD,
    HEALTH_UNTESTED,
    HEALTH_USABLE,
    TESTED_ALL,
    TESTED_NEVER,
    TESTED_TESTED,
    ExplorerQuery,
    ProviderEntry,
)
from core.activation import (
    ACTIVATION_ALL,
    ACTIVATION_BLOCKED,
    ACTIVATION_DISABLED,
    ACTIVATION_ENABLED,
    ACTIVATION_PENDING,
    ACTIVATION_ROUTABLE,
)

HEALTH_LABELS = (
    (HEALTH_ALL, "All"),
    (HEALTH_USABLE, "Usable"),
    (HEALTH_UNTESTED, "Untested"),
    (HEALTH_ATTENTION, "Attention"),
    (HEALTH_DEAD, "Dead"),
)
#: Activation buckets. "Enabled" is the operator's manual ON (configured and
#: not suspended); "Routable" additionally excludes members the engine will not
#: select, so the two are never conflated.
ACTIVATION_LABELS = (
    (ACTIVATION_ALL, "All"),
    (ACTIVATION_ENABLED, "Enabled"),
    (ACTIVATION_DISABLED, "Disabled"),
    (ACTIVATION_ROUTABLE, "Effectively routable"),
    (ACTIVATION_BLOCKED, "Blocked"),
    (ACTIVATION_PENDING, "Pending changes"),
)
COST_LABELS = (
    (COST_ALL, "All"),
    (COST_FREE, "Free"),
    (COST_PAID, "Paid"),
    (COST_UNKNOWN, "Unknown"),
)
TESTED_LABELS = (
    (TESTED_ALL, "Any"),
    (TESTED_TESTED, "Tested"),
    (TESTED_NEVER, "Never tested"),
)

#: Below this width the panel drops advanced controls into a compact set.
COMPACT_WIDTH = 640


class _ClearableLineEdit(QFrame):
    """A search field with an inline clear button and Escape-to-clear."""

    def __init__(self, placeholder: str, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("clearableEdit")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(3, 0, 2, 0)
        layout.setSpacing(1)

        self.edit = QLineEdit()
        self.edit.setPlaceholderText(placeholder)
        self.edit.setFrame(False)
        layout.addWidget(self.edit, stretch=1)

        self.btn_clear = QPushButton("×")
        self.btn_clear.setObjectName("clearEditButton")
        self.btn_clear.setFixedWidth(18)
        self.btn_clear.setToolTip("Clear search (Esc)")
        self.btn_clear.clicked.connect(self.edit.clear)
        layout.addWidget(self.btn_clear)

        self._sync()
        self.edit.textChanged.connect(self._sync)
        self.edit.installEventFilter(self)

    def _sync(self):
        self.btn_clear.setVisible(bool(self.edit.text()))

    def text(self) -> str:
        return self.edit.text()

    def setText(self, value: str) -> None:
        self.edit.setText(value)

    def clear(self) -> None:
        self.edit.clear()

    def take_focus(self) -> None:
        self.edit.setFocus(Qt.ShortcutFocusReason)
        self.edit.selectAll()

    def eventFilter(self, obj, event):
        from PySide6.QtCore import QEvent

        if obj is self.edit and event.type() == QEvent.KeyPress:
            if event.key() == Qt.Key_Escape:
                self.edit.clear()
                return True
        return super().eventFilter(obj, event)


class _ProviderFilterBox(QPushButton):
    """Provider multi-select with search, counts and stable identity.

    The button shows a compact active summary ("All providers" / "2 providers").
    The popup is a non-modal, checkable list so a provider can be toggled
    without leaving the panel; the query updates live.
    """

    changed = Signal()

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("providerFilter")
        self.setToolTip("Filter by provider. Toggle one or several.")
        self._entries: Dict[str, ProviderEntry] = {}
        self._selected: Set[str] = set()
        self._popup: Optional[QWidget] = None
        self._search: Optional[QLineEdit] = None
        self._list: Optional[QListWidget] = None
        self._sync_label()

    # ------------------------------------------------------------------ state
    def selected(self) -> Set[str]:
        return set(self._selected)

    def set_selected(self, keys: Iterable[str]) -> None:
        wanted = {str(k) for k in keys}
        if wanted == self._selected:
            return
        self._selected = wanted
        self._sync_label()
        self.changed.emit()

    def clear_selection(self) -> None:
        self.set_selected(())

    def set_entries(self, entries: Sequence[ProviderEntry]) -> None:
        """Refresh the provider catalog, keeping the user's selections.

        A provider that vanishes keeps its selection so the user's intention
        stays visible; it is shown as unavailable with a count of 0 rather than
        being silently replaced by another provider.
        """
        merged: Dict[str, ProviderEntry] = {}
        for entry in entries:
            merged[entry.key] = entry
        for key in self._selected:
            if key not in merged:
                previous = self._entries.get(key)
                merged[key] = ProviderEntry(
                    key=key,
                    label=(previous.label if previous else key),
                    count=0,
                    available=False,
                )
        self._entries = merged
        if self._popup is not None:
            self._rebuild_popup_list()
        self._sync_label()

    def summary(self) -> str:
        if not self._selected:
            return "All providers"
        count = len(self._selected)
        if count <= 2:
            labels = sorted(
                self._entries[key].label for key in self._selected if key in self._entries
            )
            if labels:
                return " · ".join(labels)
        return f"{count} providers"

    # ------------------------------------------------------------------ popup
    def mousePressEvent(self, event):
        super().mousePressEvent(event)
        self._open_popup()

    def _open_popup(self):
        popup = self._ensure_popup()
        self._rebuild_popup_list()
        if self._search is not None:
            self._search.setFocus(Qt.ShortcutFocusReason)
        popup.move(self.mapToGlobal(self.rect().bottomLeft()))
        popup.show()

    def _ensure_popup(self) -> QWidget:
        if self._popup is not None:
            return self._popup
        popup = QWidget(None, Qt.Popup)
        popup.setObjectName("providerFilterPopup")
        layout = QVBoxLayout(popup)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(3)

        search = QLineEdit()
        search.setPlaceholderText("Search providers...")
        search.setClearButtonEnabled(True)
        search.textChanged.connect(self._filter_popup_list)
        layout.addWidget(search)
        self._search = search

        provider_list = QListWidget()
        provider_list.setMinimumWidth(260)
        provider_list.setMaximumHeight(280)
        provider_list.itemChanged.connect(self._on_item_changed)
        layout.addWidget(provider_list, stretch=1)
        self._list = provider_list

        buttons = QHBoxLayout()
        buttons.setSpacing(3)
        btn_all = QPushButton("All")
        btn_all.clicked.connect(self._select_all_visible)
        btn_none = QPushButton("None")
        btn_none.clicked.connect(self._select_none)
        buttons.addWidget(btn_all)
        buttons.addWidget(btn_none)
        buttons.addStretch()
        btn_done = QPushButton("Done")
        btn_done.setObjectName("primaryAction")
        btn_done.clicked.connect(self._close_popup)
        buttons.addWidget(btn_done)
        layout.addLayout(buttons)

        self._popup = popup
        return popup

    def showEvent(self, event):
        super().showEvent(event)

    def _rebuild_popup_list(self):
        if self._list is None:
            return
        query = (self._search.text() if self._search else "").strip().lower()
        self._list.blockSignals(True)
        self._list.clear()
        for key in sorted(self._entries, key=lambda k: (
            self._entries[k].available,
            self._entries[k].label.casefold(),
        )):
            entry = self._entries[key]
            if query and query not in entry.label.lower() and query not in key.lower():
                continue
            label = entry.label if entry.available else f"{entry.label} (unavailable)"
            item = QListWidgetItem(f"{label}  ({entry.count})")
            item.setData(Qt.UserRole, key)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked if key in self._selected else Qt.Unchecked)
            if not entry.available:
                item.setForeground(Qt.gray)
            self._list.addItem(item)
        self._list.blockSignals(False)

    def _filter_popup_list(self):
        self._rebuild_popup_list()

    def _on_item_changed(self, item: QListWidgetItem):
        key = item.data(Qt.UserRole)
        if not key:
            return
        if item.checkState() == Qt.Checked:
            self._selected.add(key)
        else:
            self._selected.discard(key)
        self._sync_label()
        self.changed.emit()

    def _select_all_visible(self):
        if self._list is None:
            return
        self._list.blockSignals(True)
        for row in range(self._list.count()):
            item = self._list.item(row)
            key = item.data(Qt.UserRole)
            if key and not item.isHidden():
                item.setCheckState(Qt.Checked)
                self._selected.add(key)
        self._list.blockSignals(False)
        self._sync_label()
        self.changed.emit()

    def _select_none(self):
        if self._list is None:
            return
        self._list.blockSignals(True)
        for row in range(self._list.count()):
            item = self._list.item(row)
            if item.checkState() == Qt.Checked:
                item.setCheckState(Qt.Unchecked)
        self._list.blockSignals(False)
        self._selected.clear()
        self._sync_label()
        self.changed.emit()

    def _close_popup(self):
        if self._popup is not None:
            self._popup.close()

    def _sync_label(self):
        label = self.summary()
        self.setText(f"Provider: {label}")
        active = bool(self._selected)
        self.setProperty("active", active)
        self.style().unpolish(self)
        self.style().polish(self)


class _ExplorerPanelControls(QWidget):
    """The complete search + filter + sort surface for one panel."""

    query_changed = Signal()
    reset_requested = Signal()
    focus_by_id_requested = Signal(str)

    def __init__(self, title: str, *, allow_sort: bool, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._allow_sort = allow_sort
        self._compact = False
        self._build(title)

    # ------------------------------------------------------------------ build
    def _build(self, title: str):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(3)

        search_row = QHBoxLayout()
        search_row.setSpacing(4)
        self.search = _ClearableLineEdit(f"Search {title} (model, provider, id)...")
        self.search.edit.setToolTip(
            "Matches model id, full canonical id, display name, provider name and\n"
            "provider prefix. Several space-separated terms are ANDed.\n"
            "Shortcuts: provider:<name>, status:<usable|untested|attention|dead>,\n"
            "cost:<free|paid|unknown>. Plain text always works.\n"
            "Ctrl+F focuses this field, Esc clears it."
        )
        self.search.edit.textChanged.connect(self._on_query_changed)
        search_row.addWidget(self.search, stretch=1)
        root.addLayout(search_row)

        primary_row = QHBoxLayout()
        primary_row.setSpacing(4)

        primary_row.addWidget(QLabel("Filter by:"))
        self.cb_scope = QComboBox()
        self.cb_filter_mode = self.cb_scope
        self.cb_filter_scope = self.cb_scope
        for value, label in FILTER_SCOPE_OPTIONS:
            self.cb_scope.addItem(label, value)
        self.cb_scope.setToolTip(
            "Which identity dimension the search and the category filters apply to: "
            "Models only, Providers only, or both together (the default). "
            "An explicit provider:/status:/cost: shortcut still narrows its own "
            "category in every mode."
        )
        self.cb_scope.currentIndexChanged.connect(self._on_scope_changed)
        primary_row.addWidget(self.cb_scope)

        primary_row.addWidget(QLabel("Health:"))
        self.cb_health = QComboBox()
        for value, label in HEALTH_LABELS:
            self.cb_health.addItem(label, value)
        self.cb_health.setToolTip("Health bucket. Missing evidence is Untested, never Dead.")
        self.cb_health.currentIndexChanged.connect(self._on_query_changed)
        primary_row.addWidget(self.cb_health)

        primary_row.addWidget(QLabel("Activation:"))
        self.cb_activation = QComboBox()
        for value, label in ACTIVATION_LABELS:
            self.cb_activation.addItem(label, value)
        self.cb_activation.setToolTip(
            "Manual per-combo activation. 'Enabled' is your ON/OFF switch;\n"
            "'Effectively routable' additionally excludes models the router\n"
            "will not select, so the two are never the same count."
        )
        self.cb_activation.currentIndexChanged.connect(self._on_query_changed)
        primary_row.addWidget(self.cb_activation)

        primary_row.addWidget(QLabel("Cost:"))
        self.cb_cost = QComboBox()
        for value, label in COST_LABELS:
            self.cb_cost.addItem(label, value)
        self.cb_cost.setToolTip("Cost category. Models with no price information are Unknown.")
        self.cb_cost.currentIndexChanged.connect(self._on_query_changed)
        primary_row.addWidget(self.cb_cost)

        self.provider = _ProviderFilterBox()
        self.provider.changed.connect(self._on_query_changed)
        primary_row.addWidget(self.provider, stretch=1)

        self.btn_reset = QPushButton("Reset filters")
        self.btn_reset.setToolTip("Restore the default view for this panel.")
        self.btn_reset.clicked.connect(self._on_reset)
        primary_row.addWidget(self.btn_reset)

        root.addLayout(primary_row)

        # Collapsible advanced section.
        self.btn_advanced = QToolButton()
        self.btn_advanced.setText("Advanced filters ▸")
        self.btn_advanced.setCheckable(True)
        self.btn_advanced.setToolButtonStyle(Qt.ToolButtonTextOnly)
        self.btn_advanced.toggled.connect(self._toggle_advanced)
        advanced_header = QHBoxLayout()
        advanced_header.setSpacing(4)
        advanced_header.addWidget(self.btn_advanced)
        advanced_header.addStretch()
        self.lbl_counts = QLabel("Showing 0 of 0 models")
        advanced_header.addWidget(self.lbl_counts)
        root.addLayout(advanced_header)

        self.advanced_body = QWidget()
        self.advanced_body.setVisible(False)
        advanced_layout = QVBoxLayout(self.advanced_body)
        advanced_layout.setContentsMargins(0, 2, 0, 0)
        advanced_layout.setSpacing(3)

        extras_row = QHBoxLayout()
        extras_row.setSpacing(4)

        extras_row.addWidget(QLabel("Tested:"))
        self.cb_tested = QComboBox()
        for value, label in TESTED_LABELS:
            self.cb_tested.addItem(label, value)
        self.cb_tested.currentIndexChanged.connect(self._on_query_changed)
        extras_row.addWidget(self.cb_tested)

        for label, attribute, tip in (
            ("Routing eligible", "chk_routing", "Hide models that are not eligible for routing."),
            ("Low latency", "chk_latency", "Only models whose measured latency is low."),
            ("Recently tested", "chk_recent_tested", "Only models probed within the last 24h."),
            ("Recently successful", "chk_recent_success", "Only models whose last probe succeeded within 24h."),
        ):
            box = QCheckBox(label)
            box.setToolTip(tip)
            box.toggled.connect(self._on_query_changed)
            setattr(self, attribute, box)
            extras_row.addWidget(box)
        extras_row.addStretch()
        advanced_layout.addLayout(extras_row)

        if self._allow_sort:
            sort_row = QHBoxLayout()
            sort_row.setSpacing(4)
            sort_row.addWidget(QLabel("Sort:"))
            self.cb_sort = QComboBox()
            from core.model_explorer import SORT_OPTIONS

            for value, label in SORT_OPTIONS:
                self.cb_sort.addItem(label, value)
            self.cb_sort.setToolTip("Display order only. Routing priority is the combo order.")
            self.cb_sort.currentIndexChanged.connect(self._on_query_changed)
            sort_row.addWidget(self.cb_sort)
            self.chk_group = QCheckBox("Group by provider")
            self.chk_group.setToolTip("Lay the list out as one contiguous block per provider.")
            self.chk_group.toggled.connect(self._on_query_changed)
            sort_row.addWidget(self.chk_group)
            sort_row.addStretch()
            advanced_layout.addLayout(sort_row)

            goto_row = QHBoxLayout()
            goto_row.setSpacing(4)
            goto_row.addWidget(QLabel("Go to model id:"))
            self.txt_exact_id = QLineEdit()
            self.txt_exact_id.setPlaceholderText("exact canonical id, then Enter")
            self.txt_exact_id.setClearButtonEnabled(True)
            self.txt_exact_id.returnPressed.connect(self._emit_focus_by_id)
            goto_row.addWidget(self.txt_exact_id, stretch=1)
            self.btn_goto = QPushButton("Find")
            self.btn_goto.clicked.connect(self._emit_focus_by_id)
            goto_row.addWidget(self.btn_goto)
            advanced_layout.addLayout(goto_row)
        else:
            self.txt_exact_id = None

        self.lbl_summary = QLabel("")
        self.lbl_summary.setObjectName("filterSummary")
        self.lbl_summary.setWordWrap(True)
        advanced_layout.addWidget(self.lbl_summary)

        root.addWidget(self.advanced_body)
        self._sync_scope_controls()

    # ------------------------------------------------------------------ state
    def _toggle_advanced(self, expanded: bool):
        self.advanced_body.setVisible(expanded)
        self.btn_advanced.setText("Advanced filters ▾" if expanded else "Advanced filters ▸")
        self.query_changed.emit()

    def set_compact(self, compact: bool):
        """Compact filtering mode for narrow windows.

        Keeps search, health, cost and provider -- the controls needed to find
        anything -- and hides the rest until the panel is wide enough again.
        """
        if compact == self._compact:
            return
        self._compact = compact
        self.cb_tested.setVisible(not compact)
        for attribute in ("chk_routing", "chk_latency", "chk_recent_tested", "chk_recent_success"):
            getattr(self, attribute).setVisible(not compact)
        if self._allow_sort:
            self.cb_sort.setVisible(not compact)
            self.chk_group.setVisible(not compact)
        if compact and self.btn_advanced.isChecked():
            self.btn_advanced.setChecked(False)

    def query(self) -> ExplorerQuery:
        scope = self.cb_scope.currentData()
        if scope not in FILTER_SCOPES:
            scope = FILTER_SCOPE_COMBINED
        return ExplorerQuery(
            text=self.search.text().strip(),
            filter_mode=scope,
            scope=scope,
            providers=frozenset(self.provider.selected()),
            health=frozenset({self.cb_health.currentData()} - {HEALTH_ALL}),
            cost=frozenset({self.cb_cost.currentData()} - {COST_ALL}),
            tested=self.cb_tested.currentData(),
            routing_eligible_only=self.chk_routing.isChecked(),
            low_latency_only=self.chk_latency.isChecked(),
            recently_tested=self.chk_recent_tested.isChecked(),
            recently_successful=self.chk_recent_success.isChecked(),
            activation=self.cb_activation.currentData(),
            sort=(self.cb_sort.currentData() if self._allow_sort else "discovery"),
            group_by_provider=(self.chk_group.isChecked() if self._allow_sort else False),
        )

    def apply_query(self, query: ExplorerQuery):
        """Restore a remembered query without signalling it as a user edit."""
        self._block(True)
        scope = query.filter_mode if query.filter_mode in FILTER_SCOPES else FILTER_SCOPE_COMBINED
        self.cb_scope.setCurrentIndex(max(self.cb_scope.findData(scope), 0))
        self.search.setText(query.text)
        self.provider.set_selected(query.providers)
        self.cb_health.setCurrentIndex(max(
            self.cb_health.findData(next(iter(query.health))) if query.health else 0, 0
        ))
        self.cb_cost.setCurrentIndex(max(
            self.cb_cost.findData(next(iter(query.cost))) if query.cost else 0, 0
        ))
        self.cb_tested.setCurrentIndex(max(self.cb_tested.findData(query.tested), 0))
        self.chk_routing.setChecked(query.routing_eligible_only)
        self.chk_latency.setChecked(query.low_latency_only)
        self.chk_recent_tested.setChecked(query.recently_tested)
        self.chk_recent_success.setChecked(query.recently_successful)
        self.cb_activation.setCurrentIndex(max(self.cb_activation.findData(query.activation), 0))
        if self._allow_sort:
            self.cb_sort.setCurrentIndex(max(self.cb_sort.findData(query.sort), 0))
            self.chk_group.setChecked(query.group_by_provider)
        self._block(False)
        self._sync_scope_controls()

    def reset(self):
        self._block(True)
        self.cb_scope.setCurrentIndex(0)
        self.search.clear()
        self.provider.clear_selection()
        self.cb_health.setCurrentIndex(0)
        self.cb_cost.setCurrentIndex(0)
        self.cb_tested.setCurrentIndex(0)
        self.chk_routing.setChecked(False)
        self.chk_latency.setChecked(False)
        self.chk_recent_tested.setChecked(False)
        self.chk_recent_success.setChecked(False)
        self.cb_activation.setCurrentIndex(0)
        if self._allow_sort:
            self.cb_sort.setCurrentIndex(0)
            self.chk_group.setChecked(False)
        self._block(False)
        self._sync_scope_controls()
        self._on_query_changed()

    def set_provider_entries(self, entries: Sequence[ProviderEntry]):
        """Refresh the provider selector, preserving the user's selections."""
        self.provider.set_entries(entries)

    def set_status(self, visible: int, total: int, summary: str, empty_catalog: bool):
        counts = "No models available to add" if empty_catalog else f"Showing {visible} of {total} models"
        if empty_catalog:
            self.lbl_counts.setText(counts)
            self.lbl_summary.setText("The discovered catalogue is empty.")
        elif total and not visible:
            self.lbl_counts.setText("0 matches")
            self.lbl_summary.setText(f"No models match the current filters ({total} available).")
        else:
            self.lbl_counts.setText(counts)
            self.lbl_summary.setText(summary if summary != counts else "")

    # ---------------------------------------------------------------- helpers
    def _block(self, blocked: bool):
        for widget in (self.search.edit, self.cb_scope, self.cb_health, self.cb_cost, self.cb_tested,
                       self.cb_activation):
            widget.blockSignals(blocked)
        for attribute in ("chk_routing", "chk_latency", "chk_recent_tested", "chk_recent_success"):
            getattr(self, attribute).blockSignals(blocked)
        if self._allow_sort:
            self.cb_sort.blockSignals(blocked)
            self.chk_group.blockSignals(blocked)
        self.provider.blockSignals(blocked)

    def _on_scope_changed(self, *_args):
        self._sync_scope_controls()
        self._on_query_changed()

    def _sync_scope_controls(self):
        scope = self.cb_scope.currentData()
        if scope not in FILTER_SCOPES:
            scope = FILTER_SCOPE_COMBINED
        uses_models = scope != FILTER_SCOPE_PROVIDERS
        uses_providers = scope != FILTER_SCOPE_MODELS
        for widget in (self.cb_health, self.cb_cost, self.cb_tested, self.cb_activation):
            widget.setEnabled(uses_models)
        for attr in ("chk_routing", "chk_latency", "chk_recent_tested", "chk_recent_success"):
            getattr(self, attr).setEnabled(uses_models)
        self.provider.setEnabled(uses_providers)
        self.cb_scope.setToolTip(
            "Which identity dimension the search and the category filters apply to: "
            "Models only, Providers only, or both together (the default). "
            "An explicit provider:/status:/cost: shortcut still narrows its own "
            "category in every mode."
        )
        if scope == FILTER_SCOPE_PROVIDERS:
            self.search.edit.setPlaceholderText(
                "Search providers (provider, prefix, id)..."
            )
        elif scope == FILTER_SCOPE_MODELS:
            self.search.edit.setPlaceholderText("Search models (model, id, name)...")
        else:
            self.search.edit.setPlaceholderText(
                "Search models and providers (model, provider, id)..."
            )

    def _on_query_changed(self, *_args):
        self.query_changed.emit()

    def _on_reset(self):
        self.reset()
        self.reset_requested.emit()

    def _emit_focus_by_id(self):
        if self.txt_exact_id is not None:
            self.focus_by_id_requested.emit(self.txt_exact_id.text().strip())
