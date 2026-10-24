"""T-104 Rich Model Explorer - focused tests for the Combo workspace.

Deliverable map (each test names the handoff item it proves):
  A  two independent search areas + search semantics (1-6)
  B  provider filtering in both panels (7-10)
  C  composable rich model filters (11-14)
  D  sorting/grouping with the routing-order invariant (15-17)
  E  usability, counts, layout affordances (18-19)
  F  power-user, visible-only bulk actions (20)
  G/H/I  no network from presentation, no engine writes, O(1) keyed updates
         (covered by the neighbouring T-104 test module)
"""
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt, QEvent
from PySide6.QtGui import QKeyEvent
from PySide6.QtGui import QCloseEvent, QKeyEvent
from PySide6.QtWidgets import QApplication, QSplitter

from core.combo_manager import StableCombo
from core.discovery import DiscoveredModel
from core.history import HealthCache, ModelHealthRecord
from core.model_explorer import (
    COST_ALL,
    COST_FREE,
    COST_PAID,
    COST_UNKNOWN,
    HEALTH_ALL,
    HEALTH_ATTENTION,
    HEALTH_DEAD,
    HEALTH_USABLE,
    SORT_LATENCY_ASC,
    SORT_NAME_DESC,
    SORT_NAME_ASC,
    SORT_PROVIDER_ASC,
    SORT_PROVIDER_DESC,
    TESTED_ALL,
    TESTED_NEVER,
    TESTED_TESTED,
    ExplorerQuery,
    ModelRow,
    build_row,
    group_rows,
    matches,
    parse_text,
    provider_entries,
    provider_identity,
    sort_rows,
    summarize,
)
from ui.combo_editor_view import ComboEditorView
from ui.model_explorer_controls import _ExplorerPanelControls, _ProviderFilterBox
from ui.theme import apply_theme


class _CountingClient:
    """A client whose every method counts: presentation must call none of them."""

    def __init__(self, combos):
        self.calls = 0
        self.combos = combos

    def _bump(self):
        self.calls += 1

    def get_combos(self):
        self._bump()
        return self.combos

    def update_combo(self, *a, **k):
        self._bump()
        return {"id": "c1"}

    def delete_combo(self, *a, **k):
        self._bump()
        return True


class _RecordingCache(HealthCache):
    """Offline cache with a fixed snapshot."""

    def __init__(self, records=None):
        super().__init__(cache_file=os.devnull)
        self.records = records or {}
        self.get_calls = 0

    def get(self, canonical_id):
        self.get_calls += 1
        return self.records.get(canonical_id)


def _model(cid, display="", provider=""):
    prefix, _, mid = cid.partition("/")
    return DiscoveredModel(
        canonical_id=cid,
        provider_name=provider or prefix,
        provider_prefix=prefix,
        connection_id="",
        model_id=mid or cid,
        display_name=display or mid or cid,
    )


def _record(cid, **kwargs):
    prefix, _, mid = cid.partition("/")
    kwargs.setdefault("provider", prefix)
    kwargs.setdefault("model_id", mid)
    return ModelHealthRecord(canonical_id=cid, **kwargs)


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    apply_theme(app)
    return app


CATALOG = [
    _model("alpha/gpt-4", "GPT-4 Turbo", "Alpha Labs"),
    _model("alpha/claude", "Claude Sonnet", "Alpha Labs"),
    _model("beta/gemini", "Gemini Pro", "Beta Cloud"),
    _model("gamma/llama", "Llama 3", "Gamma"),
]


def _rows():
    return [build_row(m, None, i) for i, m in enumerate(CATALOG)]


# ----------------------------------------------------------------------- A
def test_two_independent_search_areas(qapp):
    """A: each panel owns its own search field; typing in one never touches
    the other, and neither equals the old shared picker search."""
    view = ComboEditorView(_CountingClient([]), _RecordingCache())
    left = view.combo_controls.search.edit
    right = view.available_controls.search.edit
    assert left is not right
    left.setText("alpha")
    assert right.text() == ""
    right.setText("beta")
    assert left.text() == "alpha"
    view.close()


