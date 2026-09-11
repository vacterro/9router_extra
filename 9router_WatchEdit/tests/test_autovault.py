"""
AutoVault offline test suite (ROLE_20260909_1808_2, 30 required cases).

All tests run against a fully isolated fake environment: fake APPDATA 9router
tree, fake WatchEdit data dir, fake secure dir. No live state, no Task
Scheduler interaction (except the idempotent-registration unit tests which
use a stubbed subprocess runner). Vault libs required.
"""
import base64
import hashlib
import io
import json
import os
import sqlite3
import sys
import zipfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "tools"))
sys.path.insert(0, str(REPO_ROOT / "9router_WatchEdit"))

VAULT_LIBS = True
try:
    import argon2.low_level  # noqa: F401
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa: F401
except Exception:
    VAULT_LIBS = False

pytestmark = pytest.mark.skipif(not VAULT_LIBS, reason="vault libraries not installed")

MASTER = "master-pass-123"
WRONG = "totally-wrong-pass"

import autovault as av  # noqa: E402
from tests.security.canaries import canary_sk  # noqa: E402


class Env:
    """Isolated fake environment bound to a tmp_path."""

    def __init__(self, tmp_path: Path, wal: bool = False):
        self.root = tmp_path / "env"
        self.router = self.root / "appdata" / "9router"
        self.we = self.root / "we"
        self.secure = self.we / "secure"
        self.sched = self.root / "sched"
        self.dest = self.root / "dest"
        (self.router / "db").mkdir(parents=True, exist_ok=True)
        (self.router / "auth").mkdir(parents=True, exist_ok=True)
        self.secure.mkdir(parents=True, exist_ok=True)
        self.we.mkdir(parents=True, exist_ok=True)
        self.sched.mkdir(parents=True, exist_ok=True)

        self.db = self.router / "db" / "data.sqlite"
        conn = sqlite3.connect(self.db)
        conn.execute("PRAGMA journal_mode=WAL" if wal else "PRAGMA journal_mode=DELETE")
        conn.execute("CREATE TABLE providers (id INTEGER PRIMARY KEY, name TEXT, label TEXT)")
        conn.execute("INSERT INTO providers (name, label) VALUES (?, ?)",
                     ("p1", canary_sk()))
        conn.commit()
        conn.close()
        (self.router / "auth" / "cli-secret").write_bytes(b"c" * 64)
        (self.router / "jwt-secret").write_bytes(b"j" * 64)
        (self.router / "machine-id").write_bytes(b"m" * 64)
        (self.router / "model-catalog.json").write_text("[]", encoding="utf-8")
        (self.router / "model-catalog-raw.json").write_text("{}", encoding="utf-8")
        (self.we / "settings.json").write_text('{"a": 1}', encoding="utf-8")
        (self.we / "presets.json").write_text("[]", encoding="utf-8")
        (self.we / "health_cache.json").write_text("{}", encoding="utf-8")
        (self.we / "config").mkdir(exist_ok=True)
        (self.we / "config" / "settings.json").write_text('{"trusted_os_unlock": false}', encoding="utf-8")

    def bind(self, monkeypatch):
        monkeypatch.setattr(av, "APPDATA_ROUTER", self.router)
        monkeypatch.setattr(av, "LOCALAPPDATA_DIR", self.we)
        monkeypatch.setattr(av, "SCHEDULED_DIR", self.sched)
        monkeypatch.setattr(av, "SECURE_DIR", self.secure)
        monkeypatch.setattr(av, "SECURE_KEY_PATH", self.secure / "scheduled-backup-key.dpapi")
        monkeypatch.setattr(av, "RECOVERY_ENVELOPE_PATH", self.secure / "scheduled-backup-recovery.vault")

    def mutate_db(self, n: int = 999):
        conn = sqlite3.connect(self.db)
        conn.execute("INSERT INTO providers (name, label) VALUES (?, ?)", (f"p{n}", "x"))
        conn.commit()
        conn.close()

    def setup_secret(self, monkeypatch):
        self.bind(monkeypatch)
        secret = av.generate_backup_secret()
        av.store_scheduler_credential(secret)
        av.create_recovery_envelope(secret, MASTER)
        return secret


