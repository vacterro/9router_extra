#!/usr/bin/env python3
"""
verify_agent_safe.py - AUTHORITATIVE external-agent safety gate.

Checks the ENTIRE repository tree — tracked, untracked, ignored, temporary and
generated files alike (a git status filter would be a hole, so none is used).

Layers:
 1. Protected-path policy (campaign SEC-011..014): runtime databases, .env
    files, key/cert material, credential vaults, provider-state exports,
    machine identity artifacts, private backups, runtime snapshot directories.
 2. Archive rejection: *.zip/*.tgz/*.7z/*.tar cannot be content-verified.
 3. Symlink/junction rejection WITHOUT traversal (campaign LINK-001..003):
    private runtime must be unreachable from the repository tree, and the
    scanner must never package private link-target contents.
 4. Content scan of every remaining file with the internal secret scanner.

FAIL-CLOSED (campaign section 29): unreadable files, unreadable directories,
and scanner exceptions all produce findings. "Unable to prove safe" = NOT safe.

Output is exactly:
    AGENT SAFE: YES
or
    AGENT SAFE: NO
followed by `path` + `reason` for each finding. Secret values are NEVER
printed — only reasons and (for content findings) a SHA-256 fingerprint.

Exit codes: 0 = safe, 1 = unsafe, 2 = usage error.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "9router_WatchEdit"))

from core.secret_scanner import scan_file  # noqa: E402

# Derived code caches: regenerated from scanned source.
SKIP_DIR_PARTS = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".saipen"}
# .git internals pruned (history is audited separately by check_git_history.py);
# the plain-text .git/config remains scannable.
GIT_PRUNE_DIRS = {"objects", "pack", "hooks", "info", "refs", "logs", "lfs", "filter-repo", "worktrees"}

PROTECTED_SUFFIXES = {
    ".sqlite", ".sqlite-wal", ".sqlite-shm", ".db", ".db-wal", ".db-shm",
    ".env", ".pem", ".key", ".pfx", ".p12", ".vault",
    ".zip", ".tgz", ".7z", ".tar", ".gz", ".cab",
}
PROTECTED_NAME_RE = re.compile(
    r'(?i)(^|/)(\.env(\..*)?|jwt-secret|machine-id|machineid|'
    r'providers-state-export.*|provider-state-export.*|'
    r'PRIVATE_SECRET_BACKUP.*|.*\.wvault|credentials\.(json|txt|ini)|'
    r'.*oauth.*token.*\.json|.*refresh.?token.*|id_rsa.*|.*\.kdbx)$'
)
# Directory names that constitute private runtime areas (campaign section 2/9)
PRIVATE_DIR_NAMES = {"backup", "backups", "runtime", "secrets", "private", "credentials", "vault"}


@dataclass
class Unsafe:
    path: str
    reason: str


def _linklike(p: Path) -> bool:
    """Symlinks AND Windows junctions (os.path.islink misses junctions)."""
    try:
        if os.path.islink(p):
            return True
        if p.is_dir():
            return os.path.realpath(p) != os.path.abspath(p)
    except OSError:
        return True  # cannot prove it is a plain directory -> unsafe
    return False


def _skip(path: Path, root: Path) -> bool:
    try:
        rel = path.relative_to(root)
    except ValueError:
        return True
    parts = rel.parts
    if not parts:
        return True
    if parts[0] == ".git":
        # keep top-level plain-text files (config, HEAD, ...), prune internals
        return len(parts) > 2 and parts[1] in GIT_PRUNE_DIRS
    return any(p in SKIP_DIR_PARTS for p in parts[:-1])


def verify_agent_safe(root: Path) -> List[Unsafe]:
    root = Path(root).resolve()
    findings: List[Unsafe] = []

    def add(rel_path: Path, reason: str):
        findings.append(Unsafe(str(rel_path).replace("\\", "/"), reason))

    walk_errors: List[str] = []

    def on_error(exc: OSError):
        walk_errors.append(f"{type(exc).__name__}: cannot prove directory safe: {getattr(exc, 'filename', '')}")

    # followlinks=False: symlinked/junctioned directories are NEVER traversed.
    # NOTE: on Python <3.12 os.walk does not treat junctions as symlinks, so
    # link-like entries are REMOVED from dirnames explicitly (LINK-001).
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False, onerror=on_error):
        cur = Path(dirpath)
        # prune in-place: skip derived caches and .git internals
        for d in list(dirnames):
            sub = cur / d
            if _linklike(sub):
                add(sub.relative_to(root), "external symlink/junction in repository tree (private runtime must be unreachable)")
                dirnames.remove(d)  # never descend
                continue
            if _skip(sub, root):
                dirnames.remove(d)
                continue
            if d.lower() in PRIVATE_DIR_NAMES:
                add(sub.relative_to(root), "private runtime directory inside repository (section 2)")
                dirnames.remove(d)
                continue

        for fname in filenames:
            p = cur / fname
            try:
                if os.path.islink(p):
                    add(p.relative_to(root), "external symlink in repository tree")
                    continue
            except OSError:
                add(p.relative_to(root), "unreadable: cannot prove safe")
                continue
            if _skip(p, root):
                continue
            rel = p.relative_to(root)

            # protected-path policy by suffix / name
            if p.suffix.lower() in PROTECTED_SUFFIXES:
                add(rel, f"protected file type '{p.suffix}' (section 9)")
                continue
            if PROTECTED_NAME_RE.search("/" + str(rel).replace("\\", "/")):
                add(rel, "protected filename pattern (section 9)")
                continue

            # content scan, fail-closed on scanner exceptions
            try:
                for f in scan_file(p, root):
                    findings.append(Unsafe(f.file, f"{f.reason} ({f.detail}) [sha256:{f.fingerprint}]"))
            except Exception as ex:  # campaign section 29
                add(rel, f"scanner_error: {type(ex).__name__} (failed closed)")

    for err in walk_errors:
        findings.append(Unsafe("<repository root>", f"unreadable_directory: {err}"))
    return findings


def format_result(findings: List[Unsafe]) -> str:
    if not findings:
        return "AGENT SAFE: YES"
    lines = ["AGENT SAFE: NO"]
    for u in findings:
        lines.append(f"{u.path}\n  {u.reason}")
    lines.append(f"{len(findings)} unsafe item(s). Remove or relocate before granting external-agent access.")
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Authoritative whole-tree external-agent safety gate")
    ap.add_argument("--root", default=str(REPO_ROOT))
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    findings = verify_agent_safe(Path(args.root))
    if not args.quiet:
        print(format_result(findings))
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
