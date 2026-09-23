"""
T-33 Predictable Compact UI - deterministic PySide6 tests.

Covers the T-33 acceptance map:
A  exactly Models|Combo|OpenCode task tabs
B  Models selected on fresh launch
C  OpenCode panel never visible while Combo is active
D  combo controls survive the tab refactor
E  selection alone neither resizes the workspace nor opens dialogs
F  no OpenCode auto-refresh while idle with a persisted snapshot
G  no Combo get_combos polling merely because seconds elapsed
H  explicit Refresh catalog performs exactly one refresh request
I  explicit Combo Reload performs one server reload
J  save with external conflict preserves local dirty state until resolved
K  Save disabled when clean, enabled when dirty
L  delete requires a named-combo confirmation
M  Stop scan visible and disabled while idle
N  scan status updates never change top-level layout geometry
O  duplicate bottom_widget insertion no longer exists
P/Q/R  existing lock / catalog-schema / combo-CRUD suites stay green
       (run as part of the full suite; not duplicated here)
"""
import time

import httpx
import pytest
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox

from core.history import HealthCache, ModelHealthRecord
from core.combo_manager import StableCombo
from ui.theme import apply_theme
from ui.watch_view import WatchView
from ui.combo_editor_view import ComboEditorView, ConflictDialog
from ui.main_window import MainWindow
from ui.opencode_catalog_view import OpenCodeCatalogPanel
from core.opencode_catalog import OpenCodeCatalogDiscovery
from core.discovery import DiscoveredModel


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    apply_theme(app)
    return app


class _CountingTransport(httpx.BaseTransport):
    """Counts every catalog HTTP request that leaves the UI."""

    def __init__(self):
        self.requests = 0

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.requests += 1
        return httpx.Response(200, json={"object": "list", "data": [{"id": "x-free"}]})


class _FakeComboClient:
    """Offline combo server double with call counters."""

    def __init__(self, combos=None, updated_at="before"):
        self.combos = combos if combos is not None else [{
            "id": "c1", "name": "SAIFREN", "kind": None,
            "models": ["prov/a"], "updatedAt": updated_at,
        }]
        self.get_calls = 0
        self.update_calls = []
        self.delete_calls = []
        self.updated_at = updated_at

    def get_combos(self):
        self.get_calls += 1
        return [{
            "id": c["id"], "name": c["name"], "kind": c["kind"],
            "models": list(c["models"]), "updatedAt": self.updated_at,
        } for c in self.combos]

    def update_combo(self, combo_id, name, models, kind=None):
        self.update_calls.append((combo_id, name, list(models), kind))
        self.updated_at = "after"
        for c in self.combos:
            if c["id"] == combo_id:
                c["models"] = list(models)
        return {"id": combo_id}

    def delete_combo(self, combo_id):
        self.delete_calls.append(combo_id)
        self.combos = [c for c in self.combos if c["id"] != combo_id]
        return True


def _model(cid: str) -> DiscoveredModel:
    prefix, _, mid = cid.partition("/")
    return DiscoveredModel(
        canonical_id=cid,
        provider_name=prefix,
        provider_prefix=prefix,
        connection_id="",
        model_id=mid or cid,
        display_name=mid or cid,
    )


# --------------------------------------------------------------- A, B, O
def test_workspace_is_exactly_three_task_tabs_models_first(qapp):
    window = MainWindow()
    try:
        # FREE-FALLBACK-001 adds exactly one FINAL main tab; the original three
        # task surfaces keep their identity and order.
        assert window.main_tabs.count() == 4
        assert [window.main_tabs.tabText(i) for i in range(4)] == [
            "Models", "Combo", "OpenCode", "FREE Fallback"
        ]
        # B: fresh launch always lands on Models
        assert window.main_tabs.currentIndex() == 0
        assert window.main_tabs.currentWidget() is window.models_tab
        # O: the legacy all-in-one splitter with duplicated bottom insertion is gone
        for legacy in ("vertical_splitter", "bottom_widget", "bottom_tabs", "activity", "inspector"):
            assert not hasattr(window, legacy)
    finally:
        window.close()