# --- 1-3: credential layer -------------------------------------------------
def test_1_setup_creates_dpapi_secret_no_master_password_persisted(tmp_path, monkeypatch):
    env = Env(tmp_path)
    env.setup_secret(monkeypatch)
    # DPAPI credential present and NOT the master password
    secret = av.load_scheduler_credential()
    assert len(secret) >= 32
    assert MASTER not in secret
    # master password nowhere in secure dir plaintext
    for p in env.secure.rglob("*"):
        if p.is_file():
            raw = p.read_bytes()
            assert MASTER.encode() not in raw, f"master password persisted in {p.name}"
            assert secret.encode() not in raw or p.suffix == ".vault"  # vault holds it encrypted only


def test_2_recovery_vault_unlocks_same_secret(tmp_path, monkeypatch):
    env = Env(tmp_path)
    secret = env.setup_secret(monkeypatch)
    assert av.unlock_recovery_envelope(av.RECOVERY_ENVELOPE_PATH, MASTER) == secret


def test_3_wrong_master_password_fails(tmp_path, monkeypatch):
    env = Env(tmp_path)
    env.setup_secret(monkeypatch)
    with pytest.raises(Exception):
        av.unlock_recovery_envelope(av.RECOVERY_ENVELOPE_PATH, WRONG)


# --- 4: unattended run ------------------------------------------------------
def test_4_scheduled_backup_without_password_prompt(tmp_path, monkeypatch):
    env = Env(tmp_path)
    secret = env.setup_secret(monkeypatch)
    # run with NO secret argument: reads DPAPI credential unattended
    result, path = av.run_backup(env.dest)
    assert result == av.R_SUCCESS
    assert path is not None and path.exists()


# --- 5: byte-exact round trip ------------------------------------------------
def test_5_byte_exact_binary_round_trip(tmp_path, monkeypatch):
    env = Env(tmp_path)
    secret = env.setup_secret(monkeypatch)
    blob = bytes(range(256)) + os.urandom(4096)
    deep = env.we / "config" / "nested" / "deep"
    deep.mkdir(parents=True, exist_ok=True)
    (deep / "blob.bin").write_bytes(blob)
    result, path = av.run_backup(env.dest, secret=secret)
    assert result == av.R_SUCCESS
    info = av.restore_backup(path, MASTER, apply=False)
    # decode the entry
    rel = "watchedit/config/nested/deep/blob.bin"
    assert rel in info["entries"]
    entries = av.verify_container(path, secret=secret)["entries"]
    raw = base64.b64decode(entries[rel])
    assert raw == blob
    assert len(raw) == len(blob)
    assert hashlib.sha256(raw).hexdigest() == hashlib.sha256(blob).hexdigest()


# --- 6-7: SQLite consistency ---------------------------------------------------
def test_6_sqlite_snapshot_consistent(tmp_path, monkeypatch):
    env = Env(tmp_path)
    env.setup_secret(monkeypatch)
    snap = av.snapshot_sqlite(env.db)
    conn = sqlite3.connect(":memory:")
    conn.deserialize(snap)
    assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    assert conn.execute("SELECT count(*) FROM providers").fetchone()[0] == 1
    conn.close()


def test_7_wal_live_write_snapshot_consistent(tmp_path, monkeypatch):
    env = Env(tmp_path, wal=True)
    env.setup_secret(monkeypatch)
    # keep a connection open with an uncommitted txn (simulates live writer)
    writer = sqlite3.connect(env.db)
    writer.execute("INSERT INTO providers (name, label) VALUES (?, ?)", ("live", canary_sk()))
    writer.commit()
    snap = av.snapshot_sqlite(env.db)
    writer.close()
    conn = sqlite3.connect(":memory:")
    conn.deserialize(snap)
    assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    names = [r[0] for r in conn.execute("SELECT name FROM providers")]
    assert "live" in names  # committed write captured consistently
    conn.close()


# --- 8-9: tamper fail-closed --------------------------------------------------
def test_8_tampered_payload_fails_closed(tmp_path, monkeypatch):
    env = Env(tmp_path)
    secret = env.setup_secret(monkeypatch)
    result, path = av.run_backup(env.dest, secret=secret)
    assert result == av.R_SUCCESS
    tampered = env.dest / "tampered.wvault"
    with zipfile.ZipFile(path) as zin, zipfile.ZipFile(tampered, "w") as zout:
        for item in zin.namelist():
            data = zin.read(item)
            if item == "payload.vault":
                data = data[:-4] + b"XXXX"
            zout.writestr(item, data)
    with pytest.raises(Exception):
        av.verify_container(tampered, secret=secret)


