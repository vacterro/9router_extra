"""
Tests for UI components (PySide6 / Qt)
"""
import pytest
from PySide6.QtWidgets import QApplication
from ui.theme import apply_theme
from ui.watch_view import WatchView
from ui.combo_editor_view import ComboEditorView
from ui.presets_view import PresetsView
from ui.inspector_panel import InspectorPanel
from ui.activity_panel import ActivityPanel
from ui.main_window import MainWindow
from core.router_client import RouterClient
from core.history import HealthCache, ModelHealthRecord
from core.combo_manager import PresetManager
from core.discovery import DiscoveredModel
from core.classification import HealthState, Confidence, CostStatus

@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    apply_theme(app)
    return app

def test_inspector_panel(qapp):
    panel = InspectorPanel()
    assert panel.btn_retest.isEnabled() is False

    rec = ModelHealthRecord(
        canonical_id="ag/gemini-3.8-flash-high",
        provider="Antigravity",
        model_id="gemini-3.8-flash-high",
        state=HealthState.FREE_USE.value,
        confidence=Confidence.LIVE.value,
        cost_status=CostStatus.FREE.value,
        latency_ms=850.0,
        status_code=200,
        reason="Success",
    )
    panel.set_model("ag/gemini-3.8-flash-high", rec)
    assert panel.btn_retest.isEnabled() is True
    assert panel.lbl_canonical_id.text() == "ag/gemini-3.8-flash-high"
    assert "200" in panel.lbl_http.text()

def test_activity_panel(qapp):
    panel = ActivityPanel()
    panel.set_scanning(True, 10)
    assert panel.btn_stop.isEnabled() is True

    panel.set_probe_started("ag/gemini-3.8-flash-high")
    assert "ag/gemini-3.8-flash-high" in panel._in_flight_probes

    panel.set_probe_pending("ag/gemini-3.8-flash-high", 5.2)
    assert panel._in_flight_probes["ag/gemini-3.8-flash-high"] == 5.2

    panel.set_probe_finished("ag/gemini-3.8-flash-high")
    assert "ag/gemini-3.8-flash-high" not in panel._in_flight_probes

    panel.set_scanning(False)
    assert panel.btn_stop.isEnabled() is False

def test_watch_view(qapp, tmp_path):
    cache = HealthCache(cache_file=tmp_path / "cache.json")
    view = WatchView(cache)

    models = [
        DiscoveredModel(
            canonical_id="ag/gemini-3.8-flash-high",
            provider_name="Antigravity",
            provider_prefix="ag",
            connection_id="",
            model_id="gemini-3.8-flash-high",
            display_name="gemini-3.8-flash-high",
        ),
        DiscoveredModel(
            canonical_id="wb/hy3",
            provider_name="WorkBuddy",
            provider_prefix="wb",
            connection_id="",
            model_id="hy3",
            display_name="hy3",
        ),
    ]
    view.set_models(models)
    assert view.table.rowCount() == 2

    # Test selection
    view.table.selectRow(0)
    assert view.table.currentRow() == 0

def test_main_window_instantiation(qapp):
    window = MainWindow()
    assert window.windowTitle().startswith("9router_WatchEdit")
    assert window.watch_view is not None
    assert window.combo_editor is not None
    assert window.activity is not None
    assert window.inspector is not None
    assert window.top_splitter is not None
    assert window.bottom_splitter is not None
    window.close()

def test_font_no_antialias(qapp):
    from PySide6.QtGui import QFont
    from ui.theme import get_app_font
    f = get_app_font(11)
    assert f.styleStrategy().value & QFont.StyleStrategy.NoAntialias.value
    assert f.styleStrategy().value & QFont.StyleStrategy.NoSubpixelAntialias.value
    assert f.styleStrategy().value & QFont.StyleStrategy.PreferBitmap.value
    assert f.hintingPreference() == QFont.HintingPreference.PreferFullHinting
    assert qapp.font().styleStrategy().value & QFont.StyleStrategy.NoAntialias.value
    assert qapp.font().hintingPreference() == QFont.HintingPreference.PreferFullHinting

def test_combo_editor_reorder_and_drag_drop(qapp, tmp_path):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QListWidgetItem
    from core.combo_manager import StableCombo

    client = RouterClient()
    cache = HealthCache(cache_file=tmp_path / "test_cache.json")
    view = ComboEditorView(client, cache)

    combo = StableCombo(combo_id="c1", name="TestCombo", models=["m1", "m2", "m3"])
    view.combos["c1"] = combo
    view.current_combo = combo
    view._refresh_combo_models_list()

    assert view.list_combo_models.count() == 3
    assert view.list_combo_models.item(0).data(Qt.UserRole) == "m1"

    # Simulate drag drop from picker
    view._on_model_dropped_from_picker("m_new", 1)
    assert view.current_combo.models == ["m1", "m_new", "m2", "m3"]
    assert view.list_combo_models.count() == 4
    assert view.list_combo_models.item(1).data(Qt.UserRole) == "m_new"

    # Simulate internal drag reorder: swap m1 and m_new
    item0 = view.list_combo_models.takeItem(0)
    view.list_combo_models.insertItem(1, item0)
    view._on_drag_order_changed()
    assert view.current_combo.models == ["m_new", "m1", "m2", "m3"]

