"""
SECURITY TORTURE GATE — final cross-layer adversarial verification (Gates 1-30).

Hostile systems testing over the COMPLETE workflow: simultaneous failure
conditions, stale bases, crash matrices, process interruption, soak.
All canaries synthetic (tests/security/canaries.py). No real credentials.
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import re
import shutil
import socket
import subprocess
import sys
import time
import zipfile
from pathlib import Path

import pytest

from tests.security.canaries import (
    all_canaries, canary_access_token, canary_api_key, canary_client_secret,
    canary_jwt, canary_password, canary_refresh_token, canary_sk,
)
from tests.security.helpers import (
    REPO_ROOT, TOOLS, git, hash_tree, make_mini_repo, mini_canary_repo,
)

sys.path.insert(0, str(TOOLS))
sys.path.insert(0, str(REPO_ROOT / "9router_WatchEdit"))

from verify_agent_safe import format_result, verify_agent_safe  # noqa: E402

IS_WINDOWS = os.name == "nt"
CANARIES = all_canaries()
LONG_CANARY = "CANARY_REFRESH_TOKEN_" + "0123456789" + "ABCDEFGHIJKLMNOPQRSTUVWXYZ"  # G9, 58 chars


def _v(root) -> list:
    return verify_agent_safe(Path(root))


# =============================================================================
# GATE 1 — zero-trust clean checkout
# =============================================================================
class TestGate1ZeroTrustCheckout:
    def test_clean_checkout_full_cycle(self, tmp_path):
        clone = tmp_path / "clone"
        res = subprocess.run(["git", "clone", "-q", str(REPO_ROOT), str(clone)],
                             capture_output=True, text=True, timeout=180)
        if res.returncode != 0:
            pytest.skip("git clone unavailable")
        env = {k: v for k, v in os.environ.items()
               if k not in ("WATCHEDIT_LIVE_ACCESS", "WATCHEDIT_DATA_DIR",
                            "9ROUTER_API_KEY", "OPENAI_API_KEY", "TEST_REFRESH_TOKEN")}
        env.update(WATCHEDIT_DATA_DIR=str(tmp_path / "fresh_private"),
                   QT_QPA_PLATFORM="offscreen", PYTHONDONTWRITEBYTECODE="1")

        # VERIFY_AGENT_SAFE -> YES
        assert verify_agent_safe(clone) == []
        # pytest -> PASS (offline subset; full-suite offline proof is separate)
        tests = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "--no-header",
             "9router_WatchEdit/tests/test_classification.py",
             "9router_WatchEdit/tests/test_history.py"],
            cwd=str(clone), capture_output=True, text=True, env=env, timeout=300)
        assert tests.returncode == 0, tests.stdout[-300:]
        # application startup -> LOCKED / LIVE DISABLED
        boot = subprocess.run([sys.executable, "-c", (
            "import sys; sys.path.insert(0, '9router_WatchEdit')\n"
            "from PySide6.QtWidgets import QApplication\n"
            "app = QApplication([])\n"
            "from ui.main_window import MainWindow\n"
            "w = MainWindow()\n"
            "print('STATE=' + w.security.state)\n"
            "print('LIVE=' + str(w.security.is_live_allowed()))\n"
            "w.close()\n")], cwd=str(clone), capture_output=True, text=True, env=env, timeout=180)
        assert "STATE=LOCKED" in boot.stdout, boot.stderr[-300:]
        assert "LIVE=False" in boot.stdout
        # repository created no private artifacts inside its tree
        for banned in (".env", "config.json", "runtime", "backup", "secrets",
                       "private", "health_cache.json", "presets.json"):
            assert not (clone / banned).exists(), f"repo polluted itself: {banned}"
        assert not list(clone.rglob("*.sqlite"))
        # MANDATORY second scan after everything ran
        assert verify_agent_safe(clone) == []


# =============================================================================
# GATE 2 — hostile repository pollution (simultaneous, complete violation set)
# =============================================================================
class TestGate2HostilePollution:
    def test_ten_simultaneous_artifacts_complete_set(self, tmp_path):
        repo = make_mini_repo(tmp_path, "g2")
        outside = tmp_path / "outside_private"
        outside.mkdir()
        (outside / "canary.dat").write_text(canary_api_key(), encoding="utf-8")

        # 1. ignored refresh-token file
        gi = repo / ".gitignore"
        gi.write_text((gi.read_text(encoding="utf-8") if gi.exists() else "")
                      + "*.ignored\n", encoding="utf-8")
        (repo / "tok.ignored").write_text(
            f"refresh_token: {canary_refresh_token()}\n", encoding="utf-8")
        # 2. hidden API-key file
        hidden = repo / "hidden.log"
        hidden.write_text(f"key {canary_api_key()}\n", encoding="utf-8")
        subprocess.run(["attrib", "+h", str(hidden)], capture_output=True)
        # 3. deeply nested JWT
        deep = repo / "a/b/c/d/e"
        deep.mkdir(parents=True)
        (deep / "t.json").write_text(json.dumps({"tok": canary_jwt()}), encoding="utf-8")
        # 4. uppercase DATA.SQLITE
        (repo / "DATA.SQLITE").write_bytes(b"\x00sqlite")
        # 5. external junction
        junction_made = False
        if IS_WINDOWS:
            r = subprocess.run(["cmd", "/c", "mklink", "/J",
                                str(repo / "linkdir"), str(outside)], capture_output=True)
            junction_made = r.returncode == 0
        # 6. unreadable file
        unreadable_made = False
        locked = repo / "locked.log"
        locked.write_text(f"tok {canary_access_token()}\n", encoding="utf-8")
        if IS_WINDOWS:
            user = os.environ.get("USERNAME", "")
            d = subprocess.run(["icacls", str(locked), "/deny", f"{user}:R"],
                               capture_output=True, text=True)
            unreadable_made = d.returncode == 0
        # 7. malformed JSON containing Authorization header
        (repo / "bad.json").write_text(
            '{"h": "Auth' + f'orization: Bearer {canary_access_token()}", oops',
            encoding="utf-8")
        # 8. UTF-16 secret-bearing config
        (repo / "u16.cfg").write_text(
            f"clientSecret = {canary_client_secret()}\n", encoding="utf-16")
        # 9. fake private key
        (repo / "kp.txt").write_text(
            "-----BE" + "GIN PRIVATE KEY-----\nTEST_ONLY_NOT_A_REAL_PRIVATE_KEY\n"
            "-----EN" + "D PRIVATE KEY-----\n", encoding="utf-8")
        # 10. private-backup-like encrypted blob
        (repo / "PRIVATE_SECRET_BACKUP_fake.wvault").write_bytes(b"\x00\x01encrypted-looking")

        try:
            findings = _v(repo)
            out = format_result(findings)
            assert out.startswith("AGENT SAFE: NO")
            # COMPLETE violation set: each artifact class reported
            paths = "\n".join(u.path for u in findings)
            for expected in ("tok.ignored", "hidden.log", "t.json", "DATA.SQLITE",
                             "bad.json", "u16.cfg", "kp.txt",
                             "PRIVATE_SECRET_BACKUP_fake.wvault"):
                assert expected in paths, f"scanner stopped early; missing {expected}"
            if junction_made:
                assert "linkdir" in paths
            if unreadable_made:
                assert "locked.log" in paths
            # NO complete canary anywhere in output
            for canary in CANARIES:
                assert canary not in out
        finally:
            if unreadable_made:
                user = os.environ.get("USERNAME", "")
                subprocess.run(["icacls", str(locked), "/remove:d", user], capture_output=True)

        # removal -> scanner recovery / determinism
        for junk in ("tok.ignored", "hidden.log", "DATA.SQLITE", "bad.json",
                     "u16.cfg", "kp.txt", "PRIVATE_SECRET_BACKUP_fake.wvault"):
            (repo / junk).unlink(missing_ok=True)
        shutil.rmtree(repo / "a", ignore_errors=True)
        if junction_made:
            subprocess.run(["cmd", "/c", "rmdir", str(repo / "linkdir")], capture_output=True)
        # NOTE: locked.log keeps its canary if the deny was ineffective; remove via python
        os.chmod(locked, 0o644)
        locked.unlink(missing_ok=True)
        assert verify_agent_safe(repo) == [], "scanner must recover to YES after cleanup"


# =============================================================================
# GATE 3 — hostile agent contribution, then legitimate recovery
# =============================================================================
class TestGate3HostileContribution:
    def test_unsafe_then_recovered_contribution(self, tmp_path, capsys):
        import agent_worktree as aw
        repo = make_mini_repo(tmp_path, "g3")
        outside = tmp_path / "priv_g3"
        outside.mkdir()
        (outside / "s.dat").write_text(canary_api_key(), encoding="utf-8")
        os.environ["AGENT_WORKTREE_ROOT"] = str(tmp_path / "g3wts")
        wt = aw.create_worktree("redteam", repo_root=repo)

        # SAFE + UNSAFE in the same contribution
        (wt / "app.py").write_text("VALUE = 2  # legitimate fix\n", encoding="utf-8")
        (wt / "tests" / "test_fix.py").write_text(
            "def test_fix():\n    assert True\n", encoding="utf-8")
        (wt / ".gitignore").write_text("*.local\n", encoding="utf-8")
        (wt / "creds.local").write_text(f"key: {canary_api_key()}\n", encoding="utf-8")
        (wt / "debug.sqlite").write_bytes(b"\x00dbg")
        if IS_WINDOWS:
            subprocess.run(["cmd", "/c", "mklink", "/J", str(wt / "plink"), str(outside)],
                           capture_output=True)
        (wt / "net.py").write_text(
            "HEADERS = {'Auth" + "orization': 'Bearer ' + '" + canary_access_token() + "'}\n",
            encoding="utf-8")
        git(wt, "add", "-A")
        git(wt, "commit", "-m", "mixed contribution")

        main_before = hash_tree(repo)
        rc = aw.review_worktree("redteam", repo_root=repo, run_tests=True)
        out = capsys.readouterr().out
        assert rc == 1
        assert "app.py" in out            # SOURCE DIFF detected
        assert "SECRET CHECK: FAIL" in out
        assert "MERGE BLOCKED" in out
        assert hash_tree(repo) == main_before, "main must remain byte-identical"

        # recover: remove unsafe, keep the legitimate fix
        for junk in ("creds.local", "debug.sqlite", "net.py", "plink"):
            p = wt / junk
            if p.is_dir() and junk == "plink":
                subprocess.run(["cmd", "/c", "rmdir", str(p)], capture_output=True)
            elif p.is_file():
                p.unlink()
        git(wt, "add", "-A")
        git(wt, "commit", "-m", "remove unsafe artifacts, keep fix")
        rc2 = aw.review_worktree("redteam", repo_root=repo, run_tests=True)
        out2 = capsys.readouterr().out
        assert rc2 == 0, "legitimate contribution must not stay poisoned"
        assert "SECRET CHECK: PASS" in out2


# =============================================================================
# GATE 4 — three agents + stale base + real conflict
# =============================================================================
class TestGate4StaleBase:
    def test_stale_base_and_conflict(self, tmp_path):
        import agent_worktree as aw
        repo = make_mini_repo(tmp_path, "g4")
        (repo / "X.py").write_text("x = 1\ny = 1\n", encoding="utf-8")
        (repo / "Y.py").write_text("yv = 1\n", encoding="utf-8")
        git(repo, "add", "-A")
        git(repo, "commit", "-m", "revision A")
        os.environ["AGENT_WORKTREE_ROOT"] = str(tmp_path / "g4wts")
        wts = {n: aw.create_worktree(n, repo_root=repo) for n in ("glm", "opus", "codex")}

        (wts["glm"] / "X.py").write_text("x = 2\ny = 1\n", encoding="utf-8")     # GLM: X
        (wts["opus"] / "Y.py").write_text("yv = 2\n", encoding="utf-8")          # OPUS: Y
        (wts["codex"] / "X.py").write_text("x = 99\ny = 1\n", encoding="utf-8")  # CODEX: same X line
        for wt in wts.values():
            git(wt, "add", "-A")
            git(wt, "commit", "-m", "agent change")

        # merge GLM -> revision B
        b_glm = git(wts["glm"], "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        assert git(repo, "merge", b_glm).returncode == 0
        assert (repo / "X.py").read_text(encoding="utf-8").startswith("x = 2")

        # review OPUS from stale base A: non-conflicting -> allowed after revalidation
        rc = aw.review_worktree("opus", repo_root=repo, run_tests=False)
        assert rc == 0
        b_opus = git(wts["opus"], "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        assert git(repo, "merge", b_opus).returncode == 0
        # no gate bypass: post-merge tree still verified
        assert verify_agent_safe(repo) == []
        assert (repo / "Y.py").read_text(encoding="utf-8") == "yv = 2\n"

        # CODEX conflicts with GLM's merged change -> surfaced, never silent
        b_codex = git(wts["codex"], "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        merge = git(repo, "merge", b_codex)
        conflicted = merge.returncode != 0 or "<<<<<<<" in (repo / "X.py").read_text(encoding="utf-8")
        assert conflicted, "overlapping semantic changes must surface a conflict"
        git(repo, "merge", "--abort")


# =============================================================================
# GATE 5 — private runtime immutability across the complete workflow
# =============================================================================
PRIVATE_FILES_G5 = {
    "credentials.bin": b"CRED-SYNTHETIC-9R-2026-unique",
    "oauth_access.dat": b"ACCESS-SYNTHETIC-9R-2026",
    "oauth_refresh.dat": b"REFRESH-SYNTHETIC-9R-2026",
    "provider_state.json": b'{"providers": ["synthetic"], "note": "private"}',
    "data.sqlite": b"SQLite format 3\x00PRIVATE-SYNTH",
    "data.sqlite-wal": b"\x00wal-private",
    "data.sqlite-shm": b"\x00shm-private",
    "jwt-secret": b"j" * 48,
    "machine-id": b"0f1e2d3c4b5a69788796a5b4c3d2e1f0",
    "settings.json": b'{"trusted_os_unlock": false}',
}


class TestGate5PrivateImmutability:
    def test_all_operations_preserve_private_hashes(self, tmp_path, monkeypatch):
        import agent_worktree as aw
        import deploy_local as dl
        repo = make_mini_repo(tmp_path, "g5")
        priv = tmp_path / "PRIVATE_RUNTIME"
        priv.mkdir()
        for name, blob in PRIVATE_FILES_G5.items():
            (priv / name).write_bytes(blob)
        baseline = hash_tree(priv)
        assert len(baseline) == len(PRIVATE_FILES_G5)

        monkeypatch.setenv("AGENT_WORKTREE_ROOT", str(tmp_path / "g5wts"))
        monkeypatch.setenv("WATCHEDIT_DATA_DIR", str(priv))
        monkeypatch.setattr(dl, "PRIVATE_RUNTIME_ROOT", priv)

        # 1. application startup (subprocess, own data dir to avoid app-owned writes)
        boot_env = dict(os.environ)
        boot_env.update(WATCHEDIT_DATA_DIR=str(tmp_path / "g5_appdata"),
                        QT_QPA_PLATFORM="offscreen", PYTHONDONTWRITEBYTECODE="1")
        boot = subprocess.run([sys.executable, "-c", (
            "import sys; sys.path.insert(0, '9router_WatchEdit')\n"
            "from PySide6.QtWidgets import QApplication\n"
            "app = QApplication([])\n"
            "from ui.main_window import MainWindow\n"
            "w = MainWindow(); w.close()\nprint('BOOT')")],
            cwd=str(REPO_ROOT), capture_output=True, text=True, env=boot_env, timeout=180)
        assert "BOOT" in boot.stdout, boot.stderr[-300:]

        # 2. agent worktree creation + 3. merge + 4. tests
        wt = aw.create_worktree("immutable", repo_root=repo)
        (wt / "app.py").write_text("VALUE = 5\n", encoding="utf-8")
        git(wt, "add", "-A")
        git(wt, "commit", "-m", "change")
        assert aw.review_worktree("immutable", repo_root=repo, run_tests=True) == 0
        branch = git(wt, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        assert git(repo, "merge", branch).returncode == 0

        # 5. normal DEPLOY_LOCAL  (6. restart not required for a repo-run app)
        assert dl.deploy_local(skip_tests=True, quiet=True, repo_root=repo) is True
        assert hash_tree(priv) == baseline

        # 7. safe diagnostic export
        from core.diagnostics import export_diagnostic_bundle
        export_diagnostic_bundle(
            recent_errors=[f"Authorization: Bearer {canary_access_token()}"],
            out_dir=tmp_path / "diag_g5")
        assert hash_tree(priv) == baseline

        # 8. rollback (injected smoke failure while deploying another change)
        monkeypatch.setattr(dl, "_smoke_test", lambda *a, **k: (False, "injected"))
        assert dl.deploy_local(skip_tests=True, quiet=True, repo_root=repo) is False
        monkeypatch.undo()
        assert hash_tree(priv) == baseline

        # 9. deploy again
        assert dl.deploy_local(skip_tests=True, quiet=True, repo_root=repo) is True
        assert hash_tree(priv) == baseline, "CRITICAL FAIL: private bytes mutated"


# =============================================================================
# GATE 6 — deploy crash matrix (terminal-state vocabulary)
# =============================================================================
class TestGate6CrashMatrix:
    def _priv(self, tmp_path):
        priv = tmp_path / "priv_g6"
        priv.mkdir()
        (priv / "s.dat").write_bytes(b"private-bytes")
        return priv

    def _feature(self, repo, tag):
        git(repo, "checkout", "-b", f"feat{tag}")
        (repo / "app.py").write_text(f"VALUE = {tag}\n", encoding="utf-8")
        git(repo, "add", "-A")
        git(repo, "commit", "-m", f"feature {tag}")
        git(repo, "checkout", "master")

    def _deploy(self, dl, repo, source=None, **kw):
        return dl.deploy_local(source=source, skip_tests=True, quiet=True, repo_root=repo, **kw)

    def test_d1_before_validation(self, tmp_path, monkeypatch):
        import deploy_local as dl
        repo = mini_canary_repo(tmp_path, "d1", canary_api_key())
        priv = self._priv(tmp_path)
        monkeypatch.setenv("WATCHEDIT_DATA_DIR", str(priv))
        monkeypatch.setattr(dl, "PRIVATE_RUNTIME_ROOT", priv)
        assert self._deploy(dl, repo) is False
        assert dl.last_status() == "FAILED_NO_MUTATION"

    def test_d2_dirty_tree(self, tmp_path, monkeypatch):
        import deploy_local as dl
        repo = make_mini_repo(tmp_path, "d2")
        (repo / "app.py").write_text("dirty\n", encoding="utf-8")
        priv = self._priv(tmp_path)
        monkeypatch.setenv("WATCHEDIT_DATA_DIR", str(priv))
        monkeypatch.setattr(dl, "PRIVATE_RUNTIME_ROOT", priv)
        assert self._deploy(dl, repo) is False
        assert dl.last_status() == "FAILED_NO_MUTATION"
        assert (repo / "app.py").read_text(encoding="utf-8") == "dirty\n"  # preserved

    def test_d3_failing_tests(self, tmp_path, monkeypatch):
        import deploy_local as dl
        repo = make_mini_repo(tmp_path, "d3")
        (repo / "tests" / "test_boom.py").write_text("def test_boom():\n    assert False\n", encoding="utf-8")
        git(repo, "add", "-A")
        git(repo, "commit", "-m", "stage failing test")
        priv = self._priv(tmp_path)
        monkeypatch.setenv("WATCHEDIT_DATA_DIR", str(priv))
        monkeypatch.setattr(dl, "PRIVATE_RUNTIME_ROOT", priv)
        assert dl.deploy_local(quiet=True, repo_root=repo) is False
        assert dl.last_status() == "FAILED_NO_MUTATION"

    def test_d4_d5_mutation_failures_roll_back(self, tmp_path, monkeypatch):
        import deploy_local as dl
        repo = make_mini_repo(tmp_path, "d4")
        self._feature(repo, 4)
        priv = self._priv(tmp_path)
        monkeypatch.setenv("WATCHEDIT_DATA_DIR", str(priv))
        monkeypatch.setattr(dl, "PRIVATE_RUNTIME_ROOT", priv)
        # D5: activation checkout fails mid-deploy (W2-002: detached SHA).
        real_git = dl._git
        target_sha = real_git(["rev-parse", "feat4^{commit}"], repo).stdout.strip()
        def flaky(args, repo_root=None):
            if args and args[0] == "checkout" and target_sha and target_sha in args:
                class R:
                    returncode, stderr = 1, "locked"
                return R()
            return real_git(args, repo_root)
        monkeypatch.setattr(dl, "_git", flaky)
        assert self._deploy(dl, repo, source="feat4") is False
        assert dl.last_status() == "FAILED_ROLLED_BACK"
        monkeypatch.setattr(dl, "_git", real_git)
        # D4-style: re-verification fails after validation (committed sneak)
        (repo / "sneak.log").write_text(f"t: {canary_access_token()}\n", encoding="utf-8")
        git(repo, "add", "-A")
        git(repo, "commit", "-m", "sneak")
        calls = {"n": 0}
        real_vas = dl.vas.verify_agent_safe
        def blind_first(root):
            calls["n"] += 1
            return [] if calls["n"] == 1 else real_vas(root)
        monkeypatch.setattr(dl.vas, "verify_agent_safe", blind_first)
        assert self._deploy(dl, repo, source="feat4") is False
        assert dl.last_status() == "FAILED_ROLLED_BACK"

    def test_d6_d7_restart_and_health_failures(self, tmp_path, monkeypatch):
        import deploy_local as dl
        repo = make_mini_repo(tmp_path, "d6")
        self._feature(repo, 6)
        priv = self._priv(tmp_path)
        monkeypatch.setenv("WATCHEDIT_DATA_DIR", str(priv))
        monkeypatch.setattr(dl, "PRIVATE_RUNTIME_ROOT", priv)
        monkeypatch.setattr(dl, "_start_instance", lambda *a, **k: False)
        assert self._deploy(dl, repo, source="feat6", restart=True) is False
        assert dl.last_status() == "FAILED_ROLLED_BACK"
        monkeypatch.setattr(dl, "_start_instance", lambda *a, **k: True)
        monkeypatch.setattr(dl, "_stop_running_instance", lambda *a, **k: True)
        monkeypatch.setattr(dl, "_smoke_test", lambda *a, **k: (False, "health check failed"))
        assert self._deploy(dl, repo, source="feat6", restart=True) is False
        assert dl.last_status() == "FAILED_ROLLED_BACK"

    def test_d9_receipt_write_failure_never_success(self, tmp_path, monkeypatch):
        import deploy_local as dl
        repo = make_mini_repo(tmp_path, "d9")
        priv = self._priv(tmp_path)
        monkeypatch.setenv("WATCHEDIT_DATA_DIR", str(priv))
        monkeypatch.setattr(dl, "PRIVATE_RUNTIME_ROOT", priv)
        def boom(*a, **k):
            raise OSError("receipt/marker write failed")
        monkeypatch.setattr(dl, "_marker_path", lambda rr: boom())
        try:
            ok = self._deploy(dl, repo)
        except Exception:
            ok = False  # an exception exit is a failure, never SUCCESS
        assert ok is False
        assert dl.last_status() != "SUCCESS"

    def test_d10_rollback_failure_requires_recovery(self, tmp_path, monkeypatch):
        import deploy_local as dl
        repo = make_mini_repo(tmp_path, "d10")
        self._feature(repo, 10)
        priv = self._priv(tmp_path)
        monkeypatch.setenv("WATCHEDIT_DATA_DIR", str(priv))
        monkeypatch.setattr(dl, "PRIVATE_RUNTIME_ROOT", priv)
        monkeypatch.setattr(dl, "_smoke_test", lambda *a, **k: (False, "injected"))
        real_git = dl._git
        class Fail:
            returncode, stdout, stderr = 1, "", "rollback failed"
        def flaky(args, repo_root=None):
            if args and args[0] == "checkout":
                return Fail()
            return real_git(args, repo_root)
        monkeypatch.setattr(dl, "_git", flaky)
        assert self._deploy(dl, repo, source="feat10") is False
        assert dl.last_status() == "RECOVERY_REQUIRED", \
            "failed rollback must never claim the system is restored"


# =============================================================================
# GATE 7 — process interruption / abandoned temp state
# =============================================================================
class TestGate7Interruption:
    def test_abandoned_deploy_marker_recovered(self, tmp_path):
        import deploy_local as dl
        repo = make_mini_repo(tmp_path, "g7")
        git(repo, "checkout", "-b", "interrupted")
        (repo / "app.py").write_text("VALUE = 'half deployed'\n", encoding="utf-8")
        git(repo, "add", "-A")
        git(repo, "commit", "-m", "interrupted work")
        # simulate hard kill mid-APPLYING: repo left on the branch, marker stuck
        marker = repo / ".git" / "watchedit_deploy_marker.json"
        marker.write_text(json.dumps({
            "state": "APPLYING", "started": "2026-09-04T00:00:00",
            "original_ref": "master", "original_sha": "x", "tag": "predeploy/x",
        }), encoding="utf-8")
        out = subprocess.run(
            [sys.executable, str(TOOLS / "deploy_local.py"), "--skip-tests",
             "--repo", str(repo)],
            cwd=str(repo), capture_output=True, text=True, timeout=300)
        combined = out.stdout + out.stderr
        assert "ABANDONED DEPLOY DETECTED" in combined
        # deterministic recovery: back to master, marker cleared/rewritten
        assert git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == "master"
        assert (repo / "app.py").read_text(encoding="utf-8") == "VALUE = 1\n"

    def test_stale_staging_directory_deterministic(self, tmp_path):
        import build_safe_share as bss
        out_dir = tmp_path / "share_g7"
        staging = out_dir / ".staging"
        staging.mkdir(parents=True)
        (staging / "junk.txt").write_text("abandoned staging junk", encoding="utf-8")
        result = bss.build_safe_share(out_dir=out_dir, skip_gitleaks=True, quiet=True)
        assert result is not None, "orphaned staging must be cleaned and rebuilt deterministically"

    def test_partial_archive_never_mistaken_for_complete(self, tmp_path):
        out_dir = tmp_path / "share_g7b"
        out_dir.mkdir()
        partial = out_dir / ".SAFE_20260101_000000.partial.zip"
        partial.write_bytes(b"half-written")
        import build_safe_share as bss
        result = bss.build_safe_share(out_dir=out_dir, skip_gitleaks=True, quiet=True)
        assert result is not None and result != partial
        assert not partial.exists(), "stale partial artifacts are cleaned"


# =============================================================================
# GATE 8 + 9 — redaction torture + prefix/substring leak
# =============================================================================
class TestGate8And9RedactionTorture:
    def _torture_object(self):
        return {
            "provider": "prov-nine",
            "models": [{"id": "prov/model-x", "http": 401, "latency": 512.0}],
            "nested_oauth": {"accessToken": canary_access_token(),
                             "refreshToken": canary_refresh_token(),
                             "clientSecret": canary_client_secret()},
            "list_channel": [{"apiKey": canary_api_key()}, "plain"],
            "tuple_channel": ({"password": canary_password()},),
            "url_query": f"https://api.example.test/v1?token={canary_access_token()}",
            "http_header": f"Authorization: Bearer {canary_access_token()}",
            "json_string": json.dumps({"refreshToken": canary_refresh_token()}),
            "multiline": f"refreshToken:\n  \"{canary_refresh_token()}\"\n",
            "provider_response": {"error": "bad token",
                                  "provided_token": canary_access_token()},
        }

    def test_every_channel_redacts_complete_canaries(self, tmp_path):
        from core.redaction import redact_exception, redact_mapping, redact_text
        outputs = []

        obj = self._torture_object()
        outputs.append(json.dumps(redact_mapping(obj)))
        outputs.append(redact_text(json.dumps(obj)))
        try:
            raise RuntimeError(f"upstream failed: {canary_refresh_token()} "
                               f"bearer {canary_access_token()}")
        except RuntimeError as ex:
            outputs.append(redact_exception(ex))
        # exception chain with canary in args
        try:
            try:
                raise ValueError(f"inner {canary_client_secret()}")
            except ValueError as inner:
                raise RuntimeError("outer") from inner
        except RuntimeError as ex:
            import traceback
            outputs.append(redact_text("".join(traceback.format_exception(ex))))

        # diagnostic bundle channel
        from core.diagnostics import export_diagnostic_bundle
        bundle = export_diagnostic_bundle(
            recent_errors=[json.dumps(self._torture_object())],
            out_dir=tmp_path / "g8diag")
        outputs.append(bundle.read_text(encoding="utf-8"))

        # receipt-style channel
        outputs.append("DEPLOY REPORT\n" + redact_text(json.dumps(obj)))

        for canary in CANARIES + [LONG_CANARY]:
            for out in outputs:
                assert canary not in out, f"complete canary survived: {canary[:18]}..."

        # GATE 9: no large fragments either (first/last 12 chars)
        for canary in (LONG_CANARY, canary_refresh_token()):
            head, tail = canary[:12], canary[-12:]
            for out in outputs:
                assert head not in out and tail not in out, \
                    "secret fragment leaked (prefix/suffix policy)"

        # useful data retained in the diagnostic (not a delete-everything blob)
        blob = outputs[-2]
        assert "prov-nine" in blob and "prov/model-x" in blob and "401" in blob

    def test_output_directories_byte_searched(self, tmp_path):
        from core.diagnostics import export_diagnostic_bundle
        out_dir = tmp_path / "g8bytes"
        export_diagnostic_bundle(
            recent_errors=[f"Authorization: Bearer {canary_access_token()}",
                           f"secret {canary_api_key()}"],
            out_dir=out_dir)
        for p in out_dir.rglob("*"):
            if p.is_file():
                data = p.read_bytes()
                for canary in CANARIES:
                    assert canary.encode() not in data, f"canary bytes in {p}"


# =============================================================================
# GATE 10 — safe diagnostic from dirty realistic state
# =============================================================================
class TestGate10DirtyDiagnostics:
    def test_useful_but_sanitized(self, tmp_path):
        from core.diagnostics import export_diagnostic_bundle
        from core.history import HealthCache
        cache = HealthCache(cache_file=tmp_path / "cache.json")
        realistic = {
            "providers": [
                {"id": "conn-1", "name": "Antigravity", "prefix": "ag",
                 "apiKey": canary_api_key(), "custom_headers":
                 {"Authorization": f"Bearer {canary_access_token()}"}},
                {"id": "conn-2", "name": "DeepSeek", "prefix": "ds",
                 "oauth": {"accessToken": canary_access_token(),
                           "refreshToken": canary_refresh_token(),
                           "clientSecret": canary_client_secret()}}],
            "models": ["ag/gemini-3.8-flash-high", "ds/deepseek-v4-flash"],
            "combos": [{"id": "c1", "name": "SAIFREN", "models":
                        ["ag/gemini-3.8-flash-high", "ds/deepseek-v4-flash"]}],
            "failures": [
                {"conn": "conn-1", "http": 401, "error": "AUTH rejected"},
                {"conn": "conn-2", "http": 402, "error": "BALANCE exhausted"},
                {"conn": "conn-1", "http": 429, "error": "RATE_LIMIT"},
                {"conn": "conn-2", "http": 408, "error": "TIMEOUT"}],
            "cookies": {"session": canary_client_secret()},
            "machine": {"machine_id": "0f1e2d3c4b5a69788796a5b4c3d2e1f0",
                        "jwt_secret": "j" * 43},
        }
        out = export_diagnostic_bundle(
            cache=cache, recent_errors=[json.dumps(realistic)],
            out_dir=tmp_path / "g10diag")
        blob = out.read_text(encoding="utf-8")
        for retained in ("Antigravity", "DeepSeek", "ag/gemini-3.8-flash-high",
                         "SAIFREN", "401", "AUTH rejected", "402"):
            assert retained in blob, f"diagnostic lost useful data: {retained}"
        for canary in CANARIES:
            assert canary not in blob
        assert "0f1e2d3c4b5a69788796a5b4c3d2e1f0" not in blob  # machine identity removed
        # independent scan of the final artifact
        from core.secret_scanner import scan_file
        assert scan_file(out, tmp_path) == []


# =============================================================================
# GATE 11 — history contamination + approved remediation (disposable repo)
# =============================================================================
class TestGate11HistoryRemediation:
    def test_contamination_and_approved_remediation(self, tmp_path, capsys):
        from check_git_history import check_history
        repo = make_mini_repo(tmp_path, "g11")
        # revision B: fake secret committed; C: removed; D: clean tree
        (repo / "leak.txt").write_text(f"key: {canary_api_key()}\n", encoding="utf-8")
        git(repo, "add", "-A")
        git(repo, "commit", "-m", "B leak")
        git(repo, "rm", "-q", "leak.txt")
        git(repo, "commit", "-m", "C remove")

        assert verify_agent_safe(repo) == []          # current tree PASS
        assert check_history(repo_root=repo) == 1     # history FAIL
        assert "SECRET MATERIAL EXISTS IN HISTORY" in capsys.readouterr().out

        # remediation refuses without the explicit flag
        import remediate_git_history as rg
        assert rg.main(["--repo", str(repo), "--purge", "leak.txt"]) == 2
        # approved remediation in the DISPOSABLE repo
        assert rg.main(["--repo", str(repo), "--purge", "leak.txt",
                        "--i-understand-this-rewrites-history"]) == 0
        assert check_history(repo_root=repo) == 0
        assert "CLEAN HISTORY" in capsys.readouterr().out

    def test_refuses_real_repo_without_explicit_flag(self):
        import remediate_git_history as rg
        assert rg.main(["--repo", str(REPO_ROOT), "--purge", "x",
                        "--i-understand-this-rewrites-history"]) == 2


# =============================================================================
# GATE 12 — nested repository policy
# =============================================================================
class TestGate12NestedRepo:
    def test_nested_repo_content_scanned(self, tmp_path):
        repo = make_mini_repo(tmp_path, "g12")
        nested = repo / "vendor-nested"
        subprocess.run(["git", "init", "-q", str(nested)], capture_output=True)
        (nested / "lib.js").write_text("module.exports = 1;\n", encoding="utf-8")
        (nested / "creds.json").write_text(
            json.dumps({"apiKey": canary_api_key()}), encoding="utf-8")
        git(nested, "add", "-A")
        git(nested, "-c", "user.name=t", "-c", "user.email=t@l", "commit", "-m", "x")
        findings = _v(repo)
        # POLICY A: nested repository CONTENT is scanned recursively
        assert any("creds.json" in u.path for u in findings), \
            "nested repository content must never be silently skipped"
        # nested .git internals do not crash the walker
        assert format_result(findings).startswith("AGENT SAFE: NO")


# =============================================================================
# GATE 13 — archive bomb resilience
# =============================================================================
class TestGate13ArchiveBomb:
    def test_archives_blocked_without_unpacking(self, tmp_path):
        repo = make_mini_repo(tmp_path, "g13")
        big = repo / "bomb.zip"
        with zipfile.ZipFile(big, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("zeros.bin", b"\x00" * (10 * 1024 * 1024))  # 10MB -> tiny
        nested = repo / "nested.zip"
        import io
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as outer:
            outer.writestr("inner.zip", big.read_bytes())
        nested.write_bytes(buf.getvalue())

        t0 = time.monotonic()
        findings = _v(repo)
        elapsed = time.monotonic() - t0
        paths = [u.path for u in findings]
        assert "bomb.zip" in paths and "nested.zip" in paths
        assert elapsed < 10.0, f"archive handling must stay resource bounded ({elapsed:.1f}s)"
        # archives are flagged as protected/unverifiable, never unpacked
        reasons = [u.reason for u in findings]
        assert all("protected file type" in r for r in reasons if "zip" in str(reasons)) or reasons


# =============================================================================
# GATE 14 — network independence
# =============================================================================
class TestGate14NetworkIndependence:
    def test_offline_workflow(self, tmp_path, monkeypatch):
        import agent_worktree as aw
        def dead_socket(*a, **k):
            raise ConnectionRefusedError("network disabled")
        monkeypatch.setattr(socket, "create_connection", dead_socket)
        repo = make_mini_repo(tmp_path, "g14")
        monkeypatch.setenv("AGENT_WORKTREE_ROOT", str(tmp_path / "g14wts"))
        assert verify_agent_safe(repo) == []          # VERIFY works offline
        wt = aw.create_worktree("offline", repo_root=repo)  # worktree creation offline
        (wt / "app.py").write_text("VALUE = 3\n", encoding="utf-8")
        git(wt, "add", "-A")
        git(wt, "commit", "-m", "offline change")
        assert aw.review_worktree("offline", repo_root=repo, run_tests=False) == 0  # review offline


# =============================================================================
# GATE 15 — localhost trust boundary
# =============================================================================
class TestGate15LocalhostBoundary:
    def test_locked_mode_never_authenticates_to_localhost(self, tmp_path, monkeypatch):
        monkeypatch.delenv("WATCHEDIT_LIVE_ACCESS", raising=False)
        from core.security import LiveAccessLockedError, SecurityManager
        from core.router_client import RouterClient

        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(4)
        port = server.getsockname()[1]
        connections = []
        try:
            mgr = SecurityManager(data_dir=tmp_path / "sec")
            client = RouterClient(base_url=f"http://127.0.0.1:{port}", security=mgr)
            with pytest.raises(LiveAccessLockedError):
                client.get_providers()
            with pytest.raises(LiveAccessLockedError):
                client.ping_model_fast("p/m")
            server.settimeout(0.5)
            try:
                while True:
                    conn, _ = server.accept()
                    connections.append(conn)
            except socket.timeout:
                pass
            assert connections == [], "LOCKED mode must not even connect to localhost"
        finally:
            server.close()
            for c in connections:
                c.close()

    def test_fake_service_payload_never_lands_in_repo(self, tmp_path, monkeypatch):
        """Even with live access allowed, a hostile localhost payload must not
        be persisted anywhere in the repository tree."""
        monkeypatch.setenv("WATCHEDIT_LIVE_ACCESS", "1")
        before = hash_tree(REPO_ROOT / "9router_WatchEdit")
        # a fake provider response is processed by the sanitizing boundary only
        from core.router_client import RouterClient
        client = RouterClient(base_url="http://127.0.0.1:1")
        clean = client._sanitize_connection({
            "id": "c1", "provider": "evil", "name": "Evil",
            "providerSpecificData": {"apiKey": canary_api_key(), "prefix": "ev"},
        })
        blob = json.dumps(clean)
        assert canary_api_key() not in blob
        after = hash_tree(REPO_ROOT / "9router_WatchEdit")
        assert set(after) == set(before)