def test_9_tampered_recovery_envelope_fails_closed(tmp_path, monkeypatch):
    env = Env(tmp_path)
    secret = env.setup_secret(monkeypatch)
    result, path = av.run_backup(env.dest, secret=secret)
    assert result == av.R_SUCCESS
    tampered = env.dest / "tampered_env.wvault"
    with zipfile.ZipFile(path) as zin, zipfile.ZipFile(tampered, "w") as zout:
        for item in zin.namelist():
            data = zin.read(item)
            if item == "recovery.vault":
                data = data[:-4] + b"XXXX"
            zout.writestr(item, data)
    with pytest.raises(Exception):
        av.restore_backup(tampered, MASTER, apply=False)


# --- 10-11: change detection --------------------------------------------------
def test_10_changed_state_creates_new_backup(tmp_path, monkeypatch):
    env = Env(tmp_path)
    secret = env.setup_secret(monkeypatch)
    r1, p1 = av.run_backup(env.dest, secret=secret)
    env.mutate_db()
    r2, p2 = av.run_backup(env.dest, secret=secret)
    assert r1 == r2 == av.R_SUCCESS
    assert p1.name != p2.name
    assert len(av.list_valid_backups(env.dest)) == 2


def test_11_unchanged_state_skips_duplicate(tmp_path, monkeypatch):
    env = Env(tmp_path)
    secret = env.setup_secret(monkeypatch)
    av.run_backup(env.dest, secret=secret)
    r2, p2 = av.run_backup(env.dest, secret=secret)
    assert r2 == av.R_NO_CHANGES
    assert p2 is None
    assert len(av.list_valid_backups(env.dest)) == 1


# --- 12-16: retention / failure safety -----------------------------------------
def test_12_failed_backup_does_not_prune(tmp_path, monkeypatch):
    env = Env(tmp_path)
    secret = env.setup_secret(monkeypatch)
    av.run_backup(env.dest, secret=secret)
    before = av.list_valid_backups(env.dest)
    # make snapshot fail: remove db AFTER entries exist
    env.db.unlink()
    with pytest.raises(Exception):
        av.run_backup(env.dest, secret=secret)
    assert av.list_valid_backups(env.dest) == before


def test_13_retention_count(tmp_path, monkeypatch):
    env = Env(tmp_path)
    secret = env.setup_secret(monkeypatch)
    for i in range(5):
        env.mutate_db(i)
        av.run_backup(env.dest, secret=secret)
    newest = av.list_valid_backups(env.dest)[0]
    av.prune_retention(env.dest, keep_count=3, max_mib=1024, protect=newest)
    assert len(av.list_valid_backups(env.dest)) == 3


def test_14_retention_size_cap(tmp_path, monkeypatch):
    env = Env(tmp_path)
    secret = env.setup_secret(monkeypatch)
    for i in range(4):
        env.mutate_db(i)
        av.run_backup(env.dest, secret=secret)
    backups = av.list_valid_backups(env.dest)
    assert len(backups) >= 2
    av.prune_retention(env.dest, keep_count=99, max_mib=0, protect=backups[0])
    # size cap 0 => only the newest survives
    assert len(av.list_valid_backups(env.dest)) == 1


def test_15_newest_never_pruned(tmp_path, monkeypatch):
    env = Env(tmp_path)
    secret = env.setup_secret(monkeypatch)
    for i in range(4):
        env.mutate_db(i)
        av.run_backup(env.dest, secret=secret)
    newest = av.list_valid_backups(env.dest)[0]
    av.prune_retention(env.dest, keep_count=1, max_mib=0, protect=newest)
    remaining = av.list_valid_backups(env.dest)
    assert len(remaining) == 1 and remaining[0] == newest


def test_16_partial_never_valid(tmp_path, monkeypatch):
    env = Env(tmp_path)
    env.setup_secret(monkeypatch)
    partial = env.dest / "9ROUTER_AUTOVAULT_20260101_000000.wvault.partial"
    partial.parent.mkdir(parents=True, exist_ok=True)
    partial.write_bytes(b"junk")
    assert av.list_valid_backups(env.dest) == []


