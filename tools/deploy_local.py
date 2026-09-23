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
STATUS_RECOVERED = "RECOVERED"

# W2-002: recovery outcome vocabulary. NONE = nothing to recover;
# RECOVERED = original revision restored (deploy stops, re-run required);
# RECOVERY_REQUIRED = marker exists but automatic checkout failed — the
# original marker is preserved byte-for-byte and the deploy ABORTS.
RECOVERY_NONE = "NONE"
RECOVERY_RECOVERED = "RECOVERED"
RECOVERY_REQUIRED = "RECOVERY_REQUIRED"

_last_status = STATUS_FAILED_NO_MUTATION


class DeployRecoveryRequired(RuntimeError):
    """Raised when an unresolved deploy marker cannot be auto-recovered.

    W2-002: the caller must ABORT before any validation, tag creation,
    marker rewrite, checkout, restart or smoke test, and must never overwrite
    the only transactional evidence of the interrupted deploy."""
    pass


def _resolve_target_sha(source: Optional[str], repo_root: Path) -> Optional[str]:
    """W2-002: resolve a requested ref to ONE immutable commit SHA.

    Returns the SHA, or None when the ref cannot be resolved. Deployment must
    validate and activate exactly this SHA, never a moving branch name, so the
    validated identity and the activated identity are the same object.
    """
    ref = source or "HEAD"
    res = _git(["rev-parse", f"{ref}^{{commit}}"], repo_root)
    sha = res.stdout.strip()
    return sha or None


def _activate_sha(sha: str, repo_root: Path) -> bool:
    """W2-002: activate exactly the resolved SHA (detached), never a branch."""
    res = _git(["checkout", "--detach", sha], repo_root)
    if res.returncode != 0:
        _git(["checkout", "-f", sha], repo_root)
    current = _git(["rev-parse", "HEAD"], repo_root).stdout.strip()
    return current == sha


def _validate_target_revision(repo_root: Path, target_sha: str, skip_tests: bool):
    """W2-002: validate the exact target revision in an isolated worktree.

    Runs agent-safety verification (and the test gate unless skipped) against a
    temporary worktree checked out at ``target_sha`` so the active checkout is
    never mutated before the gate passes. Returns (ok, detail, steps)."""
    steps = []
    worktree = repo_root / ".git" / "predeploy_verify_wt"
    if worktree.exists():
        _git(["worktree", "remove", "--force", str(worktree)], repo_root)
    add = _git(["worktree", "add", "--detach", "--force", str(worktree), target_sha], repo_root)
    try:
        if add.returncode != 0:
            return False, f"could not create validation worktree: {add.stderr.strip()}", steps
        try:
            findings = vas.verify_agent_safe(worktree)
        except Exception as ex:
            return False, f"target validation raised {type(ex).__name__} (fail closed)", steps
        if findings:
            return False, vas.format_result(findings), steps
        if not skip_tests:
            tr = subprocess.run(
                [sys.executable, "-m", "pytest", "-q", "--no-header"],
                cwd=str(worktree), capture_output=True, text=True, timeout=900,
            )
            if tr.returncode != 0:
                tail = (tr.stdout or tr.stderr).strip().splitlines()[-1] if tr.stdout else "tests failed"
                return False, f"target revision tests failed: {tail}", steps
        steps.append(f"validated target revision {target_sha[:10]} in isolated worktree")
        return True, "", steps
    finally:
        _git(["worktree", "remove", "--force", str(worktree)], repo_root)


def _rollback_runtime_and_source(
    repo_root: Path,
    original_ref: str,
    original_sha: str,
    original_was_running: bool,
    target_launched: bool,
) -> tuple:
    """W2-003: restore BOTH source and required runtime state.

    Source rollback alone is not rollback when the failed target process is
    still alive and the original runtime is still stopped. Returns (ok, steps);
    ok is True only when source is back at ``original_sha`` AND the runtime
    state matches the pre-deploy state (target stopped; original restarted iff
    it was running). Any incomplete restoration -> RECOVERY_REQUIRED."""
    steps = []
    ok = True

    if target_launched:
        stopped = _stop_running_instance(repo_root)
        steps.append(f"12a. rollback runtime: failed target instance "
                     f"{'stopped' if stopped else 'stop unconfirmed'}")
        ok = ok and stopped

    back1 = _git(["checkout", "-f", original_ref], repo_root)
    head = _git(["rev-parse", "HEAD"], repo_root).stdout.strip()
    if back1.returncode != 0 or head != original_sha:
        # Fall back to the immutable SHA if the branch ref is gone.
        back1 = _git(["checkout", "-f", original_sha], repo_root)
        head = _git(["rev-parse", "HEAD"], repo_root).stdout.strip()
    if back1.returncode != 0 or head != original_sha:
        steps.append("12b. rollback source: FAILED")
        return False, steps
    steps.append(f"12b. rollback source: restored {original_ref} @ {original_sha[:10]}")

    if original_was_running:
        relaunched = _start_instance(repo_root)
        steps.append(f"12c. rollback runtime: original instance "
                     f"{'restarted' if relaunched else 'restart FAILED'}")
        ok = ok and relaunched
    else:
        steps.append("12c. rollback runtime: no original instance to restart")

    return ok, steps