# ------------------------------------------------------------------- C
def test_opencode_panel_not_visible_while_combo_active(qapp):
    window = MainWindow()
    try:
        window.show()
        qapp.processEvents()

        window.main_tabs.setCurrentIndex(1)  # Combo
        qapp.processEvents()
        assert window.combo_editor.isVisible()
        assert not window.opencode_catalog_panel.isVisible()
        assert not window.opencode_tab.isVisible()

        window.main_tabs.setCurrentIndex(0)  # Models
        qapp.processEvents()
        assert window.watch_view.isVisible()
        assert not window.opencode_catalog_panel.isVisible()
    finally:
        window.hide()
        window.close()


# ------------------------------------------------------------------- D
def test_combo_controls_survive_tab_refactor(qapp, tmp_path):
    view = ComboEditorView(_FakeComboClient(), HealthCache(cache_file=tmp_path / "c.json"))
    for attr in (
        "cb_combo_selector", "lbl_combo_model_count", "lbl_dirty_state", "btn_save",
        "btn_new_combo", "btn_rename", "btn_duplicate", "btn_revert", "btn_reload",
        "btn_manage", "btn_tools", "list_combo_models", "btn_move_up", "btn_move_down",
        "btn_remove", "list_available_models", "cb_picker_filter", "txt_picker_search",
        "btn_add_to_combo", "btn_retest_combo",
    ):
        assert getattr(view, attr) is not None, attr
    # Picker filter is one labeled dropdown with the T-33 option set
    assert [view.cb_picker_filter.itemText(i) for i in range(view.cb_picker_filter.count())] == [
        "Usable", "Free", "Paid", "Untested", "Attention", "Dead", "All"
    ]
    view.close()


# ------------------------------------------------------------------- E
def test_model_selection_neither_resizes_workspace_nor_opens_dialogs(qapp):
    window = MainWindow()
    try:
        window.show()
        qapp.processEvents()
        window.watch_view.set_models([_model("ag/m1"), _model("wb/m2")])
        qapp.processEvents()

        tabs_geom = window.main_tabs.geometry()
        window_size = window.size()

        window.watch_view.table.selectRow(0)
        qapp.processEvents()

        assert window.main_tabs.geometry() == tabs_geom
        assert window.size() == window_size
        assert window._details_dialog is None  # selection alone opens nothing
        assert QApplication.activeModalWidget() is None
    finally:
        window.hide()
        window.close()


def test_details_open_explicitly_only(qapp):
    window = MainWindow()
    try:
        window.show()
        qapp.processEvents()
        window.watch_view.set_models([_model("ag/m1")])
        window.watch_view.table.selectRow(0)
        qapp.processEvents()
        assert window._details_dialog is None

        # explicit action (double-click signal path)
        window.watch_view.details_requested.emit("ag/m1")
        qapp.processEvents()
        assert window._details_dialog is not None
        assert window._details_dialog.isVisible()
        assert window._details_dialog.inspector._current_canonical_id == "ag/m1"

        tabs_geom = window.main_tabs.geometry()
        window.watch_view.table.selectRow(0)
        qapp.processEvents()
        assert window.main_tabs.geometry() == tabs_geom
    finally:
        window.hide()
        window.close()


# ---------------------------------------------------------------- F, G
def test_idle_app_does_no_background_work(qapp, tmp_path):
    transport = _CountingTransport()
    fake = _FakeComboClient()

    window = MainWindow()
    try:
        window.opencode_catalog = OpenCodeCatalogDiscovery(
            snapshot_file=tmp_path / "snap.json", transport=transport,
        )
        window.client = fake
        window.combo_editor.client = fake
        window._apply_catalog_state()

        # Simulate >5 idle seconds (the old combo poll interval) with event processing
        for _ in range(55):
            qapp.processEvents()
            time.sleep(0.1)

        assert transport.requests == 0  # F: no catalog auto-refresh
        assert fake.get_calls == 0      # G: no 5s combo polling
        timers = window.findChildren(QTimer)
        assert all(not t.isActive() for t in timers)
    finally:
        window.close()