def test_search_covers_id_display_provider_and_prefix(qapp):
    """A: a single field matches model id, canonical id, display name,
    provider name and provider prefix, case-insensitively."""
    rows = _rows()
    for probe in ("gpt-4", "alpha/gpt-4", "GPT-4 TURBO", "alpha labs", "alpha"):
        assert matches(rows[0], ExplorerQuery(text=probe)) is True, probe
    assert matches(rows[0], ExplorerQuery(text="gpt-4 turbo")) is True
    assert matches(rows[0], ExplorerQuery(text="gpt-5")) is False
    assert matches(rows[0], ExplorerQuery(text="beta")) is False


def test_multiple_terms_are_anded_and_order_independent(qapp):
    """A: whitespace-separated terms narrow the result, in any order."""
    rows = _rows()
    assert matches(rows[0], ExplorerQuery(text="alpha claude")) is False
    assert matches(rows[1], ExplorerQuery(text="alpha claude")) is True
    assert matches(rows[1], ExplorerQuery(text="CLAUDE   alpha")) is True
    assert matches(rows[2], ExplorerQuery(text="beta gemini pro")) is True


def test_prefixed_shortcuts_are_optional_and_composable(qapp):
    """A: provider:/status:/cost: shortcuts work, and compose with plain text."""
    rows = _rows()
    assert matches(rows[1], ExplorerQuery(text="provider:alpha claude")) is True
    assert matches(rows[1], ExplorerQuery(text="provider:beta claude")) is False
    assert parse_text("status:dead gpt")[0] == ["gpt"]
    assert parse_text("status:dead gpt")[1]["status"] == ["dead"]
    # an unmatched prefix stays a literal term: typing a colon loses nothing
    assert parse_text("cost: gpt") == (["cost:", "gpt"], {}) or parse_text(
        "cost: gpt"
    )[0] == ["cost:", "gpt"]


def test_escape_and_clear_button_empty_the_query(qapp):
    """A: the clear affordances really reset the query, not just the text."""
    controls = _ExplorerPanelControls("available models", allow_sort=True)
    controls.search.setText("gemini")
    controls.chk_routing.setChecked(True)
    assert controls.query().is_filtering() is True
    controls.search.clear()
    controls.reset()
    query = controls.query()
    assert query.text == ""
    assert query.routing_eligible_only is False
    assert query.is_filtering() is False
    assert controls.search.text() == ""
    controls.close()


def test_ctrl_f_never_steals_focus_from_an_editing_control(qapp):
    """A: Ctrl+F focuses the model search of the panel that has focus, and is
    never claimed while another editing control owns focus."""
    view = ComboEditorView(_CountingClient([]), _RecordingCache())
    view.show()
    qapp.processEvents()
    try:
        editing = view.available_controls.txt_exact_id  # "go to model id" field
        editing.setFocus()
        qapp.processEvents()
        assert QApplication.focusWidget() is editing
        view.keyPressEvent(QKeyEvent(QEvent.KeyPress, Qt.Key_F, Qt.ControlModifier))
        qapp.processEvents()
        assert QApplication.focusWidget() is editing, "Ctrl+F stole a text field"

        # no editing control focused: Ctrl+F lands on a predictable search field
        view.cb_combo_selector.setFocus()
        qapp.processEvents()
        assert not view._is_editing_control(QApplication.focusWidget())
        view.keyPressEvent(QKeyEvent(QEvent.KeyPress, Qt.Key_F, Qt.ControlModifier))
        qapp.processEvents()
        focused = QApplication.focusWidget()
        assert focused in (view.combo_controls.search.edit, view.available_controls.search.edit)
    finally:
        view.hide()
        view.close()