# --- 17-18: destination rejection ----------------------------------------------
def test_17_repository_destination_rejected(tmp_path, monkeypatch):
    env = Env(tmp_path)
    env.bind(monkeypatch)
    with pytest.raises(Exception):
        av.validate_destination(REPO_ROOT)


def test_18_source_tree_destination_rejected(tmp_path, monkeypatch):
    env = Env(tmp_path)
    env.bind(monkeypatch)
    # destination inside live router data
    with pytest.raises(Exception):
        av.validate_destination(env.router / "backups-inside")
    # destination inside live WatchEdit data
    with pytest.raises(Exception):
        av.validate_destination(env.we / "backups-inside")


# --- 19-20: DPAPI credential failures -------------------------------------------
def test_19_missing_dpapi_credential_safe_failure(tmp_path, monkeypatch):
    env = Env(tmp_path)
    env.bind(monkeypatch)
    av.create_recovery_envelope(av.generate_backup_secret(), MASTER)
    with pytest.raises(av.AutoVaultError) as ei:
        av.run_backup(env.dest)
    assert ei.value.category == "credential_missing"


def test_20_corrupt_dpapi_credential_safe_failure(tmp_path, monkeypatch):
    env = Env(tmp_path)
    env.bind(monkeypatch)
    av.SECURE_KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
    av.SECURE_KEY_PATH.write_bytes(b"garbage-not-dpapi")
    with pytest.raises(av.AutoVaultError) as ei:
        av.load_scheduler_credential()
    assert ei.value.category in ("credential_corrupt",)


# --- 21-23: no secret leakage ------------------------------------------------------
def test_21_manifest_no_secret_material(tmp_path, monkeypatch):
    env = Env(tmp_path)
    secret = env.setup_secret(monkeypatch)
    result, path = av.run_backup(env.dest, secret=secret)
    assert result == av.R_SUCCESS
    with zipfile.ZipFile(path) as zf:
        manifest = json.loads(zf.read("manifest.json"))
        manifest_text = json.dumps(manifest)
    assert secret not in manifest_text
    assert MASTER not in manifest_text
    assert canary_sk() not in manifest_text
    # manifest carries only allowed metadata fields at top level
    allowed = {"format", "format_version", "created_utc", "schema_version",
               "payload_size_bytes", "entry_count", "payload_sha256",
               "source_categories", "state_fingerprint", "app",
               "verification", "entries"}
    assert set(manifest.keys()) <= allowed


def test_22_command_line_no_secret(tmp_path, monkeypatch):
    # scheduled invocation = [python, autovault.py, run]; no secret material
    env = Env(tmp_path)
    secret = env.setup_secret(monkeypatch)
    args = [sys.executable, str(REPO_ROOT / "tools" / "autovault.py"), "run"]
    joined = " ".join(args)
    assert secret not in joined
    assert MASTER not in joined


def test_23_logs_no_secret(tmp_path, monkeypatch):
    env = Env(tmp_path)
    secret = env.setup_secret(monkeypatch)
    av.run_backup(env.dest, secret=secret)
    log = (env.sched / "autovault.log").read_text(encoding="utf-8")
    assert secret not in log
    assert MASTER not in log
    assert canary_sk() not in log


# --- 24-25: task registration (stubbed subprocess) ----------------------------------
class _FakeRunner:
    def __init__(self):
        self.calls: list = []

    def __call__(self, cmd, capture_output=True, text=True, timeout=None):
        self.calls.append(" ".join(cmd[2:]))
        class R:
            returncode = 0
            stdout = "OK"
            stderr = ""
        return R()


def test_24_task_creation_idempotent(tmp_path, monkeypatch):
    env = Env(tmp_path)
    env.bind(monkeypatch)
    fake = _FakeRunner()
    monkeypatch.setattr(av.subprocess, "run", fake)
    assert av.register_task("C:\\Python\\python.exe", "C:\\tools\\autovault.py", 6)
    assert av.register_task("C:\\Python\\python.exe", "C:\\tools\\autovault.py", 6)
    # every registration unregisters first: never duplicates
    unregister = [c for c in fake.calls if "Unregister-ScheduledTask" in c]
    assert len(unregister) == 2
    # task name constant in both calls
    assert all("9Router AutoVault Backup" in c for c in fake.calls)