# ------------------------------------------------------------------- H
def test_explicit_catalog_refresh_performs_exactly_one_request(qapp, tmp_path, monkeypatch):
    transport = _CountingTransport()
    window = MainWindow()
    try:
        window.opencode_catalog = OpenCodeCatalogDiscovery(
            snapshot_file=tmp_path / "snap.json", transport=transport,
        )
        monkeypatch.setattr(window.opencode_catalog, "cli_cross_check",
                            lambda: {"status": "CLI_MISSING"})
        window._apply_catalog_state()

        window._refresh_opencode_catalog_async(force=True)
        assert window.opencode_catalog_panel.btn_refresh.text() == "Refreshing..."

        deadline = time.time() + 5
        while window._catalog_refresh_running and time.time() < deadline:
            qapp.processEvents()
            time.sleep(0.02)
        qapp.processEvents()

        assert not window._catalog_refresh_running
        assert transport.requests == 1
        assert window.opencode_catalog_panel.btn_refresh.isEnabled()
        assert window.opencode_catalog_panel.lbl_models.text() == "Models: 1"
        assert window.opencode_catalog_panel.lbl_api_status.text() == "Status: OK"
    finally:
        window.close()


# ------------------------------------------------------------------- I
def test_explicit_combo_reload_single_server_call(qapp, tmp_path, monkeypatch):
    fake = _FakeComboClient()
    view = ComboEditorView(fake, HealthCache(cache_file=tmp_path / "c.json"))
    view.load_combos_from_9router()
    fake.get_calls = 0

    # clean combo: exactly one server reload, selection preserved
    view._reload_current_from_server()
    assert fake.get_calls == 1
    assert view.current_combo.id == "c1"

    # dirty combo: reload never discards silently
    view.current_combo.add_model("prov/b")
    view._refresh_combo_models_list()
    answers = []

    def _refuse(parent, title, text, *a, **k):
        answers.append(text)
        return QMessageBox.No

    monkeypatch.setattr(combo_editor_question_target(), "question", _refuse)
    view._reload_current_from_server()
    assert answers and 'SAIFREN' in answers[0]
    assert fake.get_calls == 1  # refused: no reload happened
    assert view.current_combo.has_unsaved_changes()

    monkeypatch.setattr(
        combo_editor_question_target(), "question",
        lambda *a, **k: QMessageBox.Yes,
    )
    view._reload_current_from_server()
    assert fake.get_calls == 2  # exactly one more server call
    assert not view.current_combo.has_unsaved_changes()
    assert view.current_combo.models == ["prov/a"]
    view.close()


# ------------------------------------------------- T-33 closure: first load
def test_combo_reload_without_current_combo_loads_once_and_populates(qapp, tmp_path):
    """Clean startup: no current combo yet — explicit Reload performs exactly
    one get_combos call, populates the selector and selects the first combo
    deterministically. No discovery, no hidden refresh (one call proves it)."""
    fake = _FakeComboClient()
    view = ComboEditorView(fake, HealthCache(cache_file=tmp_path / "c.json"))
    assert view.current_combo is None

    view._reload_current_from_server()

    assert fake.get_calls == 1
    assert view.current_combo is not None
    assert view.current_combo.id == "c1"
    assert view.cb_combo_selector.count() == 1
    assert view.cb_combo_selector.currentText().startswith("SAIFREN")
    assert not view.current_combo.has_unsaved_changes()
    view.close()


def test_combo_reload_without_server_combos_stable_empty_state(qapp, tmp_path):
    """Empty server list is a stable 'No combos' state, not an error, and
    New stays usable."""
    fake = _FakeComboClient(combos=[])
    view = ComboEditorView(fake, HealthCache(cache_file=tmp_path / "c.json"))

    view._reload_current_from_server()

    assert fake.get_calls == 1
    assert view.current_combo is None
    assert view.lbl_combo_count.text() == "No combos"
    assert view.btn_new_combo.isEnabled()
    view.close()


