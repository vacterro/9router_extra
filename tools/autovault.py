#!/usr/bin/env python3
"""
AutoVault - unattended encrypted backup system for 9Router + WatchEdit.

Vertical slice per ROLE_20260909_1808_2. Reuses existing tested building
blocks ONLY:

  - VaultStore (Argon2id + AES-256-GCM) from core/secret_store.py
  - DPAPI CurrentUser from core/secret_store.py
  - restrict_to_current_user ACL hardening
  - byte-preserving base64 entry encoding from tools/create_private_backup.py
    (W2-003 contract: sha256 + length verified, fail-closed)

Two-layer recovery design:

  SETUP  : random 32-byte backup secret
           -> local copy DPAPI-protected (scheduled-backup-key.dpapi)
              Task Scheduler (same Windows user) reads it unattended
           -> portable recovery envelope (scheduled-backup-recovery.vault)
              same secret encrypted with the master password via VaultStore
           -> every .wvault container embeds a copy of the envelope

  RESTORE: backup file + master password on ANY Windows machine, no DPAPI
           profile of the original machine needed.

Secrets never appear on command line, in Task Scheduler arguments, in
environment variables, logs, manifests, or the repository.

Container format (9ROUTER_AUTOVAULT_<ts>.wvault, a zip):
    manifest.json    - non-secret metadata only
    payload.vault   - VaultStore-encrypted entries (key: backup secret)
    recovery.vault  - master-password-encrypted backup secret (portable)

Payload entries are byte-exact: relative path, base64, sha256, size.
The SQLite database is captured with sqlite3 online backup API into an
in-memory snapshot (consistent under WAL / live writes, no plaintext
temp file, no stop of 9Router required).
"""
from __future__ import annotations

import base64
import datetime
import errno
import getpass
import hashlib
import io
import json
import msvcrt
import os
import re
import secrets as pysecrets
import sqlite3
import subprocess
import sys
import time
import zipfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "9router_WatchEdit"))

from config import (  # noqa: E402
    APPDATA_ROUTER, HEALTH_CACHE_FILE, LOCALAPPDATA_DIR, PRESETS_FILE,
    SECURE_DIR, SETTINGS_FILE,
)
from core.secret_store import (  # noqa: E402
    DPAPIFileStore, VaultStore, dpapi_protect, dpapi_unprotect,
    restrict_to_current_user,
)
from core.redaction import redact_text  # noqa: E402

# ---------------------------------------------------------------------------
# Constants / layout
# ---------------------------------------------------------------------------
TASK_NAME = "9Router AutoVault Backup"
FORMAT_VERSION = 1

# Secure local material (DPAPI) - NEVER a restore payload
CRED_NAME = "scheduled-backup-key"
RECOVERY_ENV_NAME = "scheduled-backup-recovery"
SECURE_KEY_PATH = SECURE_DIR / "scheduled-backup-key.dpapi"
RECOVERY_ENVELOPE_PATH = SECURE_DIR / "scheduled-backup-recovery.vault"

# Scheduled-side state (NOT secret; lives next to the log)
SCHEDULED_DIR = LOCALAPPDATA_DIR / "backups" / "scheduled"
LOG_FILE = SCHEDULED_DIR / "autovault.log"
CONFIG_FILE = SCHEDULED_DIR / "autovault-config.json"
LOCK_FILE = SCHEDULED_DIR / "autovault.lock"
STATE_FILE = SCHEDULED_DIR / "autovault-state.json"  # last fingerprint etc.
LOG_MAX_BYTES = 256 * 1024
LOG_MAX_LINES = 2000

DEFAULT_SCHEDULE_HOURS = 6
DEFAULT_RETENTION_COUNT = 20
DEFAULT_RETENTION_MIB = 1024
SCHEDULE_CHOICES = {"1": 1, "2": 3, "3": 6, "4": 12, "5": 24}

# Mutable 9Router state (from create_safety_backup.ps1 contract)
ROUTER_FILES = ["jwt-secret", "machine-id", "model-catalog.json", "model-catalog-raw.json"]
ROUTER_AUTH_FILES = ["cli-secret"]
SQLITE_DB = "db/data.sqlite"

# WatchEdit mutable state
WATCHEDIT_FILES = ["health_cache.json", "presets.json", "settings.json"]
WATCHEDIT_DIRS = ["config"]

# Never captured into any backup
EXCLUDED_DIR_NAMES = {
    "source", "runtime", "node_modules", ".next", ".next-cli-build",
    "logs", "backups", "db_backups", "engine", "__pycache__",
    "agent_state", "secure",
}
EXCLUDED_SUFFIXES = {".tgz", ".tmp", ".partial", ".log"}

MIN_MASTER_PASSWORD_LEN = 8

# Result strings for CLI/logs (also matched by tests)
R_SUCCESS = "SUCCESS"
R_NO_CHANGES = "NO CHANGES - BACKUP SKIPPED"
R_ALREADY_RUNNING = "ALREADY RUNNING"
R_FAILED = "FAILED"


class AutoVaultError(RuntimeError):
    """Safe, redacted failure. str() output is log-safe by construction."""

    def __init__(self, category: str, message: str):
        self.category = category
        super().__init__(f"[{category}] {redact_text(message)}")