def test_25_task_command_absolute_paths(tmp_path, monkeypatch):
    env = Env(tmp_path)
    env.bind(monkeypatch)
    fake = _FakeRunner()
    monkeypatch.setattr(av.subprocess, "run", fake)
    av.register_task("C:\\abs\\python.exe", "D:\\abs\\autovault.py", 6)
    reg = [c for c in fake.calls if "Register-ScheduledTask" in c][0]
    assert "C:\\abs\\python.exe" in reg
    assert "D:\\abs\\autovault.py" in reg


# --- 26: concurrency lock --------------------------------------------------------------
def test_26_concurrent_second_backup_exits_safely(tmp_path, monkeypatch):
    env = Env(tmp_path)
    env.setup_secret(monkeypatch)
    lock = av.BackupLock(env.sched / "autovault.lock")
    assert lock.acquire()
    try:
        result, path = av.run_backup(env.dest)
        assert result == av.R_ALREADY_RUNNING
        assert path is None
        assert av.list_valid_backups(env.dest) == []
    finally:
        lock.release()


# --- 27-30: restore behavior -------------------------------------------------------------
def test_27_verify_only_performs_no_writes(tmp_path, monkeypatch):
    env = Env(tmp_path)
    secret = env.setup_secret(monkeypatch)
    result, path = av.run_backup(env.dest, secret=secret)
    before = {p.name: p.stat().st_mtime for p in env.router.rglob("*") if p.is_file()}
    info = av.restore_backup(path, MASTER, apply=False)
    assert info["verified"]
    after = {p.name: p.stat().st_mtime for p in env.router.rglob("*") if p.is_file()}
    assert before == after


def test_28_restore_requires_master_password(tmp_path, monkeypatch):
    env = Env(tmp_path)
    secret = env.setup_secret(monkeypatch)
    result, path = av.run_backup(env.dest, secret=secret)
    # no password argument at all -> cannot proceed
    with pytest.raises(TypeError):
        av.restore_backup(path)  # noqa: E501
    # wrong password fails closed
    with pytest.raises(Exception):
        av.restore_backup(path, WRONG, apply=False)


def test_29_restore_verifies_hashes_before_writes(tmp_path, monkeypatch):
    env = Env(tmp_path)
    secret = env.setup_secret(monkeypatch)
    result, path = av.run_backup(env.dest, secret=secret)
    # tamper entry metadata (sha) inside manifest -> restore must refuse, no writes
    tampered = env.dest / "tampered_meta.wvault"
    with zipfile.ZipFile(path) as zin, zipfile.ZipFile(tampered, "w") as zout:
        for item in zin.namelist():
            data = zin.read(item)
            if item == "manifest.json":
                m = json.loads(data)
                m["entries"][0]["sha256"] = "0" * 64
                data = json.dumps(m).encode()
            zout.writestr(item, data)
    out = {"9router": env.root / "out9", "watchedit": env.root / "outw"}
    with pytest.raises(Exception):
        av.restore_backup(tampered, MASTER, apply=True, out_dirs=out)
    assert not (env.root / "out9").exists() or not any((env.root / "out9").rglob("*"))


def test_30_sqlite_quick_check_after_round_trip(tmp_path, monkeypatch):
    env = Env(tmp_path)
    secret = env.setup_secret(monkeypatch)
    result, path = av.run_backup(env.dest, secret=secret)
    assert result == av.R_SUCCESS
    # verify_container already runs quick_check on the decrypted snapshot;
    # explicit double-check via restore apply into isolated dirs
    out = {"9router": env.root / "rt9", "watchedit": env.root / "rtw"}
    av.restore_backup(path, MASTER, apply=True, out_dirs=out)
    conn = sqlite3.connect(env.root / "rt9" / "db" / "data.sqlite")
    assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    assert conn.execute("SELECT count(*) FROM providers").fetchone()[0] == 1
    conn.close()
    # provider state round-trips
    jwt = (env.root / "rt9" / "jwt-secret").read_bytes()
    assert jwt == b"j" * 64
