"""T-105 live UI acceptance: per-combo activation on the real Combo tab.

Drives the real MainWindow, switches models OFF with the row switch and with
the bulk button, and captures a PNG per acceptance case. Nothing is saved, so
the live router is never written to.
"""
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "windows")

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox

from core.discovery import DiscoveredModel
from core.history import HealthCache
from ui.main_window import MainWindow

OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else APP / "ui_evidence_t105"
OUT.mkdir(parents=True, exist_ok=True)

MODELS = [f"prov{i % 3:02d}/model-{i:03d}" for i in range(12)]
COMBO = {"id": "c-act", "name": "Activation Demo", "kind": "llm",
         "models": MODELS[:6], "updatedAt": "2026-10-19T00:00:00"}


def build_catalog():
    return [
        DiscoveredModel(
            canonical_id=cid,
            provider_name=f"Provider {cid.split('/')[0][-2:]}",
            provider_prefix=cid.split("/")[0],
            connection_id="conn-a",
            model_id=cid.split("/")[-1],
            display_name=cid.split("/")[-1].upper(),
        )
        for cid in MODELS
    ]


def main():
    app = QApplication.instance() or QApplication([])
    # No human is present: every message box must be non-blocking.
    QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.Yes)
    QMessageBox.information = staticmethod(lambda *a, **k: QMessageBox.Ok)
    QMessageBox.warning = staticmethod(lambda *a, **k: QMessageBox.Ok)
    QMessageBox.critical = staticmethod(lambda *a, **k: QMessageBox.Ok)
    window = MainWindow()
    window.show()
    window.main_tabs.setCurrentIndex(1)
    app.processEvents()

    editor = window.combo_editor
    editor.cache = HealthCache(cache_file=Path(os.devnull))
    editor.replace_combos([COMBO])
    editor.set_available_models(build_catalog())
    editor.cb_combo_selector.setCurrentIndex(0)
    app.processEvents()

    def shot(name):
        app.processEvents()
        window.grab().save(str(OUT / f"{name}.png"))
        print("captured", name)

    window.showMaximized()
    app.processEvents()
    shot("01-all-enabled")

    # Row switch OFF on two members: the row must read [OFF] and stay visible.
    items = editor.list_combo_models
    for index in (1, 3):
        items.item(index).setCheckState(Qt.Unchecked)
    app.processEvents()
    assert editor.current_combo.disabled == {MODELS[1], MODELS[3]}, editor.current_combo.disabled
    assert items.item(1).checkState() == Qt.Unchecked
    assert not items.item(1).isHidden()
    counts = editor.lbl_combo_model_count.text()
    assert "6 configured" in counts and "4 enabled" in counts and "2 disabled" in counts, counts
    print("counts:", counts)
    print("pool:", editor.lbl_combo_pool.text())
    shot("02-two-models-off")

    # Bulk OFF on the visible scope, with the scope preview accepted.
    editor.combo_controls.search.setText("model-00")
    editor._on_combo_query_changed()
    editor._set_activation_visible(False)
    app.processEvents()
    assert editor.current_combo.is_model_enabled(MODELS[0]) is False
    assert editor.current_combo.is_model_enabled(MODELS[1]) is False
    shot("03-bulk-off-visible")

    # Membership and routing order survive every activation edit.
    assert editor.current_combo.models == MODELS[:6], editor.current_combo.models

    # The manage surface discloses the missing combo ON/OFF switch.
    from ui.combo_editor_view import _ComboManageDialog
    dlg = _ComboManageDialog(editor.current_combo, editor)
    dlg.show()
    app.processEvents()
    assert dlg.btn_combo_off.isEnabled() is False
    assert "no combo enable/disable state" in dlg.btn_combo_off.toolTip()
    dlg.grab().save(str(OUT / "04-manage-dialog.png"))
    print("captured 04-manage-dialog")
    dlg.close()

    print("EVIDENCE_DIR", OUT)


if __name__ == "__main__":
    main()
