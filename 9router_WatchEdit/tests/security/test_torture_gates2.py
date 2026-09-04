"""
SECURITY TORTURE GATE part 2 — Gates 16-30 + final canary search.

Environment leaks, crash reports, filesystem permission attacks, private
storage unavailability, rollback scope, config injection, path normalization,
rename race, secret-free tests, false-success attacks, receipt truthfulness,
50x soak, parallel agent soak, master canary search.
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests.security.canaries import (
    all_canaries, canary_access_token, canary_api_key, canary_client_secret,
    canary_jwt, canary_refresh_token,
)
from tests.security.helpers import REPO_ROOT, TOOLS, git, hash_tree, make_mini_repo

sys.path.insert(0, str(TOOLS))
sys.path.insert(0, str(REPO_ROOT / "9router_WatchEdit"))

from verify_agent_safe import verify_agent_safe  # noqa: E402

CANARIES = all_canaries()
LONG_CANARY = "CANARY_REFRESH_TOKEN_" + "0123456789" + "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
SURFACES: list = []          # (path, may_contain_canaries: bool) registry for GATE 30


# =============================================================================
# GATE 16 — environment variable leak
# =============================================================================
class TestGate16EnvLeak:
    ENV_CANARIES = {
        "9ROUTER_API_KEY": "ENV" + canary_api_key(),
        "OPENAI_API_KEY": "ENV" + canary_client_secret(),
        "TEST_REFRESH_TOKEN": "ENV" + canary_refresh_token(),
    }

    def test_tools_never_dump_environment(self, tmp_path, monkeypatch):
        for k, v in self.ENV_CANARIES.items():
            monkeypatch.setenv(k, v)
        outputs = []

        from core.diagnostics import export_diagnostic_bundle
        out = export_diagnostic_bundle(recent_errors=["plain failure"], out_dir=tmp_path / "g16")
        outputs.append(out.read_text(encoding="utf-8"))

        import build_safe_share as bss
        zip_path = bss.build_safe_share(out_dir=tmp_path / "g16share", skip_gitleaks=True, quiet=True)
        assert zip_path is not None
        import zipfile
        with zipfile.ZipFile(zip_path) as zf:
            outputs.append(" ".join(zf.namelist()))
            for n in zf.namelist():
                outputs.append(zf.read(n).decode("utf-8", "replace"))

        findings_text = ""  # verify runs without printing env
        assert verify_agent_safe(REPO_ROOT) == []

        for v in self.ENV_CANARIES.values():
            for out_blob in outputs:
                assert v not in out_blob, "environment secret leaked into an output channel"

    def test_no_environ_dump_in_tools(self):
        """Static guard: no tool implements 'dump os.environ for debugging'."""
        pattern = re.compile(r"os\.environ\.(items|copy)\(\)")
        offenders = []
        for p in TOOLS.glob("*.py"):
            text = p.read_text(encoding="utf-8", errors="replace")
            if pattern.search(text):
                offenders.append(p.name)
        for p in (REPO_ROOT / "9router_WatchEdit" / "core").glob("*.py"):
            text = p.read_text(encoding="utf-8", errors="replace")
            if pattern.search(text):
                offenders.append(p.name)
        assert offenders == [], f"environment dump found in: {offenders}"


# =============================================================================
# GATE 17 — crash report
# =============================================================================
class TestGate17CrashReport:
    def test_crash_diagnostics_redacted(self):
        from core.redaction import redact_text
        import traceback
        try:
            try:
                raise ValueError(f"token in memory: {canary_access_token()}")
            except ValueError as inner:
                raise RuntimeError("probe crashed") from inner
        except RuntimeError as ex:
            report = redact_text("".join(traceback.format_exception(ex)))
        for canary in CANARIES:
            assert canary not in report
        assert "probe crashed" in report  # the failure itself remains diagnosable

    def test_exception_object_channels(self):
        from core.redaction import redact_exception, redact_mapping
        ex = RuntimeError(f"Authorization: Bearer {canary_access_token()}")
        assert canary_access_token() not in redact_exception(ex)
        session = {"vault_in_memory": {"token": canary_refresh_token()}}
        red = redact_mapping(session)
        assert canary_refresh_token() not in json.dumps(red)


# =============================================================================
# GATE 18 — filesystem permission attack
# =============================================================================
class TestGate18Permissions:
    def test_private_readonly_no_repo_fallback(self, tmp_path, monkeypatch):
        """Private root read-only: ops fail clearly, ZERO repo fallback writes."""
        if os.name != "nt":
            pytest.skip("Windows ACL/readonly semantics required")
        repo_state_before = hash_tree(REPO_ROOT / "9router_WatchEdit")
        monkeypatch.delenv("WATCHEDIT_LIVE_ACCESS", raising=False)
        from core.security import LOCKED, SecurityManager
        blocker = tmp_path / "blocker_file"
        blocker.write_text("file blocks any directory beneath it", encoding="utf-8")
        mgr = SecurityManager(data_dir=blocker / "secure_sub")  # NotADirectoryError path
        assert mgr.state == LOCKED
        assert mgr.storage_available is False
        with pytest.raises(Exception):
            mgr.unlock_os()  # cannot persist the grant under a blocked root
        assert mgr.is_live_allowed() is False
        # CRITICAL: no emergency private data written beside the executable/repo
        assert hash_tree(REPO_ROOT / "9router_WatchEdit") == repo_state_before

    def test_source_readonly_private_writable_boot(self, tmp_path):
        """Source read-only + private writable -> app boots (GATE 18 part 1)."""
        app_copy = tmp_path / "ro_app"
        app_copy.mkdir()
        for item in ("config.py", "run.py", "core", "ui"):
            src = REPO_ROOT / "9router_WatchEdit" / item
            if src.is_dir():
                shutil.copytree(src, app_copy / item, ignore=shutil.ignore_patterns("__pycache__"))
            else:
                shutil.copy2(src, app_copy / item)
        for p in app_copy.rglob("*"):
            os.chmod(p, 0o444)
        os.chmod(app_copy, 0o555)
        try:
            env = dict(os.environ)
            env.update(WATCHEDIT_DATA_DIR=str(tmp_path / "rw_private"),
                       QT_QPA_PLATFORM="offscreen", PYTHONDONTWRITEBYTECODE="1")
            res = subprocess.run([sys.executable, "-c", (
                "import sys; sys.path.insert(0, '.')\n"
                "from PySide6.QtWidgets import QApplication\n"
                "app = QApplication([])\n"
                "from ui.main_window import MainWindow\n"
                "w = MainWindow()\nprint('BOOT:' + w.security.state)\nw.close()\n")],
                cwd=str(app_copy), capture_output=True, text=True, env=env, timeout=180)
            assert "BOOT:LOCKED" in res.stdout, res.stderr[-300:]
        finally:
            for p in app_copy.rglob("*"):
                try:
                    os.chmod(p, 0o644)
                except OSError:
                    pass
            os.chmod(app_copy, 0o755)


# =============================================================================
# GATE 19 — private storage unavailable
# =============================================================================
class TestGate19PrivateUnavailable:
    def test_unwritable_private_root_degrades_to_locked(self, tmp_path):
        blocker = tmp_path / "blocker"
        blocker.write_text("this FILE blocks the directory path", encoding="utf-8")
        env = dict(os.environ)
        env["WATCHEDIT_DATA_DIR"] = str(blocker / "9router_WatchEdit")  # path under a FILE
        env.pop("WATCHEDIT_LIVE_ACCESS", None)
        env.update(QT_QPA_PLATFORM="offscreen", PYTHONDONTWRITEBYTECODE="1")
        res = subprocess.run([sys.executable, "-c", (
            "import sys; sys.path.insert(0, '9router_WatchEdit')\n"
            "import config\n"
            "print('AVAIL=' + str(config.PRIVATE_STORAGE_AVAILABLE))\n"
            "from core.security import SecurityManager\n"
            "m = SecurityManager()\n"
            "print('STATE=' + m.state)\n")],
            cwd=str(REPO_ROOT), capture_output=True, text=True, env=env, timeout=120)
        out = res.stdout
        assert "AVAIL=False" in out, res.stderr[-300:]
        assert "STATE=LOCKED" in out
        # no emergency fallback artifacts inside the repository
        for banned in ("runtime", "backup", "config.json", ".env"):
            assert not (REPO_ROOT / banned).exists()
        assert not (REPO_ROOT / "9router_WatchEdit" / "runtime").exists()


# =============================================================================
# GATE 20 — rollback does not roll back private state
# =============================================================================
class TestGate20RollbackScope:
    def test_source_rollback_leaves_private_activity_intact(self, tmp_path, monkeypatch):
        import deploy_local as dl
        repo = make_mini_repo(tmp_path, "g20")
        priv = tmp_path / "priv_g20"
        priv.mkdir()
        (priv / "live_state.dat").write_bytes(b"v1")
        monkeypatch.setenv("WATCHEDIT_DATA_DIR", str(priv))
        monkeypatch.setattr(dl, "PRIVATE_RUNTIME_ROOT", priv)

        assert dl.deploy_local(skip_tests=True, quiet=True, repo_root=repo) is True

        # legitimate private runtime activity DURING trusted runtime
        (priv / "live_state.dat").write_bytes(b"v2-mutated-by-runtime")

        # failing deploy -> source rollback
        monkeypatch.setattr(dl, "_smoke_test", lambda *a, **k: (False, "injected"))
        assert dl.deploy_local(skip_tests=True, quiet=True, repo_root=repo) is False

        # source rolled back (unchanged), private activity NOT restored to v1
        assert (repo / "app.py").read_text(encoding="utf-8") == "VALUE = 1\n"
        assert (priv / "live_state.dat").read_bytes() == b"v2-mutated-by-runtime", \
            "SOURCE ROLLBACK must never double as a private-data restore"


# =============================================================================
# GATE 21 — migration safety (justified N/A with evidence)
# =============================================================================
class TestGate21Migration:
    def test_no_casual_schema_migration_paths_exist(self):
        """This application owns no schema-bearing runtime database: its private
        data is JSON caches under %LOCALAPPDATA%; the 9Router engine DB is an
        EXTERNAL system whose upgrades go through apply-update.ps1, which takes
        its own APPDATA backup BEFORE touching anything. Evidence: no tool in
        this repository executes schema migrations against private databases."""
        offenders = []
        pattern = re.compile(r'(?i)(ALTER\s+TABLE|CREATE\s+TABLE|PRAGMA\s+user_version|'
                             r'schema[_-]?migrat)')
        for p in TOOLS.glob("*.py"):
            text = p.read_text(encoding="utf-8", errors="replace")
            if pattern.search(text):
                offenders.append(p.name)
        for p in (REPO_ROOT / "9router_WatchEdit" / "core").glob("*.py"):
            text = p.read_text(encoding="utf-8", errors="replace")
            if pattern.search(text):
                offenders.append(p.name)
        assert offenders == [], \
            f"unexpected migration capability (needs gated procedure): {offenders}"
        # private backups remain the explicit, separate disaster-recovery path
        assert (TOOLS / "create_private_backup.py").exists()


# =============================================================================
# GATE 22 — configuration injection via agent diff
# =============================================================================
class TestGate22ConfigInjection:
    def test_repo_relative_private_root_blocked_at_review(self, tmp_path, capsys):
        import agent_worktree as aw
        repo = make_mini_repo(tmp_path, "g22")
        os.environ["AGENT_WORKTREE_ROOT"] = str(tmp_path / "g22wts")
        wt = aw.create_worktree("cfginject", repo_root=repo)
        cfg = wt / "9router_WatchEdit" / "config.py"
        cfg.parent.mkdir(exist_ok=True)
        cfg.write_text(
            "LOCALAPPDATA_DIR = Path('private')  # redirect into repo\n", encoding="utf-8")
        (wt / "config.example.json").write_text(
            '{"private_root": "./private"}', encoding="utf-8")
        git(wt, "add", "-A")
        git(wt, "commit", "-m", "harmless config tweak")
        rc = aw.review_worktree("cfginject", repo_root=repo, run_tests=False)
        out = capsys.readouterr().out
        assert rc == 1
        assert "config-injection gate: FAIL" in out
        assert "MERGE BLOCKED" in out
        assert not (repo / "9router_WatchEdit" / "config.py").exists(), \
            "main must not receive the injected config"


# =============================================================================
# GATE 23 — path case / normalization
# =============================================================================
class TestGate23PathNormalization:
    def test_mixed_case_root_authorization(self, tmp_path):
        import apply_agent_patch as aap
        repo = make_mini_repo(tmp_path, "g23")
        diff = ("diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n"
                "@@ -1 +1 @@\n-VALUE = 1\n+VALUE = 7\n")
        pfile = tmp_path / "ok23.diff"
        pfile.write_text(diff, encoding="utf-8")
        # authorize through a DIFFERENT-CASE spelling of the same root
        mixed = Path(str(repo).swapcase()) if os.name == "nt" else repo
        if os.name == "nt":
            # Windows resolve() canonicalizes to on-disk case
            aap.apply_patch(pfile, repo_root=mixed)
            assert "VALUE = 7" in (repo / "app.py").read_text(encoding="utf-8")

    def test_case_and_separator_escapes_blocked(self, tmp_path):
        import apply_agent_patch as aap
        repo = make_mini_repo(tmp_path, "g23b")
        for evil in ("../" + "escape.txt", " ..\\\\escape.txt", "a/../../esc.txt"):
            diff = (f"diff --git a/x b/{evil}\nnew file mode 100644\n"
                    f"--- /dev/null\n+++ b/{evil}\n@@ -0,0 +1 @@\n+x\n")
            pfile = tmp_path / f"evil_{abs(hash(evil))}.diff"
            pfile.write_text(diff, encoding="utf-8")
            with pytest.raises(aap.PatchRejected):
                aap.apply_patch(pfile, repo_root=repo)

    def test_verify_case_insensitive_protection(self, tmp_path):
        repo = make_mini_repo(tmp_path, "g23c")
        (repo / "Machine-Id".swapcase()).write_text("id" * 16, encoding="utf-8")
        assert _any_path(find := verify_agent_safe(repo), "machine-id")
        (repo / "machine-ID").unlink()
        (repo / "JWT-Secret".swapcase()).write_text("j" * 32, encoding="utf-8")
        assert _any_path(verify_agent_safe(repo), "jwt-secret")


def _any_path(findings, fragment: str) -> bool:
    return any(fragment.lower() in u.path.lower() for u in findings)


# =============================================================================
# GATE 24 — secret file rename race (immutable staging / final revalidation)
# =============================================================================
class TestGate24RenameRace:
    def test_post_validation_replacement_blocked(self, tmp_path, monkeypatch):
        import build_safe_share as bss
        real_zipfile = bss.zipfile
        staging_dir_holder = {}

        class SwapZip:
            swapped = False
            def __init__(self, *a, **k):
                self._zf = real_zipfile.ZipFile(*a, **k)
            def __enter__(self):
                self._zf.__enter__()
                return self
            def __exit__(self, *a):
                return self._zf.__exit__(*a)
            def write(self, p, arcname=None, **k):
                if not SwapZip.swapped:
                    SwapZip.swapped = True
                    target = staging_dir_holder.get("staging")
                    if target:
                        victim = target / "tools" / "secret_scan.py"
                        if victim.exists():
                            # replace validated content with a secret-bearing file
                            victim.write_text(
                                victim.read_text(encoding="utf-8")
                                + f"\nkey = '{canary_api_key()}'\n", encoding="utf-8")
                return self._zf.write(p, str(arcname) if arcname else None, **k)

        orig_collect = bss.collect_candidates
        def tracking_collect():
            cands = orig_collect()
            staging_dir_holder["staging"] = None  # set later
            return cands
        monkeypatch.setattr(bss, "collect_candidates", tracking_collect)
        # patch staging creation to record it
        orig_rmtree = shutil.rmtree
        def record_staging(path, *a, **k):
            return orig_rmtree(path, *a, **k)
        real_build = bss.build_safe_share
        out_dir = tmp_path / "g24share"

        # simplest: run once with the mutating zip and a hook that captures staging
        import types
        monkeypatch.setattr(bss, "zipfile", types.SimpleNamespace(
            ZipFile=SwapZip, ZIP_DEFLATED=real_zipfile.ZIP_DEFLATED))
        # capture staging path by wrapping shutil.copy2 used in staging loop
        real_copy2 = shutil.copy2
        def copy2_tracker(src, dst, *a, **k):
            staging_dir_holder["staging"] = Path(dst).parents[1] \
                if Path(dst).parents[0].name == "tools" else staging_dir_holder.get("staging")
            return real_copy2(src, dst, *a, **k)
        import build_safe_share as bss2
        monkeypatch.setattr(bss2.shutil, "copy2", copy2_tracker)
        result = real_build(out_dir=out_dir, skip_gitleaks=True, quiet=True)
        assert result is None, "post-validation file replacement must abort the export"


# =============================================================================
# GATE 25 — tests themselves must be secret-free
# =============================================================================
class TestGate25SecretFreeTests:
    def test_tests_directory_scans_clean(self):
        from core.secret_scanner import scan_tree
        findings = scan_tree(REPO_ROOT / "9router_WatchEdit" / "tests")
        assert findings == [], f"test artifacts contain secret-shaped material: {findings}"

    def test_no_persistent_artifacts_in_repo_after_campaign(self):
        for banned in (".pytest_cache",):
            # .pytest_cache is regenerated; ensure it contains no canaries
            cache = REPO_ROOT / banned
            if cache.exists():
                for p in cache.rglob("*"):
                    if p.is_file():
                        blob = p.read_text(encoding="utf-8", errors="replace")
                        for canary in CANARIES:
                            assert canary not in blob


# =============================================================================
# GATE 26 — false success attack (exception != clean)
# =============================================================================
class TestGate26FalseSuccess:
    def test_verify_exception_fails_closed(self, tmp_path, monkeypatch):
        import verify_agent_safe as vas
        repo = make_mini_repo(tmp_path, "g26")
        def boom(root):
            raise RuntimeError("validator crashed")
        monkeypatch.setattr(vas, "scan_file", boom)
        findings = vas.verify_agent_safe(repo)
        assert findings and any("scanner_error" in u.reason for u in findings)

    def test_deploy_validator_exception_never_success(self, tmp_path, monkeypatch):
        import deploy_local as dl
        repo = make_mini_repo(tmp_path, "g26b")
        def boom(root):
            raise RuntimeError("validator crashed")
        monkeypatch.setattr(dl.vas, "verify_agent_safe", boom)
        assert dl.deploy_local(skip_tests=True, quiet=True, repo_root=repo) is False
        assert dl.last_status() == "FAILED_NO_MUTATION"

    def test_premerge_gate_exception_fails_closed(self, monkeypatch):
        import run_premerge_gate as gate
        import verify_agent_safe as vas
        def boom(root):
            raise RuntimeError("validator crashed")
        monkeypatch.setattr(vas, "verify_agent_safe", boom)
        rc = gate.main(["--skip-tests"])
        assert rc == 1, "premerge gate must FAIL when a validator raises"

    def test_diagnostics_scanner_exception_aborts(self, tmp_path, monkeypatch):
        from core import diagnostics
        from core import secret_scanner
        def boom(*a, **k):
            raise RuntimeError("scanner crashed")
        monkeypatch.setattr(diagnostics, "scan_tree", boom)
        with pytest.raises(RuntimeError):
            diagnostics.export_diagnostic_bundle(out_dir=tmp_path / "g26c")
        leftovers = [p for p in (tmp_path / "g26c").glob("*") if not p.name.startswith(".")]
        assert leftovers == []


# =============================================================================
# GATE 27 — receipt truthfulness
# =============================================================================
class TestGate27Receipts:
    def test_receipts_match_terminal_states(self, tmp_path, monkeypatch, capsys):
        import deploy_local as dl
        repo = make_mini_repo(tmp_path, "g27")
        priv = tmp_path / "priv_g27"
        priv.mkdir()
        (priv / "s.dat").write_bytes(b"x")
        monkeypatch.setenv("WATCHEDIT_DATA_DIR", str(priv))
        monkeypatch.setattr(dl, "PRIVATE_RUNTIME_ROOT", priv)

        capsys.readouterr()
        dl.deploy_local(skip_tests=True, quiet=False, repo_root=repo)
        success_receipt = capsys.readouterr().out
        assert "result: SUCCESS" in success_receipt

        monkeypatch.setattr(dl, "_smoke_test", lambda *a, **k: (False, "health"))
        dl.deploy_local(skip_tests=True, quiet=False, repo_root=repo)
        rollback_receipt = capsys.readouterr().out
        assert "result: FAILED_ROLLED_BACK" in rollback_receipt
        assert "SUCCESS" not in rollback_receipt.replace("FAILED_ROLLED_BACK", "")

        (repo / "dirty.txt").write_text("x", encoding="utf-8")
        dl.deploy_local(skip_tests=True, quiet=False, repo_root=repo)
        no_mutation_receipt = capsys.readouterr().out
        assert "result: FAILED_NO_MUTATION" in no_mutation_receipt
        (repo / "dirty.txt").unlink()

        # every receipt scans clean
        receipts = tmp_path / "g27_receipts"
        receipts.mkdir()
        (receipts / "success.txt").write_text(success_receipt, encoding="utf-8")
        (receipts / "rollback.txt").write_text(rollback_receipt, encoding="utf-8")
        (receipts / "no_mutation.txt").write_text(no_mutation_receipt, encoding="utf-8")
        from core.secret_scanner import scan_tree
        assert scan_tree(receipts) == []


# =============================================================================
# GATE 28 — repeatability soak (50 iterations)
# =============================================================================
class TestGate28Soak:
    def test_fifty_iteration_workflow_soak(self, tmp_path, monkeypatch):
        import agent_worktree as aw
        repo = make_mini_repo(tmp_path, "g28")
        monkeypatch.setenv("AGENT_WORKTREE_ROOT", str(tmp_path / "g28wts"))
        SURFACES.append((tmp_path, True))
        rng = random.Random(20260904)
        approved = 0

        for i in range(50):
            wt = aw.create_worktree(f"soak{i}", repo_root=repo)
            (wt / "app.py").write_text(f"VALUE = {i}\n", encoding="utf-8")
            if rng.random() < 0.2:
                # non-destructive injected failure: rejected secret change
                (wt / f"bad{i}.log").write_text(f"t: {canary_access_token()}\n", encoding="utf-8")
                git(wt, "add", "-A")
                git(wt, "commit", "-m", f"iter {i}")
                rc = aw.review_worktree(f"soak{i}", repo_root=repo, run_tests=False)
                assert rc == 1, "secret iterations must always be rejected"
                (wt / f"bad{i}.log").unlink()
                git(wt, "add", "-A")
                git(wt, "commit", "-m", "fix")
                assert aw.review_worktree(f"soak{i}", repo_root=repo, run_tests=False) == 0
            else:
                git(wt, "add", "-A")
                git(wt, "commit", "-m", f"iter {i}")
                assert aw.review_worktree(f"soak{i}", repo_root=repo, run_tests=False) == 0
            branch = git(wt, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
            assert git(repo, "merge", branch).returncode == 0
            aw.remove_worktree(f"soak{i}", repo_root=repo)
            approved += 1

        # end-of-soak cleanliness
        assert approved == 50
        wts_left = git(repo, "worktree", "list", "--porcelain").stdout
        assert wts_left.count("worktree ") == 1, "leaked worktrees"
        assert verify_agent_safe(repo) == []
        assert git(repo, "status", "--porcelain").stdout.strip() == ""
        assert git(repo, "fsck", "--no-progress").returncode == 0


# =============================================================================
# GATE 29 — parallel agent soak (3 agents x 5 rounds)
# =============================================================================
class TestGate29ParallelSoak:
    def test_three_agents_mixed_outcomes(self, tmp_path, monkeypatch):
        import agent_worktree as aw
        repo = make_mini_repo(tmp_path, "g29")
        monkeypatch.setenv("AGENT_WORKTREE_ROOT", str(tmp_path / "g29wts"))
        SURFACES.append((tmp_path, True))
        names = ("glm", "opus", "codex")
        wts = {n: aw.create_worktree(f"{n}_r0", repo_root=repo) for n in names}
        scenario = {
            ("glm", 0): "independent", ("opus", 0): "independent", ("codex", 0): "independent",
            ("glm", 1): "secret", ("opus", 1): "testfail", ("codex", 1): "independent",
            ("glm", 2): "independent", ("opus", 2): "independent", ("codex", 2): "conflict",
            ("glm", 3): "independent", ("opus", 3): "secret", ("codex", 3): "independent",
            ("glm", 4): "independent", ("opus", 4): "independent", ("codex", 4): "independent",
        }
        merged_markers = []
        for rnd in range(5):
            for n in names:
                wt = wts[n]
                kind = scenario[(n, rnd)]
                (wt / f"{n}_r{rnd}.py").write_text(f"# {n} round {rnd}\n", encoding="utf-8")
                if kind == "secret":
                    (wt / f"{n}_bad_r{rnd}.log").write_text(
                        f"k: {canary_api_key()}\n", encoding="utf-8")
                if kind == "testfail":
                    (wt / "tests" / f"test_{n}_fail.py").write_text(
                        "def test_x():\n    assert False\n", encoding="utf-8")
                if kind == "conflict":
                    (wt / "app.py").write_text("VALUE = 'codex-edit'\n", encoding="utf-8")
                    (repo / "app.py").write_text("VALUE = 'main-moved-on'\n", encoding="utf-8")
                    git(repo, "add", "-A")
                    git(repo, "commit", "-m", "main moves on")
                shutil.rmtree(wt / "tests" / "__pycache__", ignore_errors=True)
                git(wt, "add", "-A")
                git(wt, "commit", "-m", f"{n} r{rnd} {kind}")
                rc = aw.review_worktree(f"{n}_r0", repo_root=repo, run_tests=(kind != "independent"))
                if kind in ("secret", "testfail"):
                    assert rc == 1, f"{kind} must be rejected"
                if kind == "secret":
                    (wt / f"{n}_bad_r{rnd}.log").unlink()
                    git(wt, "add", "-A")
                    git(wt, "commit", "-m", "remove secret")
                if kind == "testfail":
                    (wt / "tests" / f"test_{n}_fail.py").unlink()
                    git(wt, "add", "-A")
                    git(wt, "commit", "-m", "fix tests")
                    assert aw.review_worktree(f"{n}_r0", repo_root=repo, run_tests=True) == 0
                if kind == "independent":
                    assert rc == 0
                    branch = git(wt, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
                    assert git(repo, "merge", branch).returncode == 0
                    merged_markers.append(f"{n}_r{rnd}.py")
            # codex conflict round: attempt merge, expect conflict or already-diverged block
            if any(scenario[(n, rnd)] == "conflict" for n in names):
                branch = git(wts["codex"], "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
                m = git(repo, "merge", branch)
                if m.returncode == 0:
                    content = (repo / "app.py").read_text(encoding="utf-8")
                    assert "<<<<<<<" not in content  # git resolved syntactically identical edit
                else:
                    git(repo, "merge", "--abort")
                    # surface handled; realign the branch so later rounds merge cleanly
                    (wts["codex"] / "app.py").write_text(
                        "VALUE = 'main-moved-on'" + chr(10), encoding="utf-8")
                    git(wts["codex"], "add", "-A")
                    git(wts["codex"], "commit", "-m", "realign after conflict")

        # only approved commits reached main
        assert verify_agent_safe(repo) == []
        for marker in merged_markers:
            assert (repo / marker).exists()
        for n in names:
            for other in names:
                if other != n:
                    bad = list(repo.glob(f"{other}_bad_*.log"))
                    assert not bad, "rejected branch artifacts leaked into main"


# =============================================================================
# GATE 30 — master canary search (runs LAST; name ordering enforced)
# =============================================================================
class TestGate30MasterCanarySearch:
    def test_zz_master_canary_search(self, tmp_path):
        """Final sweep across every generated surface. Approved matches are
        allowed ONLY inside intentionally isolated synthetic private storage
        (registered SURFACES with may_contain_canaries=True)."""
        search_targets = [(REPO_ROOT, False)]
        # all real agent worktree roots (should be none persistent)
        wts_root = REPO_ROOT.parent / "agent_worktrees"
        if wts_root.exists():
            search_targets.append((wts_root, False))
        # diagnostics / share output under the real private root
        priv_root = Path(os.environ.get("LOCALAPPDATA", "")) / "9router_WatchEdit"
        for sub in ("runtime", "share"):
            target = priv_root / sub
            if target.exists():
                search_targets.append((target, False))
        # registered session surfaces: repo-like paths must be clean
        for surface, may_contain in SURFACES:
            if not may_contain:
                search_targets.append((Path(surface), False))

        canaries = CANARIES + [LONG_CANARY, "TEST_ONLY_9ROUTER_CANARY_TOKEN"]
        # Approved synthetic-canary surfaces: the security test suite itself
        # (canary DEFINITIONS + torture fixtures + their compiled caches).
        # Those sources are independently gated by the repo scanner, which
        # requires them to contain fragments only (KNOWN_TEST_FAKES allowlist).
        def _approved(p: Path) -> bool:
            return "tests" in p.parts and "security" in p.parts
        hits = []
        for root, _ in search_targets:
            root = Path(root)
            if not root.exists():
                continue
            for p in root.rglob("*"):
                if not p.is_file() or ".git" in p.parts or _approved(p):
                    continue
                try:
                    blob = p.read_bytes()
                except OSError:
                    continue
                for canary in canaries:
                    if canary.encode() in blob:
                        hits.append((str(p), canary[:20]))
        assert hits == [], f"MASTER CANARY LEAK: {hits}"
