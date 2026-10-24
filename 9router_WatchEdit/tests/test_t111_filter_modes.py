"""T-111 (SRC-050) - ComboEdit filter modes, search scope and activation.

The operator asked for filtering by Models, by Providers, separately or
together, plus search and per-item enable/disable. The explorer already owned
the matching logic; T-111 adds the explicit *scope* mode, per-provider
activation from the row menu, and proves the two dimensions never leak into
each other.

Deliverable map:
  A  scope constants + ExplorerQuery scope semantics (models / providers / both)
  B  scope is presentation-only and round-trips through JSON
  C  the control panel exposes the scope switch and keeps facets coherent
  D  both panels honour the scope end to end
  E  per-provider activation from a combo row (menu + API)
"""
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEvent, QPoint, Qt
from PySide6.QtWidgets import QApplication, QMenu, QMessageBox

from core.discovery import DiscoveredModel
from core.history import HealthCache
from core.model_explorer import (
    FILTER_SCOPE_ALL,
    FILTER_SCOPE_COMBINED,
    FILTER_SCOPE_MODELS,
    FILTER_SCOPE_OPTIONS,
    FILTER_SCOPE_PROVIDERS,
    FILTER_SCOPES,
    HEALTH_DEAD,
    ExplorerQuery,
    build_row,
    matches,
    summarize,
)
from ui import combo_editor_view
from ui.combo_editor_view import ComboEditorView
from ui.model_explorer_controls import _ExplorerPanelControls
from ui.theme import apply_theme


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    apply_theme(app)
    return app


class _Engine:
    """Minimal engine: stores the models array verbatim, like 9Router."""

    def __init__(self, combos):
        self.combos = [dict(c) for c in combos]
        self.update_calls = []

    def get_combos(self):
        return [dict(c) for c in self.combos]

    def update_combo(self, combo_id, name, models, kind=None):
        self.update_calls.append((combo_id, name, list(models), kind))
        row = next((c for c in self.combos if c.get("id") == combo_id), None)
        if row is None:
            return None
        row["name"] = name
        row["models"] = list(models)
        row["kind"] = kind
        row["updatedAt"] = f"t{len(self.update_calls)}"
        return dict(row)

    def create_combo(self, name, models, kind=None):
        row = {"id": "new", "name": name, "models": list(models), "kind": kind}
        self.combos.append(row)
        return dict(row)

    def delete_combo(self, combo_id):
        before = len(self.combos)
        self.combos = [c for c in self.combos if c.get("id") != combo_id]
        return len(self.combos) != before


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


CATALOG = [
    _model("alpha/gpt-4", "GPT-4 Turbo", "Alpha Labs"),
    _model("alpha/claude", "Claude Sonnet", "Alpha Labs"),
    _model("beta/gemini", "Gemini Pro", "Beta Cloud"),
]


def _rows():
    return [build_row(m, None, i) for i, m in enumerate(CATALOG)]


def _patch_messages(monkeypatch, view):
    info, warn, crit = [], [], []
    view._msgs = (info, warn, crit)
    monkeypatch.setattr(
        combo_editor_view.QMessageBox, "information",
        lambda *a, **k: info.append(a[1:]),
    )
    monkeypatch.setattr(
        combo_editor_view.QMessageBox, "warning",
        lambda *a, **k: warn.append(a[1:]),
    )
    monkeypatch.setattr(
        combo_editor_view.QMessageBox, "critical",
        lambda *a, **k: crit.append(a[1:]),
    )
    monkeypatch.setattr(
        combo_editor_view.QMessageBox, "question",
        lambda *a, **k: QMessageBox.Yes,
    )
    return info, warn, crit


def _view(qapp, tmp_path, engine, models=CATALOG):
    view = ComboEditorView(engine, HealthCache(cache_file=tmp_path / "cache.json"))
    view.replace_combos(engine.get_combos())
    view.set_available_models(list(models))
    return view


def _teardown(view, qapp):
    view.close()
    view.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    qapp.processEvents()


