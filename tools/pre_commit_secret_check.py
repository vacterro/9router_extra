#!/usr/bin/env python3
"""
pre_commit_secret_check.py - Git pre-commit secret gate.

Blocks commits containing likely secrets. Install once with:

    git config core.hooksPath tools/hooks

(tools/hooks/pre-commit calls this script) or copy it into .git/hooks/pre-commit.

False-positive override (explicit, auditable, never the default workflow):

    git commit --no-verify   # ONLY for verified false positives; document why

NOT a security boundary by itself: .gitignore and this hook reduce accidents;
the SAFE share scanner remains the mandatory gate for anything leaving the machine.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "9router_WatchEdit"))

from core.secret_scanner import format_report, scan_file  # noqa: E402


def main() -> int:
    staged = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACM"],
        capture_output=True, text=True, cwd=str(REPO_ROOT),
    )
    if staged.returncode != 0:
        return 0  # not a git repo / no index: nothing to gate

    findings = []
    for rel in staged.stdout.splitlines():
        rel = rel.strip()
        if not rel:
            continue
        p = REPO_ROOT / rel
        if p.is_file():
            findings.extend(scan_file(p, REPO_ROOT))

    if findings:
        print(format_report(findings))
        print("COMMIT BLOCKED by pre-commit secret check.")
        print("If every finding above is a VERIFIED false positive, document it and use --no-verify.")
        return 1

    print("pre-commit secret check: clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())
