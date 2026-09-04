#!/usr/bin/env python3
"""
check_git_history.py - Git history secret audit.

Reports exactly one of:
    NO GIT REPOSITORY          (worktree scan only)
    CLEAN HISTORY              (no secret patterns in any committed blob)
    SECRET MATERIAL EXISTS IN HISTORY
                                (+ explicit remediation plan, NO history rewrite)

Usage:
    python tools/check_git_history.py
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "9router_WatchEdit"))

from core.secret_scanner import Finding, format_report, scan_file  # noqa: E402


def _git(args, repo_root=None, **kw):
    root = str(repo_root) if repo_root else str(REPO_ROOT)
    return subprocess.run(["git", "-C", root] + args, capture_output=True, text=True, **kw)


def check_history(repo_root: Path = None) -> int:
    repo_root = Path(repo_root) if repo_root else REPO_ROOT
    is_repo = _git(["rev-parse", "--is-inside-work-tree"], repo_root).returncode == 0
    if not is_repo:
        print("NO GIT REPOSITORY")
        print("No history to scan. Current worktree can be scanned with tools/secret_scan.py.")
        print("When a repository is initialized, install the pre-commit hook (see SECURITY_LOCAL.md).")
        return 0

    print("Scanning full commit history for secret patterns ...")
    listing = _git(["rev-list", "--all"], repo_root)
    if listing.returncode != 0:
        print("GIT ERROR: rev-list failed", file=sys.stderr)
        return 2
    commits = [c for c in listing.stdout.split() if c.strip()]

    findings = []
    seen_blobs = set()
    tmp_dir = repo_root / ".git_scan_tmp"
    tmp_dir.mkdir(exist_ok=True)
    for commit in commits:
        ls = _git(["ls-tree", "-r", commit], repo_root)
        for line in ls.stdout.splitlines():
            parts = line.split()
            if len(parts) < 4:
                continue
            mode, otype, sha, path = parts[0], parts[1], parts[2], " ".join(parts[3:])
            if otype != "blob" or sha in seen_blobs:
                continue
            seen_blobs.add(sha)
            cat = _git(["cat-file", "blob", sha], repo_root)
            if "\x00" in cat.stdout[:1024]:
                continue  # binary blob
            # keep the original suffix so JSON blobs keep structured scanning
            suffix = Path(path).suffix or ".txt"
            tmp = tmp_dir / f".scan_{sha[:12]}{suffix}"
            tmp.write_text(cat.stdout, encoding="utf-8", errors="replace")
            try:
                for f in scan_file(tmp, tmp_dir):
                    # attribute the finding to the original commit + path
                    findings.append(Finding(
                        file=f"{commit[:10]}:{path}",
                        reason=f.reason, detail=f.detail, fingerprint=f.fingerprint))
            finally:
                tmp.unlink(missing_ok=True)

    import shutil as _shutil
    _shutil.rmtree(tmp_dir, ignore_errors=True)

    if findings:
        print("SECRET MATERIAL EXISTS IN HISTORY")
        print(format_report(findings))
        print(
            "\nREMEDIATION PLAN (do NOT silently rewrite history):\n"
            " 1. Rotate/revoke every credential whose fingerprint appears above.\n"
            " 2. Remove the files from future commits (git rm + .gitignore).\n"
            " 3. Only after rotation, consider 'git filter-repo' to purge blobs —\n"
            "    coordinate with all clones/remotes first.\n"
            " 4. If this repository was ever published while secrets existed, treat them as compromised."
        )
        return 1

    print("CLEAN HISTORY")
    print(f"Scanned {len(seen_blobs)} unique blobs across {len(commits)} commits: no secret patterns found.")
    return 0


if __name__ == "__main__":
    sys.exit(check_history())
