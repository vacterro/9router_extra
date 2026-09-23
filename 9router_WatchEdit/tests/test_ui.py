"""
Tests for UI components (PySide6 / Qt)
"""
import pytest
from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QDialogButtonBox, QTextEdit
from ui.theme import apply_theme
from ui.watch_view import WatchView
from ui.combo_editor_view import ComboEditorView, DiffConfirmDialog
from ui.presets_view import PresetsView
from ui.inspector_panel import InspectorPanel
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

def test_activity_panel_removed(qapp):
    # T-33: the tall ActivityPanel was replaced by the compact scan footer
    import os
    assert not os.path.exists(os.path.join(os.path.dirname(__file__), "..", "ui", "activity_panel.py"))


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
    # T-33: one top-level task workspace, no legacy vertical split surfaces.
    # FREE-FALLBACK-001 adds exactly one FINAL tab (provider control plane).
    assert window.main_tabs.count() == 4
    assert [window.main_tabs.tabText(i) for i in range(4)] == [
        "Models", "Combo", "OpenCode", "FREE Fallback"
    ]
    assert window.main_tabs.currentIndex() == 0
    assert not hasattr(window, "activity")
    assert not hasattr(window, "inspector")
    assert not hasattr(window, "vertical_splitter")
    assert not hasattr(window, "bottom_tabs")
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

def test_diff_confirm_dialog_controls(qapp):
    from core.combo_manager import ComboDiff

    dialog = DiffConfirmDialog(ComboDiff(added=[("provider/model", 0)]))
    dialog.show()
    qapp.processEvents()

    buttons = dialog.findChild(QDialogButtonBox)
    text = dialog.findChild(QTextEdit)
    apply_button = buttons.button(QDialogButtonBox.Ok)
    cancel_button = buttons.button(QDialogButtonBox.Cancel)

    assert buttons is not None
    assert dialog.layout().indexOf(buttons) >= 0
    assert buttons.parentWidget() is dialog
    assert apply_button.text() == "Apply Changes"
    assert apply_button.isVisible()
    assert cancel_button.isVisible()
    assert text.isVisible()
    assert "provider/model" in text.toPlainText()

    QTest.mouseClick(apply_button, Qt.LeftButton)
    assert dialog.result() == QDialog.Accepted
    dialog.close()
    dialog.deleteLater()

    dialog = DiffConfirmDialog(ComboDiff(removed=["provider/model"]))
    dialog.show()
    qapp.processEvents()
    QTest.mouseClick(dialog.findChild(QDialogButtonBox).button(QDialogButtonBox.Cancel), Qt.LeftButton)
    assert dialog.result() == QDialog.Rejected
    dialog.close()
    dialog.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    qapp.processEvents()


def test_combo_editor_save_apply_and_cancel(qapp, tmp_path, monkeypatch):
    from core.combo_manager import StableCombo
    from ui import combo_editor_view

    class FakeClient:
        def __init__(self):
            self.models = ["existing/model"]
            self.updated_at = "before"
            self.update_calls = []

        def get_combos(self):
            return [{
                "id": "c1",
                "name": "TestCombo",
                "kind": None,
                "models": list(self.models),
                "updatedAt": self.updated_at,
            }]

        def update_combo(self, combo_id, name, models, kind=None):
            self.update_calls.append((combo_id, name, list(models), kind))
            self.models = list(models)
            self.updated_at = "after"
            return {"id": combo_id, "name": name, "models": list(models)}

    client = FakeClient()
    cache = HealthCache(cache_file=tmp_path / "test_cache.json")
    view = ComboEditorView(client, cache)
    combo = StableCombo(
        combo_id="c1",
        name="TestCombo",
        models=["existing/model", "cline/cline-free/muse-spark-1.3-contributor"],
        baseline_models=["existing/model"],
        baseline_updated_at="before",
    )
    view.current_combo = combo
    messages = []
    monkeypatch.setattr(combo_editor_view.QMessageBox, "information", lambda *args: messages.append(args[1:]))
    monkeypatch.setattr(combo_editor_view.DiffConfirmDialog, "exec", lambda self: QDialog.Accepted)

    view.save_current_combo()

    assert client.update_calls == [(
        "c1",
        "TestCombo",
        ["existing/model", "cline/cline-free/muse-spark-1.3-contributor"],
        None,
    )]
    assert client.get_combos()[0]["models"] == combo.models
    assert combo.has_unsaved_changes() is False
    assert messages and messages[-1][0] == "Verified & Saved"

    combo.add_model("another/model")
    monkeypatch.setattr(combo_editor_view.DiffConfirmDialog, "exec", lambda self: QDialog.Rejected)
    view.save_current_combo()
    assert len(client.update_calls) == 1
    view.close()
    view.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    qapp.processEvents()


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

