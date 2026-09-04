#!/usr/bin/env python3
"""
apply_agent_patch.py - GUARDED patch-application fallback.

Patches are only a fallback transport (the primary workflow is ordinary Git
commits). This tool ensures a patch can never become a filesystem backdoor:

 - target paths are parsed from the diff headers (--- a/...  +++ b/...)
 - every target is CANONICALIZED (Path.resolve) before authorization
 - absolute Windows paths, UNC paths, drive-relative paths and any '..'
   escape are rejected (PATCH-001..004)
 - protected target names are rejected case-insensitively (PATCH-005):
   *.sqlite/-wal/-shm, *.db, .env*, *.pem, *.key, *.pfx, *.p12, *.vault,
   machine-id, jwt-secret, provider-state exports, private backup patterns
 - renames are validated on the FINAL target name (PATCH-006):
   safe.txt -> data.sqlite is blocked
 - the actual application is delegated to `git apply --check` + `git apply`

Usage:
    python tools/apply_agent_patch.py <patch.diff> [--repo ROOT] [--dry-run]
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path, PureWindowsPath
from typing import List, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent

PROTECTED_SUFFIXES = {
    ".sqlite", ".sqlite-wal", ".sqlite-shm", ".db", ".db-wal", ".db-shm",
    ".env", ".pem", ".key", ".pfx", ".p12", ".vault",
}
PROTECTED_NAME_RE = re.compile(
    r'(?i)(^|/)(\.env(\..*)?|jwt-secret|machine-id|machineid|'
    r'providers-state-export.*|provider-state-export.*|PRIVATE_SECRET_BACKUP.*|.*\.wvault)$'
)

# diff header lines that carry target paths are matched inline in
# extract_targets(); authorization always uses the FINAL (new) target name.


class PatchRejected(RuntimeError):
    pass


def extract_targets(diff_text: str) -> List[str]:
    """Extract every (old, new) target path mentioned in diff headers.

    For renames the NEW path is the final target and is what authorization
    uses (PATCH-006)."""
    targets = set()
    for line in diff_text.splitlines():
        m = re.match(r'^diff --git a/(.+?) b/(.+)$', line)
        if m:
            targets.add(m.group(2))
            continue
        m = re.match(r'^rename to (.+)$', line)
        if m:
            targets.add(m.group(1))
            continue
        m = re.match(r'^new file mode', line)
        if m:
            continue
        m = re.match(r'^copy to (.+)$', line)
        if m:
            targets.add(m.group(1))
            continue
        m = re.match(r'^\+\+\+ (?:b/)?(.+)$', line)
        if m and not m.group(1).startswith("/dev"):
            targets.add(m.group(1))
    return sorted(targets)


def authorize_target(raw_target: str, repo_root: Path) -> Path:
    """Canonicalize and authorize a single patch target path."""
    t = raw_target.strip().strip('"')
    if not t or t == "/dev/null":
        raise PatchRejected(f"empty/dev-null target: {raw_target!r}")

    pure = PureWindowsPath(t)
    if pure.drive or t.startswith("\\\\") or t.startswith("/") or t.startswith("\\"):
        raise PatchRejected(f"absolute/UNC/drive path rejected: {raw_target!r}")

    resolved = (repo_root / t).resolve()
    # GATE 23: canonical, case-normalized containment (no naive prefix checks)
    resolved_nc = os.path.normcase(str(resolved))
    root_nc = os.path.normcase(str(repo_root.resolve()))
    if not (resolved_nc == root_nc or resolved_nc.startswith(root_nc + os.sep)):
        raise PatchRejected(f"path escapes repository root: {raw_target!r}")

    if ".git" in resolved.parts:
        raise PatchRejected(f".git internals are protected: {raw_target!r}")

    # PATCH-005: case-insensitive protected suffix/name on the FINAL target
    name = resolved.name
    suffix = resolved.suffix.lower()
    if suffix in PROTECTED_SUFFIXES:
        raise PatchRejected(f"protected file type '{suffix}' (case-insensitive): {raw_target!r}")
    posixish = "/" + str(resolved.relative_to(repo_root.resolve())).replace("\\", "/")
    if PROTECTED_NAME_RE.search(posixish):
        raise PatchRejected(f"protected filename pattern: {raw_target!r}")
    return resolved


def apply_patch(patch_file: Path, repo_root: Path = REPO_ROOT, dry_run: bool = False) -> List[Path]:
    repo_root = Path(repo_root).resolve()
    diff_text = Path(patch_file).read_text(encoding="utf-8", errors="replace")

    targets = extract_targets(diff_text)
    if not targets:
        raise PatchRejected("no recognizable target paths in patch")

    authorized = [authorize_target(t, repo_root) for t in targets]

    check = subprocess.run(["git", "-C", str(repo_root), "apply", "--check", "-"],
                           input=diff_text, capture_output=True, text=True)
    if check.returncode != 0:
        raise PatchRejected(f"git apply --check failed: {check.stderr.strip()}")
    if dry_run:
        return authorized
    apply = subprocess.run(["git", "-C", str(repo_root), "apply", "-"],
                           input=diff_text, capture_output=True, text=True)
    if apply.returncode != 0:
        raise PatchRejected(f"git apply failed (tree left untouched by --check gate): {apply.stderr.strip()}")
    return authorized


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Guarded patch-application fallback")
    ap.add_argument("patch", help="unified diff file to apply")
    ap.add_argument("--repo", default=str(REPO_ROOT))
    ap.add_argument("--dry-run", action="store_true", help="authorize only")
    args = ap.parse_args(argv)
    try:
        targets = apply_patch(Path(args.patch), Path(args.repo), dry_run=args.dry_run)
    except PatchRejected as ex:
        print(f"PATCH BLOCKED: {ex}", file=sys.stderr)
        return 1
    for t in targets:
        print(f"authorized + applied: {t}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
