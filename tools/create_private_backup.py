#!/usr/bin/env python3
"""
create_private_backup.py - FULL PRIVATE BACKUP creator (explicit user action).

Packages local runtime/private state into a password-encrypted archive OUTSIDE
the repository:

    %LOCALAPPDATA%\\9router_WatchEdit\\backups\\private\\PRIVATE_SECRET_BACKUP_<ts>.wvault

The name is deliberately loud: six months from now, nobody should upload a file
called PRIVATE_SECRET_BACKUP_*.wvault anywhere by accident.

Contents (all local, never from the repository): config/, secure/, health
cache, presets, settings. The .wvault container is a zip whose payload entries
are encrypted with the WatchEdit vault crypto (Argon2id + AES-256-GCM).

Password is prompted interactively (getpass); it is never written to disk,
passed on the command line, or logged.

Usage:
    python tools/create_private_backup.py [--password-env VAR]   # non-interactive tests only
"""
from __future__ import annotations

import argparse
import datetime
import getpass
import io
import json
import os
import sys
import zipfile
from pathlib import Path
from typing import List, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "9router_WatchEdit"))

from config import (  # noqa: E402
    HEALTH_CACHE_FILE, LOCALAPPDATA_DIR, PRESETS_FILE, PRIVATE_BACKUP_DIR,
    SECURE_DIR, SETTINGS_FILE,
)
from core.secret_store import VaultStore  # noqa: E402


def _collect_sources() -> List[Path]:
    sources: List[Path] = []
    candidates: List[Path] = [HEALTH_CACHE_FILE, PRESETS_FILE, SETTINGS_FILE]
    if SECURE_DIR.exists():
        candidates.extend(sorted(SECURE_DIR.rglob("*")))
    config_dir = LOCALAPPDATA_DIR / "config"
    if config_dir.exists():
        candidates.extend(sorted(config_dir.rglob("*")))
    for p in candidates:
        p = Path(p)
        if p.is_file():
            sources.append(p)
    return sources


def create_private_backup(password: str, out_dir: Path = PRIVATE_BACKUP_DIR) -> Path:
    if not password or len(password) < 8:
        raise ValueError("Master password must be at least 8 characters")
    out_dir = Path(out_dir).resolve()
    # BACKUP-001: private backups must NEVER be written inside the repository
    if str(out_dir).startswith(str(REPO_ROOT.resolve()) + os.sep) or out_dir == REPO_ROOT.resolve():
        raise ValueError(
            "Private backup destination must be OUTSIDE the repository "
            f"(refused: {out_dir} is inside {REPO_ROOT})"
        )
    out_dir.mkdir(parents=True, exist_ok=True)

    sources = _collect_sources()
    if not sources:
        raise RuntimeError("No local private material found to back up")

    # 1. Stage plaintext entries ONLY in memory (never a plaintext temp file).
    entries: dict = {}
    for p in sources:
        try:
            rel = str(p.relative_to(LOCALAPPDATA_DIR))
        except ValueError:
            rel = p.name
        entries[rel] = p.read_bytes().decode("utf-8", "replace")

    # 2. Encrypt the whole manifest with the vault construction.
    vault_path = out_dir / ".tmp_vault.vault"
    if vault_path.exists():
        vault_path.unlink()
    vault = VaultStore(vault_path)
    vault.create(password, secrets=entries)

    # 3. Wrap in a clearly-named container.
    stamp = datetime.datetime.now().strftime("%Y-%m-%d")
    out_path = out_dir / f"PRIVATE_SECRET_BACKUP_{stamp}.wvault"
    n = 1
    while out_path.exists():
        n += 1
        out_path = out_dir / f"PRIVATE_SECRET_BACKUP_{stamp}_{n}.wvault"
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("WARNING.txt",
                    "PRIVATE SECRET BACKUP - NEVER SHARE, NEVER UPLOAD, NEVER COMMIT.\n"
                    "Payload is Argon2id+AES-256-GCM encrypted; requires the master password.\n")
        zf.write(vault_path, "payload.vault")
        zf.writestr("manifest.json", json.dumps({
            "created": datetime.datetime.now().isoformat(),
            "entry_count": len(entries),
            "entries": sorted(entries.keys()),
            "crypto": "argon2id + aes-256-gcm",
        }, indent=2))
    vault_path.unlink()

    from core.secret_store import restrict_to_current_user
    restrict_to_current_user(out_dir)
    return out_path


class _NoEchoArgumentParser(argparse.ArgumentParser):
    """REDACT-006: argparse echoes offending argument VALUES into its usage
    error. A password passed by mistake must never be printed back."""

    def error(self, message):
        self.print_usage(sys.stderr)
        sys.stderr.write("error: unrecognized/invalid argument (value redacted)\n")
        sys.exit(2)


def main(argv=None) -> int:
    import argparse
    ap = _NoEchoArgumentParser(
        description="Create encrypted PRIVATE backup (outside repository)",
        allow_abbrev=False)  # REDACT-006: --password must never abbreviate --password-env
    ap.add_argument("--password-env", default=None,
                    help="env var holding the master password (non-interactive use only)")
    args = ap.parse_args(argv)

    if args.password_env:
        password = os.environ.get(args.password_env, "")
    else:
        print("FULL PRIVATE BACKUP - creates an encrypted archive under:")
        print(f"  {PRIVATE_BACKUP_DIR}")
        pw1 = getpass.getpass("Master password (min 8 chars): ")
        pw2 = getpass.getpass("Repeat master password: ")
        if pw1 != pw2:
            print("Passwords do not match.", file=sys.stderr)
            return 1
        password = pw1

    try:
        path = create_private_backup(password)
    except Exception as ex:
        print(f"PRIVATE BACKUP FAILED: {ex}", file=sys.stderr)
        return 1
    print(f"PRIVATE backup written: {path}")
    print("This file contains secrets. Keep it outside the repository and never share it.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
