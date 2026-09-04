#!/usr/bin/env python3
"""
deploy_local.py - Trusted local deploy: SOURCE -> validate -> RUNNING APP.

External agents modify SOURCE (repository/worktrees). This tool is run by the
TRUSTED local user to bring the reviewed code live. The application runs from
the repository tree, so "deploy code" = validating and activating a reviewed
git revision; PRIVATE RUNTIME STATE is never touched:

    %LOCALAPPDATA%\\9router_WatchEdit\\  (credentials, vault, cache, backups)
    %APPDATA%\\9router\\                 (running 9Router engine + its DB)

Pipeline (task section 12):
 1. verify repository secret-free      (VERIFY_AGENT_SAFE)
 2. verify working tree/revision       (git: clean tree, record revision)
 3. run unit tests                     (offline suite)
 4. build if required                  (pure Python: no build step)
 5. create source rollback point       (git tag predeploy/<ts>)
 6. stop/reload service only if needed (--restart; skipped by default)
 7. deploy CODE only                   (--source <ref> checkout, optional)
 8. preserve private runtime state     (never written; reported)
 9. restart/reload                     (only with --restart)
10. local smoke test                   (offscreen import + locked boot)
11. report; on failure -> ROLLBACK CODE (private state untouched)

Usage:
    python tools/deploy_local.py [--source <ref>] [--restart] [--skip-tests]
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import List, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "9router_WatchEdit"))

# DEPLOY MANIFEST (task section 11): the deployable code surface.
DEPLOY_MANIFEST = [
    "9router_WatchEdit/core", "9router_WatchEdit/ui", "9router_WatchEdit/tests",
    "9router_WatchEdit/tools", "9router_WatchEdit/config.py", "9router_WatchEdit/run.py",
    "tools", "docs",
]
NEVER_DEPLOY_HINTS = ("backup", "secrets", "private", "runtime", "vault", ".sqlite", ".env")

import verify_agent_safe as vas  # noqa: E402  (module-level for testability)

PRIVATE_RUNTIME_ROOT = Path(os.environ.get("WATCHEDIT_DATA_DIR")
                            or Path(os.environ.get("LOCALAPPDATA", "")) / "9router_WatchEdit")


def _git(args: List[str], repo_root: Path = REPO_ROOT) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo_root)] + args, capture_output=True, text=True)


# Terminal states (GATE 6 vocabulary)
STATUS_SUCCESS = "SUCCESS"
STATUS_FAILED_NO_MUTATION = "FAILED_NO_MUTATION"
STATUS_FAILED_ROLLED_BACK = "FAILED_ROLLED_BACK"
STATUS_RECOVERY_REQUIRED = "RECOVERY_REQUIRED"

_last_status = STATUS_FAILED_NO_MUTATION


def last_status() -> str:
    """Terminal state of the most recent deploy (GATE 6/27 vocabulary)."""
    return _last_status


def _marker_path(repo_root: Path) -> Path:
    return repo_root / ".git" / "watchedit_deploy_marker.json"


def _recover_abandoned_deploy(repo_root: Path, log) -> bool:
    """GATE 7: detect a hard-interrupted deploy via its transactional marker
    and recover deterministically (checkout the recorded original ref).
    Never mistake an incomplete operation for a completed one."""
    marker = _marker_path(repo_root)
    if not marker.exists():
        return False
    try:
        info = json.loads(marker.read_text(encoding="utf-8"))
    except Exception:
        info = {}
    state = info.get("state", "UNKNOWN")
    if state in ("COMPLETED", "ROLLED_BACK"):
        marker.unlink(missing_ok=True)
        return False
    original_ref = info.get("original_ref", "")
    log(f"ABANDONED DEPLOY DETECTED (state={state}, started={info.get('started', '?')})")
    if original_ref:
        res = _git(["checkout", "-f", original_ref], repo_root)
        _git(["checkout", original_ref], repo_root)
        if res.returncode == 0:
            log(f"Recovered: checked out {original_ref}. Re-run deploy to retry.")
            marker.unlink(missing_ok=True)
            return True
    log("RECOVERY_REQUIRED: could not restore recorded original revision automatically.")
    return False


def deploy_local(source: Optional[str] = None, restart: bool = False, skip_tests: bool = False,
                 quiet: bool = False, repo_root: Path = REPO_ROOT) -> bool:
    global _last_status
    steps: List[str] = []

    def log(msg: str):
        if not quiet:
            print(msg)

    def finish(status: str, deployed: bool, extra: list = None) -> bool:
        global _last_status
        _last_status = status
        for line in (extra or []):
            log(line)
        log(f"result: {status}")
        return deployed

    # 0. Recover any hard-interrupted previous deploy (GATE 7)
    _recover_abandoned_deploy(repo_root, log)

    # 1. Secret-free repository — GATE 26: validator exceptions fail closed
    try:
        findings = vas.verify_agent_safe(repo_root)
    except Exception as ex:
        return finish(STATUS_FAILED_NO_MUTATION, False,
                      [f"DEPLOY ABORTED: safety validator raised {type(ex).__name__} (fail closed)."])
    if findings:
        log(vas.format_result(findings))
        log("\nDEPLOY ABORTED: repository is not agent-safe.")
        return finish(STATUS_FAILED_NO_MUTATION, False)
    steps.append("1. secret-free verification: OK")

    # 2. Working tree / revision
    if _git(["rev-parse", "--is-inside-work-tree"], repo_root).returncode != 0:
        return finish(STATUS_FAILED_NO_MUTATION, False,
                      ["DEPLOY ABORTED: not a git repository."])
    status = _git(["status", "--porcelain"], repo_root)
    if status.stdout.strip():
        return finish(STATUS_FAILED_NO_MUTATION, False,
                      ["DEPLOY ABORTED: working tree not clean. Commit or stash first.",
                       status.stdout.strip()])
    original_ref = _git(["rev-parse", "--abbrev-ref", "HEAD"], repo_root).stdout.strip()
    original_sha = _git(["rev-parse", "HEAD"], repo_root).stdout.strip()
    steps.append(f"2. revision: {original_ref} @ {original_sha[:10]} (clean)")

    # 3. Unit tests
    if not skip_tests:
        tr = subprocess.run([sys.executable, "-m", "pytest", "-q", "--no-header"],
                            cwd=str(repo_root), capture_output=True, text=True, timeout=900)
        if tr.returncode != 0:
            tail_line = (tr.stdout or tr.stderr).strip().splitlines()[-1] if tr.stdout else "tests failed"
            return finish(STATUS_FAILED_NO_MUTATION, False,
                          [tail_line, "DEPLOY ABORTED: unit tests failed."])
        tail = (tr.stdout or "").strip().splitlines()[-1]
        steps.append(f"3. unit tests: OK ({tail})")
    else:
        steps.append("3. unit tests: SKIPPED (explicit flag)")

    # 4. Build (pure Python application: no build step)
    steps.append("4. build: not required (pure Python source deployment)")

    # 5. Rollback point + transactional marker (GATE 7)
    tag = f"predeploy/{time.strftime('%Y%m%d_%H%M%S')}"
    _git(["tag", "-f", tag, original_sha], repo_root)
    marker = _marker_path(repo_root)
    marker.write_text(json.dumps({
        "state": "VALIDATED", "started": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "original_ref": original_ref, "original_sha": original_sha, "tag": tag,
    }), encoding="utf-8")
    steps.append(f"5. rollback point: git tag {tag} + transactional marker")

    deployed = False
    try:
        # RACE-001: re-verify immediately before mutation — an earlier PASS
        # must never permanently authorize a since-changed tree.
        marker.write_text(json.dumps({
            "state": "APPLYING", "started": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "original_ref": original_ref, "original_sha": original_sha, "tag": tag,
        }), encoding="utf-8")
        try:
            findings2 = vas.verify_agent_safe(repo_root)
        except Exception as ex:
            log(f"DEPLOY FAILED: re-verification raised {type(ex).__name__} (fail closed).")
            raise RuntimeError("re-verification crashed")
        if findings2:
            log("DEPLOY FAILED: repository became unsafe after initial verification (race protection).")
            raise RuntimeError("re-verification failed")

        # 6. Stop running instance only when required
        if restart:
            stopped = _stop_running_instance(repo_root)
            steps.append(f"6. running instance: {'stopped' if stopped else 'none found'}")
        else:
            steps.append("6. running instance: left as-is (no --restart)")

        # 7. Deploy CODE only
        if source:
            res = _git(["checkout", source], repo_root)
            if res.returncode != 0:
                log(f"DEPLOY FAILED at checkout of {source}: {res.stderr.strip()}")
                raise RuntimeError("checkout failed")
            steps.append(f"7. code deployed: checked out {source}")
        else:
            steps.append(f"7. code deployed: {original_ref} @ {original_sha[:10]} (already active)")

        # 8. Private runtime state preserved (never written by deploy)
        steps.append(f"8. private runtime preserved: {PRIVATE_RUNTIME_ROOT} (untouched)")

        # 9. Restart / reload
        if restart:
            launched = _start_instance(repo_root)
            steps.append(f"9. app restart: {'launched' if launched else 'launch FAILED'}")
            if not launched:
                raise RuntimeError("restart failed")
        else:
            steps.append("9. app restart: skipped (no --restart)")

        # 10. Local smoke test
        marker.write_text(json.dumps({
            "state": "VERIFYING", "started": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "original_ref": original_ref, "original_sha": original_sha, "tag": tag,
        }), encoding="utf-8")
        ok, detail = _smoke_test(repo_root)
        steps.append(f"10. smoke test: {'OK' if ok else detail}")
        if not ok:
            raise RuntimeError(f"smoke test failed: {detail}")

        deployed = True
        marker.write_text(json.dumps({
            "state": "COMPLETED", "started": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "original_ref": original_ref, "original_sha": original_sha, "tag": tag,
        }), encoding="utf-8")
    except RuntimeError as ex:
        # 12. Rollback code; private state remains untouched (GATE 20:
        # SOURCE ROLLBACK only — never a private-data restore)
        back1 = _git(["checkout", "-f", original_ref], repo_root)
        back2 = _git(["checkout", original_ref], repo_root)
        if back1.returncode == 0 or back2.returncode == 0:
            marker.write_text(json.dumps({
                "state": "ROLLED_BACK", "started": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "original_ref": original_ref, "original_sha": original_sha, "tag": tag,
            }), encoding="utf-8")
            log(f"ROLLBACK CODE -> {original_ref} @ {original_sha[:10]} ({ex})")
            log("Private runtime state was not touched.")
            deployed = False
        else:
            # GATE 6/D10: rollback itself failed — never claim the system is restored
            log(f"ROLLBACK FAILED after deploy failure ({ex}).")
            log("RECOVERY_REQUIRED: manual intervention needed; original revision "
                f"{original_ref} @ {original_sha[:10]} (tag {tag}).")
            deployed = False
    finally:
        # 11. Report — receipt status reflects the ACTUAL terminal outcome
        global _last_status
        if deployed:
            _last_status = STATUS_SUCCESS
        elif marker.exists():
            try:
                final_state = json.loads(marker.read_text(encoding="utf-8")).get("state")
            except Exception:
                final_state = None
            _last_status = (STATUS_FAILED_ROLLED_BACK if final_state == "ROLLED_BACK"
                            else STATUS_RECOVERY_REQUIRED)
        else:
            _last_status = STATUS_FAILED_NO_MUTATION
        log("")
        log("DEPLOY LOCAL REPORT")
        for s in steps:
            log("  " + s)
        log(f"  result: {_last_status}")
    return deployed


def _stop_running_instance(repo_root: Path = REPO_ROOT) -> bool:
    """Stops WatchEdit instances launched from THIS repository (pythonw run.py)."""
    try:
        listing = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'run\\.py' -and $_.CommandLine -match '9router_WatchEdit' } | Select-Object -ExpandProperty ProcessId"],
            capture_output=True, text=True, timeout=30)
        pids = [p.strip() for p in listing.stdout.split() if p.strip().isdigit()]
        for pid in pids:
            subprocess.run(["taskkill", "/PID", pid, "/F"], capture_output=True)
        return bool(pids)
    except Exception:
        return False


def _start_instance(repo_root: Path = REPO_ROOT) -> bool:
    try:
        subprocess.Popen(["cmd", "/c", str(REPO_ROOT / "START_WATCHEDIT.bat")],
                         cwd=str(REPO_ROOT), creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0))
        return True
    except Exception:
        return False


def _smoke_test(repo_root: Path = REPO_ROOT) -> (bool, str):
    # Repositories without the desktop app (e.g. test mini-repos) have no UI
    # to boot; the smoke test applies only when the app is present.
    if not (repo_root / "9router_WatchEdit" / "ui" / "main_window.py").is_file():
        return True, ""
    code = (
        "import os, sys\n"
        "os.environ['QT_QPA_PLATFORM']='offscreen'\n"
        "os.environ.pop('WATCHEDIT_LIVE_ACCESS', None)\n"
        "sys.path.insert(0, r'" + str(repo_root / "9router_WatchEdit") + "')\n"
        "from PySide6.QtWidgets import QApplication\n"
        "app = QApplication([])\n"
        "from ui.main_window import MainWindow\n"
        "w = MainWindow()\n"
        "assert w.security.state == 'LOCKED' or w.security.is_live_allowed()\n"
        "w.close()\n"
        "print('SMOKE_OK')\n"
    )
    env = dict(os.environ)
    env["QT_QPA_PLATFORM"] = "offscreen"
    try:
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                           timeout=120, env=env, cwd=str(repo_root / "9router_WatchEdit"))
    except subprocess.TimeoutExpired:
        return False, "smoke test timed out"
    if "SMOKE_OK" in r.stdout:
        return True, ""
    return False, (r.stderr or r.stdout).strip()[-300:]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Trusted local deploy (code only, private state preserved)")
    ap.add_argument("--source", default=None, help="git ref to deploy (default: current HEAD)")
    ap.add_argument("--restart", action="store_true", help="stop and relaunch the running app")
    ap.add_argument("--skip-tests", action="store_true")
    ap.add_argument("--repo", default=str(REPO_ROOT), help="repository to deploy (default: this project)")
    args = ap.parse_args(argv)
    return 0 if deploy_local(source=args.source, restart=args.restart,
                             skip_tests=args.skip_tests, repo_root=Path(args.repo)) else 1


if __name__ == "__main__":
    sys.exit(main())
