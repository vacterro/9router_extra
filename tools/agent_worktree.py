#!/usr/bin/env python3
"""
agent_worktree.py - External-agent Git worktree helper.

Agents work as ordinary code contributors on isolated worktrees:

    CREATE_AGENT_WORKTREE.cmd <name>   ->  tools/agent_worktree.py create <name>
    REVIEW_AGENT_WORKTREE.cmd <name>   ->  tools/agent_worktree.py review <name>

create:
  - REFUSES to run unless VERIFY_AGENT_SAFE passes on the main tree
  - creates <worktree_root>/<repo>_<name> on branch agent/<name>/<timestamp>
  - default worktree root: %AGENT_WORKTREE_ROOT% or <repo_parent>\\agent_worktrees
    (always OUTSIDE the main repository tree)

review:
  - shows branch, commits vs main, changed files, diff stats,
    protected-path + secret-scan status of the worktree, and unit tests
  - NEVER merges automatically: the trusted user decides MERGE/CHERRY-PICK/REJECT
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from typing import List, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "9router_WatchEdit"))


def _git(args: List[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(cwd)] + args, capture_output=True, text=True)


def _require_git(repo_root: Path) -> None:
    if _git(["rev-parse", "--is-inside-work-tree"], repo_root).returncode != 0:
        raise RuntimeError(f"Not a git repository: {repo_root}. Initialize git first (see SECURITY_LOCAL.md).")


def worktree_root(repo_root: Path = REPO_ROOT) -> Path:
    env = os.environ.get("AGENT_WORKTREE_ROOT")
    if env:
        return Path(env)
    return repo_root.parent / "agent_worktrees"


def _worktree_name(repo_root: Path, name: str) -> str:
    return f"{repo_root.name}_{name}"


def create_worktree(name: str, repo_root: Path = REPO_ROOT) -> Path:
    from verify_agent_safe import verify_agent_safe
    _require_git(repo_root)

    findings = verify_agent_safe(repo_root)
    if findings:
        from verify_agent_safe import format_result
        print(format_result(findings))
        print("\nWORKTREE CREATION BLOCKED: main repository is not agent-safe.")
        raise SystemExit(1)

    wt_root = worktree_root(repo_root)
    wt_root.mkdir(parents=True, exist_ok=True)
    wt_path = wt_root / _worktree_name(repo_root, name)
    if wt_path.exists():
        raise RuntimeError(f"Worktree already exists: {wt_path}")

    branch = f"agent/{name}/{time.strftime('%Y%m%d_%H%M%S')}"
    res = _git(["worktree", "add", "-b", branch, str(wt_path), "HEAD"], repo_root)
    if res.returncode != 0:
        raise RuntimeError(f"git worktree add failed: {res.stderr.strip()}")

    print("AGENT WORKTREE CREATED")
    print(f"  path:   {wt_path}")
    print(f"  branch: {branch}")
    print("The worktree contains the same SAFE repository content on an independent branch.")
    print("Grant the external agent access to the worktree path, not the main tree.")
    return wt_path


def _find_worktree(name: str, repo_root: Path) -> Optional[Path]:
    res = _git(["worktree", "list", "--porcelain"], repo_root)
    want = _worktree_name(repo_root, name)
    for line in res.stdout.splitlines():
        if line.startswith("worktree "):
            p = Path(line.split(" ", 1)[1])
            if p.name == want:
                return p
    return None


def review_worktree(name: str, repo_root: Path = REPO_ROOT, run_tests: bool = True) -> int:
    from verify_agent_safe import format_result, verify_agent_safe
    _require_git(repo_root)
    wt = _find_worktree(name, repo_root)
    if wt is None:
        print(f"No worktree found for agent '{name}' under {worktree_root(repo_root)}")
        return 2

    branch = _git(["rev-parse", "--abbrev-ref", "HEAD"], wt).stdout.strip()
    main_branch = _git(["rev-parse", "--abbrev-ref", "HEAD"], repo_root).stdout.strip()

    print("AGENT WORKTREE REVIEW")
    print(f"  worktree: {wt}")
    print(f"  branch:   {branch}   (main tree is on {main_branch})")

    commits = _git(["log", "--oneline", f"{main_branch}..HEAD"], wt)
    n_commits = len([l for l in commits.stdout.splitlines() if l.strip()])
    print(f"  commits:  {n_commits} ahead of {main_branch}")
    if commits.stdout.strip():
        for line in commits.stdout.strip().splitlines()[:20]:
            print(f"    {line}")

    diff = _git(["diff", "--stat", f"{main_branch}...HEAD"], wt)
    print("  changed files:")
    files = _git(["diff", "--name-status", f"{main_branch}...HEAD"], wt).stdout.strip()
    if files:
        for line in files.splitlines():
            print(f"    {line}")
        print("  diff stats:")
        for line in diff.stdout.strip().splitlines()[-5:]:
            print(f"    {line}")
    else:
        print("    (no changes)")

    findings = verify_agent_safe(wt)
    print("  protected-path/secret scan:", "CLEAN" if not findings else "UNSAFE")
    if findings:
        print(format_result(findings))

    if run_tests:
        print("  unit tests:")
        tr = subprocess.run([sys.executable, "-m", "pytest", "-q", "--no-header"],
                            cwd=str(wt), capture_output=True, text=True, timeout=600)
        tail = (tr.stdout or tr.stderr).strip().splitlines()[-1] if (tr.stdout or tr.stderr).strip() else "no output"
        print(f"    {tail}")
        tests_ok = tr.returncode == 0
    else:
        tests_ok = True

    blocked = bool(findings)
    print()
    if blocked:
        print("MERGE BLOCKED: worktree contains protected/secret material.")
        return 1
    if not tests_ok:
        print("MERGE BLOCKED: unit tests failed in worktree.")
        return 1
    print("Manual decision required: MERGE / CHERRY-PICK / REJECT (nothing is merged automatically).")
    print(f"  merge:       git merge {branch}")
    print(f"  cherry-pick: git cherry-pick <sha>")
    print(f"  reject:      git worktree remove {wt} && git branch -D {branch}")
    return 0


def remove_worktree(name: str, repo_root: Path = REPO_ROOT) -> int:
    _require_git(repo_root)
    wt = _find_worktree(name, repo_root)
    if wt is None:
        print(f"No worktree found for agent '{name}'")
        return 2
    branch = _git(["rev-parse", "--abbrev-ref", "HEAD"], wt).stdout.strip()
    res = _git(["worktree", "remove", "--force", str(wt)], repo_root)
    if res.returncode != 0:
        print(f"git worktree remove failed: {res.stderr.strip()}")
        return 1
    if branch.startswith("agent/"):
        _git(["branch", "-D", branch], repo_root)
    print(f"Removed worktree {wt} (branch {branch})")
    return 0


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="External-agent worktree helper")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("create", "review", "remove"):
        sp = sub.add_parser(name)
        sp.add_argument("agent")
    sub.add_parser("list")
    args = ap.parse_args(argv)

    if args.cmd == "create":
        create_worktree(args.agent)
        return 0
    if args.cmd == "review":
        return review_worktree(args.agent)
    if args.cmd == "remove":
        return remove_worktree(args.agent)
    if args.cmd == "list":
        res = _git(["worktree", "list"], REPO_ROOT)
        print(res.stdout)
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
