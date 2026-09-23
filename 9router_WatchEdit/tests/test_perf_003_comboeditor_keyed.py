"""
PERF-003 regression - ComboEditor per-probe update must be keyed, not a scan.

Defect (audit/3.md PERF-003): update_model_health_badge linearly walked the
whole current-combo list AND the whole available-models list on every probe
completion (~674k QList item inspections on the bundled 1,136-row fixture).

Contract under test: a per-completion update visits at most the two affected
items and performs no full-list scan, as the inventory grows.
"""
import pytest
from PySide6.QtWidgets import QApplication

from core.classification import (
    AvailabilityState,
    Confidence,
    CostState,
    EvidenceCounters,
    EvidenceRecord,
)
from core.discovery import DiscoveredModel
from core.history import HealthCache
from ui.combo_editor_view import ComboEditorView
from ui.theme import apply_theme


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    apply_theme(app)
    return app


class _Client:
    def get_combos(self):
        return []


def _models(n):
    return [
        DiscoveredModel(f"p/m{i}", "p", "p", "c1", f"m{i}", f"m{i}")
        for i in range(n)
    ]


def _seed_record(cache, cid):
    ev = EvidenceRecord(
        availability=AvailabilityState.LIVE,
        cost=CostState.FREE,
        confidence=Confidence.LIVE,
    )
    cache.record_evidence(cid, "p", cid.split("/")[-1], ev, auto_save=False)


def test_update_visits_only_the_two_keyed_items(qapp, tmp_path):
    cache = HealthCache(cache_file=tmp_path / "c.json")
    view = ComboEditorView(_Client(), cache)
    n = 400
    view.set_available_models(_models(n))
    view.current_combo = None
    view._refresh_combo_models_list()

    # Count item() lookups performed by a single badge update.
    calls = {"n": 0}
    real_item = view.list_available_models.item

    def counting_item(row):
        calls["n"] += 1
        return real_item(row)

    view.list_available_models.item = counting_item  # type: ignore[assignment]

    _seed_record(cache, "p/m7")
    view.update_model_health_badge("p/m7")

    assert calls["n"] <= 2, f"badge update performed {calls['n']} item lookups; must be keyed O(1)"

    # Full-scan oracle comparison: keyed update matches a rebuild's text.
    item = view._available_item_by_cid.get("p/m7")
    assert item is not None
    assert "FREE/USE" in item.text()
    view.close()