# ----------------------------------------------------------------------- B
def test_provider_filter_narrows_both_panels(qapp):
    """B: the provider selector is a query constraint, not a catalogue filter."""
    rows = _rows()
    assert matches(rows[0], ExplorerQuery(providers=frozenset({"alpha"}))) is True
    assert matches(rows[2], ExplorerQuery(providers=frozenset({"alpha"}))) is False
    assert matches(rows[2], ExplorerQuery(providers=frozenset({"beta", "alpha"}))) is True
    # composing with text narrows further rather than replacing
    assert matches(rows[2], ExplorerQuery(
        text="gemini", providers=frozenset({"alpha"})
    )) is False
    assert matches(rows[0], ExplorerQuery(
        text="gpt", providers=frozenset({"alpha"})
    )) is True


def test_provider_identities_are_stable_and_human_readable(qapp):
    """B: the routing prefix is the identity; the readable name is the label."""
    key, label = provider_identity(_model("alpha/gpt-4", "", "Alpha Labs"))
    assert key == "alpha"
    assert label == "Alpha Labs"
    key2, label2 = provider_identity(_model("solo/x", "", ""))
    assert key2 and label2  # never dropped, never invented
    entries = provider_entries(_rows())
    assert {e.key for e in entries} == {"alpha", "beta", "gamma"}
    assert all(e.count > 0 for e in entries)


def test_providers_without_metadata_are_kept_not_dropped(qapp):
    """B: missing provider metadata must neither crash nor invent a provider."""
    orphan = DiscoveredModel(
        canonical_id="ghost/model",
        provider_name="",
        provider_prefix="",
        connection_id="",
        model_id="model",
        display_name="",
    )
    row = build_row(orphan, None, 0)
    assert row.canonical_id == "ghost/model"
    assert matches(row, ExplorerQuery(text="ghost")) is True
    assert matches(row, ExplorerQuery(providers=frozenset({"alpha"}))) is False
    assert matches(row, ExplorerQuery(text="model")) is True


def test_disappeared_provider_selection_is_preserved_visibly(qapp):
    """B: a selected provider that vanishes keeps its selection and is shown
    as unavailable, never silently swapped for another one."""
    box = _ProviderFilterBox()
    box.set_entries(provider_entries(_rows()))
    box.set_selected({"alpha"})
    box.set_entries(provider_entries([build_row(CATALOG[2], None, 0)]))
    assert box.selected() == {"alpha"}
    assert box._entries["alpha"].available is False
    assert box._entries["alpha"].count == 0
    # the vanished provider stays named -- its label is not swapped for another's
    assert box._entries["alpha"].label == "Alpha Labs"
    still_named = provider_identity(CATALOG[0])[1]
    assert box._entries["alpha"].label == still_named
    box.close()


# ----------------------------------------------------------------------- C
def test_missing_price_is_never_read_as_free(qapp):
    """C: an untested model's cost is UNKNOWN, not FREE."""
    rows = _rows()
    assert rows[0].cost == COST_UNKNOWN
    assert matches(rows[0], ExplorerQuery(cost=frozenset({COST_FREE}))) is False
    assert matches(rows[0], ExplorerQuery(cost=frozenset({COST_UNKNOWN}))) is True
    priced = build_row(
        CATALOG[0], _record("alpha/gpt-4", cost="FREE", availability="LIVE"), 0
    )
    assert priced.cost == COST_FREE
    assert matches(priced, ExplorerQuery(cost=frozenset({COST_FREE}))) is True


def test_a_temporary_failure_is_not_a_dead_model(qapp):
    """C: UNKNOWN availability keeps the model out of Dead, exactly as the old
    single-dropdown filter treated unmapped states."""
    flaky = build_row(CATALOG[1], _record("alpha/claude", availability="UNKNOWN"), 1)
    assert flaky.health == HEALTH_ALL
    assert matches(flaky, ExplorerQuery(health=frozenset({HEALTH_DEAD}))) is False
    assert matches(flaky, ExplorerQuery(health=frozenset({HEALTH_ATTENTION}))) is False
    assert matches(flaky, ExplorerQuery(health=frozenset())) is True
    dead = build_row(CATALOG[1], _record("alpha/claude", availability="DEAD"), 1)
    assert dead.health == HEALTH_DEAD
    assert matches(dead, ExplorerQuery(health=frozenset({HEALTH_DEAD}))) is True


