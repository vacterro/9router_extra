"""
T-33 closure — deterministic constructor-boundary regression tests.

The previous idle test constructed MainWindow() BEFORE installing its
counting client, so a startup background refresh could already have run by
then; the shared conftest no-op stub of MainWindow.refresh_all_async hid the
bug from every test in the suite.

These tests fix the test contract:

* the live boundaries are instrumented at CLASS level before MainWindow is
  instantiated (ModelDiscovery.discover_all, RouterClient.get_combos,
  ScannerWorker.run_scan, OpenCodeCatalogDiscovery.refresh/cli_cross_check);
* the conftest stub of refresh_all_async is counter-overrideen for this
  module only, so a reintroduced startup refresh is actually observable;
* security is a deterministic scripted double (no DPAPI, no env var), so
  the unlocked / trusted-silent-unlock / locked cases are reproducible.

Proven:
A  unlocked startup: zero discover_all / get_combos / probe / catalog work
B  trusted silent-unlock startup: state may become OS_VAULT, still zero work
C  manual unlock via the real SecurityDialog action: still zero work, no
   implicit refresh, no tab change
D  explicit Models Refresh after unlock: exactly one logical refresh
"""
import threading

import pytest
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from core.discovery import ModelDiscovery
from core.opencode_catalog import OpenCodeCatalogDiscovery
from core.probe import ScannerWorker
from core.router_client import RouterClient
from core.security import LOCKED, OS_VAULT, LiveAccessLockedError
from ui.main_window import MainWindow
from ui.security_ui import SecurityDialog
from ui.theme import apply_theme

# Captured at import time — before any autouse fixture can patch the class.
_REAL_REFRESH_ALL_ASYNC = MainWindow.refresh_all_async


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    apply_theme(app)
    return app


@pytest.fixture(autouse=True)
def _restore_real_startup_refresh(monkeypatch):
    """Runs after the conftest autouse stub (conftest fixtures instantiate
    first), so this module observes the REAL refresh_all_async. Without this,
    a reintroduced startup refresh would be silently swallowed."""
    monkeypatch.setattr(MainWindow, "refresh_all_async", _REAL_REFRESH_ALL_ASYNC)


class _LiveCounters:
    def __init__(self):
        self.discover_all_calls = 0
        self.get_combos_calls = 0
        self.probe_scan_calls = 0
        self.catalog_refresh_calls = 0
        self._discovery_seen = threading.Event()
        self._combos_seen = threading.Event()

    def record_discovery(self):
        self.discover_all_calls += 1
        self._discovery_seen.set()
        return []

    def record_combos(self):
        self.get_combos_calls += 1
        self._combos_seen.set()
        return []

    def wait_for_stray_live_work(self, timeout: float = 1.5) -> None:
        """Deterministic grace window: any wrongly triggered live path would
        record itself here before the zero-assertions run."""
        self._discovery_seen.wait(timeout)
        self._combos_seen.wait(timeout)


@pytest.fixture
def live_counters(monkeypatch):
    counters = _LiveCounters()

    def _discover(self, *a, **k):
        return counters.record_discovery()

    def _combos(self, *a, **k):
        return counters.record_combos()

    def _scan(self, *a, **k):
        counters.probe_scan_calls += 1

    def _catalog_refresh(self, *a, **k):
        counters.catalog_refresh_calls += 1
        return {"fresh": False, "status": "SKIPPED", "changed": False,
                "diff": None, "fetch": False, "events": []}

    def _cli_cross(self, *a, **k):
        return {"status": "SKIPPED"}

    monkeypatch.setattr(ModelDiscovery, "build_snapshot", _discover)
    monkeypatch.setattr(RouterClient, "get_combos", _combos)
    monkeypatch.setattr(ScannerWorker, "run_scan", _scan)
    monkeypatch.setattr(OpenCodeCatalogDiscovery, "refresh", _catalog_refresh)
    monkeypatch.setattr(OpenCodeCatalogDiscovery, "cli_cross_check", _cli_cross)
    return counters


