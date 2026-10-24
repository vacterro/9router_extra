"""
Tests for core/router_client.py
"""
import sqlite3

import pytest
from core.router_client import RouterClient

BASE_URL = "http://127.0.0.1:99999"


def test_sqlite_combo_read_missing_db_is_legitimate_empty(tmp_path):
    client = RouterClient(base_url=BASE_URL, db_path=tmp_path / "absent.sqlite")
    assert client._get_combos_sqlite() == []


def test_sqlite_combo_read_failure_is_not_hidden_as_empty(tmp_path):
    """T-100: a corrupt DB must raise, never masquerade as an empty table."""
    db = tmp_path / "corrupt.sqlite"
    db.write_bytes(b"this is not a sqlite database" * 8)
    client = RouterClient(base_url=BASE_URL, db_path=db)

    with pytest.raises(sqlite3.DatabaseError):
        client._get_combos_sqlite()


def test_sqlite_combo_read_closes_connection_on_every_path(tmp_path, monkeypatch):
    """T-100: the read-only connection is closed even when the read fails."""
    from core import router_client

    db = tmp_path / "corrupt.sqlite"
    db.write_bytes(b"this is not a sqlite database" * 8)
    client = RouterClient(base_url=BASE_URL, db_path=db)

    closed = []
    real_connect = sqlite3.connect

    class _TrackedConnection:
        def __init__(self, conn):
            self._conn = conn

        def cursor(self):
            return self._conn.cursor()

        def close(self):
            closed.append(True)
            self._conn.close()

    def tracking_connect(*args, **kwargs):
        return _TrackedConnection(real_connect(*args, **kwargs))

    monkeypatch.setattr(router_client.sqlite3, "connect", tracking_connect)
    with pytest.raises(sqlite3.DatabaseError):
        client._get_combos_sqlite()
    assert closed == [True]


def test_cli_token_generation(tmp_path, monkeypatch):
    from core import router_client

    machine = tmp_path / "synthetic-machine"
    secret = tmp_path / "synthetic-cli"
    machine.write_text("test-machine", encoding="utf-8")
    secret.write_text("test-cli", encoding="utf-8")
    monkeypatch.setattr(router_client, "MACHINE_ID_FILE", machine)
    monkeypatch.setattr(router_client, "CLI_SECRET_FILE", secret)
    client = RouterClient()
    token = client.get_cli_token()
    assert isinstance(token, str)
    assert len(token) == 16
    # Must be deterministic for the same machine
    token2 = client.get_cli_token()
    assert token == token2

@pytest.mark.integration
@pytest.mark.local_trusted
def test_local_connection_inspection():
    client = RouterClient()
    # Test reading combos (either from API or SQLite fallback)
    combos = client.get_combos()
    assert isinstance(combos, list)
    if combos:
        c = combos[0]
        assert "id" in c
        assert "name" in c
        assert "models" in c
        assert isinstance(c["models"], list)

    # Test reading providers
    providers = client.get_providers()
    assert isinstance(providers, list)
    assert len(providers) > 0