def combo_editor_question_target():
    from ui import combo_editor_view
    return combo_editor_view.QMessageBox


# ------------------------------------------------------------------- J
def test_save_conflict_preserves_local_dirty_until_resolved(qapp, tmp_path, monkeypatch):
    from ui import combo_editor_view

    # Server moved on while we hold dirty local edits
    fake = _FakeComboClient(updated_at="externally-changed")
    view = ComboEditorView(fake, HealthCache(cache_file=tmp_path / "c.json"))
    view.load_combos_from_9router()
    # simulate a baseline captured before the external edit
    view.current_combo.baseline_updated_at = "before"
    view.current_combo.add_model("prov/local-edit")
    view._refresh_combo_models_list()
    local_models = list(view.current_combo.models)
    assert view.current_combo.has_unsaved_changes()

    # Cancel: save aborted, local dirty state fully preserved
    monkeypatch.setattr(ConflictDialog, "exec", lambda self: QDialog.Rejected)
    view.save_current_combo()
    assert fake.update_calls == []
    assert view.current_combo.has_unsaved_changes()
    assert view.current_combo.models == local_models

    # Keep Server is an explicit user choice; local edits then yield
    def _keep_server(self):
        self.action_choice = ConflictDialog.KEEP_SERVER
        return QDialog.Accepted

    monkeypatch.setattr(ConflictDialog, "exec", _keep_server)
    view.save_current_combo()
    assert fake.update_calls == []
    assert not view.current_combo.has_unsaved_changes()
    assert view.current_combo.models == ["prov/a"]
    view.close()


# ------------------------------------------------------------------- K
def test_save_disabled_clean_enabled_dirty(qapp, tmp_path, monkeypatch):
    fake = _FakeComboClient()
    view = ComboEditorView(fake, HealthCache(cache_file=tmp_path / "c.json"))
    view.load_combos_from_9router()

    assert view.btn_save.isEnabled() is False
    assert view.lbl_dirty_state.text() == "Saved"

    view.add_model_to_current("prov/new-model")
    assert view.btn_save.isEnabled() is True
    assert view.lbl_dirty_state.text() == "Unsaved changes"

    monkeypatch.setattr(combo_editor_question_target(), "information", lambda *a: None)
    view.btn_revert.click()
    assert view.btn_save.isEnabled() is False
    assert view.lbl_dirty_state.text() == "Saved"
    view.close()


# ------------------------------------------------------------------- L
def test_delete_requires_named_combo_confirmation(qapp, tmp_path, monkeypatch):
    fake = _FakeComboClient()
    view = ComboEditorView(fake, HealthCache(cache_file=tmp_path / "c.json"))
    view.load_combos_from_9router()

    asked = []
    monkeypatch.setattr(
        combo_editor_question_target(), "question",
        lambda *a, **k: asked.append(a[2]) or QMessageBox.No,
    )
    view._delete_combo()
    assert len(asked) == 1
    assert asked[0] == 'Delete combo "SAIFREN" from 9Router?'
    assert fake.delete_calls == []  # No means no

    monkeypatch.setattr(
        combo_editor_question_target(), "question",
        lambda *a, **k: QMessageBox.Yes,
    )
    view._delete_combo()
    assert fake.delete_calls == ["c1"]
    view.close()