# ----------------------------------------------------------------------- A
def test_scope_constants_are_stable_and_complete():
    """A: the mode is a closed set with one default, so a stale preference can
    never put the explorer in an undefined mode."""
    assert FILTER_SCOPE_COMBINED == "both"
    assert FILTER_SCOPE_ALL == FILTER_SCOPE_COMBINED
    assert FILTER_SCOPES == (
        FILTER_SCOPE_COMBINED, FILTER_SCOPE_MODELS, FILTER_SCOPE_PROVIDERS,
    )
    assert [value for value, _label in FILTER_SCOPE_OPTIONS] == list(FILTER_SCOPES)
    assert ExplorerQuery().filter_mode == FILTER_SCOPE_COMBINED
    assert ExplorerQuery().scope == FILTER_SCOPE_COMBINED


def test_models_only_scope_ignores_provider_identity(qapp):
    """A: in Models mode a plain term matches model text only; provider names
    and the provider selector are not part of the search."""
    row = _rows()[0]
    assert matches(row, ExplorerQuery(text="gpt", filter_mode=FILTER_SCOPE_MODELS))
    assert not matches(row, ExplorerQuery(text="labs",
                                         filter_mode=FILTER_SCOPE_MODELS))
    # the provider selector is a provider-dimension constraint, so it is off
    assert matches(row, ExplorerQuery(providers=frozenset({"beta"}),
                                      filter_mode=FILTER_SCOPE_MODELS))
    # model facets still apply
    dead = build_row(CATALOG[0], {"availability": "DEAD"}, 0)
    assert matches(dead, ExplorerQuery(health=frozenset({HEALTH_DEAD}),
                                       filter_mode=FILTER_SCOPE_MODELS)) is True


def test_providers_only_scope_ignores_model_identity(qapp):
    """A: in Providers mode a plain term matches provider text only, and every
    model facet is ignored rather than silently narrowing the provider list."""
    row = _rows()[0]
    assert matches(row, ExplorerQuery(text="alpha",
                                      filter_mode=FILTER_SCOPE_PROVIDERS))
    assert not matches(row, ExplorerQuery(text="gpt",
                                          filter_mode=FILTER_SCOPE_PROVIDERS))
    assert matches(row, ExplorerQuery(providers=frozenset({"alpha"}),
                                      filter_mode=FILTER_SCOPE_PROVIDERS))
    assert not matches(row, ExplorerQuery(providers=frozenset({"beta"}),
                                          filter_mode=FILTER_SCOPE_PROVIDERS))
    # facets that describe a model are not a provider constraint
    assert matches(row, ExplorerQuery(health=frozenset({HEALTH_DEAD}),
                                      filter_mode=FILTER_SCOPE_PROVIDERS))
    assert matches(row, ExplorerQuery(tested="tested",
                                      filter_mode=FILTER_SCOPE_PROVIDERS))


def test_combined_scope_is_the_union_of_both_dimensions(qapp):
    """A: combined (the default) searches both identities and applies every
    facet, exactly as before T-111."""
    row = _rows()[0]
    assert matches(row, ExplorerQuery(text="gpt"))
    assert matches(row, ExplorerQuery(text="alpha labs"))
    assert not matches(row, ExplorerQuery(providers=frozenset({"beta"})))
    assert row.model_blob and row.provider_blob
    assert "gpt-4" in row.model_blob and "gpt-4" not in row.provider_blob
    assert "alpha labs" in row.provider_blob and "alpha labs" not in row.model_blob


def test_is_filtering_respects_the_scope(qapp):
    """A: a constraint outside the active scope is not an active filter, so the
    counts line never claims the list is narrowed when it is not."""
    assert ExplorerQuery(providers=frozenset({"alpha"}),
                         filter_mode=FILTER_SCOPE_MODELS).is_filtering() is False
    assert ExplorerQuery(health=frozenset({HEALTH_DEAD}),
                         filter_mode=FILTER_SCOPE_PROVIDERS).is_filtering() is False
    assert ExplorerQuery(text="x",
                         filter_mode=FILTER_SCOPE_PROVIDERS).is_filtering() is True
    assert ExplorerQuery(providers=frozenset({"alpha"}),
                         filter_mode=FILTER_SCOPE_PROVIDERS).is_filtering() is True


def test_summary_names_the_active_scope(qapp):
    """A: the summary tells the operator which dimension is being filtered."""
    text = summarize(ExplorerQuery(filter_mode=FILTER_SCOPE_MODELS), 1, 3)
    assert "scope: models" in text
    text = summarize(ExplorerQuery(filter_mode=FILTER_SCOPE_PROVIDERS), 1, 3)
    assert "scope: providers" in text
    assert "scope:" not in summarize(ExplorerQuery(), 3, 3)


