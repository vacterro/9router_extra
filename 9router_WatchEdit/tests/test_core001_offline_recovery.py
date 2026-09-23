"""
CORE-001 regressions — emergency offline recovery must be fail-closed.

Two independent defects are pinned here:

A. Offline proof. API/HTTP unreachability is NOT proof that the 9Router
   runtime/database owner is stopped. The recovery boundary requires positive
   process/runtime ownership evidence; LIVE and UNKNOWN both refuse and leave
   the database untouched.

B. Backup consistency. A committed WAL transaction may exist only in
   `data.sqlite-wal`, so raw file copying can produce a recovery backup that
   silently misses committed rows. The production backup uses SQLite's online
   backup API and validates the result before any mutation.

All tests are hermetic: no real 9Router instance is required. The runtime
ownership probe and HTTP reachability check are injected per test.
"""
import sqlite3
from pathlib import Path

import pytest

from core import router_client
from core.router_client import (
    OfflineRecoveryRefused,
    OfflineState,
    RouterClient,
)

BASE_URL = "http://127.0.0.1:99999"


def _make_wal_db(db_path: Path, rows=("c1", "A")):
    """Create a WAL-mode db and keep the writer connection open.

    Keeping the connection open prevents the last-connection checkpoint, so the
    committed row stays represented through the active WAL file (the exact
    condition that defeats raw main-file copying).
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        "CREATE TABLE combos (id TEXT PRIMARY KEY, name TEXT, kind TEXT, "
        "models TEXT, createdAt TEXT, updatedAt TEXT)"
    )
    conn.execute(
        "INSERT INTO combos (id, name, kind, models, createdAt, updatedAt) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (rows[0], rows[1], "llm", '["m1","m2"]', "2026-01-01T00:00:00", "2026-01-01T00:00:00"),
    )
    conn.commit()
    return conn


def _client(tmp_path, monkeypatch, db_path: Path) -> RouterClient:
    monkeypatch.setattr(router_client, "BACKUP_DIR", tmp_path / "db_backups")
    return RouterClient(base_url=BASE_URL, db_path=db_path)


def _read_row(db_path: Path):
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute("SELECT id, name FROM combos WHERE id='c1'").fetchone()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 1. WAL BACKUP
# ---------------------------------------------------------------------------
def test_wal_only_committed_row_survives_production_backup(tmp_path, monkeypatch):
    db_path = tmp_path / "data.sqlite"
    writer = _make_wal_db(db_path)
    try:
        wal = Path(str(db_path) + "-wal")
        assert wal.exists() and wal.stat().st_size > 0, "row must live in the active WAL"

        # Negative control: raw main-file copy loses the committed row. This is
        # what makes the test fail against the previous shutil.copy2 backend.
        import shutil
        raw_copy = tmp_path / "raw_copy.sqlite"
        shutil.copy2(db_path, raw_copy)
        raw = sqlite3.connect(str(raw_copy))
        try:
            try:
                raw_rows = raw.execute("SELECT id, name FROM combos").fetchall()
            except sqlite3.OperationalError:
                raw_rows = []
        finally:
            raw.close()
        assert raw_rows == [], "raw copy must NOT see the WAL-only row"

        client = _client(tmp_path, monkeypatch, db_path)
        backup = client._create_db_backup()
        assert backup.exists()

        reopened = sqlite3.connect(str(backup))
        try:
            row = reopened.execute("SELECT id, name FROM combos WHERE id='c1'").fetchone()
            integrity = reopened.execute("PRAGMA integrity_check").fetchone()
        finally:
            reopened.close()

        assert row == ("c1", "A"), "committed WAL row must be present in the backup"
        assert integrity is not None and integrity[0] == "ok"
    finally:
        writer.close()


# ---------------------------------------------------------------------------
# 2. LIVE OWNER REFUSAL
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("op", ["update", "delete"])
def test_live_owner_refuses_even_when_api_unreachable(tmp_path, monkeypatch, op):
    db_path = tmp_path / "data.sqlite"
    writer = _make_wal_db(db_path)
    try:
        client = _client(tmp_path, monkeypatch, db_path)
        monkeypatch.setattr(RouterClient, "is_server_reachable", lambda self, timeout=3.0: False)
        monkeypatch.setattr(
            router_client, "_find_router_runtime_processes",
            lambda base_url: [{"pid": 1336, "name": "node.exe"}],
        )
        before = _read_row(db_path)

        with pytest.raises(OfflineRecoveryRefused) as ex:
            if op == "update":
                client.offline_recovery_update_combo("c1", "renamed", ["m1"], allow_offline_wal_mutation=True)
            else:
                client.offline_recovery_delete_combo("c1", allow_offline_wal_mutation=True)

        assert "LIVE" in str(ex.value)
        assert _read_row(db_path) == before, "no mutation may occur on a live owner"
    finally:
        writer.close()


# ---------------------------------------------------------------------------
# 3. UNKNOWN OWNER REFUSAL
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("op", ["update", "delete"])
def test_unknown_ownership_fails_closed(tmp_path, monkeypatch, op):
    db_path = tmp_path / "data.sqlite"
    writer = _make_wal_db(db_path)
    try:
        client = _client(tmp_path, monkeypatch, db_path)
        monkeypatch.setattr(RouterClient, "is_server_reachable", lambda self, timeout=3.0: False)
        monkeypatch.setattr(router_client, "_find_router_runtime_processes", lambda base_url: None)
        before = _read_row(db_path)

        with pytest.raises(OfflineRecoveryRefused) as ex:
            if op == "update":
                client.offline_recovery_update_combo("c1", "renamed", ["m1"], allow_offline_wal_mutation=True)
            else:
                client.offline_recovery_delete_combo("c1", allow_offline_wal_mutation=True)

        assert "UNKNOWN" in str(ex.value)
        assert _read_row(db_path) == before, "UNKNOWN must never be downgraded to OFFLINE"
    finally:
        writer.close()


def test_missing_opt_in_still_required(tmp_path, monkeypatch):
    db_path = tmp_path / "data.sqlite"
    writer = _make_wal_db(db_path)
    try:
        client = _client(tmp_path, monkeypatch, db_path)
        with pytest.raises(PermissionError):
            client.offline_recovery_delete_combo("c1")
    finally:
        writer.close()


# ---------------------------------------------------------------------------
# 4. BACKUP FAILURE
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("op", ["update", "delete"])
def test_backup_creation_failure_never_enters_mutation(tmp_path, monkeypatch, op):
    db_path = tmp_path / "data.sqlite"
    writer = _make_wal_db(db_path)
    try:
        client = _client(tmp_path, monkeypatch, db_path)
        monkeypatch.setattr(RouterClient, "is_server_reachable", lambda self, timeout=3.0: False)
        monkeypatch.setattr(router_client, "_find_router_runtime_processes", lambda base_url: [])
        monkeypatch.setattr(
            RouterClient, "_create_db_backup",
            lambda self: (_ for _ in ()).throw(RuntimeError("Recovery backup failed: injected")),
        )
        entered = []
        monkeypatch.setattr(RouterClient, "_update_combo_sqlite", lambda self, *a, **k: entered.append("u"))
        monkeypatch.setattr(RouterClient, "_delete_combo_sqlite", lambda self, *a, **k: entered.append("d"))
        before = _read_row(db_path)

        with pytest.raises(RuntimeError, match="backup failed"):
            if op == "update":
                client.offline_recovery_update_combo("c1", "renamed", ["m1"], allow_offline_wal_mutation=True)
            else:
                client.offline_recovery_delete_combo("c1", allow_offline_wal_mutation=True)

        assert entered == [], "mutation must never be entered after a backup failure"
        assert _read_row(db_path) == before
    finally:
        writer.close()


# ---------------------------------------------------------------------------
# 5. BACKUP VALIDATION FAILURE
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("op", ["update", "delete"])
def test_backup_validation_failure_never_enters_mutation(tmp_path, monkeypatch, op):
    db_path = tmp_path / "data.sqlite"
    writer = _make_wal_db(db_path)
    try:
        client = _client(tmp_path, monkeypatch, db_path)
        monkeypatch.setattr(RouterClient, "is_server_reachable", lambda self, timeout=3.0: False)
        monkeypatch.setattr(router_client, "_find_router_runtime_processes", lambda base_url: [])
        monkeypatch.setattr(
            RouterClient, "_validate_db_backup",
            staticmethod(lambda backup_file: (_ for _ in ()).throw(
                RuntimeError("Recovery backup validation failed: injected")
            )),
        )
        entered = []
        monkeypatch.setattr(RouterClient, "_update_combo_sqlite", lambda self, *a, **k: entered.append("u"))
        monkeypatch.setattr(RouterClient, "_delete_combo_sqlite", lambda self, *a, **k: entered.append("d"))
        before = _read_row(db_path)

        with pytest.raises(RuntimeError, match="validation failed"):
            if op == "update":
                client.offline_recovery_update_combo("c1", "renamed", ["m1"], allow_offline_wal_mutation=True)
            else:
                client.offline_recovery_delete_combo("c1", allow_offline_wal_mutation=True)

        assert entered == [], "mutation must never be entered after a validation failure"
        assert _read_row(db_path) == before
    finally:
        writer.close()


def test_real_backup_validation_rejects_integrity_failure(tmp_path, monkeypatch):
    """_validate_db_backup must require an explicit successful result."""
    client = _client(tmp_path, monkeypatch, tmp_path / "data.sqlite")
    corrupt = tmp_path / "corrupt.sqlite"
    corrupt.write_bytes(b"not a sqlite database at all")
    with pytest.raises(RuntimeError, match="validation failed"):
        client._validate_db_backup(corrupt)


# ---------------------------------------------------------------------------
# 6. SUCCESS PATH
# ---------------------------------------------------------------------------
def test_success_path_update_and_delete_after_positive_offline_proof(tmp_path, monkeypatch):
    db_path = tmp_path / "data.sqlite"
    writer = _make_wal_db(db_path)
    try:
        client = _client(tmp_path, monkeypatch, db_path)
        monkeypatch.setattr(RouterClient, "is_server_reachable", lambda self, timeout=3.0: False)
        monkeypatch.setattr(router_client, "_find_router_runtime_processes", lambda base_url: [])

        updated = client.offline_recovery_update_combo("c1", "renamed", ["m1", "m2"], allow_offline_wal_mutation=True)
        assert updated is not None and updated["name"] == "renamed"

        assert client.offline_recovery_delete_combo("c1", allow_offline_wal_mutation=True) is True
        assert _read_row(db_path) is None

        backups = sorted((tmp_path / "db_backups").glob("data_*.sqlite"))
        assert backups, "a recovery backup must have been created"
        for backup in backups:
            reopened = sqlite3.connect(f"file:{backup}?mode=ro", uri=True)
            try:
                assert reopened.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
                reopened.execute("SELECT id FROM combos").fetchall()
            finally:
                reopened.close()
    finally:
        writer.close()


def test_offline_update_missing_combo_returns_failure_without_mutating_existing_row(tmp_path, monkeypatch):
    db_path = tmp_path / "data.sqlite"
    writer = _make_wal_db(db_path)
    try:
        client = _client(tmp_path, monkeypatch, db_path)
        monkeypatch.setattr(RouterClient, "is_server_reachable", lambda self, timeout=3.0: False)
        monkeypatch.setattr(router_client, "_find_router_runtime_processes", lambda base_url: [])

        before = _read_row(db_path)
        updated = client.offline_recovery_update_combo(
            "missing", "renamed", ["m2"], allow_offline_wal_mutation=True,
        )
        assert updated is None
        assert _read_row(db_path) == before
    finally:
        writer.close()


def test_verify_offline_ownership_state_mapping(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch, tmp_path / "data.sqlite")

    monkeypatch.setattr(RouterClient, "is_server_reachable", lambda self, timeout=3.0: True)
    assert client.verify_offline_ownership() is OfflineState.LIVE

    monkeypatch.setattr(RouterClient, "is_server_reachable", lambda self, timeout=3.0: False)
    monkeypatch.setattr(router_client, "_find_router_runtime_processes", lambda base_url: [{"pid": 1}])
    assert client.verify_offline_ownership() is OfflineState.LIVE

    monkeypatch.setattr(router_client, "_find_router_runtime_processes", lambda base_url: None)
    assert client.verify_offline_ownership() is OfflineState.UNKNOWN

    monkeypatch.setattr(router_client, "_find_router_runtime_processes", lambda base_url: [])
    assert client.verify_offline_ownership() is OfflineState.OFFLINE_VERIFIED


# ---------------------------------------------------------------------------
# Runtime-ownership probe parser (deterministic; no live 9Router needed)
# ---------------------------------------------------------------------------
class _FakeCompleted:
    def __init__(self, stdout: str, returncode: int = 0):
        self.stdout = stdout
        self.returncode = returncode


def _probe_with(monkeypatch, payload: str, returncode: int = 0):
    monkeypatch.setattr(router_client.sys, "platform", "win32")
    monkeypatch.setattr(router_client.shutil, "which", lambda name: "powershell.exe")
    monkeypatch.setattr(
        router_client.subprocess, "run",
        lambda *a, **k: _FakeCompleted(payload, returncode),
    )
    return router_client._find_router_runtime_processes(BASE_URL)


def test_probe_flags_live_9router_owner_by_command_line(monkeypatch):
    import json as _json
    payload = _json.dumps({
        "procs": [
            {"ProcessId": 1, "Name": "node.exe",
             "ExecutablePath": r"C:\nodejs\node.exe",
             "CommandLine": r"C:\nodejs\node.exe C:\nodejs\node_modules\9router\app\custom-server.js"},
            {"ProcessId": 2, "Name": "explorer.exe",
             "ExecutablePath": r"C:\Windows\explorer.exe", "CommandLine": "explorer.exe"},
        ],
        "conns": [],
    })
    owners = _probe_with(monkeypatch, payload)
    assert [o["pid"] for o in owners] == [1]


def test_probe_flags_listener_owner_even_without_path_match(monkeypatch):
    import json as _json
    payload = _json.dumps({
        "procs": [{"ProcessId": 7, "Name": "node.exe", "ExecutablePath": "", "CommandLine": "node server"}],
        "conns": [{"OwningProcess": 7}],
    })
    owners = _probe_with(monkeypatch, payload)
    assert [o["pid"] for o in owners] == [7]


def test_probe_does_not_match_watchedit_repo_segment(monkeypatch):
    import json as _json
    payload = _json.dumps({
        "procs": [{"ProcessId": 3, "Name": "python.exe", "ExecutablePath": r"V:\x\9router_WatchEdit\python.exe",
                   "CommandLine": r"python V:\x\_9router_extra\9router_WatchEdit\run.py"}],
        "conns": [],
    })
    owners = _probe_with(monkeypatch, payload)
    assert owners == [], "WatchEdit control-plane paths must not count as the 9Router owner"


def test_probe_indeterminate_on_query_failure(monkeypatch):
    monkeypatch.setattr(router_client.sys, "platform", "win32")
    monkeypatch.setattr(router_client.shutil, "which", lambda name: "powershell.exe")
    monkeypatch.setattr(
        router_client.subprocess, "run",
        lambda *a, **k: (_ for _ in ()).throw(OSError("powershell unavailable")),
    )
    assert router_client._find_router_runtime_processes(BASE_URL) is None


def test_probe_returns_none_on_non_windows(monkeypatch):
    monkeypatch.setattr(router_client.sys, "platform", "linux")
    assert router_client._find_router_runtime_processes(BASE_URL) is None
