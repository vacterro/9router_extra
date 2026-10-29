"""
Shift+Arrow reorder in the Combo editor: one modifier moves the selected
model to the span's edge instead of one row.
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import Qt, QEvent
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import QApplication, QListWidgetItem

from core.combo_manager import StableCombo
from core.history import HealthCache
from ui.combo_editor_view import ComboEditorView


class _RecordingCache(HealthCache):
    """Offline cache with a fixed snapshot."""

    def __init__(self, records=None):
        super().__init__(cache_file=os.devnull)
        self.records = records or {}

    def get(self, canonical_id):
        return self.records.get(canonical_id)


class _CountingClient:
    def __init__(self, combos):
        self.combos = combos

    def get_combos(self):
        return self.combos


@pytest.fixture
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def _make_view():
    view = ComboEditorView(
        _CountingClient([]), _RecordingCache()
    )
    combo = StableCombo("c1", "C1", ["a/b/m1", "a/b/m2", "a/b/m3", "a/b/m4"])
    view.combos = {"c1": combo}
    view.current_combo = combo
    return view, combo


def test_shift_up_moves_selected_model_to_top(qapp):
    view, combo = _make_view()
    view._refresh_combo_models_list()
    view.list_combo_models.setCurrentRow(3)
    view._move_to_top()
    assert combo.models == ["a/b/m4", "a/b/m1", "a/b/m2", "a/b/m3"]
    view.close()


def test_shift_down_moves_selected_model_to_bottom(qapp):
    view, combo = _make_view()
    view._refresh_combo_models_list()
    view.list_combo_models.setCurrentRow(0)
    view._move_to_bottom()
    assert combo.models == ["a/b/m2", "a/b/m3", "a/b/m4", "a/b/m1"]
    view.close()


def test_shift_arrow_keypress_reaches_the_handlers(qapp):
    view, combo = _make_view()
    view.show()
    qapp.processEvents()
    view._refresh_combo_models_list()
    view.list_combo_models.setCurrentRow(2)
    view.list_combo_models.setFocus()
    qapp.processEvents()

    view.keyPressEvent(QKeyEvent(QEvent.KeyPress, Qt.Key_Up, Qt.ShiftModifier))
    assert combo.models == ["a/b/m3", "a/b/m1", "a/b/m2", "a/b/m4"]

    view.close()


def test_shift_arrow_does_not_move_when_filtering(qapp):
    view, combo = _make_view()
    view._refresh_combo_models_list()
    view.list_combo_models.setCurrentRow(3)

    view.combo_controls.search.setText("m2")
    view.list_combo_models.setFocus()
    before = list(combo.models)
    event = QKeyEvent(QEvent.KeyPress, Qt.Key_Up, Qt.ShiftModifier)
    assert view._handle_shift_arrow(event) is False
    assert combo.models == before

    view.close()