# ----------------------------------------------------------------------- B
def test_scope_round_trips_and_aliases_agree():
    """B: a remembered mode survives a restart and both public names agree."""
    query = ExplorerQuery(text="gpt", filter_mode=FILTER_SCOPE_MODELS)
    data = query.to_dict()
    assert data["filter_mode"] == FILTER_SCOPE_MODELS
    assert data["scope"] == FILTER_SCOPE_MODELS
    restored = ExplorerQuery.from_dict(data)
    assert restored.filter_mode == FILTER_SCOPE_MODELS
    assert restored.scope == FILTER_SCOPE_MODELS
    # the older ``scope`` spelling is accepted too
    assert ExplorerQuery.from_dict({"scope": FILTER_SCOPE_PROVIDERS}).filter_mode \
        == FILTER_SCOPE_PROVIDERS
    # malformed values fall back to the default instead of raising
    assert ExplorerQuery.from_dict({"filter_mode": "nonsense"}).filter_mode \
        == FILTER_SCOPE_COMBINED
    # conflicting aliases converge on one value
    mixed = ExplorerQuery(filter_mode=FILTER_SCOPE_MODELS,
                          scope=FILTER_SCOPE_PROVIDERS)
    assert mixed.filter_mode == mixed.scope


# ----------------------------------------------------------------------- C
def test_control_panel_exposes_the_scope_switch(qapp):
    """C: the switch exists, defaults to combined and drives the query."""
    controls = _ExplorerPanelControls("available models", allow_sort=True)
    try:
        assert controls.cb_scope is controls.cb_filter_mode
        assert controls.cb_scope is controls.cb_filter_scope
        assert controls.cb_scope.count() == len(FILTER_SCOPE_OPTIONS)
        assert controls.query().filter_mode == FILTER_SCOPE_COMBINED

        controls.cb_scope.setCurrentIndex(controls.cb_scope.findData(FILTER_SCOPE_MODELS))
        assert controls.query().filter_mode == FILTER_SCOPE_MODELS
        assert controls.query().scope == FILTER_SCOPE_MODELS
        assert controls.cb_scope.currentText() == "Models only"
    finally:
        controls.close()


def test_scope_switch_disables_facets_it_cannot_apply(qapp):
    """C: a control that the current mode ignores is visibly disabled, so the
    operator is never offered a filter that would silently do nothing."""
    controls = _ExplorerPanelControls("available models", allow_sort=True)
    try:
        controls.cb_scope.setCurrentIndex(controls.cb_scope.findData(FILTER_SCOPE_PROVIDERS))
        assert controls.provider.isEnabled() is True
        for widget in (controls.cb_health, controls.cb_cost, controls.cb_tested,
                       controls.cb_activation):
            assert widget.isEnabled() is False
        assert controls.chk_routing.isEnabled() is False
        assert "provider" in controls.search.edit.placeholderText().lower()

        controls.cb_scope.setCurrentIndex(controls.cb_scope.findData(FILTER_SCOPE_MODELS))
        assert controls.provider.isEnabled() is False
        assert controls.cb_health.isEnabled() is True
        assert controls.chk_routing.isEnabled() is True
        assert "model" in controls.search.edit.placeholderText().lower()
    finally:
        controls.close()


def test_apply_and_reset_restore_the_scope(qapp):
    """C: a restored preference sets the switch; reset returns to combined."""
    controls = _ExplorerPanelControls("available models", allow_sort=True)
    try:
        controls.apply_query(ExplorerQuery(text="x", filter_mode=FILTER_SCOPE_PROVIDERS))
        assert controls.cb_scope.currentData() == FILTER_SCOPE_PROVIDERS
        assert controls.provider.isEnabled() is True
        assert controls.cb_health.isEnabled() is False
        assert controls.query().filter_mode == FILTER_SCOPE_PROVIDERS

        controls.reset()
        assert controls.cb_scope.currentData() == FILTER_SCOPE_COMBINED
        assert controls.query().filter_mode == FILTER_SCOPE_COMBINED
        assert controls.provider.isEnabled() is True
        assert controls.cb_health.isEnabled() is True
    finally:
        controls.close()


