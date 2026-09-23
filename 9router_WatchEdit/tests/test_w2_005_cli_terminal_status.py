"""
W2-005 regressions - CLI exit/message semantics reflect the scanner terminal state.

Defect (audit/3.md W2-005): run_cli_mode discarded ScannerWorker.run_scan's
terminal status and unconditionally printed "Scan completed successfully.",
so FAILED / COMPLETED_PERSISTENCE_FAILED / CANCELLED / LOCKED or a missing
requested combo all exited 0. A genuine LOCKED boundary escaped as an uncaught
LiveAccessLockedError traceback.

Contract under test: only COMPLETED prints success and exits 0; every other
terminal state is non-zero and labelled; a missing combo is non-zero; LOCKED is
a controlled non-zero result with no traceback.
"""
import types

import pytest

import run as run_mod
from core.security import LiveAccessLockedError


def _args(**overrides):
    base = types.SimpleNamespace(
        router_url="http://127.0.0.1:99999",
        list_combos=False,
        combo=None,
        scan="quick",
    )
    base.__dict__.update(overrides)
    return base


class _FakeClient:
    def __init__(self, *a, **k):
        self.base_url = "http://127.0.0.1:99999"

    def is_server_reachable(self, timeout=3.0):
        return True

    def get_combos(self):
        return []


class _FakeDiscovery:
    def __init__(self, client):
        pass

    def discover_all(self):
        return []


class _FakeWorker:
    terminal = "COMPLETED"

    def __init__(self, client, cache):
        self.on_probe_started = None
        self.on_probe_pending = None
        self.on_probe_finished = None

    def run_scan(self, models, mode=None, target_combo_models=None):
        return _FakeWorker.terminal


class _FakeCache:
    def __init__(self, *a, **k):
        pass

    def get(self, cid):
        return None


@pytest.fixture
def cli_env(monkeypatch):
    monkeypatch.setattr(run_mod, "RouterClient", _FakeClient)
    monkeypatch.setattr(run_mod, "HealthCache", _FakeCache)
    monkeypatch.setattr(run_mod, "ModelDiscovery", _FakeDiscovery)
    monkeypatch.setattr(run_mod, "ScannerWorker", _FakeWorker)
    return monkeypatch


@pytest.mark.parametrize(
    ("terminal", "expected_exit", "expect_success"),
    [
        ("COMPLETED", 0, True),
        ("FAILED", run_mod.CLI_EXIT_FAILED, False),
        ("COMPLETED_PERSISTENCE_FAILED", run_mod.CLI_EXIT_PERSISTENCE_FAILED, False),
        ("CANCELLED", run_mod.CLI_EXIT_CANCELLED, False),
        ("LOCKED", run_mod.CLI_EXIT_LOCKED, False),
    ],
)
def test_cli_terminal_status_maps_to_exit_and_message(cli_env, capsys, terminal, expected_exit, expect_success):
    _FakeWorker.terminal = terminal
    code = run_mod.run_cli_mode(_args())
    out = capsys.readouterr().out
    assert code == expected_exit
    assert ("Scan completed successfully." in out) is expect_success
    if not expect_success:
        assert "completed successfully" not in out


def test_cli_busy_scan_is_nonzero(cli_env, capsys, monkeypatch):
    monkeypatch.setattr(_FakeWorker, "run_scan", lambda self, *a, **k: None)
    code = run_mod.run_cli_mode(_args())
    out = capsys.readouterr().out
    assert code == run_mod.CLI_EXIT_BUSY
    assert "completed successfully" not in out


def test_cli_combo_not_found_is_nonzero(cli_env, capsys, monkeypatch):
    class _ComboClient(_FakeClient):
        def get_combos(self):
            return [{"id": "c1", "name": "OTHER", "models": ["m/1"]}]

    monkeypatch.setattr(run_mod, "RouterClient", _ComboClient)
    code = run_mod.run_cli_mode(_args(combo="MISSING"))
    out = capsys.readouterr().out
    assert code == run_mod.CLI_EXIT_NOT_FOUND
    assert "completed successfully" not in out


def test_cli_locked_is_controlled_nonzero_without_traceback(cli_env, capsys, monkeypatch):
    def locked(self, timeout=3.0):
        raise LiveAccessLockedError("secrets locked")

    monkeypatch.setattr(_FakeClient, "is_server_reachable", locked)
    code = run_mod.run_cli_mode(_args())
    out = capsys.readouterr().out
    assert code == run_mod.CLI_EXIT_LOCKED
    assert "LOCKED" in out
    assert "Traceback" not in out
    assert "completed successfully" not in out


def test_main_exits_with_cli_code(cli_env, monkeypatch):
    _FakeWorker.terminal = "FAILED"
    monkeypatch.setattr(run_mod.sys, "argv", ["watchedit", "--cli", "--router-url", "http://127.0.0.1:99999"])
    with pytest.raises(SystemExit) as exc:
        run_mod.main()
    assert exc.value.code == run_mod.CLI_EXIT_FAILED
