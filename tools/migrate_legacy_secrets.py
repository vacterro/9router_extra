#!/usr/bin/env python3
"""
migrate_legacy_secrets.py - One-time legacy private-data migration.

Detects secret-bearing files INSIDE the repository, reports

    LEGACY PRIVATE DATA DETECTED

and relocates them to the LOCAL SECRET LAYER:

    %LOCALAPPDATA%\\9router_WatchEdit\\backups\\legacy_<timestamp>\\

Safety properties:
 - relocation is verified (size + sha256) BEFORE the repository copy is removed
 - nothing is deleted if verification fails
 - secret values are never printed
 - restrictive ACLs (current user only) applied where practical
 - the provider-state export additionally produces a sanitized fixture in
   9router_WatchEdit/tests/fixtures/ before the private original is removed

Usage:
    python tools/migrate_legacy_secrets.py [--scan-only] [--repo ROOT]
"""
from __future__ import annotations

import argparse
import hashlib
import re
import shutil
import sys
import time
from pathlib import Path
from typing import List

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "9router_WatchEdit"))

from config import LEGACY_BACKUP_ROOT  # noqa: E402

DETECT_RULES = [
    re.compile(r"(?i)(^|/)backup/providers-state-export\.json$"),
    re.compile(r"(?i)(^|/)backup/data-snapshot/.*$"),
    re.compile(r"(?i)(^|/)(jwt-secret|machine-id)$"),
    re.compile(r"(?i)\.sqlite(-wal|-shm)?$"),
    re.compile(r"(?i)(^|/)\.env(\..*)?$"),
    re.compile(r"(?i)(^|/)credentials\.json$"),
    re.compile(r"(?i)\.vault$"),
]

# Never touch these even if names match (tooling, engine artifacts).
KEEP_RULES = [
    re.compile(r"(?i)(^|/)\.git/"),
    re.compile(r"(?i)(^|/)packages/.*\.tgz$"),   # public engine release tarballs
    re.compile(r"(?i)(^|/)9router_WatchEdit/tests/fixtures/.*_sanitized.*\.json$"),
]


def detect_private_files(repo_root: Path) -> List[Path]:
    hits: List[Path] = []
    for p in sorted(repo_root.rglob("*")):
        if not p.is_file():
            continue
        rel = "/" + str(p.relative_to(repo_root)).replace("\\", "/")
        if any(rx.search(rel) for rx in KEEP_RULES):
            continue
        if any(rx.search(rel) for rx in DETECT_RULES):
            hits.append(p)
    return hits


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def migrate(scan_only: bool = False, repo_root: Path = None, legacy_root: Path = None) -> int:
    repo_root = Path(repo_root) if repo_root else REPO_ROOT
    legacy_root = Path(legacy_root) if legacy_root else LEGACY_BACKUP_ROOT

    private_files = detect_private_files(repo_root)
    if not private_files:
        print("NO LEGACY PRIVATE DATA DETECTED IN REPOSITORY")
        return 0

    print("LEGACY PRIVATE DATA DETECTED")
    print("=" * 60)
    for p in private_files:
        print(f"  {p.relative_to(repo_root)}  ({p.stat().st_size} bytes)")
    print("=" * 60)

    if scan_only:
        print("Scan-only mode: nothing moved.")
        return 1  # signal: cleanup still required

    stamp = time.strftime("%Y%m%d_%H%M%S")
    target_root = legacy_root / f"legacy_{stamp}"
    target_root.mkdir(parents=True, exist_ok=True)

    # Sanitized fixture generation from the provider export (before removal)
    fixture_note = "no provider export found"
    export = repo_root / "backup" / "providers-state-export.json"
    if export in private_files:
        sys.path.insert(0, str(REPO_ROOT / "tools"))
        from sanitize_9router_state import sanitize_export
        fixture = repo_root / "9router_WatchEdit" / "tests" / "fixtures" / "provider_state_sanitized.json"
        try:
            n = sanitize_export(export, fixture)
            fixture_note = f"sanitized fixture written: {fixture} ({n} fields redacted)"
        except Exception as ex:
            print(f"WARNING: fixture generation failed ({type(ex).__name__}); export still moved privately.")

    moved, failed = [], []
    for p in private_files:
        rel = str(p.relative_to(repo_root)).replace("\\", "/")
        dst = target_root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copy2(p, dst)
            if _sha256(p) != _sha256(dst) or p.stat().st_size != dst.stat().st_size:
                raise IOError("verification mismatch after copy")
            p.unlink()  # verified: remove repository copy
            moved.append(rel)
        except Exception as ex:
            failed.append((rel, f"{type(ex).__name__}: {ex}"))

    from core.secret_store import restrict_to_current_user
    restrict_to_current_user(target_root)

    print(f"Relocated {len(moved)} file(s) to:\n  {target_root}")
    print(fixture_note)
    if failed:
        print("FAILED (repository copies KEPT for safety):", file=sys.stderr)
        for rel, why in failed:
            print(f"  {rel}: {why}", file=sys.stderr)
        return 2
    print("Repository is now free of the detected private material.")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Migrate legacy private data out of the repository")
    ap.add_argument("--scan-only", action="store_true")
    ap.add_argument("--repo", default=None)
    args = ap.parse_args(argv)
    return migrate(scan_only=args.scan_only, repo_root=Path(args.repo) if args.repo else None)


if __name__ == "__main__":
    sys.exit(main())
