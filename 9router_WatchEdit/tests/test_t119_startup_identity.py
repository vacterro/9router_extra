"""T-119 - the startup half of the client-identity mechanism.

``apply-update.ps1`` guards the install path, but an engine can also be replaced
by a bare ``npm install -g`` or by a hand copy. The window therefore verifies the
same declaration on every launch, which is the only point that sees every
install regardless of how it was made.

These tests drive the window's method directly with the engine module patched,
so no real install is read or written.
"""
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "9router_WatchEdit"))

from core import engine_identity as ei  # noqa: E402


class _FakeWindow:
    """The only attribute _check_engine_identity touches."""

    def __init__(self):
        self.messages = []

        class _Bar:
            def showMessage(inner_self, text):
                self.messages.append(text)

        self.status_bar = _Bar()

    @property
    def last(self):
        return self.messages[-1] if self.messages else ""


def _run(monkeypatch, result=None, raises=None):
    from ui.main_window import MainWindow

    if raises is not None:
        monkeypatch.setattr(ei, "ensure", lambda *a, **k: (_ for _ in ()).throw(raises))
    else:
        monkeypatch.setattr(ei, "ensure", lambda *a, **k: result)

    window = _FakeWindow()
    MainWindow._check_engine_identity(window)
    return window


def _state(provider="workbuddy", state=ei.IDENTITY_PRESENT, **kwargs):
    return ei.IdentityState(provider, state, **kwargs)


def test_a_good_install_is_silent(monkeypatch):
    """Startup must not nag about a healthy engine."""
    window = _run(monkeypatch, {
        "outcome": "OK", "states": [_state()], "backups": [], "messages": ["ok"],
    })

    assert window.messages == []


def test_a_repaired_install_says_a_restart_is_needed(monkeypatch):
    window = _run(monkeypatch, {
        "outcome": "REPAIRED",
        "states": [_state()],
        "backups": ["C:/rollback/5330.js.bak"],
        "messages": ["workbuddy: official client identity present"],
    })

    assert "repaired" in window.last.lower()
    assert "restart" in window.last.lower()


def test_a_failed_repair_names_the_consequence_and_the_fix(monkeypatch):
    window = _run(monkeypatch, {
        "outcome": "REPAIR_FAILED",
        "states": [_state(state=ei.IDENTITY_MISSING, missing_tokens=["workbuddy-ai/"])],
        "backups": [],
        "messages": ["workbuddy: identity MISSING"],
    })

    assert "failed" in window.last.lower()
    assert "rebuild" in window.last.lower()
    assert "reject" in window.last.lower()


def test_an_absent_engine_is_not_reported_as_a_problem(monkeypatch):
    """No 9router install at all is a normal state for the WatchEdit app."""
    window = _run(monkeypatch, {
        "outcome": "UNAVAILABLE",
        "states": [_state(state=ei.IDENTITY_UNAVAILABLE, detail="no compiled engine")],
        "backups": [],
        "messages": ["workbuddy: UNKNOWN - no compiled engine"],
    })

    assert window.messages == []


def test_an_exception_in_the_check_never_blocks_startup(monkeypatch):
    """A broken checker must not stop the window from opening."""
    window = _run(monkeypatch, raises=RuntimeError("disk on fire"))

    assert "unavailable" in window.last.lower()
    assert "disk on fire" in window.last