class _ScriptedSecurity:
    """Deterministic SecurityManager double: fixed state machine, no DPAPI,
    no local settings file, no WATCHEDIT_LIVE_ACCESS env influence."""

    def __init__(self, state: str = LOCKED, trusted: bool = False):
        self._state = state
        self.trusted = trusted
        self._listeners = []

    # ---------------------------------------------------------- observers
    @property
    def state(self) -> str:
        return self._state

    def is_live_allowed(self) -> bool:
        return self._state in (OS_VAULT, "UNLOCKED")

    def is_locked(self) -> bool:
        return not self.is_live_allowed()

    def require_live(self, operation: str) -> None:
        if not self.is_live_allowed():
            raise LiveAccessLockedError(operation)

    def on_state_changed(self, cb) -> None:
        self._listeners.append(cb)

    def _set_state(self, new_state: str) -> None:
        if new_state == self._state:
            return
        self._state = new_state
        for cb in list(self._listeners):
            try:
                cb(new_state)
            except Exception:
                pass

    # ------------------------------------------------------------ unlock
    @property
    def trusted_os_unlock_enabled(self) -> bool:
        return self.trusted

    def set_trusted_os_unlock(self, enabled: bool) -> None:
        self.trusted = bool(enabled)
        if enabled:
            self.try_silent_unlock()

    def grant_exists(self) -> bool:
        return True

    def vault_exists(self) -> bool:
        return False

    @property
    def vault_secret_names(self):
        return []

    def try_silent_unlock(self) -> bool:
        if self.trusted and self.grant_exists():
            self._set_state(OS_VAULT)
            return True
        return False

    def unlock_os(self) -> None:
        self._set_state(OS_VAULT)

    def unlock_with_password(self, password: str) -> None:
        self._set_state("UNLOCKED")

    # ------------------------------------------------------------- lock
    def lock(self) -> None:
        self._set_state(LOCKED)

    def revoke_os_grant(self) -> None:
        self._set_state(LOCKED)


@pytest.fixture
def scripted_security(monkeypatch):
    """Installs a scripted security double as ui.main_window's
    get_default_security BEFORE MainWindow is constructed."""
    def _install(security: _ScriptedSecurity) -> _ScriptedSecurity:
        monkeypatch.setattr("ui.main_window.get_default_security", lambda: security)
        return security
    return _install


def _assert_zero_live_work(counters: _LiveCounters):
    assert counters.discover_all_calls == 0
    assert counters.get_combos_calls == 0
    assert counters.probe_scan_calls == 0
    assert counters.catalog_refresh_calls == 0


# ------------------------------------------------------------------- A
def test_a_unlocked_startup_performs_zero_live_work(qapp, live_counters, scripted_security):
    scripted_security(_ScriptedSecurity(state="UNLOCKED"))
    window = MainWindow()
    try:
        assert window.security.is_live_allowed()  # live access allowed, still idle
        live_counters.wait_for_stray_live_work()
        _assert_zero_live_work(live_counters)

        # construction leaves no background timers of any kind running
        for _ in range(10):
            qapp.processEvents()
        assert all(not t.isActive() for t in window.findChildren(QTimer))
    finally:
        window.close()


# ------------------------------------------------------------------- B
def test_b_trusted_silent_unlock_startup_performs_zero_live_work(qapp, live_counters, scripted_security):
    security = _ScriptedSecurity(state=LOCKED, trusted=True)
    security.try_silent_unlock()  # mirrors get_default_security's startup contract
    scripted_security(security)

    window = MainWindow()
    try:
        assert window.security.state == OS_VAULT  # auto-unlocked at startup
        live_counters.wait_for_stray_live_work()
        _assert_zero_live_work(live_counters)
    finally:
        window.close()


# ------------------------------------------------------------------- C
def test_c_manual_unlock_does_not_refresh_or_change_tabs(qapp, live_counters, scripted_security):
    scripted_security(_ScriptedSecurity(state=LOCKED))
    window = MainWindow()
    try:
        window.main_tabs.setCurrentIndex(1)  # user parked on Combo
        qapp.processEvents()
        assert window.security.is_locked()

        # Same construction the fixed _open_security_controls performs:
        # no refresh callback is wired to unlock anymore.
        dlg = SecurityDialog(window.security, window)
        dlg._unlock_os()  # successful unlock through the real dialog action

        assert window.security.state == OS_VAULT
        assert window.security_indicator.text() == "SECRETS: OS VAULT"

        live_counters.wait_for_stray_live_work()
        _assert_zero_live_work(live_counters)
        assert window.main_tabs.currentIndex() == 1  # no tab change
    finally:
        window.close()


# ------------------------------------------------------------------- D
def test_d_explicit_refresh_after_unlock_runs_exactly_once(qapp, live_counters, scripted_security):
    security = scripted_security(_ScriptedSecurity(state=LOCKED))
    window = MainWindow()
    try:
        security.unlock_os()
        assert window.security.is_live_allowed()

        # The explicit Models Refresh action (WatchView button signal path).
        window.watch_view.refresh_inventory_requested.emit()
        assert live_counters._discovery_seen.wait(5.0)
        assert live_counters._combos_seen.wait(5.0)

        assert live_counters.discover_all_calls == 1  # exactly one logical refresh
        assert live_counters.get_combos_calls == 1
        assert live_counters.probe_scan_calls == 0
    finally:
        window.close()
