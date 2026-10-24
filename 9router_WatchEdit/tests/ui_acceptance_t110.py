"""T-110 live UI acceptance: upstream attribution + combo failover diagnostics.

Drives the real MainWindow, seeds a local health history, and captures the
failover summary in the left header plus the Upstream diagnostics surface.
Nothing is saved and nothing is written to the live router: the diagnostics
are read-only by construction.
"""
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "windows")

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))

from PySide6.QtWidgets import QApplication, QMessageBox

from core.discovery import DiscoveredModel
from core.history import HealthCache
from core.upstream_attribution import attribute_upstream
from ui.combo_editor_view import _UpstreamDiagnosticsDialog
from ui.main_window import MainWindow

OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else APP / "ui_evidence_t110"
OUT.mkdir(parents=True, exist_ok=True)

MODELS = [f"prov{i % 3:02d}/model-{i:03d}" for i in range(12)]
COMBO = {"id": "c-fail", "name": "Failover Demo", "kind": "llm",
         "models": MODELS[:4], "updatedAt": "2026-10-19T00:00:00"}


class _HealthStub:
    """Minimal health record for the acceptance run (no probing, no writes)."""

    def __init__(self, availability, status_code=0, reason=""):
        self.availability = availability
        self.status_code = status_code
        self.reason = reason
        self.note = ""
        self.last_tested_at = "2026-10-19T00:00:00"


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
    cache = HealthCache(cache_file=Path(os.devnull))
    editor.cache = cache
    editor.replace_combos([COMBO])
    editor.set_available_models(build_catalog())
    editor.cb_combo_selector.setCurrentIndex(0)
    app.processEvents()

    # Local health history: one member is LIVE, one is DEAD, one is untested.
    cache.records[MODELS[0]] = _HealthStub("LIVE")
    cache.records[MODELS[1]] = _HealthStub("DEAD", status_code=500, reason="upstream 500")
    editor._refresh_combo_models_list()
    app.processEvents()

    summary = editor.lbl_failover.text()
    print("failover summary:", summary)
    assert "4 published" in summary, summary
    assert f"serving {MODELS[0]}" in summary, summary
    assert f"{MODELS[1]} fails over" in summary, summary
    assert "-> " + MODELS[2] in summary, summary

    # An unsaved draft is named as such and never presented as published.
    editor.current_combo.set_model_enabled(MODELS[3], False)
    editor._refresh_combo_models_list()
    app.processEvents()
    draft_summary = editor.lbl_failover.text()
    print("failover summary (unsaved draft):", draft_summary)
    assert "4 published" in draft_summary, draft_summary
    assert "draft 3 enabled (unsaved)" in draft_summary, draft_summary
    editor.current_combo.set_model_enabled(MODELS[3], True)
    editor._refresh_combo_models_list()
    app.processEvents()

    def shot(name):
        app.processEvents()
        window.grab().save(str(OUT / f"{name}.png"))
        print("captured", name)

    window.showMaximized()
    app.processEvents()
    shot("01-failover-summary")

    # The engine state after a verified save: the third member is configured
    # but disabled, so it is NOT published and never enters the chain.
    # mark_clean() is what a verified save does, so the view sees the engine's
    # own list instead of an unsaved draft.
    combo = editor.current_combo
    combo.set_model_enabled(MODELS[2], False)
    combo.mark_clean()
    editor._refresh_combo_models_list()
    app.processEvents()
    summary = editor.lbl_failover.text()
    print("failover summary (one disabled):", summary)
    assert "3 published" in summary, summary
    assert "1 disabled" in summary, summary
    assert "unsaved" not in summary, summary
    shot("02-disabled-member-not-published")

    # The upstream attribution is read-only: reachability, version and owner.
    attribution = attribute_upstream(editor.client)
    print("attribution:", attribution)

    report = editor._failover_report()
    dlg = _UpstreamDiagnosticsDialog(attribution, report, editor)
    dlg.show()
    app.processEvents()
    body = dlg.members_text.toPlainText()
    assert "Published members: 3" in body, body
    assert MODELS[2] in body, body
    assert "not published" in body, body
    dlg.grab().save(str(OUT / "03-upstream-diagnostics.png"))
    print("captured 03-upstream-diagnostics")
    dlg.close()

    print("EVIDENCE_DIR", OUT)


if __name__ == "__main__":
    main()