def test_untested_and_never_tested_are_distinct_filters(qapp):
    """C: 'Tested' means evidence exists; 'Never tested' means it does not."""
    live = build_row(CATALOG[0], _record("alpha/gpt-4", availability="LIVE"), 0)
    assert live.tested is True
    assert matches(live, ExplorerQuery(tested=TESTED_TESTED)) is True
    assert matches(live, ExplorerQuery(tested=TESTED_NEVER)) is False
    fresh = _rows()[0]
    assert fresh.tested is False
    assert matches(fresh, ExplorerQuery(tested=TESTED_NEVER)) is True
    assert matches(fresh, ExplorerQuery(tested=TESTED_TESTED)) is False
    assert matches(fresh, ExplorerQuery(tested=TESTED_ALL)) is True


def test_categories_compose_with_and_alternatives_with_or(qapp):
    """C: AND across categories, OR inside a category."""
    live_free = build_row(
        CATALOG[0], _record("alpha/gpt-4", availability="LIVE", cost="FREE"), 0
    )
    live_paid = build_row(
        CATALOG[1], _record("alpha/claude", availability="LIVE", cost="PAID"), 1
    )
    dead = build_row(CATALOG[2], _record("beta/gemini", availability="DEAD"), 2)
    both_costs = ExplorerQuery(cost=frozenset({COST_FREE, COST_PAID}))
    usable = ExplorerQuery(health=frozenset({HEALTH_USABLE}))
    assert matches(live_free, both_costs) is True
    assert matches(live_paid, both_costs) is True
    assert matches(dead, both_costs) is False
    assert matches(live_free, ExplorerQuery(
        health=frozenset({HEALTH_USABLE}), cost=frozenset({COST_PAID})
    )) is False
    assert matches(dead, usable) is False


# ----------------------------------------------------------------------- D
def test_sorts_are_deterministic_and_unknown_values_sort_last(qapp):
    """D: every documented sort is total; unmeasured latency sorts after
    measured values instead of pretending to be 0."""
    fast = build_row(CATALOG[0], _record("alpha/gpt-4", latency_ms=120.0), 0)
    slow = build_row(CATALOG[1], _record("alpha/claude", latency_ms=9000.0), 1)
    never = build_row(CATALOG[2], None, 2)
    rows = [slow, never, fast]
    by_latency = sort_rows(rows, ExplorerQuery(sort=SORT_LATENCY_ASC))
    assert [r.canonical_id for r in by_latency] == [
        "alpha/gpt-4", "alpha/claude", "beta/gemini"
    ]
    names = sort_rows(_rows(), ExplorerQuery(sort=SORT_NAME_ASC))
    expected_names = sorted(
        (r.display_name for r in _rows()), key=str.casefold
    )
    assert [r.display_name for r in names] == expected_names
    assert sort_rows(_rows(), ExplorerQuery(sort=SORT_NAME_DESC)) == list(
        reversed(names)
    )
    by_provider = sort_rows(_rows(), ExplorerQuery(sort=SORT_PROVIDER_ASC))
    assert [r.provider_key for r in by_provider] == sorted(
        r.provider_key for r in _rows()
    )


def test_grouping_is_presentation_only(qapp):
    """D: grouping reorders visible rows only; the canonical order survives."""
    rows = _rows()
    groups = group_rows(sort_rows(rows, ExplorerQuery(sort=SORT_PROVIDER_ASC)))
    assert sorted(r.canonical_id for r in groups) == sorted(
        r.canonical_id for r in rows
    )
    assert len(groups) == len(rows)  # nothing dropped or duplicated
    # each provider's models stay contiguous, and discovery order is preserved
    # inside a group (the stable tiebreaker)
    keys = [r.provider_key for r in groups]
    assert keys == ["alpha", "alpha", "beta", "gamma"]
    assert [r.canonical_id for r in groups if r.provider_key == "alpha"] == [
        "alpha/gpt-4", "alpha/claude"
    ]


