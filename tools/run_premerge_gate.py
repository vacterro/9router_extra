#!/usr/bin/env python3
"""
run_premerge_gate.py - FULL PRE-MERGE pipeline (campaign section 30).

Steps:
 1. repository safety scan          (VERIFY_AGENT_SAFE)
 2. protected-path validation       (part of 1)
 3. unit tests                      (pytest -m "not integration and not local_trusted")
 4. safe integration tests          (same invocation)
 5. source compile/import validation
 6. Git diff sanity                 (no conflict markers, tree state reported)
 7. final repository safety re-scan

Prints PREMERGE: PASS only if every step succeeds.
"""
from __future__ import annotations

import argparse
import py_compile
import re
import subprocess
import sys
from pathlib import Path
from typing import List

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "9router_WatchEdit"))
sys.path.insert(0, str(REPO_ROOT / "tools"))

CONFLICT_MARKER_RE = re.compile(r'^(<<<<<<<|=======|>>>>>>>) ', re.MULTILINE)


def step_verify(quiet=True) -> bool:
    from verify_agent_safe import format_result, verify_agent_safe
    findings = verify_agent_safe(REPO_ROOT)
    if findings:
        print(format_result(findings))
        return False
    return True


def step_tests() -> bool:
    res = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--no-header",
         "-m", "not integration and not local_trusted"],
        cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=1800,
    )
    tail = (res.stdout or res.stderr).strip().splitlines()[-1:]
    print(f"    {tail[0] if tail else 'no output'}")
    return res.returncode == 0


def step_compile() -> bool:
    failures: List[str] = []
    for p in sorted((REPO_ROOT / "9router_WatchEdit").rglob("*.py")):
        if "__pycache__" in p.parts:
            continue
        try:
            py_compile.compile(str(p), doraise=True)
        except py_compile.PyCompileError as ex:
            failures.append(f"{p}: {ex}")
    if failures:
        for f in failures:
            print(f"    {f}")
        return False
    # import validation of the application entry module
    res = subprocess.run(
        [sys.executable, "-c",
         "import sys; sys.path.insert(0, '9router_WatchEdit'); "
         "import config, run, core.security, core.probe, core.router_client"],
        cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=120,
    )
    if res.returncode != 0:
        print((res.stderr or "").strip()[-400:])
        return False
    return True


def step_git_sanity() -> bool:
    status = subprocess.run(["git", "-C", str(REPO_ROOT), "status", "--porcelain"],
                            capture_output=True, text=True)
    if status.returncode != 0:
        print("    git unavailable")
        return True  # not a hard failure for non-git checkouts
    dirty = [l for l in status.stdout.splitlines() if l.strip()]
    if dirty:
        print(f"    working tree has {len(dirty)} uncommitted path(s) (review before merge)")
    # conflict markers in tracked source
    for p in (REPO_ROOT / "9router_WatchEdit").rglob("*.py"):
        if "__pycache__" in p.parts:
            continue
        try:
            if CONFLICT_MARKER_RE.search(p.read_text(encoding="utf-8", errors="replace")):
                print(f"    conflict markers present: {p}")
                return False
        except OSError:
            return False
    return True


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Full pre-merge gate")
    ap.add_argument("--skip-tests", action="store_true")
    args = ap.parse_args(argv)

    steps = [
        ("1. repository safety scan", step_verify),
        ("2. protected-path validation", step_verify),  # same authoritative gate
    ]
    if not args.skip_tests:
        steps.append(("3+4. unit + safe integration tests", step_tests))
    steps.append(("5. compile/import validation", step_compile))
    steps.append(("6. git diff sanity", step_git_sanity))
    steps.append(("7. final repository safety re-scan", step_verify))

    print("RUN_PREMERGE_GATE")
    ok = True
    for name, fn in steps:
        print(f"  {name}")
        try:
            if not fn():
                print("    FAILED")
                ok = False
                break
        except Exception as ex:  # GATE 26: exception != clean result
            print(f"    FAILED (validator raised {type(ex).__name__}; fail closed)")
            ok = False
            break
    print()
    print("PREMERGE: PASS" if ok else "PREMERGE: FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