# ----------------------------------------------------------------------- D
def test_both_panels_honour_models_only_search(qapp, tmp_path, monkeypatch):
    """D: end to end, a provider name typed in Models mode matches nothing,
    while the same text in combined mode matches that provider's models."""
    engine = _Engine([{"id": "c1", "name": "C1",
                       "models": ["alpha/gpt-4", "alpha/claude", "beta/gemini"]}])
    view = _view(qapp, tmp_path, engine)
    _patch_messages(monkeypatch, view)
    try:
        view.cb_combo_selector.setCurrentIndex(0)

        view.combo_controls.cb_scope.setCurrentIndex(
            view.combo_controls.cb_scope.findData(FILTER_SCOPE_MODELS)
        )
        view.combo_controls.search.setText("alpha labs")
        view._on_combo_query_changed()
        hidden = [
            i for i in range(view.list_combo_models.count())
            if view.list_combo_models.isRowHidden(i)
        ]
        assert len(hidden) == 3  # provider text is not a model match

        view.combo_controls.cb_scope.setCurrentIndex(
            view.combo_controls.cb_scope.findData(FILTER_SCOPE_COMBINED)
        )
        view._on_combo_query_changed()
        visible = [
            view.list_combo_models.item(i).data(Qt.UserRole)
            for i in range(view.list_combo_models.count())
            if not view.list_combo_models.isRowHidden(i)
        ]
        assert visible == ["alpha/gpt-4", "alpha/claude"]
        # filtering never rewrote the combo's routing order
        assert view.current_combo.models == [
            "alpha/gpt-4", "alpha/claude", "beta/gemini"
        ]

        # the right panel uses the same scope semantics
        view.set_available_models([_model("delta/phi", "Phi", "Delta Cloud")])
        view.available_controls.cb_scope.setCurrentIndex(
            view.available_controls.cb_scope.findData(FILTER_SCOPE_MODELS)
        )
        view.available_controls.search.setText("delta cloud")
        view._on_available_query_changed()
        assert view.available_controls.lbl_counts.text() == "0 matches"
    finally:
        _teardown(view, qapp)


def test_providers_only_mode_filters_by_provider_identity(qapp, tmp_path, monkeypatch):
    """D: Providers mode searches only provider text, on both panels."""
    engine = _Engine([{"id": "c1", "name": "C1",
                       "models": ["alpha/gpt-4", "beta/gemini"]}])
    view = _view(qapp, tmp_path, engine)
    _patch_messages(monkeypatch, view)
    try:
        view.cb_combo_selector.setCurrentIndex(0)
        view.combo_controls.cb_scope.setCurrentIndex(
            view.combo_controls.cb_scope.findData(FILTER_SCOPE_PROVIDERS)
        )
        view.combo_controls.search.setText("beta")
        view._on_combo_query_changed()
        visible = [
            view.list_combo_models.item(i).data(Qt.UserRole)
            for i in range(view.list_combo_models.count())
            if not view.list_combo_models.isRowHidden(i)
        ]
        assert visible == ["beta/gemini"]
        assert view.current_combo.models == ["alpha/gpt-4", "beta/gemini"]
    finally:
        _teardown(view, qapp)


def test_per_item_toggle_still_works_under_a_scope(qapp, tmp_path, monkeypatch):
    """D: per-item enable/disable is independent of the filter mode."""
    engine = _Engine([{"id": "c1", "name": "C1",
                       "models": ["alpha/gpt-4", "beta/gemini"]}])
    view = _view(qapp, tmp_path, engine)
    _patch_messages(monkeypatch, view)
    try:
        view.cb_combo_selector.setCurrentIndex(0)
        view.combo_controls.cb_scope.setCurrentIndex(
            view.combo_controls.cb_scope.findData(FILTER_SCOPE_MODELS)
        )
        view.combo_controls.search.setText("gpt")
        view._on_combo_query_changed()
        item = view._combo_item_by_cid["alpha/gpt-4"]
        item.setCheckState(Qt.Unchecked)
        assert view.current_combo.is_model_enabled("alpha/gpt-4") is False
        assert view.current_combo.models == ["alpha/gpt-4", "beta/gemini"]
    finally:
        _teardown(view, qapp)