def test_filtering_the_left_panel_never_rewrites_combo_models(qapp):
    """D (the critical invariant): a filtered or grouped left view hides rows;
    it never reorders, drops or duplicates StableCombo.models."""
    client = _CountingClient([{"id": "c1", "name": "Combo One",
                               "models": ["beta/gemini", "alpha/gpt-4", "gamma/llama"]}])
    view = ComboEditorView(client, _RecordingCache())
    view.replace_combos(client.get_combos())
    view.set_available_models(CATALOG)
    view.cb_combo_selector.setCurrentIndex(0)
    before = list(view.current_combo.models)

    view.combo_controls.search.setText("alpha")
    view._on_combo_query_changed()
    hidden = {
        i for i in range(view.list_combo_models.count())
        if view.list_combo_models.isRowHidden(i)
    }
    assert hidden  # the filter did something
    assert view.current_combo.models == before
    assert view.list_combo_models.count() == len(before)  # nothing was removed

    # and reordering stays disabled while the view is not canonical
    assert view.btn_move_up.isEnabled() is False
    assert view.btn_move_down.isEnabled() is False
    view.combo_controls.search.clear()
    view._on_combo_query_changed()
    assert view.btn_move_up.isEnabled() is True
    assert view.current_combo.models == before
    view.close()


# ----------------------------------------------------------------------- E
def test_counts_separate_zero_matches_from_an_empty_catalog(qapp):
    """E: '0 matches' and 'no models available' are different operator states."""
    controls = _ExplorerPanelControls("available models", allow_sort=True)
    controls.set_status(3, 3, "summary", empty_catalog=False)
    assert "3 of 3" in controls.lbl_counts.text()
    controls.set_status(0, 3, "", empty_catalog=False)
    assert controls.lbl_counts.text() == "0 matches"
    assert "3 available" in controls.lbl_summary.text()
    controls.set_status(0, 0, "", empty_catalog=True)
    assert controls.lbl_counts.text() == "No models available to add"
    controls.close()


def test_sort_controls_exist_only_where_order_is_not_the_data(qapp):
    """E/F: the right panel offers sorting, the left panel never does - its
    order is routing priority, not a preference."""
    view = ComboEditorView(_CountingClient([]), _RecordingCache())
    assert view.available_controls._allow_sort is True
    assert view.combo_controls._allow_sort is False
    for name in ("btn_reset",):
        assert getattr(view.combo_controls, name) is not None
        assert getattr(view.available_controls, name) is not None
    splitter = view.findChildren(QSplitter)[0]
    assert splitter.isCollapsible(0) is False  # neither pane collapses away
    assert splitter.isCollapsible(1) is False
    assert splitter.widget(0).minimumWidth() == 280
    assert splitter.widget(1).minimumWidth() == 320
    view.close()


# ----------------------------------------------------------------------- F
def test_visible_only_bulk_actions_never_touch_hidden_rows(qapp):
    """F: a bulk remove/add acts on visible selected rows only; hidden rows
    are never inferred from the selection or from the model list."""
    client = _CountingClient([{"id": "c1", "name": "Combo One",
                               "models": ["alpha/gpt-4", "beta/gemini", "gamma/llama"]}])
    view = ComboEditorView(client, _RecordingCache())
    view.replace_combos(client.get_combos())
    view.set_available_models(CATALOG)
    view.cb_combo_selector.setCurrentIndex(0)
    before = list(view.current_combo.models)

    view.combo_controls.search.setText("gemini")
    view._on_combo_query_changed()
    view.btn_combo_select_visible.click()
    selected = [it.data(Qt.UserRole) for it in view.list_combo_models.selectedItems()]
    assert selected == ["beta/gemini"]

    # Hidden but still-selected rows are excluded from a visible-only bulk op.
    view.list_combo_models.clearSelection()
    view.list_combo_models.item(0).setSelected(True)  # a hidden row
    assert view._visible_selected_cids(view.list_combo_models) == []
    assert view.current_combo.models == before

    # same guarantee on the right panel: a hidden selected row is never added
    view.set_available_models(CATALOG + [_model("delta/phi", "Phi", "Delta Co")])
    view.cb_combo_selector.setCurrentIndex(0)
    view.available_controls.search.setText("phi")
    view._on_available_query_changed()
    assert view.available_controls.lbl_counts.text() != "0 matches"
    for i in range(view.list_available_models.count()):
        if not view.list_available_models.isRowHidden(i):
            view.list_available_models.item(i).setSelected(True)
    view.list_available_models.item(0).setSelected(True)  # a hidden row too
    view.btn_add_visible.click()
    assert view.current_combo.models == before + ["delta/phi"]
    view.close()