def last_status() -> str:
    """Terminal state of the most recent deploy (GATE 6/27 vocabulary)."""
    return _last_status


def _marker_path(repo_root: Path) -> Path:
    return repo_root / ".git" / "watchedit_deploy_marker.json"


def _recover_abandoned_deploy(repo_root: Path, log) -> str:
    """GATE 7 + W2-002: detect a hard-interrupted deploy via its transactional
    marker and recover deterministically (checkout the recorded original ref).

    Returns RECOVERY_NONE / RECOVERY_RECOVERED / RECOVERY_REQUIRED.
    Never mistake an incomplete operation for a completed one: the boolean
    return of the legacy API could not distinguish "nothing to recover" from
    "recovery failed", which let a failed recovery fall through to SUCCESS and
    overwrite the original marker."""
    marker = _marker_path(repo_root)
    if not marker.exists():
        return RECOVERY_NONE
    try:
        info = json.loads(marker.read_text(encoding="utf-8"))
    except Exception:
        info = {}
    state = info.get("state", "UNKNOWN")
    if state in ("COMPLETED", "ROLLED_BACK"):
        marker.unlink(missing_ok=True)
        return RECOVERY_NONE
    original_ref = info.get("original_ref", "")
    log(f"ABANDONED DEPLOY DETECTED (state={state}, started={info.get('started', '?')})")
    if original_ref:
        res = _git(["checkout", "-f", original_ref], repo_root)
        _git(["checkout", original_ref], repo_root)
        if res.returncode == 0:
            log(f"Recovered: checked out {original_ref}. Re-run deploy to retry.")
            marker.unlink(missing_ok=True)
            return RECOVERY_RECOVERED
    log("RECOVERY_REQUIRED: could not restore recorded original revision automatically.")
    return RECOVERY_REQUIRED


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

    # 0. Recover any hard-interrupted previous deploy (GATE 7 + W2-002)
    recovery = _recover_abandoned_deploy(repo_root, log)
    if recovery == RECOVERY_REQUIRED:
        # Preserve the original marker byte-for-byte; abort before validation,
        # tag creation, marker rewrite, checkout, restart or smoke test.
        return finish(STATUS_RECOVERY_REQUIRED, False,
                      ["DEPLOY ABORTED: unresolved recovery marker. Restore the recorded "
                       "original revision manually (see marker original_ref), remove the "
                       "marker only after the tree is verified, then re-run deploy."])
    if recovery == RECOVERY_RECOVERED:
        # Recovery and deployment are separate transactional phases: stop here
        # so the recovered state can be inspected before a new deploy mutates it.
        return finish(STATUS_RECOVERED, False,
                      ["Abandoned deploy recovered. Re-run deploy to retry."])

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

    # W2-002: resolve the requested source to ONE immutable target SHA before
    # any validation, so the validated identity and the activated identity are
    # the same object (never a moving branch name).
    target_sha = _resolve_target_sha(source, repo_root)
    if target_sha is None:
        return finish(STATUS_FAILED_NO_MUTATION, False,
                      [f"DEPLOY ABORTED: cannot resolve source {source!r} to a commit SHA."])
    if source:
        steps.append(f"2b. target revision resolved: {source} -> {target_sha[:10]}")

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
    # W2-002: the target must have passed agent-safety verification as the SAME
    # revision that will be activated. When it differs from the current HEAD,
    # validate the target in an ISOLATED temporary worktree so the active
    # checkout is not mutated before the gate passes.
    if target_sha != original_sha:
        ok, detail, td_steps = _validate_target_revision(repo_root, target_sha, skip_tests)
        steps.extend(td_steps)
        if not ok:
            return finish(STATUS_FAILED_NO_MUTATION, False,
                          [f"DEPLOY ABORTED: target revision {target_sha[:10]} failed validation.", detail])

    tag = f"predeploy/{time.strftime('%Y%m%d_%H%M%S')}"
    _git(["tag", "-f", tag, original_sha], repo_root)
    marker = _marker_path(repo_root)
    marker.write_text(json.dumps({
        "state": "VALIDATED", "started": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "original_ref": original_ref, "original_sha": original_sha,
        "target_sha": target_sha, "tag": tag,
    }), encoding="utf-8")
    steps.append(f"5. rollback point: git tag {tag} + transactional marker")

    deployed = False
    # W2-003: explicit runtime transition state for rollback correctness.
    original_was_running = False
    target_launched = False
    verification_started = False
    try:
        # RACE-001: re-verify immediately before mutation — an earlier PASS
        # must never permanently authorize a since-changed tree.
        marker.write_text(json.dumps({
            "state": "APPLYING", "started": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "original_ref": original_ref, "original_sha": original_sha,
            "target_sha": target_sha, "tag": tag,
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
            original_was_running = stopped
            steps.append(f"6. running instance: {'stopped' if stopped else 'none found'}")
        else:
            steps.append("6. running instance: left as-is (no --restart)")

        # 7. Activate CODE only — exactly the resolved immutable SHA (W2-002)
        if not _activate_sha(target_sha, repo_root):
            log(f"DEPLOY FAILED at activation of {target_sha[:10]}")
            raise RuntimeError("activation failed")
        steps.append(f"7. code deployed: activated {target_sha[:10]}")

        # 8. Private runtime state preserved (never written by deploy)
        steps.append(f"8. private runtime preserved: {PRIVATE_RUNTIME_ROOT} (untouched)")

        # 9. Restart / reload
        if restart:
            launched = _start_instance(repo_root)
            target_launched = launched
            steps.append(f"9. app restart: {'launched' if launched else 'launch FAILED'}")
            if not launched:
                raise RuntimeError("restart failed")
        else:
            steps.append("9. app restart: skipped (no --restart)")

        # 10. Local smoke test
        marker.write_text(json.dumps({
            "state": "VERIFYING", "started": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "original_ref": original_ref, "original_sha": original_sha,
            "target_sha": target_sha, "tag": tag,
        }), encoding="utf-8")
        verification_started = True
        ok, detail = _smoke_test(repo_root)
        steps.append(f"10. smoke test: {'OK' if ok else detail}")
        if not ok:
            raise RuntimeError(f"smoke test failed: {detail}")

        deployed = True
        marker.write_text(json.dumps({
            "state": "COMPLETED", "started": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "original_ref": original_ref, "original_sha": original_sha,
            "target_sha": target_sha, "tag": tag,
        }), encoding="utf-8")
    except RuntimeError as ex:
        # 12. W2-003: restore BOTH source and required runtime state. Source
        # rollback alone is not rollback when the failed target process is still
        # alive and the original runtime is still stopped.
        restored, rb_steps = _rollback_runtime_and_source(
            repo_root, original_ref, original_sha,
            original_was_running, target_launched,
        )
        steps.extend(rb_steps)
        if restored:
            marker.write_text(json.dumps({
                "state": "ROLLED_BACK", "started": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "original_ref": original_ref, "original_sha": original_sha,
                "target_sha": target_sha, "tag": tag,
            }), encoding="utf-8")
            log(f"ROLLBACK -> {original_ref} @ {original_sha[:10]} ({ex})")
            log("Private runtime state was not touched.")
            deployed = False
        else:
            marker.write_text(json.dumps({
                "state": "RECOVERY_REQUIRED", "started": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "original_ref": original_ref, "original_sha": original_sha,
                "target_sha": target_sha, "tag": tag,
            }), encoding="utf-8")
            log(f"ROLLBACK INCOMPLETE after deploy failure ({ex}).")
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
    """Stops WatchEdit instances launched from THE REQUESTED repository only.

    W2-004: process identity is the resolved repo_root path, not the generic
    'run.py + 9router_WatchEdit' text match, so instances started from other
    worktrees/checkouts are never eligible for taskkill."""
    repo_root = Path(repo_root).resolve()
    target = str(repo_root).replace("'", "''")
    # Match the resolved repo path as substring of the command line after
    # path-separator normalization; PID ownership is re-checked per taskkill.
    ps = (
        "$t = '" + target + "'\n"
        "Get-CimInstance Win32_Process -Filter \"Name LIKE '%python%'\" | Where-Object {\n"
        "  $c = $_.CommandLine\n"
        "  $c -and $c.Contains('9router_WatchEdit') -and $c.Contains('run.py') -and\n"
        "  (($c -replace '/', '\\') -like \"*$t*\")\n"
        "} | Select-Object -ExpandProperty ProcessId"
    )
    try:
        listing = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True, text=True, timeout=30)
        pids = [p.strip() for p in listing.stdout.split() if p.strip().isdigit()]
        stopped = []
        for pid in pids:
            res = subprocess.run(["taskkill", "/PID", pid, "/F"], capture_output=True, text=True)
            if res.returncode == 0:
                stopped.append(pid)
        return bool(stopped)
    except Exception:
        return False


def _start_instance(repo_root: Path = REPO_ROOT) -> bool:
    """W2-004: launches the START script of THE REQUESTED repository (never the
    module-global REPO_ROOT), with cwd pinned to the same repo."""
    try:
        repo_root = Path(repo_root).resolve()
        script = repo_root / "START_WATCHEDIT.bat"
        if not script.is_file():
            return False
        subprocess.Popen(["cmd", "/c", str(script)],
                         cwd=str(repo_root), creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0))
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
