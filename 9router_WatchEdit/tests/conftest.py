"""
Shared pytest configuration.

The default unit suite runs with WATCHEDIT_LIVE_ACCESS=1 so legacy unit tests
that exercise RouterClient network methods against MockTransport / monkeypatched
fakes keep working without UI unlock. Tests that specifically verify the
LOCKED security boundary must monkeypatch.deleteenv("WATCHEDIT_LIVE_ACCESS").

Integration tests (pytest -m integration) use the same env var to unlock live
access on trusted machines — explicit, documented, never default in code.
"""
import pytest


@pytest.fixture(autouse=True)
def _allow_live_for_unit_tests(monkeypatch):
    monkeypatch.setenv("WATCHEDIT_LIVE_ACCESS", "1")
    yield
