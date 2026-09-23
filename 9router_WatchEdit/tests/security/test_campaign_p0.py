"""
Adversarial security campaign - P0 scenarios (campaign sections 2-11, 29).

All canaries are synthetic (tests/security/canaries.py). No real credential
is ever inspected, copied, printed or fixtureized.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from tests.security.canaries import (
    canary_access_token, canary_api_key, canary_client_secret, canary_jwt,
    canary_password, canary_pem, canary_refresh_token, canary_sk,
    canary_bearer_line,
)
from tests.security.helpers import (
    REPO_ROOT, TOOLS, git, hash_tree, make_mini_repo, mini_canary_repo,
)

from verify_agent_safe import format_result, verify_agent_safe  # noqa: E402


def _v(root) -> list:
    return verify_agent_safe(Path(root))


# =============================================================================
# SEC-001..014 - entire tree secret scan
# =============================================================================
class TestSecScan:
    def test_sec_001_tracked_secret(self, tmp_path):
        repo = mini_canary_repo(tmp_path, "sec001", canary_api_key())
        git(repo, "add", "-A")
        git(repo, "commit", "-m", "accident")
        findings = _v(repo)
        assert findings, "tracked secret must fail the gate"
        out = format_result(findings)
        assert out.startswith("AGENT SAFE: NO")
        assert canary_api_key() not in out  # never the complete canary
        assert "leaked.txt" in out  # path identifies the offending file

    def test_sec_001_exit_code_nonzero(self, tmp_path):
        import verify_agent_safe as vas
        repo = mini_canary_repo(tmp_path, "sec001b", canary_refresh_token())
        assert vas.main(["--root", str(repo), "--quiet"]) == 1
        clean = make_mini_repo(tmp_path, "sec001c")
        assert vas.main(["--root", str(clean), "--quiet"]) == 0

    def test_sec_002_untracked_secret(self, tmp_path):
        repo = make_mini_repo(tmp_path, "sec002")
        (repo / "random.tmp").write_text(f"x: {canary_access_token()}\n", encoding="utf-8")
        st = git(repo, "status", "--porcelain").stdout
        assert "??" in st  # untracked
        assert _v(repo), "untracked secret must fail the filesystem-based gate"

    def test_sec_003_gitignored_secret(self, tmp_path):
        repo = make_mini_repo(tmp_path, "sec003")
        gi = repo / ".gitignore"
        existing = gi.read_text(encoding="utf-8") if gi.exists() else ""
        gi.write_text(existing + "private-test/\n*.secret-test\n", encoding="utf-8")
        d = repo / "private-test"
        d.mkdir()
        (d / "ignored.secret-test").write_text(
            f"refresh_token: {canary_refresh_token()}\n", encoding="utf-8")
        # confirm git really ignores it
        assert "ignored.secret-test" not in git(repo, "status", "--porcelain").stdout
        findings = _v(repo)
        assert any("ignored.secret-test" in u.path for u in findings), \
            ".gitignore is not a security boundary"

    def test_sec_004_deeply_nested(self, tmp_path):
        repo = make_mini_repo(tmp_path, "sec004")
        deep = repo / Path("a/b/c/d/e/f/g/h")
        deep.mkdir(parents=True)
        (deep / "config.json").write_text(
            json.dumps({"refreshToken": canary_refresh_token()}), encoding="utf-8")
        assert any("config.json" in u.path for u in _v(repo))

    @pytest.mark.parametrize("variant", [
        "apiKey", "apikey", "API_KEY", "api_key", "accessToken", "ACCESS_TOKEN",
        "refreshToken", "refresh_token", "clientSecret", "client_secret",
        "authorization", "Authorization", "password", "PASSWORD", "secret", "jwt",
    ])
    def test_sec_005_key_variants(self, tmp_path, variant):
        repo = make_mini_repo(tmp_path, f"sec005_{variant}")
        (repo / "cfg.json").write_text(
            json.dumps({variant: canary_client_secret()}), encoding="utf-8")
        assert _v(repo), f"variant {variant} must be classified suspicious"

    def test_sec_006_value_pattern_without_field_name(self, tmp_path):
        repo = make_mini_repo(tmp_path, "sec006")
        (repo / "data.json").write_text(
            json.dumps({"thing": canary_sk()}), encoding="utf-8")
        reasons = [u.reason for u in _v(repo)]
        assert any("provider_api_key" in r for r in reasons), \
            "suspicious value must be detected independently of field name"

    def test_sec_007_unknown_value_format(self, tmp_path):
        repo = make_mini_repo(tmp_path, "sec007")
        odd_value = "something-that-does-not-" + "look-like-a-known-provider-token"
        (repo / "data.json").write_text(json.dumps({"refreshToken": odd_value}),
                                        encoding="utf-8")
        assert any("sensitive_json_key" in u.reason for u in _v(repo)), \
            "semantic field name itself is sensitive"

    def test_sec_008_authorization_header(self, tmp_path):
        repo = make_mini_repo(tmp_path, "sec008")
        (repo / "req.txt").write_text(canary_bearer_line() + "\n", encoding="utf-8")
        out = format_result(_v(repo))
        assert _v(repo)
        assert canary_access_token() not in out  # never reproduce the header

    def test_sec_009_jwt(self, tmp_path):
        repo = make_mini_repo(tmp_path, "sec009")
        (repo / "logs").mkdir()
        (repo / "logs" / "debug.txt").write_text(f"auth={canary_jwt()}\n", encoding="utf-8")
        assert any("jwt_token" in u.reason for u in _v(repo))

    def test_sec_010_private_key(self, tmp_path):
        repo = make_mini_repo(tmp_path, "sec010")
        (repo / "key_material.txt").write_text(canary_pem(), encoding="utf-8")
        assert any("private_key_block" in u.reason for u in _v(repo))

    def test_sec_011_sqlite_without_credentials(self, tmp_path):
        repo = make_mini_repo(tmp_path, "sec011")
        (repo / "temporary.sqlite").write_bytes(b"SQLite format 3\x00" + b"\x00" * 32)
        assert any("protected file type" in u.reason for u in _v(repo)), \
            "policy must not depend on sqlite content analysis"

    @pytest.mark.parametrize("suffix", ["data.sqlite-wal", "data.sqlite-shm"])
    def test_sec_012_wal_shm(self, tmp_path, suffix):
        repo = make_mini_repo(tmp_path, f"sec012_{suffix}")
        (repo / suffix).write_bytes(b"\x00" * 16)
        assert any(suffix in u.path for u in _v(repo))

    def test_sec_013_machine_id(self, tmp_path):
        repo = make_mini_repo(tmp_path, "sec013")
        (repo / "machine-id").write_text("0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0\n", encoding="utf-8")
        assert any(u.path.endswith("machine-id") for u in _v(repo))

    @pytest.mark.parametrize("relpath,content", [
        ("backup/providers-state-export.json", '{"conns": []}'),
        ("backup/data-snapshot/jwt-secret", "x" * 32),
        ("backup/data-snapshot/machine-id", "y" * 32),
        ("backup/data-snapshot/data.sqlite", "SQLite format 3\x00"),
    ])
    def test_sec_014_original_contamination_regression(self, tmp_path, relpath, content):
        repo = make_mini_repo(tmp_path, "sec014_" + relpath.replace("/", "_").replace(".", ""))
        p = repo / relpath
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        assert _v(repo), f"{relpath} must independently make AGENT SAFE fail"


# =============================================================================
# REDACT-001..006 - no secret leak in output
# =============================================================================
class TestRedaction:
    def test_reduct_001_log_redaction(self):
        from core.redaction import redact_exception
        try:
            raise RuntimeError(f"upstream rejected {canary_refresh_token()}")
        except RuntimeError as ex:
            red = redact_exception(ex)
        assert canary_refresh_token() not in red

    def test_reduct_002_nested_object(self):
        from core.redaction import redact_mapping
        obj = {"provider": {"account": {"oauth": {"refreshToken": canary_refresh_token()}}}}
        red = redact_mapping(obj)
        assert canary_refresh_token() not in json.dumps(red)
        assert red["provider"]["account"]["oauth"]["refreshToken"] == "<REDACTED>"

    def test_reduct_003_list_redaction(self):
        from core.redaction import redact_mapping
        obj = {"conns": [{"apiKey": canary_api_key()}, {"clientSecret": canary_client_secret()}]}
        red = redact_mapping(obj)
        blob = json.dumps(red)
        assert canary_api_key() not in blob and canary_client_secret() not in blob

    def test_reduct_004_error_body(self):
        from core.redaction import redact_mapping
        body = {"error": "bad token", "provided_token": canary_access_token()}
        red = redact_mapping(body)
        assert canary_access_token() not in json.dumps(red)
        assert red["error"] == "bad token"  # normalized error preserved

    def test_reduct_005_exception_string(self):
        from core.redaction import redact_exception
        red = redact_exception(RuntimeError(f"failed with Authoriza" "tion: Bearer " + canary_access_token()))
        assert canary_access_token() not in red
        assert "<REDACTED>" in red

    def test_reduct_006_command_line_safety(self, tmp_path):
        """Master password is never accepted as a command-line argument.

        REDACT-006 regression: argparse prefix abbreviation previously let
        `--password x` bind to `--password-env`, leaking the secret into
        the command line and shell history."""
        res = subprocess.run(
            [sys.executable, str(TOOLS / "create_private_backup.py"),
             "--password", "x" * 12],
            capture_output=True, text=True, timeout=60, cwd=str(REPO_ROOT),
            env={**os.environ, "WATCHEDIT_DATA_DIR": str(tmp_path / "priv")},
        )
        assert res.returncode == 2, "parser must reject --password (unrecognized argument)"
        assert "unrecognized" in res.stderr
        assert "x" * 12 not in res.stderr


# =============================================================================
# TREE-001..005 - safe repo physical boundary
# =============================================================================
class TestTreeBoundary:
    def test_tree_001_no_private_roots(self):
        for d in ("backup", "private", "secrets", "runtime"):
            assert not (REPO_ROOT / d).exists(), f"repo/{d} must not exist"
        assert verify_agent_safe(REPO_ROOT) == []

    def test_tree_002_003_clean_checkout(self, tmp_path):
        """Fresh clone: app boots LOCKED, tests run, no secrets created inside."""
        clone = tmp_path / "clone"
        res = subprocess.run(
            ["git", "clone", "-q", str(REPO_ROOT), str(clone)],
            capture_output=True, text=True, timeout=180)
        if res.returncode != 0:
            pytest.skip("git clone unavailable in test environment")
        env = dict(os.environ)
        env.update(WATCHEDIT_DATA_DIR=str(tmp_path / "fresh_private"),
                   QT_QPA_PLATFORM="offscreen", PYTHONDONTWRITEBYTECODE="1")
        boot = subprocess.run([sys.executable, "-c", (
            "import sys; sys.path.insert(0, '9router_WatchEdit')\n"
            "from PySide6.QtWidgets import QApplication\n"
            "app = QApplication([])\n"
            "from ui.main_window import MainWindow\n"
            "w = MainWindow()\n"
            "print('STATE=' + w.security.state)\n"
            "w.close()\n")], cwd=str(clone), capture_output=True, text=True, env=env, timeout=180)
        assert "STATE=LOCKED" in boot.stdout, boot.stderr[-400:]
        tests = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "--no-header",
             "9router_WatchEdit/tests/test_classification.py",
             "9router_WatchEdit/tests/test_history.py"],
            cwd=str(clone), capture_output=True, text=True, env=env, timeout=300)
        assert tests.returncode == 0, tests.stdout[-400:]
        # no secrets / runtime DB created inside the checkout
        assert verify_agent_safe(clone) == []

    def test_tree_004_runtime_path_escape(self, tmp_path):
        """Run the app; afterwards the repository gained no runtime files.

        The app runs in a SUBPROCESS: creating/destroying QApplication
        multiple times in one pytest process is a native crash source."""
        before = hash_tree(REPO_ROOT / "9router_WatchEdit")
        env = dict(os.environ)
        env.update(WATCHEDIT_DATA_DIR=str(tmp_path / "external_runtime"),
                   QT_QPA_PLATFORM="offscreen", PYTHONDONTWRITEBYTECODE="1")
        res = subprocess.run(
            [sys.executable, "-c", (
                "import sys; sys.path.insert(0, '9router_WatchEdit')\n"
                "from PySide6.QtWidgets import QApplication\n"
                "app = QApplication([])\n"
                "from ui.main_window import MainWindow\n"
                "w = MainWindow()\n"
                "w.close()\n"
                "print('RUN_OK')\n")],
            cwd=str(REPO_ROOT), capture_output=True, text=True, env=env, timeout=180)
        assert "RUN_OK" in res.stdout, res.stderr[-400:]
        after = hash_tree(REPO_ROOT / "9router_WatchEdit")
        new_files = set(after) - set(before)
        assert not new_files, f"runtime created files inside repo: {new_files}"

    def test_tree_005_read_only_source(self, tmp_path):
        """Read-only source tree still boots using external runtime state."""
        app_copy = tmp_path / "ro_app"
        app_copy.mkdir()
        import shutil
        for item in ("config.py", "run.py", "core", "ui"):
            src = REPO_ROOT / "9router_WatchEdit" / item
            if src.is_dir():
                shutil.copytree(src, app_copy / item,
                                ignore=shutil.ignore_patterns("__pycache__"))
            else:
                shutil.copy2(src, app_copy / item)
        for p in app_copy.rglob("*"):
            os.chmod(p, 0o444)
        os.chmod(app_copy, 0o555)
        try:
            env = dict(os.environ)
            env.update(WATCHEDIT_DATA_DIR=str(tmp_path / "external_state"),
                       QT_QPA_PLATFORM="offscreen", PYTHONDONTWRITEBYTECODE="1")
            res = subprocess.run([sys.executable, "-c", (
                "import sys; sys.path.insert(0, '.')\n"
                "from PySide6.QtWidgets import QApplication\n"
                "app = QApplication([])\n"
                "from ui.main_window import MainWindow\n"
                "w = MainWindow()\n"
                "print('BOOT_OK:' + w.security.state)\n"
                "w.close()\n")], cwd=str(app_copy), capture_output=True, text=True,
                env=env, timeout=180)
            assert "BOOT_OK:LOCKED" in res.stdout or "BOOT_OK:OS_VAULT" in res.stdout, \
                res.stderr[-400:]
        finally:
            for p in app_copy.rglob("*"):
                try:
                    os.chmod(p, 0o644)
                except OSError:
                    pass
            os.chmod(app_copy, 0o755)


# =============================================================================
# LINK-001..003 - symlink / junction escape
# =============================================================================
class TestLinks:
    def _junction(self, link: Path, target: Path) -> bool:
        res = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                             capture_output=True, text=True)
        return res.returncode == 0

    def test_link_001_external_junction_flagged_not_traversed(self, tmp_path):
        if os.name != "nt":
            pytest.skip("junction test is Windows-specific")
        repo = make_mini_repo(tmp_path, "link001")
        private_dir = tmp_path / "outside_private"
        private_dir.mkdir()
        (private_dir / "private_secret.dat").write_text(canary_refresh_token(), encoding="utf-8")
        link = repo / "runtime_link"
        if not self._junction(link, private_dir):
            pytest.skip("mklink /J unavailable (needs junction support)")
        findings = _v(repo)
        assert any("symlink/junction" in u.reason for u in findings)
        # MUST NOT traverse: findings must not mention private target contents
        assert not any("private_secret.dat" in u.path for u in findings)
        assert format_result(findings).startswith("AGENT SAFE: NO")

    def test_link_002_internal_symlink_also_rejected(self, tmp_path):
        repo = make_mini_repo(tmp_path, "link002")
        target = repo / "app.py"
        link = repo / "alias.py"
        try:
            os.symlink(target, link)
        except (OSError, NotImplementedError):
            pytest.skip("os.symlink requires developer mode on Windows")
        findings = _v(repo)
        # deterministic policy: ALL links rejected without approval
        assert any("symlink" in u.reason for u in findings)

    def test_link_003_broken_link_no_crash(self, tmp_path):
        repo = make_mini_repo(tmp_path, "link003")
        link = repo / "broken"
        try:
            os.symlink(tmp_path / "does_not_exist_anywhere", link)
        except (OSError, NotImplementedError):
            pytest.skip("os.symlink requires developer mode on Windows")
        findings = _v(repo)  # must not raise
        assert any("broken" in u.path for u in findings)


# =============================================================================
# EXPORT-001..005 - safe export / diagnostic export
# =============================================================================
class TestExports:
    def test_export_001_clean_diagnostic(self, tmp_path):
        from core.diagnostics import export_diagnostic_bundle
        out = export_diagnostic_bundle(recent_errors=["plain failure"], out_dir=tmp_path / "d1")
        assert out.exists()

    def test_export_002_canary_aborts(self, tmp_path, monkeypatch):
        from core import diagnostics
        # disable redaction so the canary would survive -> export must ABORT
        monkeypatch.setattr(diagnostics, "redact_mapping", lambda x, **k: x)
        monkeypatch.setattr(diagnostics, "redact_text", lambda x: x)
        with pytest.raises(RuntimeError):
            diagnostics.export_diagnostic_bundle(
                recent_errors=[f"Authorization: Bearer {canary_access_token()}"],
                out_dir=tmp_path / "d2")
        leftovers = list((tmp_path / "d2").glob("*"))
        assert all(p.name.startswith(".") for p in leftovers), \
            "no successfully-named bundle may remain after abort"

    def test_export_003_independent_artifact_scan(self, tmp_path):
        import build_safe_share as bss
        out_dir = tmp_path / "share"
        zip_path = bss.build_safe_share(out_dir=out_dir, skip_gitleaks=True, quiet=True)
        assert zip_path is not None
        unpacked = tmp_path / "unpacked"
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(unpacked)
        from core.secret_scanner import scan_tree
        assert scan_tree(unpacked) == []
        assert verify_agent_safe(unpacked) == []

    def test_export_004_error_midway_no_half_archive(self, tmp_path, monkeypatch):
        import build_safe_share as bss
        real_zipfile = bss.zipfile

        class FailingZip:
            calls = 0
            def __init__(self, *a, **k):
                self._zf = real_zipfile.ZipFile(*a, **k)
            def __enter__(self):
                self._zf.__enter__()
                return self
            def __exit__(self, *a):
                return self._zf.__exit__(*a)
            def write(self, *a, **k):
                FailingZip.calls += 1
                if FailingZip.calls >= 3:
                    raise OSError("simulated disk failure midway")
                return self._zf.write(*a, **k)

        monkeypatch.setattr(bss, "zipfile", __import__("types").SimpleNamespace(
            ZipFile=FailingZip, ZIP_DEFLATED=real_zipfile.ZIP_DEFLATED))
        result = bss.build_safe_share(out_dir=tmp_path / "share2", skip_gitleaks=True, quiet=True)
        assert result is None
        leftovers = list((tmp_path / "share2").glob("*.zip")) + list((tmp_path / "share2").glob("*.partial*"))
        assert leftovers == [], "no half-built archive with final name may remain"

    def test_export_005_zip_path_safety(self, tmp_path):
        import build_safe_share as bss
        zip_path = bss.build_safe_share(out_dir=tmp_path / "share3", skip_gitleaks=True, quiet=True)
        assert zip_path is not None
        with zipfile.ZipFile(zip_path) as zf:
            names = zf.namelist()
        for n in names:
            assert not n.startswith(("/", "\\")), f"absolute path in archive: {n}"
            assert ".." not in Path(n).parts, f"traversal in archive: {n}"
            assert ":\\\\" not in n and n[1:2] != ":", f"drive path in archive: {n}"
            assert "AppData" not in n and "LOCALAPPDATA" not in n


# =============================================================================
# GITSEC-001..003 - git history
# =============================================================================
class TestGitHistory:
    def test_gitsec_001_clean_tree_dirty_history(self, tmp_path, capsys):
        from check_git_history import check_history
        repo = make_mini_repo(tmp_path, "gitsec1")
        (repo / "leak.json").write_text(
            json.dumps({"apiKey": canary_api_key()}), encoding="utf-8")
        git(repo, "add", "-A")
        git(repo, "commit", "-m", "accidental leak")
        git(repo, "rm", "-q", "leak.json")
        git(repo, "commit", "-m", "remove leak")
        # current tree passes the filesystem gate...
        assert verify_agent_safe(repo) == []
        # ...but history is contaminated
        rc = check_history(repo_root=repo)
        out = capsys.readouterr().out
        assert rc == 1
        assert "SECRET MATERIAL EXISTS IN HISTORY" in out

    def test_gitsec_002_report_redaction(self, tmp_path, capsys):
        from check_git_history import check_history
        repo = make_mini_repo(tmp_path, "gitsec2")
        (repo / "leak.txt").write_text(f"token: {canary_access_token()}\n", encoding="utf-8")
        git(repo, "add", "-A")
        git(repo, "commit", "-m", "leak")
        git(repo, "rm", "-q", "leak.txt")
        git(repo, "commit", "-m", "clean")
        check_history(repo_root=repo)
        out = capsys.readouterr().out
        assert canary_access_token() not in out
        assert "leak.txt" in out and "reason" in out

    def test_gitsec_003_clean_history(self, tmp_path, capsys):
        from check_git_history import check_history
        repo = make_mini_repo(tmp_path, "gitsec3")
        rc = check_history(repo_root=repo)
        assert rc == 0
        assert "CLEAN HISTORY" in capsys.readouterr().out


# =============================================================================
# WT-001..006 - worktree isolation
# =============================================================================
class TestWorktrees:
    def _worktree(self, repo, name, tmp_path):
        import agent_worktree as aw
        old = os.environ.get("AGENT_WORKTREE_ROOT")
        os.environ["AGENT_WORKTREE_ROOT"] = str(tmp_path / "wts")
        try:
            return aw.create_worktree(name, repo_root=repo)
        finally:
            if old is None:
                os.environ.pop("AGENT_WORKTREE_ROOT", None)
            else:
                os.environ["AGENT_WORKTREE_ROOT"] = old

    def test_wt_001_create_independent(self, tmp_path):
        repo = make_mini_repo(tmp_path, "wt001")
        wt = self._worktree(repo, "glm", tmp_path)
        assert wt.is_dir() and str(wt) != str(repo)
        branch = git(wt, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        assert branch.startswith("agent/glm/")

    def test_wt_002_unsafe_main_blocks(self, tmp_path):
        repo = make_mini_repo(tmp_path, "wt002")
        (repo / ".gitignore").write_text("*.log\n", encoding="utf-8")
        (repo / "junk.log").write_text(f"key = {canary_sk()}\n", encoding="utf-8")
        import agent_worktree as aw
        os.environ["AGENT_WORKTREE_ROOT"] = str(tmp_path / "wts2")
        with pytest.raises(SystemExit):
            aw.create_worktree("blocked", repo_root=repo)

    def test_wt_003_three_parallel_agents(self, tmp_path):
        repo = make_mini_repo(tmp_path, "wt003")
        wts = [self._worktree(repo, n, tmp_path) for n in ("glm", "opus", "codex")]
        for n, wt in zip(("glm", "opus", "codex"), wts):
            (wt / "tests" / f"test_{n}.py").write_text(
                f"def test_{n}():\n    assert '{n}' == '{n}'\n", encoding="utf-8")
            assert git(wt, "add", "-A").returncode == 0
            assert git(wt, "commit", "-m", f"agent {n}").returncode == 0
        for n, wt in zip(("glm", "opus", "codex"), wts):
            others = [f"test_{o}.py" for o in ("glm", "opus", "codex") if o != n]
            for o in others:
                assert not (wt / "tests" / o).exists(), "uncommitted changes leaked across worktrees"

    def test_wt_004_same_file_conflict_surfaced(self, tmp_path):
        repo = make_mini_repo(tmp_path, "wt004")
        wt1 = self._worktree(repo, "glm", tmp_path)
        wt2 = self._worktree(repo, "opus", tmp_path)
        (wt1 / "app.py").write_text("VALUE_GLM = 1\n", encoding="utf-8")
        (wt2 / "app.py").write_text("VALUE_OPUS = 1\n", encoding="utf-8")
        for wt in (wt1, wt2):
            git(wt, "add", "-A")
            git(wt, "commit", "-m", "same line edit")
        b1 = git(wt1, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        b2 = git(wt2, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        assert git(repo, "merge", b1).returncode == 0
        merge2 = git(repo, "merge", b2)
        conflicted = merge2.returncode != 0 or "<<<<<<<" in (repo / "app.py").read_text(encoding="utf-8")
        assert conflicted, "conflicting edits must surface a conflict, never silently choose one"
        git(repo, "merge", "--abort")

    def test_wt_005_agent_adds_secret(self, tmp_path, capsys):
        repo = make_mini_repo(tmp_path, "wt005")
        wt = self._worktree(repo, "badagent", tmp_path)
        (wt / "app.py").write_text("VALUE = 7\n", encoding="utf-8")  # safe fix
        (wt / "creds.json").write_text(
            json.dumps({"apiKey": canary_api_key()}), encoding="utf-8")
        git(wt, "add", "-A")
        git(wt, "commit", "-m", "fix plus oops")
        import agent_worktree as aw
        rc = aw.review_worktree("badagent", repo_root=repo, run_tests=True)
        out = capsys.readouterr().out
        assert "SECRET CHECK: FAIL" in out
        assert "MERGE BLOCKED" in out
        assert rc == 1
        # main remains unchanged
        assert (repo / "app.py").read_text(encoding="utf-8") == "VALUE = 1\n"
        assert not (repo / "creds.json").exists()

    def test_wt_006_agent_adds_sqlite(self, tmp_path, capsys):
        repo = make_mini_repo(tmp_path, "wt006")
        wt = self._worktree(repo, "dbg", tmp_path)
        (wt / "debug.sqlite").write_bytes(b"\x00sqlite-debug")
        git(wt, "add", "-A")
        git(wt, "commit", "-m", "add debug db")
        import agent_worktree as aw
        rc = aw.review_worktree("dbg", repo_root=repo, run_tests=False)
        out = capsys.readouterr().out
        assert "MERGE BLOCKED" in out and rc == 1
        assert not (repo / "debug.sqlite").exists()


# =============================================================================
# DEPLOY-001..006 - deploy preserves private state
# =============================================================================
PRIVATE_FILES = {
    "credentials.dat": b"cred-bytes-9r",
    "provider_state.dat": b"provider-state-bytes",
    "data.sqlite": b"SQLite format 3\x00private",
    "oauth_state.dat": b"oauth-state-bytes",
    "settings.json": b'{"trusted_os_unlock": false}',
}


class TestDeploy:
    def _private_root(self, tmp_path, name):
        root = tmp_path / name
        root.mkdir()
        for fname, blob in PRIVATE_FILES.items():
            (root / fname).write_bytes(blob)
        return root

    def _feature(self, repo):
        git(repo, "checkout", "-b", "feature")
        (repo / "app.py").write_text("VALUE = 42\n", encoding="utf-8")
        git(repo, "add", "-A")
        git(repo, "commit", "-m", "reviewed change")
        git(repo, "checkout", "master")

    def test_deploy_001_private_state_hash_identical(self, tmp_path, monkeypatch):
        import deploy_local as dl
        repo = make_mini_repo(tmp_path, "dep1")
        self._feature(repo)
        priv = self._private_root(tmp_path, "priv1")
        before = hash_tree(priv)
        monkeypatch.setenv("WATCHEDIT_DATA_DIR", str(priv))
        monkeypatch.setattr(dl, "PRIVATE_RUNTIME_ROOT", priv)
        assert dl.deploy_local(source="feature", skip_tests=True, quiet=True, repo_root=repo) is True
        assert (repo / "app.py").read_text(encoding="utf-8") == "VALUE = 42\n"
        assert hash_tree(priv) == before, "ordinary code deployment must be unable to mutate private files"

    def test_deploy_002_midway_failure_rollback_exact(self, tmp_path, monkeypatch):
        import deploy_local as dl
        repo = make_mini_repo(tmp_path, "dep2")
        self._feature(repo)
        priv = self._private_root(tmp_path, "priv2")
        before_repo = hash_tree(repo)
        before_priv = hash_tree(priv)
        monkeypatch.setenv("WATCHEDIT_DATA_DIR", str(priv))
        monkeypatch.setattr(dl, "PRIVATE_RUNTIME_ROOT", priv)
        monkeypatch.setattr(dl, "_smoke_test", lambda *a, **k: (False, "injected midway failure"))
        ok = dl.deploy_local(source="feature", skip_tests=True, quiet=True, repo_root=repo)
        assert ok is False
        after_priv = hash_tree(priv)
        assert after_priv == before_priv
        # no mixed-version state: master content restored exactly
        assert (repo / "app.py").read_text(encoding="utf-8") == "VALUE = 1\n"
        assert git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() in ("master", "main")

    def test_deploy_003_failing_test_blocks(self, tmp_path, monkeypatch):
        import deploy_local as dl
        repo = make_mini_repo(tmp_path, "dep3")
        (repo / "tests" / "test_fail.py").write_text(
            "def test_fail():\n    assert False, 'staged failure'\n", encoding="utf-8")
        git(repo, "add", "-A")
        git(repo, "commit", "-m", "stage failing test")
        priv = self._private_root(tmp_path, "priv3")
        monkeypatch.setenv("WATCHEDIT_DATA_DIR", str(priv))
        monkeypatch.setattr(dl, "PRIVATE_RUNTIME_ROOT", priv)
        assert dl.deploy_local(skip_tests=False, quiet=True, repo_root=repo) is False
        assert (repo / "app.py").read_text(encoding="utf-8") == "VALUE = 1\n"
        assert git(repo, "tag", "-l", "predeploy/*").stdout.strip() == "", \
            "deployment must not start (no rollback point) when tests fail"

    def test_deploy_004_secret_scan_failure_blocks(self, tmp_path, monkeypatch):
        import deploy_local as dl
        repo = mini_canary_repo(tmp_path, "dep4", canary_api_key())
        priv = self._private_root(tmp_path, "priv4")
        monkeypatch.setenv("WATCHEDIT_DATA_DIR", str(priv))
        monkeypatch.setattr(dl, "PRIVATE_RUNTIME_ROOT", priv)
        assert dl.deploy_local(skip_tests=True, quiet=True, repo_root=repo) is False

    def test_deploy_005_dirty_main_refuses_and_preserves(self, tmp_path, monkeypatch):
        import deploy_local as dl
        repo = make_mini_repo(tmp_path, "dep5")
        (repo / "app.py").write_text("VALUE = 'uncommitted local work'\n", encoding="utf-8")
        priv = self._private_root(tmp_path, "priv5")
        monkeypatch.setenv("WATCHEDIT_DATA_DIR", str(priv))
        monkeypatch.setattr(dl, "PRIVATE_RUNTIME_ROOT", priv)
        assert dl.deploy_local(skip_tests=True, quiet=True, repo_root=repo) is False
        assert (repo / "app.py").read_text(encoding="utf-8") == "VALUE = 'uncommitted local work'\n", \
            "deployment must never discard uncommitted modifications"

    def test_deploy_006_mutation_failure_rolls_back(self, tmp_path, monkeypatch):
        import deploy_local as dl
        repo = make_mini_repo(tmp_path, "dep6")
        self._feature(repo)
        priv = self._private_root(tmp_path, "priv6")
        monkeypatch.setenv("WATCHEDIT_DATA_DIR", str(priv))
        monkeypatch.setattr(dl, "PRIVATE_RUNTIME_ROOT", priv)
        real_git = dl._git
        target_sha = real_git(["rev-parse", "feature^{commit}"], repo).stdout.strip()
        def flaky_checkout(args, repo_root=None):
            # W2-002: activation now checks out the resolved SHA (detached).
            if args and args[0] == "checkout" and target_sha and target_sha in args:
                class R:
                    returncode = 1
                    stderr = "error: unable to unlink 'app.py': file locked by running process"
                return R()
            return real_git(args, repo_root)
        monkeypatch.setattr(dl, "_git", flaky_checkout)
        ok = dl.deploy_local(source="feature", skip_tests=True, quiet=True, repo_root=repo)
        assert ok is False, "locked file must fail deployment predictably"
        assert (repo / "app.py").read_text(encoding="utf-8") == "VALUE = 1\n"
        assert git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() in ("master", "main")


# =============================================================================
# PATCH-001..006 - malicious patch content
# =============================================================================
def _diff(new_path: str, content: str = "x\n") -> str:
    return (
        f"diff --git a/app.py b/{new_path}\n"
        f"new file mode 100644\n"
        f"--- /dev/null\n"
        f"+++ b/{new_path}\n"
        f"@ -0,0 +1 @@\n"
        f"+{content}"
    )


class TestPatchGuard:
    def _apply(self, tmp_path, diff_text, name="patch.diff"):
        import apply_agent_patch as aap
        repo = make_mini_repo(tmp_path, name.replace(".diff", "").replace(".", ""))
        pfile = tmp_path / name
        pfile.write_text(diff_text, encoding="utf-8")
        return repo, pfile

    def test_patch_001_relative_escape(self, tmp_path):
        import apply_agent_patch as aap
        repo, pfile = self._apply(tmp_path, _diff("../../private/secrets.txt"))
        with pytest.raises(aap.PatchRejected, match="escapes repository root"):
            aap.apply_patch(pfile, repo_root=repo)

    def test_patch_002_absolute_windows_path(self, tmp_path):
        import apply_agent_patch as aap
        evil = os.environ.get("LOCALAPPDATA", r"C:\Users\x\AppData\Local") + \
            r"\9router_WatchEdit\secure\evil.txt"
        repo, pfile = self._apply(tmp_path, _diff(evil))
        with pytest.raises(aap.PatchRejected, match="absolute"):
            aap.apply_patch(pfile, repo_root=repo)

    def test_patch_003_unc_path(self, tmp_path):
        import apply_agent_patch as aap
        repo, pfile = self._apply(tmp_path, _diff(r"\\server\share\file.txt"))
        with pytest.raises(aap.PatchRejected):
            aap.apply_patch(pfile, repo_root=repo)

    def test_patch_004_drive_relative_and_odd_paths(self, tmp_path):
        import apply_agent_patch as aap
        for odd in ("C:evil.txt", "C:/evil.txt", "\\\\?\\C:\\tmp\\evil.txt"):
            repo, pfile = self._apply(tmp_path, _diff(odd), name=f"p4_{abs(hash(odd))}.diff")
            with pytest.raises(aap.PatchRejected):
                aap.apply_patch(pfile, repo_root=repo)

    @pytest.mark.parametrize("name", ["Data.SQLite", "DATA.SQLITE", "data.sqlite"])
    def test_patch_005_case_insensitive_protected(self, tmp_path, name):
        import apply_agent_patch as aap
        repo, pfile = self._apply(tmp_path, _diff(name), name="p5.diff")
        with pytest.raises(aap.PatchRejected, match="protected"):
            aap.apply_patch(pfile, repo_root=repo)

    def test_patch_006_rename_into_protected_path(self, tmp_path):
        import apply_agent_patch as aap
        repo = make_mini_repo(tmp_path, "p6")
        diff = (
            "diff --git a/safe.txt b/data.sqlite\n"
            "rename from safe.txt\n"
            "rename to data.sqlite\n"
        )
        pfile = tmp_path / "p6.diff"
        pfile.write_text(diff, encoding="utf-8")
        with pytest.raises(aap.PatchRejected, match="protected"):
            aap.apply_patch(pfile, repo_root=repo)

    def test_patch_normal_diff_applies(self, tmp_path):
        import apply_agent_patch as aap
        repo = make_mini_repo(tmp_path, "pok")
        diff = (
            "diff --git a/app.py b/app.py\n"
            "--- a/app.py\n"
            "+++ b/app.py\n"
            "@@ -1 +1 @@\n"
            "-VALUE = 1\n"
            "+VALUE = 99\n"
        )
        pfile = tmp_path / "ok.diff"
        pfile.write_text(diff, encoding="utf-8")
        aap.apply_patch(pfile, repo_root=repo)
        assert "VALUE = 99" in (repo / "app.py").read_text(encoding="utf-8")


# =============================================================================
# RACE-001..003 - TOCTOU
# =============================================================================
class TestRaces:
    def test_race_001_secret_inserted_after_initial_pass(self, tmp_path, monkeypatch):
        """Verify passes once, secret appears before mutation -> deploy fails."""
        import deploy_local as dl
        import verify_agent_safe as vas
        repo = make_mini_repo(tmp_path, "race1")
        real = vas.verify_agent_safe
        state = {"calls": 0}
        def racing_verify(root):
            state["calls"] += 1
            if state["calls"] == 1:
                return []  # initial pass
            return real(root)  # subsequent: truth
        monkeypatch.setattr(dl.vas, "verify_agent_safe", racing_verify)
        # secret is COMMITTED: the tree stays clean, so only the re-verification
        # immediately before mutation can catch the race
        (repo / "sneaky.log").write_text(f"tok: {canary_access_token()}\n", encoding="utf-8")
        git(repo, "add", "-A")
        git(repo, "commit", "-m", "sneak one in")
        ok = dl.deploy_local(skip_tests=True, quiet=True, repo_root=repo)
        assert ok is False, "a previous standalone PASS must not authorize changed state"
        assert state["calls"] >= 2, "deploy must re-verify close to mutation"

    def test_race_002_base_revision_changes(self, tmp_path):
        """Review passes; main later receives a canary; merge result re-fails the gate."""
        import agent_worktree as aw
        repo = make_mini_repo(tmp_path, "race2")
        os.environ["AGENT_WORKTREE_ROOT"] = str(tmp_path / "wts_r2")
        wt = aw.create_worktree("latebase", repo_root=repo)
        (wt / "app.py").write_text("VALUE = 5\n", encoding="utf-8")
        git(wt, "add", "-A")
        git(wt, "commit", "-m", "safe change")
        rc = aw.review_worktree("latebase", repo_root=repo, run_tests=False)
        assert rc == 0  # approved at this point in time
        # main changes underneath (someone commits a canary)
        (repo / "oops.json").write_text(
            json.dumps({"refreshToken": canary_refresh_token()}), encoding="utf-8")
        git(repo, "add", "-A")
        git(repo, "commit", "-m", "oops on main")
        # merging the agent branch must NOT launder the canary away
        branch = git(wt, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        assert git(repo, "merge", branch).returncode == 0
        findings = verify_agent_safe(repo)
        assert findings, "post-merge revalidation must catch main-side contamination"
        assert any("oops.json" in u.path for u in findings)

    def test_race_003_staged_file_mutated_after_hash(self, tmp_path, monkeypatch):
        import build_safe_share as bss
        real_zipfile = bss.zipfile
        staging_dir = tmp_path / "share_r3" / ".staging"

        class MutatingZip:
            mutated = False
            def __init__(self, *a, **k):
                self._zf = real_zipfile.ZipFile(*a, **k)
            def __enter__(self):
                self._zf.__enter__()
                return self
            def __exit__(self, *a):
                return self._zf.__exit__(*a)
            def write(self, p, arcname=None, **k):
                # mutate a DIFFERENT staged file after validation, before its write
                if not MutatingZip.mutated:
                    MutatingZip.mutated = True
                    staged = staging_dir / "tools" / "verify_agent_safe.py"
                    if staged.exists():
                        with open(staged, "a", encoding="utf-8") as fh:
                            fh.write(f"\ninjected {canary_api_key()}\n")
                return self._zf.write(p, str(arcname) if arcname else None, **k)

        monkeypatch.setattr(bss, "zipfile", __import__("types").SimpleNamespace(
            ZipFile=MutatingZip, ZIP_DEFLATED=real_zipfile.ZIP_DEFLATED))
        result = bss.build_safe_share(out_dir=tmp_path / "share_r3", skip_gitleaks=True, quiet=True)
        assert result is None, "staged-file mutation after validation must abort the export"


# =============================================================================
# Section 29 - fail-closed matrix
# =============================================================================
class TestFailClosed:
    def test_scanner_exception_fails_closed(self, tmp_path, monkeypatch):
        import verify_agent_safe as vas
        repo = make_mini_repo(tmp_path, "fc1")
        def boom(path, root=None):
            raise RuntimeError("simulated scanner crash")
        monkeypatch.setattr(vas, "scan_file", boom)
        findings = vas.verify_agent_safe(repo)
        assert any("scanner_error" in u.reason for u in findings)
        assert format_result(findings).startswith("AGENT SAFE: NO")

    def test_unreadable_file_fails_closed(self, tmp_path):
        repo = make_mini_repo(tmp_path, "fc2")
        secret = repo / "locked.secret"
        secret.write_text(f"tok: {canary_access_token()}\n", encoding="utf-8")
        os.chmod(secret, 0o000)
        try:
            findings = _v(repo)
        finally:
            os.chmod(secret, 0o644)
        assert findings, "cannot-read must equal NOT safe"
        assert any("unreadable" in u.reason or "locked.secret" in u.path for u in findings)

    def test_git_failure_blocks_deploy(self, tmp_path, monkeypatch):
        import deploy_local as dl
        repo = make_mini_repo(tmp_path, "fc3")
        class Fail:
            returncode = 128
            stdout = ""
            stderr = "fatal: not a git repository"
        monkeypatch.setattr(dl, "_git", lambda *a, **k: Fail())
        assert dl.deploy_local(skip_tests=True, quiet=True, repo_root=repo) is False

    def test_oversized_file_fails_closed(self, tmp_path):
        repo = make_mini_repo(tmp_path, "fc4")
        big = repo / "huge.log"
        big.write_bytes(b"A" * (8 * 1024 * 1024 + 1024))
        findings = _v(repo)
        assert any("oversized" in u.reason for u in findings), \
            "explicit policy required above size limits; never a silent skip"

    def test_unknown_protected_type(self, tmp_path):
        repo = make_mini_repo(tmp_path, "fc5")
        (repo / "keepass.kdbx").write_bytes(b"\x00kdbx")
        assert any("keepass.kdbx" in u.path for u in _v(repo))