# ----------------------------------------------------------------------- E
def test_provider_activation_api_targets_one_provider_only(qapp, tmp_path, monkeypatch):
    """E: a per-provider action is scoped by provider key, not by the filter
    and not by the current selection."""
    engine = _Engine([{"id": "c1", "name": "C1",
                       "models": ["alpha/gpt-4", "beta/gemini", "alpha/claude"]}])
    view = _view(qapp, tmp_path, engine)
    _patch_messages(monkeypatch, view)
    try:
        view.cb_combo_selector.setCurrentIndex(0)
        # hide everything: the action must still cover the provider's members
        view.combo_controls.search.setText("no-such-model")
        view._on_combo_query_changed()

        changed = view._set_activation_for_provider("alpha", False)
        assert sorted(changed) == ["alpha/claude", "alpha/gpt-4"]
        assert view.current_combo.is_model_enabled("beta/gemini") is True
        assert view.current_combo.models == [
            "alpha/gpt-4", "beta/gemini", "alpha/claude"
        ]

        # alias spellings used by the context menu resolve to the same action
        assert view._set_activation_provider_key("beta", False) == ["beta/gemini"]
        assert view._set_activation_for_provider_key("beta", True) == ["beta/gemini"]
    finally:
        _teardown(view, qapp)


def test_toggle_provider_flips_all_of_its_members(qapp, tmp_path, monkeypatch):
    """E: the provider toggle is ON when any member is ON, OFF when all are."""
    engine = _Engine([{"id": "c1", "name": "C1",
                       "models": ["alpha/gpt-4", "alpha/claude", "beta/gemini"]}])
    view = _view(qapp, tmp_path, engine)
    _patch_messages(monkeypatch, view)
    try:
        view.cb_combo_selector.setCurrentIndex(0)
        changed = view._toggle_activation_provider("alpha")
        assert sorted(changed) == ["alpha/claude", "alpha/gpt-4"]
        assert view.current_combo.is_model_enabled("alpha/gpt-4") is False
        assert view.current_combo.is_model_enabled("beta/gemini") is True

        # all OFF -> toggle back ON
        changed = view._toggle_activation_provider("alpha")
        assert sorted(changed) == ["alpha/claude", "alpha/gpt-4"]
        assert view.current_combo.is_model_enabled("alpha/gpt-4") is True
    finally:
        _teardown(view, qapp)


def test_combo_context_menu_offers_provider_activation(qapp, tmp_path, monkeypatch):
    """E: the row menu exposes both provider actions with the provider's label,
    and they are wired to the per-provider API."""
    engine = _Engine([{"id": "c1", "name": "C1",
                       "models": ["alpha/gpt-4", "beta/gemini"]}])
    view = _view(qapp, tmp_path, engine)
    _patch_messages(monkeypatch, view)

    class _FakeAction:
        def __init__(self, text):
            self._text = text
            self.triggered = False

        def text(self):
            return self._text

        def setEnabled(self, enabled):
            self._enabled = enabled

        def trigger(self):
            if self._cb is not None:
                self._cb()

    class _FakeMenu:
        def __init__(self, parent=None):
            self._actions = []

        def addAction(self, text, callback=None):
            a = _FakeAction(text)
            a._cb = callback
            self._actions.append(a)
            return a

        def addSeparator(self):
            return None

        def actions(self):
            return self._actions

        def exec(self, *args, **kwargs):
            return None

    monkeypatch.setattr("PySide6.QtWidgets.QMenu", _FakeMenu)
    try:
        view.cb_combo_selector.setCurrentIndex(0)
        item = view._combo_item_by_cid["alpha/gpt-4"]
        monkeypatch.setattr(view.list_combo_models, "itemAt", lambda _pos: item)
        view._on_combo_list_context_menu(QPoint(0, 0))

        menu = view._fake_menu if hasattr(view, "_fake_menu") else None
        # the menu instance is created inside the method; recover it by
        # re-running with capture
        instances = []
        orig_init = _FakeMenu.__init__

        def _record_init(self, parent=None):
            orig_init(self, parent)
            instances.append(self)

        _FakeMenu.__init__ = _record_init
        view._on_combo_list_context_menu(QPoint(0, 0))
        menu = instances[-1]
        texts = [a.text() for a in menu.actions()]
        assert "Enable all Alpha Labs models in this combo" in texts
        assert "Disable all Alpha Labs models in this combo" in texts

        disable = next(
            a for a in menu.actions()
            if a.text() == "Disable all Alpha Labs models in this combo"
        )
        disable.trigger()
        assert view.current_combo.is_model_enabled("alpha/gpt-4") is False
        assert view.current_combo.is_model_enabled("beta/gemini") is True
        assert view.current_combo.models == ["alpha/gpt-4", "beta/gemini"]
    finally:
        _teardown(view, qapp)
