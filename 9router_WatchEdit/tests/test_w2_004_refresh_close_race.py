"""
W2-004 regressions - close must revoke a claimed-but-not-started refresh worker.

Defect (audit/3.md W2-004): claim_follow_up could succeed, then close() ran,
then the worker thread started and immediately began discovery.build_snapshot(
query_live=True). The documented close invariant "no new refresh may start" had
a race: post-close network/discovery work began even though publication was
later blocked.

Contract under test: begin_worker is the atomic start boundary consumed under
the controller lock immediately before any discovery/live I/O. If close wins
the race, begin_worker returns False and the worker performs no work.
"""
import threading
import time

import pytest
from PySide6.QtWidgets import QApplication

from core.classification import CatalogState
from core.discovery import DiscoveredModel, ModelDiscovery
from core.refresh_controller import RefreshController
from ui.main_window import MainWindow
from ui.theme import apply_theme

_REAL_REFRESH_ALL_ASYNC = MainWindow.refresh_all_async


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    apply_theme(app)
    return app


# ---------------------------------------------------------------------------
# Controller-level: begin_worker is the atomic start boundary
# ---------------------------------------------------------------------------
def test_claim_then_close_revokes_worker_start():
    c = RefreshController(lambda: 7)
    outcome, g = c.begin_refresh()
    from core.refresh_controller import REFRESH_STARTED
    assert (outcome, g) == (REFRESH_STARTED, 7)

    # Prepare a follow-up reservation and claim it (pre-close).
    c._running = True
    c._active_generation = 7
    fu = c.finish_pass(7)  # reserved follow-up: active=7 but not running
    # Simulate the exact interleaving: close() before the worker activates.
    c.close()
    assert c.begin_worker(7) is False
    assert not c.running
    assert c.active_generation is None
    assert c.pending_follow_up is None


def test_begin_worker_allows_start_before_close():
    c = RefreshController(lambda: 1)
    c.begin_refresh()
    assert c.begin_worker(1) is True
    assert c.running


# ---------------------------------------------------------------------------
# Window-level barrier: close between claim and worker start performs no I/O
# ---------------------------------------------------------------------------
def test_close_between_claim_and_worker_start_does_no_discovery(qapp, monkeypatch):
    monkeypatch.setattr(MainWindow, "refresh_all_async", _REAL_REFRESH_ALL_ASYNC)
    window = MainWindow()
    build_calls = []

    class _Boom(Exception):
        pass

    def build_snapshot(*a, **k):
        build_calls.append(1)
        return []

    monkeypatch.setattr(window.discovery, "build_snapshot", build_snapshot)

    started = threading.Event()

    def worker_and_close():
        # Claim a pass, then close BEFORE the worker's begin_worker runs.
        window._refresh_controller.begin_refresh()
        window._refresh_controller.close()
        started.set()
        # Directly invoke the task body: begin_worker must refuse and exit.
        window._background_discovery_task(window._refresh_controller.active_generation or 999)

    t = threading.Thread(target=worker_and_close)
    t.start()
    t.join(timeout=5)
    assert started.is_set()
    time.sleep(0.1)
    assert build_calls == [], "no discovery I/O may begin after close revoked the start"
    window.close()
