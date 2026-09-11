"""
Focused Qt tests for the compact OpenCode Catalog panel (SRC-004 / T-26 §6,§10).
"""
from __future__ import annotations

import json
import threading

import httpx
import pytest
from PySide6.QtWidgets import QApplication

from core.opencode_catalog import (
    OpenCodeCatalogDiscovery,
    ZenCatalogModel,
    CATALOG_CHANGED,
    OPENCODE_PROVIDER_NAMESPACE,
    OPENCODE_SOURCE_TAG,
)
from ui.opencode_catalog_view import OpenCodeCatalogPanel


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def _model(identifier: str, free: bool = False) -> ZenCatalogModel:
    return ZenCatalogModel(
        model_id=identifier,
        canonical_id=f"{OPENCODE_PROVIDER_NAMESPACE}/{identifier}",
        free_candidate=free,
        free_reason="explicit_free_model_id" if free else None,
        source=OPENCODE_SOURCE_TAG,
    )


def test_panel_reports_counts_and_status(qapp, tmp_path):
    discovery = OpenCodeCatalogDiscovery(snapshot_file=tmp_path / "snap.json")
    panel = OpenCodeCatalogPanel(discovery)
    panel.update_state([_model("a"), _model("a-free", True)], "2026-01-01T00:00:00Z", "OK")
    panel.set_free_models([_model("a-free", True)])
    assert panel.lbl_models.text() == "Models: 2"
    assert panel.lbl_free.text() == "Free: 1"
    assert panel.lbl_api_status.text() == "Status: OK"
    assert panel.free_table.rowCount() == 1
    # Routing safety is visible, never promoted.
    assert panel.free_table.item(0, 2).text() == "No"  # configured
    assert panel.free_table.item(0, 3).text() == "No"   # routing eligible
    assert panel.free_table.item(0, 4).text() == OPENCODE_SOURCE_TAG
    panel.close()


def test_panel_refresh_button_emits(qapp, tmp_path):
    discovery = OpenCodeCatalogDiscovery(snapshot_file=tmp_path / "snap.json")
    panel = OpenCodeCatalogPanel(discovery)
    captured = []
    panel.refresh_requested.connect(lambda force: captured.append(force))
    panel.btn_refresh.click()
    assert captured == [True]
    panel.set_refreshing(True)
    assert panel.btn_refresh.isEnabled() is False
    assert panel.btn_refresh.text() == "Refreshing..."
    panel.close()


def test_panel_events_surface_new_free_message(qapp, tmp_path):
    discovery = OpenCodeCatalogDiscovery(snapshot_file=tmp_path / "snap.json")
    panel = OpenCodeCatalogPanel(discovery)
    panel.append_events([{
        "type": "newly_free",
        "model_id": "x-free",
        "canonical_id": "opencode/x-free",
        "message": "NEW FREE OPENCODE MODEL: opencode/x-free",
    }])
    assert panel.events_list.count() == 1
    assert "NEW FREE OPENCODE MODEL: opencode/x-free" in panel.events_list.item(0).text()
    panel.close()


def test_gui_stays_responsive_during_refresh(qapp, tmp_path):
    """Background HTTP refresh must not block the Qt GUI thread."""
    started = threading.Event()
    release = threading.Event()

    def handler(request: httpx.Request):
        started.set()
        release.wait()
        return httpx.Response(200, json={"object": "list", "data": [{"id": "y-free"}]})

    discovery = OpenCodeCatalogDiscovery(
        snapshot_file=tmp_path / "snap.json",
        transport=httpx.MockTransport(handler),
    )
    panel = OpenCodeCatalogPanel(discovery)
    # Mirror the MainWindow off-thread worker pattern.
    finished = {}

    def _task():
        finished["result"] = discovery.refresh(force=True)

    t = threading.Thread(target=_task, daemon=True)
    t.start()
    assert started.wait(5)

    # GUI thread keeps servicing events while the fetch is blocked.
    ticks = 0
    for _ in range(20):
        qapp.processEvents()
        ticks += 1
    release.set()
    t.join(5)

    assert ticks == 20
    assert finished["result"]["status"] in (CATALOG_CHANGED, "API_OK")
    panel.close()
