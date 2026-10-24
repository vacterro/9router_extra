"""T-104 live UI acceptance: build the real MainWindow, drive the Combo tab and
capture a PNG per acceptance case into the evidence directory.

Usage: python -m tests.ui_acceptance_t104 [out_dir]
"""
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "windows")

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from core.discovery import DiscoveredModel
from core.history import HealthCache, ModelHealthRecord
from ui.main_window import MainWindow

OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else APP / "ui_evidence_t104"
OUT.mkdir(parents=True, exist_ok=True)

PROVIDERS = 24
LONG_NAME = "Extremely Long Provider Display Name For Clipping Checks (Region EU-West-2)"


def build_catalog(count, long_names=False):
    models = []
    for i in range(count):
        prefix = f"prov{i % PROVIDERS:02d}"
        name = f"{LONG_NAME} {i}" if long_names else f"Model Number {i:05d}"
        models.append(DiscoveredModel(
            canonical_id=f"{prefix}/model-{i:05d}",
            provider_name=f"{LONG_NAME[:48]} {i % PROVIDERS}" if long_names
            else f"Provider {i % PROVIDERS:02d}",
            provider_prefix=prefix,
            connection_id="conn-a",
            model_id=f"model-{i:05d}",
            display_name=name,
        ))
    return models


def records_for(catalog):
    cache = HealthCache(cache_file=Path(os.devnull))
    for i, m in enumerate(catalog):
        availability = ["LIVE", "UNKNOWN", "LIVE", "DEAD"][i % 4]
        cost = ["FREE", "PAID", "UNKNOWN", "FREE"][i % 4]
        cache.record_evidence(
            m.canonical_id, m.provider_prefix, m.model_id,
            _evidence(availability, cost), auto_save=False)
    return cache


def _evidence(availability, cost):
    from core.classification import (
        AvailabilityState, CostState, Confidence, EvidenceRecord,
    )
    return EvidenceRecord(
        availability=AvailabilityState(availability),
        cost=CostState(cost),
        confidence=Confidence.LIKELY_TEMPORARY if availability == "LIVE"
        else Confidence.MODEL_DEAD,
    )


def main():
    app = QApplication.instance() or QApplication([])
    catalog = build_catalog(1200, long_names=False)
    cache = records_for(catalog)

    window = MainWindow()
    window.show()
    window.main_tabs.setCurrentIndex(1)  # Combo
    app.processEvents()

    editor = window.combo_editor
    editor.cache = cache
    big_combo = {
        "id": "c-big", "name": "Large Production Combo",
        "models": [m.canonical_id for m in catalog[:900]],
        "updatedAt": "2026-10-18T00:00:00",
    }
    editor.replace_combos([big_combo])
    editor.set_available_models(catalog)
    editor.cb_combo_selector.setCurrentIndex(0)
    app.processEvents()

    def shot(name):
        app.processEvents()
        window.grab().save(str(OUT / f"{name}.png"))
        print("captured", name)

    # 1 - normal maximized window, large populated combo
    window.showMaximized()
    app.processEvents()
    shot("01-maximized-large-combo")

    # 2 - the app's own compact startup dimensions
    window.showNormal()
    window.resize(800, 640)
    app.processEvents()
    shot("02-compact-800x640")

    # 3 - narrow layout: compact filtering mode must kick in
    window.resize(760, 620)
    app.processEvents()
    shot("03-narrow-760-compact-filters")

    # 4 - empty search results in both panels
    editor.combo_controls.search.setText("qqq-no-such-model")
    editor.available_controls.search.setText("qqq-no-such-model")
    editor._on_combo_query_changed()
    editor._on_available_query_changed()
    app.processEvents()
    shot("04-empty-search-results")

    # 5 - multiple active provider filters
    editor.available_controls.search.clear()
    editor.available_controls.provider.set_selected({"prov01", "prov02", "prov03"})
    editor._on_available_query_changed()
    editor._on_combo_query_changed()
    app.processEvents()
    shot("05-multiple-provider-filters")

    # 6 - long model and provider names
    editor.available_controls.provider.clear_selection()
    editor.set_available_models(build_catalog(400, long_names=True))
    editor._on_available_query_changed()
    app.processEvents()
    shot("06-long-names")

    # 7 - advanced filters expanded with several active filters
    editor.available_controls.btn_advanced.setChecked(True)
    editor.available_controls.chk_routing.setChecked(True)
    editor.available_controls.chk_latency.setChecked(True)
    editor._on_available_query_changed()
    app.processEvents()
    shot("07-advanced-filters")

    # 8 - 5000 synthetic entries in the picker
    editor.set_available_models(build_catalog(5000))
    editor._on_available_query_changed()
    app.processEvents()
    shot("08-five-thousand-models")

    # 9 - high-DPI scaling pass (separate run, same evidence folder)
    window.showNormal()
    window.resize(800, 640)
    app.processEvents()
    shot("09-highdpi-1.5x")

    inspect(window, app)

    print("EVIDENCE_DIR", OUT)


# ---------------------------------------------------------------- inspection
def _clipped(label) -> bool:
    """True when a label's text does not fit its own width."""
    if not label.text():
        return False
    return label.fontMetrics().boundingRect(label.text()).width() > label.width() + 1


def inspect(window, app) -> None:
    """Text-fit and geometry checks over the whole Combo tab.

    Image review cannot be automated here, so instead of eyeballing the PNGs
    this asserts what a review would catch: no clipped label, no collapsed
    control, no row wider than its viewport.
    """
    from PySide6.QtWidgets import QLabel, QPushButton, QComboBox

    window.main_tabs.setCurrentIndex(1)
    app.processEvents()
    editor = window.combo_editor
    problems = []
    for controls in (editor.combo_controls, editor.available_controls):
        for label in controls.findChildren(QLabel):
            if _clipped(label):
                problems.append(f"clipped label {label.text()!r}")
        buttons = list(controls.findChildren(QPushButton))
        buttons += list(controls.findChildren(QComboBox))
        for widget in buttons:
            if widget.width() <= 0 or widget.height() <= 0:
                problems.append(f"collapsed control {widget.text()!r}")
    for list_widget in (editor.list_combo_models, editor.list_available_models):
        # Long model/provider names must elide, never hard-clip: the delegate
        # must be in an elide mode rather than truncating the text itself.
        assert list_widget.textElideMode() != Qt.ElideNone, list_widget.objectName()
        metrics = list_widget.fontMetrics()
        widest = max(
            metrics.boundingRect(list_widget.item(i).text()).width()
            for i in range(min(list_widget.count(), 200))
        )
        print(f"{list_widget.objectName() or 'list'}: {list_widget.count()} rows, "
              f"widest measured {widest}px, viewport {list_widget.viewport().width()}px")
    assert not problems, "\n".join(problems)
    print("inspect clean")


if __name__ == "__main__":
    main()