# ---------------------------------------------------------------------------
# Single-instance lock (OS-safe, no external deps)
# ---------------------------------------------------------------------------
class BackupLock:
    """msvcrt.locking-based exclusive lock file. Process death releases it."""

    def __init__(self, lock_path: Path):
        self.path = Path(lock_path)
        self._fh = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._fh = open(self.path, "a+b")
            self._fh.seek(0)
            msvcrt.locking(self._fh.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except (OSError, PermissionError):
            if self._fh:
                self._fh.close()
                self._fh = None
            return False

    def release(self) -> None:
        if self._fh:
            try:
                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
            self._fh.close()
            self._fh = None


# ---------------------------------------------------------------------------
# Logging (compact, redacted, bounded)
# ---------------------------------------------------------------------------
def _rotate_log_if_needed(path: Path) -> None:
    try:
        if not path.exists() or path.stat().st_size < LOG_MAX_BYTES:
            return
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines[-LOG_MAX_LINES // 2:]) + "\n")
    except OSError:
        pass


def log_run(result: str, run_id: str, duration: float, fingerprint_prefix: str,
            filename: str, size: int, entry_count: int,
            error_category: str = "", scheduled_dir: Optional[Path] = None) -> None:
    d = Path(scheduled_dir) if scheduled_dir else Path(SCHEDULED_DIR)
    if not d.is_absolute():
        return
    d.mkdir(parents=True, exist_ok=True)
    _rotate_log_if_needed(d / "autovault.log")
    line = "\t".join(str(x) for x in [
        datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        run_id, result, f"{duration:.1f}s", fingerprint_prefix[:16],
        filename, size, entry_count, redact_text(error_category),
    ])
    try:
        with open(d / "autovault.log", "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def _read_state(scheduled_dir: Optional[Path] = None) -> Dict:
    sd = Path(scheduled_dir) if scheduled_dir else Path(SCHEDULED_DIR)
    try:
        return json.loads((sd / "autovault-state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write_state(scheduled_dir: Optional[Path], data: Dict) -> None:
    d = Path(scheduled_dir) if scheduled_dir else Path(SCHEDULED_DIR)
    if not d.is_absolute():
        return
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / ".autovault-state.tmp"
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(d / "autovault-state.json")


# ---------------------------------------------------------------------------
# Credential layer: random secret + DPAPI local copy + recovery envelope
# ---------------------------------------------------------------------------
def generate_backup_secret() -> str:
    """32 random bytes -> base64 string used as VaultStore password input.
    High entropy by construction; never derived from a user password."""
    return base64.b64encode(pysecrets.token_bytes(32)).decode("ascii")


def store_scheduler_credential(secret: str) -> None:
    """Local unattended copy: DPAPI CurrentUser blob (fail-closed)."""
    if not SECURE_DIR.is_absolute():
        raise AutoVaultError("storage", "private storage unavailable; cannot store scheduler credential")
    store = DPAPIFileStore(SECURE_DIR)
    store.set_secret(CRED_NAME, secret)
    # also keep the canonical filename from the role spec
    try:
        SECURE_DIR.mkdir(parents=True, exist_ok=True)
        (SECURE_DIR / "scheduled-backup-key.dpapi").write_bytes(
            dpapi_protect(secret.encode("utf-8")))
        restrict_to_current_user(SECURE_DIR / "scheduled-backup-key.dpapi")
    except OSError as ex:
        raise AutoVaultError("storage", f"cannot write DPAPI key: {ex}")


def load_scheduler_credential() -> str:
    """Reads the DPAPI-protected backup secret. Wrong user / corrupt blob ->
    SecretStoreError; converted into a safe AutoVaultError by callers."""
    p = SECURE_DIR / "scheduled-backup-key.dpapi"
    if not SECURE_DIR.is_absolute() or not p.exists():
        raise AutoVaultError("credential_missing",
                             "DPAPI scheduler credential missing; run setup again")
    try:
        secret = dpapi_unprotect(p.read_bytes()).decode("utf-8", "replace")
    except Exception:
        raise AutoVaultError("credential_corrupt",
                             "DPAPI scheduler credential unreadable on this profile")
    if len(secret) < 32:
        raise AutoVaultError("credential_corrupt", "DPAPI scheduler credential malformed")
    return secret


def create_recovery_envelope(secret: str, master_password: str) -> None:
    """Portable envelope: master password -> Argon2id -> AES-256-GCM over the
    backup secret. Idempotent re-creation (setup may be re-run)."""
    if not SECURE_DIR.is_absolute():
        raise AutoVaultError("storage", "private storage unavailable")
    SECURE_DIR.mkdir(parents=True, exist_ok=True)
    if RECOVERY_ENVELOPE_PATH.exists():
        RECOVERY_ENVELOPE_PATH.unlink()
    vault = VaultStore(RECOVERY_ENVELOPE_PATH)
    vault.create(master_password, secrets={"backup_secret": secret})


def unlock_recovery_envelope(envelope_path: Path, master_password: str) -> str:
    vault = VaultStore(Path(envelope_path))
    secrets = vault.load(master_password)
    secret = secrets.get("backup_secret", "")
    if len(secret) < 32:
        raise AutoVaultError("envelope_corrupt", "recovery envelope has no valid secret")
    return secret


def verify_recovery_envelope(envelope_path: Path, master_password: str) -> bool:
    try:
        unlock_recovery_envelope(envelope_path, master_password)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# State collection
# ---------------------------------------------------------------------------
def _iter_watchedit_files() -> List[Tuple[str, Path]]:
    """(archive_relpath, abs_path) for WatchEdit mutable state. SECURE_DIR is
    deliberately excluded: its DPAPI blobs are machine-bound and must never be
    restored onto another machine's profile."""
    out: List[Tuple[str, Path]] = []
    for name in WATCHEDIT_FILES:
        p = LOCALAPPDATA_DIR / name
        if p.is_file():
            out.append((f"watchedit/{name}", p))
    for sub in WATCHEDIT_DIRS:
        d = LOCALAPPDATA_DIR / sub
        if d.is_dir():
            for p in sorted(d.rglob("*")):
                if p.is_file():
                    rel = p.relative_to(d)
                    if any(part in EXCLUDED_DIR_NAMES for part in rel.parts):
                        continue
                    out.append((f"watchedit/{sub}/{rel.as_posix()}", p))
    return out


def snapshot_sqlite(db_path: Path) -> bytes:
    """Online backup API: consistent snapshot even under WAL / live writes.
    Never touches disk with plaintext (in-memory dest only)."""
    db_path = Path(db_path)
    if not db_path.is_file():
        raise AutoVaultError("sqlite_missing", f"database not found: {db_path.name}")
    src = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    try:
        src.execute("PRAGMA busy_timeout=10000")
        dest = sqlite3.connect(":memory:")
        try:
            src.backup(dest)  # online backup API, WAL-safe
            dest.execute("PRAGMA quick_check")  # consistency gate on snapshot
            data = bytearray(dest.serialize())
        finally:
            dest.close()
        # The source may be in WAL mode; serialize() preserves the WAL header
        # (write/read version bytes = 2). A fresh :memory: connection defaults to
        # rollback journal and refuses to open a WAL-flagged image. The snapshot
        # is already a consistent checkpoint, so rewrite the header to rollback.
        if len(data) >= 20 and data[18] == 2 and data[19] == 2:
            data[18] = 1
            data[19] = 1
        return bytes(data)
    finally:
        src.close()


def _router_auth_files() -> List[Tuple[str, Path]]:
    out = []
    auth_dir = APPDATA_ROUTER / "auth"
    if auth_dir.is_dir():
        for p in sorted(auth_dir.rglob("*")):
            if p.is_file():
                rel = p.relative_to(auth_dir)
                if any(part in EXCLUDED_DIR_NAMES for part in rel.parts):
                    continue
                out.append((f"9router/auth/{rel.as_posix()}", p))
    return out


def collect_state(sqlite_snapshot: Optional[bytes] = None) -> Tuple[Dict[str, str], List[Dict]]:
    """Collects ALL selected mutable state as base64 entries + manifest metadata.
    Byte-preserving (W2-003 contract)."""
    entries: Dict[str, str] = {}
    meta: List[Dict] = []

    def add(rel: str, raw: bytes):
        if rel in entries:
            return
        entries[rel] = base64.b64encode(raw).decode("ascii")
        meta.append({
            "path": rel, "encoding": "base64",
            "size_bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
        })

    if sqlite_snapshot is not None:
        add(f"9router/{SQLITE_DB}", sqlite_snapshot)
    for name in ROUTER_FILES:
        p = APPDATA_ROUTER / name
        if p.is_file():
            add(f"9router/{name}", p.read_bytes())
    for rel, p in _router_auth_files():
        add(rel, p.read_bytes())
    for rel, p in _iter_watchedit_files():
        add(rel, p.read_bytes())

    if not entries:
        raise AutoVaultError("no_state", "no mutable state found to back up")
    return entries, meta


def compute_fingerprint(entries_meta: List[Dict]) -> str:
    """Deterministic state fingerprint from hashes/lengths/paths only."""
    h = hashlib.sha256()
    for m in sorted(entries_meta, key=lambda x: x["path"]):
        h.update(m["path"].encode("utf-8"))
        h.update(m["sha256"].encode("ascii"))
        h.update(str(m["size_bytes"]).encode("ascii"))
        h.update(b"\x00")
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Container creation (atomic, verified)
# ---------------------------------------------------------------------------
def _container_name(run_ts: datetime.datetime, fingerprint: str) -> str:
    return f"9ROUTER_AUTOVAULT_{run_ts.strftime('%Y%m%d_%H%M%S')}.wvault"


def _write_container(path: Path, entries: Dict[str, str], meta: List[Dict],
                     secret: str, recovery_envelope_bytes: bytes,
                     fingerprint: str, run_ts: datetime.datetime) -> None:
    payload_path = path.with_name(path.name + ".payload.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if payload_path.exists():
            payload_path.unlink()
        vault = VaultStore(payload_path)
        vault.create(secret, secrets=entries)
        # manifest: non-secret metadata ONLY
        manifest = {
            "format": "9router-autovault",
            "format_version": FORMAT_VERSION,
            "created_utc": run_ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "schema_version": 1,
            "payload_size_bytes": payload_path.stat().st_size,
            "entry_count": len(entries),
            "payload_sha256": hashlib.sha256(payload_path.read_bytes()).hexdigest(),
            "source_categories": sorted({m["path"].split("/")[0] for m in meta}),
            "state_fingerprint": fingerprint,
            "app": "9router_WatchEdit AutoVault",
            "verification": "pending",
            "entries": meta,
        }
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("manifest.json", json.dumps(manifest, indent=2))
            zf.write(payload_path, "payload.vault")
            zf.writestr("recovery.vault", recovery_envelope_bytes)
            zf.writestr("WARNING.txt",
                        "9ROUTER AUTOVAULT BACKUP.\n"
                        "Payload: Argon2id + AES-256-GCM.\n"
                        "Restore requires the MASTER PASSWORD via RESTORE_BACKUP.\n"
                        "Never share, never upload, never commit.\n")
    finally:
        payload_path.unlink(missing_ok=True)


def verify_container(path: Path, secret: Optional[str] = None,
                      envelope_bytes: Optional[bytes] = None,
                      master_password: Optional[str] = None,
                      check_sqlite: bool = True) -> Dict:
    """Full verification: container structure, manifest/payload hash, entry
    hash+length, optional secret source, SQLite quick_check. Fail-closed."""
    path = Path(path)
    with zipfile.ZipFile(path) as zf:
        names = set(zf.namelist())
        if names != {"manifest.json", "payload.vault", "recovery.vault", "WARNING.txt"}:
            raise AutoVaultError("container_corrupt", "unexpected container contents")
        manifest = json.loads(zf.read("manifest.json"))
        payload_bytes = zf.read("payload.vault")
        envelope = zf.read("recovery.vault")

    if hashlib.sha256(payload_bytes).hexdigest() != manifest.get("payload_sha256"):
        raise AutoVaultError("payload_tampered", "payload SHA-256 mismatch")
    if len(payload_bytes) != int(manifest.get("payload_size_bytes", -1)):
        raise AutoVaultError("payload_tampered", "payload length mismatch")

    resolved = secret
    if resolved is None:
        if envelope_bytes is None:
            envelope_bytes = envelope
        if master_password is None:
            raise AutoVaultError("verify_config", "no secret source for verification")
        resolved = unlock_recovery_envelope_via_bytes(envelope_bytes, master_password)
    elif envelope_bytes is not None and master_password is not None:
        # cross-check DPAPI secret == envelope secret
        if resolved != unlock_recovery_envelope_via_bytes(envelope_bytes, master_password):
            raise AutoVaultError("secret_mismatch", "DPAPI and recovery secrets differ")

    tmp = path.with_name(path.name + ".verify.tmp")
    try:
        tmp.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_bytes(payload_bytes)
        vault = VaultStore(tmp)
        entries = vault.load(resolved)
    finally:
        tmp.unlink(missing_ok=True)

    meta = {m["path"]: m for m in manifest.get("entries", [])}
    if len(meta) != len(entries):
        raise AutoVaultError("payload_tampered", "entry count mismatch")
    for rel, b64 in entries.items():
        m = meta.get(rel)
        if m is None:
            raise AutoVaultError("payload_tampered", f"unmanifested entry {rel}")
        raw = base64.b64decode(b64.encode("ascii"), validate=True)
        if len(raw) != int(m["size_bytes"]):
            raise AutoVaultError("payload_tampered", f"length mismatch {rel}")
        if hashlib.sha256(raw).hexdigest() != m["sha256"]:
            raise AutoVaultError("payload_tampered", f"SHA-256 mismatch {rel}")
        if check_sqlite and rel == f"9router/{SQLITE_DB}":
            _sqlite_quick_check(raw)
    return {"manifest": manifest, "entries": entries, "recovery_vault": envelope}


def _sqlite_quick_check(raw: bytes) -> None:
    try:
        conn = sqlite3.connect(":memory:")
        try:
            conn.deserialize(raw)
            row = conn.execute("PRAGMA quick_check").fetchone()
        finally:
            conn.close()
        if not row or str(row[0]).lower() != "ok":
            raise AutoVaultError("sqlite_invalid", "snapshot quick_check failed")
    except AutoVaultError:
        raise
    except Exception as ex:
        raise AutoVaultError("sqlite_invalid", f"snapshot unreadable: {type(ex).__name__}")


def unlock_recovery_envelope_via_bytes(envelope_bytes: bytes, master_password: str) -> str:
    tmp_dir = None
    import tempfile
    try:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td) / "recovery.vault"
            tmp.write_bytes(envelope_bytes)
            return unlock_recovery_envelope(tmp, master_password)
    except AutoVaultError:
        raise
    except Exception:
        raise AutoVaultError("envelope_corrupt", "recovery envelope unusable")


# ---------------------------------------------------------------------------
# Destination validation
# ---------------------------------------------------------------------------
def validate_destination(dest: Path) -> Optional[str]:
    """Returns a warning string (non-blocking) or raises on rejection."""
    dest = Path(dest).resolve()
    repo = REPO_ROOT.resolve()
    router_root = APPDATA_ROUTER.resolve() if str(APPDATA_ROUTER) else None
    we_root = LOCALAPPDATA_DIR.resolve() if str(LOCALAPPDATA_DIR) and LOCALAPPDATA_DIR.is_absolute() else None

    def inside(child: Path, parent: Path) -> bool:
        try:
            child.relative_to(parent)
            return True
        except ValueError:
            return False

    if inside(dest, repo):
        raise AutoVaultError("dest_rejected", "destination inside repository")
    if router_root and (inside(dest, router_root) or inside(router_root, dest)):
        raise AutoVaultError("dest_rejected", "destination overlaps live 9Router data")
    if we_root and (inside(dest, we_root) or inside(we_root, dest)):
        raise AutoVaultError("dest_rejected", "destination overlaps live WatchEdit data")
    if not dest.exists():
        dest.mkdir(parents=True, exist_ok=True)
    restrict_to_current_user(dest)
    # same-volume heuristic (non-blocking warning)
    try:
        same = os.stat(dest).st_dev == os.stat(APPDATA_ROUTER).st_dev if APPDATA_ROUTER.exists() else False
    except OSError:
        same = False
    return ("A backup on the same physical storage does not protect against disk failure."
            if same else None)


def list_valid_backups(dest: Path) -> List[Path]:
    out = []
    d = Path(dest)
    if not d.is_dir():
        return out
    for p in sorted(d.iterdir()):
        if p.is_file() and p.suffix == ".wvault" and p.name.startswith("9ROUTER_AUTOVAULT_") \
                and not p.name.endswith(".partial"):
            out.append(p)
    return sorted(out, key=lambda p: p.stat().st_mtime, reverse=True)


# ---------------------------------------------------------------------------
# Retention (prune only after a new verified backup)
# ---------------------------------------------------------------------------
def prune_retention(dest: Path, keep_count: int, max_mib: int,
                    protect: Path) -> List[str]:
    """Deletes oldest verified backups beyond count/size caps. NEVER deletes
    the newest valid backup or the just-created one. Returns deleted names."""
    deleted: List[str] = []
    valid = list_valid_backups(dest)
    if not valid:
        return deleted
    protect = Path(protect).resolve()
    newest = valid[0].resolve()

    candidates = [p for p in valid if p.resolve() != newest and p.resolve() != protect]
    # count cap
    if len(valid) > keep_count:
        excess = len(valid) - keep_count
        for p in candidates[:excess]:
            deleted.append(p.name)
            p.unlink()
    # size cap
    valid = list_valid_backups(dest)
    total = sum(p.stat().st_size for p in valid)
    cap = max_mib * 1024 * 1024
    if total > cap and len(valid) > 1:
        for p in reversed(valid[1:]):  # oldest first, newest never touched
            if total <= cap:
                break
            total -= p.stat().st_size
            deleted.append(p.name)
            p.unlink()
    return deleted


# ---------------------------------------------------------------------------
# Backup run (scheduled / manual)
# ---------------------------------------------------------------------------
def run_backup(dest: Path, secret: Optional[str] = None,
               master_password: Optional[str] = None,
               scheduled_dir: Optional[Path] = None,
               retention_count: Optional[int] = None,
               retention_mib: Optional[int] = None,
               force: bool = False) -> Tuple[str, Optional[Path]]:
    """Returns (result, path|None). Raises AutoVaultError on failure.
    Never receives a password on a scheduled run: secret comes from DPAPI."""
    dest = Path(dest).resolve()
    sd = Path(scheduled_dir) if scheduled_dir else Path(SCHEDULED_DIR)
    lock = BackupLock(sd / "autovault.lock")
    if not lock.acquire():
        return R_ALREADY_RUNNING, None
    run_id = pysecrets.token_hex(4)
    t0 = time.monotonic()
    try:
        cfg = _read_config(scheduled_dir)
        keep_count = retention_count or int(cfg.get("retention_count", DEFAULT_RETENTION_COUNT))
        keep_mib = retention_mib or int(cfg.get("retention_mib", DEFAULT_RETENTION_MIB))

        snap = snapshot_sqlite(APPDATA_ROUTER / SQLITE_DB)
        entries, meta = collect_state(snap)
        fingerprint = compute_fingerprint(meta)

        prev = _read_state(scheduled_dir).get("last_fingerprint", "")
        if not force and fingerprint == prev:
            log_run(R_NO_CHANGES.replace(" - ", " "), run_id, time.monotonic() - t0,
                    fingerprint, "", 0, 0, scheduled_dir=scheduled_dir)
            return R_NO_CHANGES, None

        if secret is None:
            secret = load_scheduler_credential()
        if not RECOVERY_ENVELOPE_PATH.exists():
            raise AutoVaultError("envelope_missing", "recovery envelope missing; run setup again")
        envelope_bytes = RECOVERY_ENVELOPE_PATH.read_bytes()

        ts = datetime.datetime.now()
        final = dest / _container_name(ts, fingerprint)
        n = 1
        while final.exists():
            n += 1
            final = dest / f"9ROUTER_AUTOVAULT_{ts.strftime('%Y%m%d_%H%M%S')}_{n}.wvault"
        partial = final.with_name(final.name + ".partial")
        _write_container(partial, entries, meta, secret, envelope_bytes, fingerprint, ts)

        verify_container(partial, secret=secret, check_sqlite=True)
        os.replace(partial, final)  # atomic: valid name only after verification

        # verify once more at final location, then prune
        verify_container(final, secret=secret, check_sqlite=True)
        prune_retention(dest, keep_count, keep_mib, protect=final)
        _write_state(scheduled_dir, {
            "last_fingerprint": fingerprint,
            "last_backup": final.name,
            "last_backup_utc": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "last_result": R_SUCCESS,
            "entry_count": len(entries),
        })
        log_run(R_SUCCESS, run_id, time.monotonic() - t0, fingerprint,
                final.name, final.stat().st_size, len(entries), scheduled_dir=scheduled_dir)
        return R_SUCCESS, final
    finally:
        lock.release()


def _read_config(scheduled_dir: Optional[Path] = None) -> Dict:
    sd = Path(scheduled_dir) if scheduled_dir else Path(SCHEDULED_DIR)
    try:
        return json.loads((sd / "autovault-config.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write_config(scheduled_dir: Optional[Path], data: Dict) -> None:
    d = Path(scheduled_dir) if scheduled_dir else Path(SCHEDULED_DIR)
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / ".autovault-config.tmp"
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(d / "autovault-config.json")


# ---------------------------------------------------------------------------
# Task Scheduler integration (idempotent, current user, no stored password)
# ---------------------------------------------------------------------------
def _ps_quote(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def register_task(exe: str, script: str, hours: int, task_name: str = TASK_NAME) -> bool:
    """Registers/updates the scheduled task (overwrite semantics = idempotent,
    never duplicate names). InteractiveToken: no stored Windows password, runs
    only when this user is logged on - compatible with DPAPI CurrentUser."""
    ps = "\n".join([
        "$ErrorActionPreference = 'Stop'",
        "try { Unregister-ScheduledTask -TaskName " + _ps_quote(task_name) + " -Confirm:$false -ErrorAction SilentlyContinue } catch {}",
        "$a = New-ScheduledTaskAction -Execute " + _ps_quote(exe) + " -Argument " + _ps_quote('"' + script + '" run'),
        "$t = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(2) -RepetitionInterval (New-TimeSpan -Hours " + str(int(hours)) + ") -RepetitionDuration (New-TimeSpan -Days 3650)",
        "$s = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 1) -Hidden",
        "$s.RestartCount = 2",
        "$p = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited",
        "Register-ScheduledTask -TaskName " + _ps_quote(task_name) + " -Action $a -Trigger $t -Settings $s -Principal $p | Out-Null",
        "Write-Output OK",
    ])
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                           capture_output=True, text=True, timeout=60)
        return r.returncode == 0 and "OK" in r.stdout
    except Exception:
        return False


def remove_task(task_name: str = TASK_NAME) -> bool:
    ps = ("try { Unregister-ScheduledTask -TaskName " + _ps_quote(task_name) +
          " -Confirm:$false -ErrorAction Stop; Write-Output OK } catch { Write-Output NO }")
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                           capture_output=True, text=True, timeout=60)
        return r.returncode == 0 and "OK" in r.stdout
    except Exception:
        return False


def task_status(task_name: str = TASK_NAME) -> Dict:
    ps = "\n".join([
        "$ErrorActionPreference = 'Stop'",
        "try {",
        "  $t = Get-ScheduledTask -TaskName " + _ps_quote(task_name) + " -ErrorAction Stop",
        "  $i = $t | Get-ScheduledTaskInfo",
        "  Write-Output ('STATE=' + $t.State)",
        "  Write-Output ('LAST=' + $i.LastRunTime.ToString('o'))",
        "  Write-Output ('LASTRESULT=' + $i.LastTaskResult)",
        "  Write-Output ('NEXT=' + $i.NextRunTime.ToString('o'))",
        "} catch { Write-Output 'STATE=MISSING' }",
    ])
    out = {"state": "MISSING", "last_run": "", "last_result": "", "next_run": ""}
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                          capture_output=True, text=True, timeout=60)
        for line in (r.stdout or "").splitlines():
            if line.startswith("STATE="):
                out["state"] = line[6:]
            elif line.startswith("LAST="):
                out["last_run"] = line[5:]
            elif line.startswith("LASTRESULT="):
                out["last_result"] = line[11:]
            elif line.startswith("NEXT="):
                out["next_run"] = line[5:]
    except Exception:
        pass
    return out


# ---------------------------------------------------------------------------
# Setup wizard
# ---------------------------------------------------------------------------
def _prompt(label: str, default: str = "") -> str:
    try:
        v = input(f"{label}: ").strip()
    except EOFError:
        v = ""
    return v or default


def _read_password_win(prompt: str):
    """Windows console password reader via msvcrt, no echo. Returns None when
    no console input is available (redirected stdin / EOF) so callers fall
    back to another reader instead of hanging."""
    print(prompt, end="", flush=True)
    chars: List[str] = []
    while True:
        try:
            ch = msvcrt.getwch()
        except (OSError, EOFError):
            print()
            return None
        if ch in ("\r", "\n"):
            print()
            break
        if ch in ("\x00", "\xe0"):  # arrow/function keys: swallow prefix+code
            msvcrt.getwch()
            continue
        if ch == "\x03":  # Ctrl+C
            print()
            raise KeyboardInterrupt
        if ch == "\x08":  # Backspace
            if chars:
                chars.pop()
            continue
        chars.append(ch)
    return "".join(chars)


def read_password(prompt: str) -> str:
    """Echo-free password read. On Windows the msvcrt console reader runs
    FIRST (deterministic in cmd/PowerShell/Windows Terminal); getpass opens
    CON$ and can block with no echo when the console handle is unusual.
    Non-tty stdin falls back to plain input() so piped runs never deadlock."""
    if sys.platform == "win32":
        try:
            if sys.stdin is not None and sys.stdin.isatty():
                pw = _read_password_win(prompt)
                if pw is not None:
                    return pw
        except Exception:
            pass
        if sys.stdin is None or not sys.stdin.isatty():
            return input(prompt)
        try:
            return getpass.getpass(prompt)
        except Exception:
            return input(prompt)
    try:
        return getpass.getpass(prompt)
    except Exception:
        return input(prompt)


def read_master_password_twice(env: Optional[str] = None) -> Optional[str]:
    """env = NAME of an environment variable holding the password
    (non-interactive use only; the value itself never hits argv/logs)."""
    if env:
        pw = os.environ.get(env, "")
        if not pw:
            print(f"Environment variable {env} is empty.", file=sys.stderr)
            return None
        if len(pw) < MIN_MASTER_PASSWORD_LEN:
            print(f"Master password must be at least {MIN_MASTER_PASSWORD_LEN} characters.",
                  file=sys.stderr)
            return None
        return pw
    pw1 = read_password("Create master password (min 8 chars): ")
    pw2 = read_password("Repeat master password: ")
    if pw1 != pw2:
        print("Passwords do not match.")
        return None
    if len(pw1) < MIN_MASTER_PASSWORD_LEN:
        print(f"Master password must be at least {MIN_MASTER_PASSWORD_LEN} characters.")
        return None
    return pw1


def setup(dest: Path, hours: int, retention_count: int, retention_mib: int,
          master_password: str, task_name: str = TASK_NAME,
          register: bool = True) -> Dict:
    """Full setup: validate config, store credentials, register task, run and
    verify the first real backup. Fail-closed at every step."""
    warning = validate_destination(dest)
    if warning:
        print("WARNING: " + warning)

    if not VaultStore.available():
        raise AutoVaultError("deps", "vault libraries missing (cryptography, argon2-cffi)")

    # credentials
    secret = generate_backup_secret()
    store_scheduler_credential(secret)
    create_recovery_envelope(secret, master_password)
    # envelope round-trip gate before anything else succeeds
    if not verify_recovery_envelope(RECOVERY_ENVELOPE_PATH, master_password):
        raise AutoVaultError("envelope_corrupt", "recovery envelope failed round-trip")

    _write_config(SCHEDULED_DIR, {
        "destination": str(Path(dest).resolve()),
        "schedule_hours": int(hours),
        "retention_count": int(retention_count),
        "retention_mib": int(retention_mib),
        "task_name": task_name,
        "setup_utc": datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%SZ"),
    })

    task_ok = True
    if register:
        task_ok = register_task(sys.executable, str(Path(__file__).resolve()), hours, task_name)
        if not task_ok:
            raise AutoVaultError("task_register", "Task Scheduler registration failed")

        result, path = run_backup(Path(dest), secret=secret, force=True)
        if result != R_SUCCESS or path is None:
            raise AutoVaultError("first_backup", f"first backup failed: {result}")

    return {
        "task_registered": task_ok,
        "backup_path": path,
        "warning": warning,
        "destination": str(Path(dest).resolve()),
        "schedule_hours": hours,
    }


# ---------------------------------------------------------------------------
# Restore
# ---------------------------------------------------------------------------
def restore_backup(backup_path: Path, master_password: str, out_dirs: Optional[Dict[str, Path]] = None,
                   apply: bool = False) -> Dict:
    """Authenticates via master password -> recovery envelope -> payload.
    apply=False: verify-only, no writes. apply=True writes to out_dirs
    (or the LIVE locations). Fails closed on any integrity failure BEFORE
    any write."""
    backup_path = Path(backup_path)
    if not backup_path.is_file():
        raise AutoVaultError("backup_missing", f"backup not found: {backup_path.name}")
    with zipfile.ZipFile(backup_path) as zf:
        envelope = zf.read("recovery.vault")
    secret = unlock_recovery_envelope_via_bytes(envelope, master_password)
    info = verify_container(backup_path, secret=secret, check_sqlite=True)
    if not apply:
        return {"verified": True, "entries": sorted(info["entries"].keys()),
                "manifest": info["manifest"]}

    # apply: decode all entries, verify again, then write
    entries = {}
    meta = {m["path"]: m for m in info["manifest"]["entries"]}
    for rel, b64 in info["entries"].items():
        raw = base64.b64decode(b64.encode("ascii"), validate=True)
        m = meta[rel]
        if len(raw) != int(m["size_bytes"]) or hashlib.sha256(raw).hexdigest() != m["sha256"]:
            raise AutoVaultError("payload_tampered", f"entry invalid before write: {rel}")
        entries[rel] = raw

    if out_dirs is None:
        # live layout: 9router/* -> APPDATA_ROUTER, watchedit/* -> LOCALAPPDATA_DIR
        from core.redaction import redact_mapping
        out_dirs = {"9router": APPDATA_ROUTER, "watchedit": LOCALAPPDATA_DIR}

    written: List[str] = []
    for rel, raw in entries.items():
        cat, _, sub = rel.partition("/")
        base = Path(out_dirs[cat])
        target = (base / sub).resolve()
        try:
            target.relative_to(base.resolve())
        except ValueError:
            raise AutoVaultError("restore_escape", f"entry escapes destination: {rel}")
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".restore.tmp")
        tmp.write_bytes(raw)
        os.replace(tmp, target)
        written.append(rel)
    return {"verified": True, "restored": written, "entries": sorted(entries.keys()),
            "manifest": info["manifest"]}


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------
def status(scheduled_dir: Optional[Path] = None) -> str:
    cfg = _read_config(scheduled_dir)
    dest = cfg.get("destination", "")
    task = task_status(cfg.get("task_name", TASK_NAME))
    state = _read_state(scheduled_dir)
    lines = ["9Router AutoVault status", ""]
    state_map = {"Running": "Enabled (running now)", "Ready": "Enabled",
                 "Disabled": "Disabled", "MISSING": "Missing"}
    lines.append(f"Task: {state_map.get(task['state'], task['state'])}")
    lines.append(f"Last scheduled run: {task['last_run'] or 'never'}")
    lines.append(f"Last backup: {state.get('last_backup', 'none')} ({state.get('last_backup_utc', '-')})")
    lines.append(f"Last result: {state.get('last_result', '-')}")
    lines.append(f"Next scheduled run: {task['next_run'] or '-'}")
    lines.append(f"Destination: {dest or 'not configured'}")
    if dest:
        valid = list_valid_backups(Path(dest))
        total = sum(p.stat().st_size for p in valid)
        lines.append(f"Backup count: {len(valid)}")
        lines.append(f"Total size: {total / (1024 * 1024):.1f} MiB")
    lines.append(f"Recovery envelope: {'Present' if RECOVERY_ENVELOPE_PATH.exists() else 'Missing'}")
    lines.append(f"DPAPI scheduler credential: {'Present' if (SECURE_DIR / 'scheduled-backup-key.dpapi').exists() else 'Missing'}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="9Router AutoVault", allow_abbrev=False)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run", help="scheduled/manual backup run")
    s = sub.add_parser("setup", help="interactive setup wizard")
    s.add_argument("--password-env", default=None,
                  help="env var holding the master password (non-interactive use only)")
    r = sub.add_parser("restore", help="restore from a backup")
    r.add_argument("backup", nargs="?", help="backup .wvault (default: newest in destination)")
    r.add_argument("--verify-only", action="store_true")
    r.add_argument("--password-env", default=None,
                  help="env var holding the master password (non-interactive use only)")
    sub.add_parser("status", help="show status")
    sub.add_parser("remove", help="remove task + credential")
    args = ap.parse_args(argv)

    if args.cmd == "run":
        cfg = _read_config(SCHEDULED_DIR)
        dest = cfg.get("destination", "")
        if not dest:
            print("AUTO BACKUP: NOT CONFIGURED - run SETUP_AUTO_BACKUP.cmd first", file=sys.stderr)
            return 2
        try:
            result, path = run_backup(Path(dest))
            print(f"AUTO BACKUP: {result}" + (f" - {path.name}" if path else ""))
            return 0 if result in (R_SUCCESS, R_NO_CHANGES, R_ALREADY_RUNNING) else 1
        except AutoVaultError as ex:
            print(f"AUTO BACKUP: {R_FAILED} {ex}", file=sys.stderr)
            log_run(R_FAILED, "-", 0.0, "", "", 0, 0, error_category=ex.category)
            return 1
        except Exception as ex:
            print(f"AUTO BACKUP: {R_FAILED} {redact_text(str(ex))}", file=sys.stderr)
            log_run(R_FAILED, "-", 0.0, "", "", 0, 0, error_category=type(ex).__name__)
            return 1

    if args.cmd == "setup":
        print("9Router AutoVault Setup")
        print()
        router_state = "Detected" if (APPDATA_ROUTER / SQLITE_DB).is_file() else "NOT FOUND"
        we_state = "Detected" if (LOCALAPPDATA_DIR / "settings.json").is_file() else "NOT FOUND"
        print(f"9Router state: {router_state}")
        print(f"WatchEdit state: {we_state}")
        if "NOT FOUND" in (router_state, we_state):
            print("Mutable state missing - aborting.", file=sys.stderr)
            return 1
        print()
        default_dest = str(SCHEDULED_DIR)
        dest_s = _prompt("Backup destination", default_dest)
        dest = Path(dest_s).expanduser()
        try:
            warning = validate_destination(dest)
        except AutoVaultError as ex:
            print(f"Destination rejected: {ex}", file=sys.stderr)
            return 1
        if warning:
            print("WARNING: " + warning)
        print()
        print("Schedule:")
        for k, v in SCHEDULE_CHOICES.items():
            mark = " (default)" if v == DEFAULT_SCHEDULE_HOURS else ""
            print(f"  {k}. every {v} hours{mark}")
        hours = int(SCHEDULE_CHOICES.get(_prompt("Choose schedule [1-5]", "3"), DEFAULT_SCHEDULE_HOURS))
        rc = _prompt("Retention count (default 20)", "20")
        rm = _prompt("Retention size MiB (default 1024)", "1024")
        try:
            retention_count = max(1, int(rc or 20))
            retention_mib = max(1, int(rm or 1024))
        except ValueError:
            retention_count, retention_mib = DEFAULT_RETENTION_COUNT, DEFAULT_RETENTION_MIB
        print()
        print(f"Destination: {dest.resolve()}")
        print(f"Schedule: every {hours} hours")
        print(f"Retention: {retention_count} backups / {retention_mib} MiB")
        print("Encryption: Argon2id + AES-256-GCM")
        print("           Windows DPAPI for unattended local execution")
        print()
        _prompt("Press Enter to continue", "")
        print("(Input is hidden while typing the password.)")
        pw = read_master_password_twice(args.password_env)
        if pw is None:
            return 1
        print()
        try:
            result = setup(dest, hours, retention_count, retention_mib, pw)
        except AutoVaultError as ex:
            print(f"SETUP FAILED: {ex}", file=sys.stderr)
            print("Previous backups (if any) remain untouched.", file=sys.stderr)
            return 1
        except Exception as ex:
            print(f"SETUP FAILED: {redact_text(str(ex))}", file=sys.stderr)
            return 1
        print()
        print("AUTO BACKUP: ACTIVE")
        print(f"Last backup: {result['backup_path'].name}")
        print(f"Next run: every {hours} hours (Task Scheduler '{TASK_NAME}', StartWhenAvailable)")
        print(f"Destination: {result['destination']}")
        print("Restore protection: Master Password")
        return 0

    if args.cmd == "restore":
        cfg = _read_config(SCHEDULED_DIR)
        if args.backup:
            bp = Path(args.backup)
        else:
            dest = cfg.get("destination", "")
            valid = list_valid_backups(Path(dest)) if dest else []
            if not valid:
                print("No backups found. Provide a .wvault path.", file=sys.stderr)
                return 1
            bp = valid[0]
        if args.password_env:
            pw = os.environ.get(args.password_env, "")
            if not pw:
                print(f"Environment variable {args.password_env} is empty.", file=sys.stderr)
                return 1
        else:
            print("(Input is hidden while typing.)")
            pw = read_password("Master password: ")
        try:
            if args.verify_only:
                info = restore_backup(bp, pw, apply=False)
                print(f"VERIFY OK: {len(info['entries'])} entries, backup decryptable and internally valid")
                print("No live state was modified.")
                return 0
            info = restore_backup(bp, pw, apply=False)
            print("Backup verified. Would restore:")
            for rel in info["entries"]:
                print(f"  {rel}")
            ans = _prompt("Type RESTORE to apply to live state", "")
            if ans != "RESTORE":
                print("Aborted. Nothing was modified.")
                return 0
            # safety backup of current state before destructive restore
            print("Creating safety backup of current state first...")
            cfg_dest = cfg.get("destination", str(SCHEDULED_DIR / "pre-restore"))
            cur_secret = None
            try:
                cur_secret = load_scheduler_credential()
            except AutoVaultError:
                pass
            if cur_secret and RECOVERY_ENVELOPE_PATH.exists():
                r, _p = run_backup(Path(cfg_dest), secret=cur_secret, force=True)
            else:
                r, _p = None, None
            if r != R_SUCCESS:
                print("WARNING: pre-restore safety backup failed; continuing is risky.")
                if _prompt("Continue anyway? (yes/no)", "no").lower() != "yes":
                    print("Aborted. Nothing was modified.")
                    return 0
            result = restore_backup(bp, pw, apply=True)
            print(f"Restored {len(result['restored'])} entries.")
            print("Restart 9Router to load the restored state.")
            return 0
        except AutoVaultError as ex:
            print(f"RESTORE FAILED (nothing modified): {ex}", file=sys.stderr)
            return 1

    if args.cmd == "status":
        print(status())
        return 0

    if args.cmd == "remove":
        ok_task = remove_task()
        try:
            DPAPIFileStore(SECURE_DIR).delete_secret(CRED_NAME)
            (SECURE_DIR / "scheduled-backup-key.dpapi").unlink(missing_ok=True)
            ok_cred = True
        except Exception:
            ok_cred = False
        ok_env = True
        try:
            RECOVERY_ENVELOPE_PATH.unlink(missing_ok=True)
        except OSError:
            ok_env = False
        _write_state(SCHEDULED_DIR, {"last_result": "REMOVED"})
        print(f"Task removed: {ok_task}")
        print(f"Scheduler credential removed: {ok_cred}")
        print(f"Recovery envelope removed: {ok_env}")
        print("Backups on disk were NOT deleted.")
        return 0 if (ok_task or ok_cred or ok_env) else 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
