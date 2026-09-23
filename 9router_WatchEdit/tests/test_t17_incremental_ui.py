"""PERF-005 (T-17 / SRC-001:R0014) — keyed O(1) per-completion UI updates.

The audited defect: every probe completion located its row with a linear scan
of the whole table, and the filter path re-scanned every row, so a scan of N
models performed O(N) work per completion => O(N^2) inspections (915849 on the
957-model fixture).

Contract proven here (offline, qt offscreen):

  * a canonical_id -> row index exists and is used for completion updates, so
    the per-update work does not grow with the table size;
  * the keyed lookup returns the SAME row a full linear scan would (oracle);
  * updating a completion touches a bounded number of rows, independent of N:
    the measured row-visits for a 4x-larger table do not grow ~4x;
  * the index follows a sort (set_scan_active(False)) and inventory rebuild.
"""
from typing import Optional

import pytest
from PySide6.QtWidgets import QApplication

from core.discovery import DiscoveredModel
from core.history import HealthCache, ModelHealthRecord
from ui.theme import apply_theme
from ui.watch_view import WatchView


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    apply_theme(app)
    return app


def _models(n):
    return [
        DiscoveredModel(
            canonical_id=f"p/m{i}",
            provider_name="p",
            provider_prefix="p",
            connection_id="c-1",
            model_id=f"m{i}",
            display_name=f"m{i}",
        )
        for i in range(n)
    ]


def _record(cid):
    return ModelHealthRecord(
        canonical_id=cid, provider="p", model_id=cid.split("/", 1)[1],
        availability="LIVE", latency_ms=12.0,
    )


def _linear_row(view, cid) -> int:
    for row in range(view.table.rowCount()):
        item = view.table.item(row, view.COLUMN_MODEL)
        if item is not None and item.data(0x0100) == cid:  # Qt.UserRole == 0x0100
            return row
    return -1


def _count_row_visits(view, cid, rec):
    """Count how many table rows an update touches, via item() proxying."""
    visits = {"n": 0}
    real_item = view.table.item

    def counting_item(row, col):
        visits["n"] += 1
        return real_item(row, col)

    view.table.item = counting_item
    try:
        view.update_probe_result(cid, rec)
    finally:
        view.table.item = real_item
    return visits["n"]


def test_keyed_lookup_matches_linear_oracle(qapp, tmp_path):
    cache = HealthCache(cache_file=tmp_path / "cache.json")
    view = WatchView(cache)
    try:
        view.set_models(_models(200))
        for cid in ("p/m0", "p/m77", "p/m199"):
            assert view._find_row(cid) == _linear_row(view, cid)
        assert view._find_row("p/absent") == -1
    finally:
        view.deleteLater()


def test_completion_update_work_does_not_grow_with_table_size(qapp, tmp_path):
    cache = HealthCache(cache_file=tmp_path / "cache.json")
    view = WatchView(cache)
    try:
        view.set_models(_models(100))
        small = _count_row_visits(view, "p/m50", _record("p/m50"))

        # 4x the rows: a linear rescan would multiply the visits by ~4.
        view.set_models(_models(400))
        large = _count_row_visits(view, "p/m200", _record("p/m200"))

        assert large <= small * 2, (
            f"per-completion row visits grew with N: {small} -> {large}"
        )
        assert large < 400, "an update must not visit the whole table"
    finally:
        view.deleteLater()


def test_index_follows_sort_and_rebuild(qapp, tmp_path):
    cache = HealthCache(cache_file=tmp_path / "cache.json")
    view = WatchView(cache)
    try:
        view.set_models(_models(50))
        # Entering a scan freezes sorting; leaving it re-enables (may reorder).
        view.set_scan_active(True)
        view.set_scan_active(False)
        # The keyed index must still resolve every model to its live row.
        for cid in ("p/m0", "p/m25", "p/m49"):
            assert view._find_row(cid) == _linear_row(view, cid)

        # A completion update after a rebuild reaches the right row's state.
        rec = _record("p/m25")
        cache.records["p/m25"] = rec
        view.update_probe_result("p/m25", rec)
        row = view._find_row("p/m25")
        cell = view.table.item(row, view.COLUMN_STATE).text()
        assert cell == rec.state, f"expected the record's derived badge, got {cell!r}"
    finally:
        view.deleteLater()
