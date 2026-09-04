#!/usr/bin/env python3
"""
remediate_git_history.py - APPROVED history remediation (GATE 11).

Purges named paths from the ENTIRE git history of a repository using
`git filter-branch` (no external tooling required). This REWRITES HISTORY:
all commit ids change. Run it only on a disposable/confirmed-contaminated
repository, after rotating every credential whose fingerprint was reported
by check_git_history.py.

Safety rails:
 - requires the explicit flag --i-understand-this-rewrites-history
 - refuses to run against this project's own repository by default
   (--allow-real-repo needed for that, intended for the trusted user only)
 - refuses when the worktree is dirty

Usage:
    python tools/remediate_git_history.py --repo <disposable-repo> \
        --purge leak.txt more/leaks.json --i-understand-this-rewrites-history
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path
from typing import List, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "9router_WatchEdit"))


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)


def remediate(repo: Path, purge: List[str]) -> bool:
    expr = " ".join(f"git rm -r --cached --ignore-unmatch '{p}'" for p in purge)
    env_args = ["-c", "filter.branch.warnEmpty=true"]
    res = subprocess.run(
        ["git", "-C", str(repo)] + env_args +
        ["filter-branch", "-f", "--index-filter", expr, "--prune-empty",
         "--tag-name-filter", "cat", "--", "--all"],
        capture_output=True, text=True, timeout=1800,
    )
    if res.returncode != 0:
        print(f"filter-branch failed: {res.stderr.strip()[-400:]}", file=sys.stderr)
        return False
    # purge old refs left by filter-branch so the blobs become unreachable
    for ref in ("refs/original", "refs/replace"):
        _git(repo, "for-each-ref", "--format=%(refname)", ref).stdout.strip().splitlines()
        for line in _git(repo, "for-each-ref", "--format=%(refname)", ref).stdout.split():
            _git(repo, "update-ref", "-d", line.strip())
    _git(repo, "reflog", "expire", "--expire=now", "--all")
    _git(repo, "gc", "--prune=now", "--aggressive")
    return True


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Approved git history remediation (destructive)")
    ap.add_argument("--repo", required=True, help="repository to remediate")
    ap.add_argument("--purge", required=True, nargs="+", help="paths to purge from history")
    ap.add_argument("--i-understand-this-rewrites-history", action="store_true")
    ap.add_argument("--allow-real-repo", action="store_true",
                    help="permit running against this project's own repository (trusted user only)")
    args = ap.parse_args(argv)

    if not args.i_understand_this_rewrites_history:
        print("REFUSED: pass --i-understand-this-rewrites-history after rotating exposed credentials.",
              file=sys.stderr)
        return 2
    repo = Path(args.repo).resolve()
    if repo == REPO_ROOT.resolve() and not args.allow_real_repo:
        print("REFUSED: refusing to rewrite this project's own repository without --allow-real-repo.",
              file=sys.stderr)
        return 2
    if _git(repo, "status", "--porcelain").stdout.strip():
        print("REFUSED: worktree is dirty. Commit or stash first.", file=sys.stderr)
        return 2

    if not remediate(repo, args.purge):
        return 1
    print(f"History rewritten; purged: {', '.join(args.purge)}")
    print("Verify with: python tools/check_git_history.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