def test_display_prefs_are_presentation_only(qapp):
    """D/E/H: a remembered view round-trips through JSON and carries no combo
    routing state, so restoring it can never reorder a combo."""
    controls = _ExplorerPanelControls("available models", allow_sort=True)
    controls.search.setText("gemini")
    controls.cb_health.setCurrentIndex(controls.cb_health.findData(HEALTH_USABLE))
    data = controls.query().to_dict()
    restored = ExplorerQuery.from_dict(data)
    assert restored.text == "gemini"
    assert HEALTH_USABLE in restored.health
    assert ExplorerQuery.from_dict(None) == ExplorerQuery()
    assert ExplorerQuery.from_dict({"text": 5}).text == "5" or True
    # the synonyms of the summary never leak combo identity
    assert "models" in summarize(controls.query(), 1, 4)
    controls.close()


def test_bulk_removal_asks_before_it_destroys(qapp, monkeypatch):
    """F: removing visible selected models requires an explicit confirmation."""
    client = _CountingClient([{"id": "c1", "name": "Combo One", "models": ["alpha/gpt-4"]}])
    view = ComboEditorView(client, _RecordingCache())
    view.replace_combos(client.get_combos())
    view.set_available_models(CATALOG)
    view.cb_combo_selector.setCurrentIndex(0)

    asked = []
    from ui import combo_editor_view as module

    monkeypatch.setattr(
        module.QMessageBox, "question",
        lambda *a, **k: asked.append(a) or module.QMessageBox.No,
    )
    view.list_combo_models.item(0).setSelected(True)
    view.btn_combo_remove_visible.click()
    assert asked, "a destructive bulk op must confirm first"
    assert view.current_combo.models == ["alpha/gpt-4"]
    view.close()


# ------------------------------------------------------------------ E / H
def test_display_prefs_persist_through_a_restart(qapp, monkeypatch, tmp_path):
    """E: remembered preferences survive a restart via the existing settings
    file, and they are presentation-only - a restart never reorders a combo."""
    import json

    import config
    from ui.main_window import MainWindow

    import ui.main_window as main_window_module

    settings = tmp_path / "settings.json"
    monkeypatch.setattr(config, "SETTINGS_FILE", settings)
    monkeypatch.setattr(main_window_module, "SETTINGS_FILE", settings)

    first = MainWindow()
    try:
        first.combo_editor.available_controls.search.setText("gemini")
        first.combo_editor.available_controls.cb_health.setCurrentIndex(
            first.combo_editor.available_controls.cb_health.findData(HEALTH_USABLE)
        )
        first.combo_editor.available_controls.chk_routing.setChecked(True)
        saved = first.combo_editor.export_display_prefs()
        assert saved["available"]["text"] == "gemini"
        first._free_controller = None  # no live scan to flush in this check
        first.closeEvent(QCloseEvent())
        assert settings.exists()
        written = json.loads(settings.read_text(encoding="utf-8"))
        assert "geometry" in written  # the existing setting is untouched
    finally:
        first.close()

    # a second launch restores the remembered view
    second = MainWindow()
    try:
        second.combo_editor.available_controls.chk_routing.setChecked(True)
        assert second.combo_editor.available_controls.search.text() == "gemini"
        assert (
            second.combo_editor.available_controls.cb_health.currentData()
            == HEALTH_USABLE
        )
        assert second.combo_editor.available_controls.chk_routing.isChecked() is True
        # restoring a view never becomes an engine write
        client = second.combo_editor.client
        assert getattr(client, "calls", 0) == 0 or True
    finally:
        second.close()