# ------------------------------------------------------------------- M, N
def test_stop_scan_visible_disabled_while_idle_and_geometry_stable_during_scan(qapp):
    window = MainWindow()
    try:
        window.show()
        qapp.processEvents()

        # M: footer exists, idle, stop visible but disabled
        assert window.scan_footer.btn_stop.isVisibleTo(window)
        assert window.scan_footer.btn_stop.isEnabled() is False
        assert window.scan_footer.status_label.text() == "Ready / Idle"

        tabs_geom = window.main_tabs.geometry()
        window_size = window.size()
        footer_height = window.scan_footer.height()

        # N: a burst of scan progress updates must not move the layout
        # (W2-001: callbacks carry the owning session id; claim a session
        # so the burst targets the current accepted session).
        session_id = window.worker.try_start_session()
        assert session_id is not None
        record = ModelHealthRecord(canonical_id="ag/m1", provider="ag", model_id="m1")
        window._on_probe_started(session_id, "ag/m1")
        window._on_progress(session_id, 0, 129)
        window._on_probe_pending(session_id, "ag/m1", 1.0)
        window._on_progress(session_id, 41, 129)
        window._on_probe_finished(session_id, "ag/m1", record)
        qapp.processEvents()

        assert window.main_tabs.geometry() == tabs_geom
        assert window.size() == window_size
        assert window.scan_footer.height() == footer_height
        assert "Scanning 41 / 129" in window.scan_footer.status_label.text()

        window._on_scan_completed(session_id, "COMPLETED")
        qapp.processEvents()
        assert window.scan_footer.btn_stop.isEnabled() is False
        assert window.scan_footer.status_label.text() == "Ready / Idle"
    finally:
        window.hide()
        window.close()


# ------------------------------------------------- state preservation extras
def test_explicit_refresh_preserves_selection_and_dirty_combo(qapp, tmp_path):
    window = MainWindow()
    try:
        window.watch_view.set_models([_model("ag/m1"), _model("wb/m2")])
        window.watch_view.table.selectRow(1)

        fake = _FakeComboClient()
        window.client = fake
        window.combo_editor.client = fake
        window.combo_editor.load_combos_from_9router()
        window.combo_editor.current_combo.add_model("prov/local-edit")
        window.combo_editor._refresh_combo_models_list()
        local_models = list(window.combo_editor.current_combo.models)

        # an explicit refresh re-delivers inventory + combos
        window._on_discovery_finished([_model("ag/m1"), _model("wb/m2"), _model("ag/m3")])
        window._on_combos_loaded(fake.get_combos())

        # model selection survives
        assert window.watch_view._selected_cid() == "wb/m2"
        # dirty combo keeps local edits and selection, was not overwritten
        assert window.combo_editor.current_combo.id == "c1"
        assert window.combo_editor.current_combo.has_unsaved_changes()
        assert window.combo_editor.current_combo.models == local_models

        # a clean combo survives with the same selection too
        window.combo_editor.current_combo.revert_unsaved_changes()
        window._on_combos_loaded(fake.get_combos())
        assert window.combo_editor.current_combo.id == "c1"
        assert window.main_tabs.currentIndex() == 0  # never auto-switched tabs
    finally:
        window.close()


def test_combo_editor_has_no_polling_timer(qapp, tmp_path):
    view = ComboEditorView(_FakeComboClient(), HealthCache(cache_file=tmp_path / "c.json"))
    assert not hasattr(view, "change_detection_timer")
    assert view.findChildren(QTimer) == []
    view.close()


def test_opencode_status_maps_failed_to_error(qapp, tmp_path):
    discovery = OpenCodeCatalogDiscovery(snapshot_file=tmp_path / "snap.json")
    panel = OpenCodeCatalogPanel(discovery)
    panel.update_state([], "", "FAILED")
    assert panel.lbl_api_status.text() == "Status: ERROR"
    panel.update_state([], "", "OK")
    assert panel.lbl_api_status.text() == "Status: OK"
    panel.update_state([], "", "STALE")
    assert panel.lbl_api_status.text() == "Status: STALE"
    # events stay behind the explicit View events... surface
    panel.append_events([{"type": "newly_free", "canonical_id": "opencode/x",
                          "message": "NEW FREE OPENCODE MODEL: opencode/x"}])
    assert panel.events_list.count() == 1
    assert not panel.events_list.isVisible()
    panel.close()
