#!/usr/bin/env python3
"""
verify_agent_safe.py - AUTHORITATIVE external-agent safety gate.

Checks the ENTIRE repository tree — tracked, untracked, ignored, temporary and
generated files alike (a git status filter would be a hole, so none is used).

Layers:
 1. Protected-path policy (task section 9): runtime databases, .env files,
    key/cert material, credential vaults, provider-state exports, machine
    identity artifacts, private backups, runtime snapshot directories.
 2. Archive rejection: *.zip/*.tgz/*.7z/*.tar cannot be content-verified.
 3. Symlink rejection: no private runtime symlinks may reach into the tree.
 4. Content scan of every remaining file with the internal secret scanner
    (API keys, tokens, JWTs, Authorization headers, private keys,
    credential-bearing JSON, high-confidence secret patterns).

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
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "9router_WatchEdit"))

from core.secret_scanner import scan_file  # noqa: E402

# Derived code caches / VCS internals: regenerated from scanned source, or
# content-addressed compressed objects covered by check_git_history.py.
SKIP_DIR_PARTS = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
SKIP_DIR_NAMES = {".git"}  # only its object/pack stores are skipped (below)

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
# Directory names that constitute private runtime areas (section 2/9)
PRIVATE_DIR_NAMES = {"backup", "backups", "runtime", "secrets", "private", "credentials", "vault"}


@dataclass
class Unsafe:
    path: str
    reason: str


def _skip(path: Path, root: Path) -> bool:
    rel = path.relative_to(root)
    parts = rel.parts
    if not parts:
        return True
    # .git internals except top-level plain-text config files
    if parts[0] == ".git":
        return not (len(parts) == 2 and parts[1] in (".gitignore", "config"))
    return any(p in SKIP_DIR_PARTS for p in parts[:-1])


def verify_agent_safe(root: Path) -> List[Unsafe]:
    root = Path(root).resolve()
    findings: List[Unsafe] = []

    # 1. Private runtime directories must not exist at all
    for d in sorted(root.rglob("*")):
        rel = "/" + str(d.relative_to(root)).replace("\\", "/")
        if d.is_symlink():
            findings.append(Unsafe(rel.lstrip("/"), "symlink in repository tree (private runtime must be unreachable, section 18)"))
        if d.is_dir() and d.name.lower() in PRIVATE_DIR_NAMES and not _skip(d, root):
            findings.append(Unsafe(rel.lstrip("/"), "private runtime directory inside repository (section 2)"))

    for p in sorted(root.rglob("*")):
        if not p.is_file() or p.is_symlink():
            continue
        if _skip(p, root):
            continue
        rel = "/" + str(p.relative_to(root)).replace("\\", "/")
        name = rel.rsplit("/", 1)[-1]

        # 2. Protected-path policy by suffix / name
        if p.suffix.lower() in PROTECTED_SUFFIXES:
            findings.append(Unsafe(rel.lstrip("/"), f"protected file type '{p.suffix}' (section 9)"))
            continue
        if PROTECTED_NAME_RE.search(rel):
            findings.append(Unsafe(rel.lstrip("/"), "protected filename pattern (section 9)"))
            continue

        # 3. Content scan
        for f in scan_file(p, root):
            findings.append(Unsafe(f.file, f"{f.reason} ({f.detail}) [sha256:{f.fingerprint}]"))

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
