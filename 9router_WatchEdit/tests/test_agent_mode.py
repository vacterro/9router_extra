"""
9router_WatchEdit - Direct-Repo External Agent Mode acceptance tests.

Covers task sections 23 A-K of the DIRECT-REPO EXTERNAL AGENT MODE wave:
  A. VERIFY_AGENT_SAFE reports YES on the entire repository tree
  B. repository unit tests run without any private local state
  D. fake credential in an ignored file -> AGENT SAFE: NO
  E. fake SQLite runtime DB in the repository -> AGENT SAFE: NO
  F. agent worktree commit can be reviewed and merged normally
  G. DEPLOY_LOCAL updates code without modifying private runtime state
  I. no repository-relative private-state fallback exists in source
  K. three parallel agent worktrees operate without touching each other
(C, H, J are covered by test_secret_isolation / integration suites.)
"""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
TOOLS = REPO_ROOT / "tools"
sys.path.insert(0, str(TOOLS))
sys.path.insert(0, str(REPO_ROOT / "9router_WatchEdit"))

from verify_agent_safe import format_result, verify_agent_safe  # noqa: E402


# =============================================================================
# A: authoritative gate says YES on the real repository tree
# =============================================================================
class TestVerifyAgentSafe:
    def test_agent_safe_yes_on_repository(self):
        findings = verify_agent_safe(REPO_ROOT)
        assert findings == [], format_result(findings)

    def test_output_contract(self):
        assert format_result([]) == "AGENT SAFE: YES"
        result = format_result(verify_agent_safe(REPO_ROOT))
        assert result.startswith("AGENT SAFE: YES")

    def test_d_fake_credential_in_ignored_file_blocks(self, tmp_path):
        """23(D): credential in a .gitignored file still fails the gate."""
        repo = tmp_path / "minirepo"
        repo.mkdir()
        (repo / ".gitignore").write_text("*.log\n", encoding="utf-8")
        (repo / "app.py").write_text("print('hi')\n", encoding="utf-8")
        ignored = repo / "notes.log"  # ignored by git, but INSIDE the tree
        from tests.security.canaries import canary_sk
        ignored.write_text(f'router_key = "{canary_sk()}"\n', encoding="utf-8")

        findings = verify_agent_safe(repo)
        assert findings, "ignored file with credential must fail the gate"
        assert any("notes.log" in u.path for u in findings)
        out = format_result(findings)
        assert out.startswith("AGENT SAFE: NO")
        assert "notes.log" in out
        # never the value
        assert "sk-TESTabcdef" not in out

    def test_e_fake_sqlite_blocks(self, tmp_path):
        """23(E): fake runtime SQLite DB in the repository fails the gate."""
        repo = tmp_path / "minirepo2"
        repo.mkdir()
        (repo / "app.py").write_text("print('hi')\n", encoding="utf-8")
        # at repository root: blocked by protected file type
        (repo / "data.sqlite").write_bytes(b"SQLite format 3\x00" + b"\x00" * 64)
        findings = verify_agent_safe(repo)
        assert any("protected file type" in u.reason and "data.sqlite" in u.path for u in findings)

    def test_e_sqlite_in_private_dir_blocks(self, tmp_path):
        """SEC-011/012: a private runtime directory is flagged AND not descended."""
        repo = tmp_path / "minirepo2b"
        repo.mkdir()
        (repo / "app.py").write_text("print('hi')\n", encoding="utf-8")
        (repo / "runtime").mkdir()
        (repo / "runtime" / "data.sqlite").write_bytes(b"\x00" * 64)
        (repo / "runtime" / "data.sqlite-wal").write_bytes(b"\x00" * 8)
        (repo / "runtime" / "data.sqlite-shm").write_bytes(b"\x00" * 8)
        findings = verify_agent_safe(repo)
        reasons = [u.reason for u in findings]
        assert any("private runtime directory" in r for r in reasons)
        assert any("runtime" in u.path for u in findings)
        # WAL / SHM independently blocked when placed at repo root
        repo2 = tmp_path / "minirepo2c"
        repo2.mkdir()
        (repo2 / "app.py").write_text("x\n", encoding="utf-8")
        for suffix in ("data.sqlite-wal", "data.sqlite-shm"):
            (repo2 / suffix).write_bytes(b"\x00" * 8)
        findings2 = verify_agent_safe(repo2)
        assert {u.path for u in findings2} >= {"data.sqlite-wal", "data.sqlite-shm"}

    def test_env_and_machine_identity_block(self, tmp_path):
        repo = tmp_path / "minirepo3"
        repo.mkdir()
        (repo / ".env").write_text("A=1\n", encoding="utf-8")
        (repo / "machine-id").write_text("x" * 32, encoding="utf-8")
        (repo / "jwt-secret").write_text("y" * 32, encoding="utf-8")
        findings = verify_agent_safe(repo)
        flagged = {u.path for u in findings}
        assert {".env", "machine-id", "jwt-secret"} <= flagged

    def test_archive_files_block(self, tmp_path):
        """Unscannable archives can never sit in the agent tree."""
        repo = tmp_path / "minirepo4"
        repo.mkdir()
        (repo / "engine.tgz").write_bytes(b"\x1f\x8b" + b"\x00" * 32)
        findings = verify_agent_safe(repo)
        assert any("engine.tgz" in u.path for u in findings)


# =============================================================================
# B: no private local state required to run repository unit tests
# =============================================================================
class TestNoPrivateStateRequired:
    def test_unit_subset_with_empty_private_root(self, tmp_path):
        """23(B): fresh empty private data dir + offline -> tests still run.

        NOTE: the inner suite must not reference this class (no recursion)."""
        env = dict(os.environ)
        env["WATCHEDIT_DATA_DIR"] = str(tmp_path / "fresh_private_root")
        env["QT_QPA_PLATFORM"] = "offscreen"
        res = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "--no-header",
             "9router_WatchEdit/tests/test_classification.py",
             "9router_WatchEdit/tests/test_history.py",
             "9router_WatchEdit/tests/test_discovery.py"],
            cwd=str(REPO_ROOT), capture_output=True, text=True, env=env, timeout=300,
        )
        assert res.returncode == 0, (res.stdout or res.stderr)[-800:]


# =============================================================================
# I: no repository-relative private-state fallback
# =============================================================================
class TestNoRepoRelativePrivateFallback:
    def test_source_never_references_repo_private_dirs(self):
        """23(I): source must discover private state via env/LOCALAPPDATA only."""
        forbidden = re.compile(
            r'''(?ix)(Path\(\s*["'](?:\./)?(?:backup|secrets|private|runtime)(?:/|["'])|
                             ["']\./(?:backup|secrets|private|runtime)/)'''
        )
        offenders = []
        for p in (REPO_ROOT / "9router_WatchEdit").rglob("*.py"):
            if "__pycache__" in p.parts or "tests" in p.parts or not p.is_file():
                continue  # production sources only; test fixtures may quote hostile patterns
            text = p.read_text(encoding="utf-8", errors="replace")
            for m in forbidden.finditer(text):
                offenders.append(f"{p.relative_to(REPO_ROOT)}: ...{text[max(0, m.start()-30):m.end()+30]}...")
        assert offenders == [], f"repo-relative private fallback found: {offenders}"

    def test_config_derives_private_paths_from_env_or_localappdata(self):
        import config
        # honour env override and never point inside the repository
        assert str(REPO_ROOT) not in str(config.SECURE_DIR)
        assert str(REPO_ROOT) not in str(config.PRIVATE_BACKUP_DIR)
        assert config.SECURE_DIR.is_dir()  # created outside the repo


# =============================================================================
# F + K: agent worktree create / review / merge / parallel isolation
# =============================================================================
def _make_tmp_git_repo(tmp_path: Path) -> Path:
    """Miniature git repo with the same helper scripts + scanner core."""
    import shutil
    repo = tmp_path / "gitrepo"
    repo.mkdir()
    (repo / "app.py").write_text("VALUE = 1\n", encoding="utf-8")

    # tools/ + the scanner module the tools import
    (repo / "tools").mkdir()
    for name in ("verify_agent_safe.py", "agent_worktree.py", "pre_commit_secret_check.py"):
        shutil.copy2(TOOLS / name, repo / "tools" / name)
    core = repo / "9router_WatchEdit" / "core"
    core.mkdir(parents=True)
    (repo / "9router_WatchEdit" / "__init__.py").write_text("", encoding="utf-8")
    (core / "__init__.py").write_text("", encoding="utf-8")
    shutil.copy2(REPO_ROOT / "9router_WatchEdit" / "core" / "secret_scanner.py", core / "secret_scanner.py")

    g = lambda *a: subprocess.run(["git", "-C", str(repo), *a], capture_output=True, text=True)
    g("init")
    g("config", "user.name", "t")
    g("config", "user.email", "t@local")
    g("add", "-A")
    g("commit", "-m", "base")
    return repo


class TestAgentWorktrees:
    def test_f_create_review_merge(self, tmp_path, monkeypatch):
        """23(F): agent commits review cleanly and merge normally."""
        import agent_worktree as aw
        repo = _make_tmp_git_repo(tmp_path)
        monkeypatch.setenv("AGENT_WORKTREE_ROOT", str(tmp_path / "wts"))
        wt = aw.create_worktree("glm", repo_root=repo)
        assert wt.is_dir()
        assert str(REPO_ROOT) not in str(wt)  # outside main tree

        # agent makes a normal commit on its branch
        (wt / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
        g = lambda *a: subprocess.run(["git", "-C", str(wt), *a], capture_output=True, text=True)
        assert g("add", "-A").returncode == 0
        assert g("commit", "-m", "Fix bounded provider scanner").returncode == 0

        rc = aw.review_worktree("glm", repo_root=repo, run_tests=False)
        assert rc == 0

        # trusted user merges normally
        m = subprocess.run(["git", "-C", str(repo), "merge",
                            subprocess.run(["git", "-C", str(wt), "rev-parse", "--abbrev-ref", "HEAD"],
                                           capture_output=True, text=True).stdout.strip()],
                           capture_output=True, text=True)
        assert m.returncode == 0
        assert (repo / "app.py").read_text(encoding="utf-8") == "VALUE = 2\n"

    def test_k_three_parallel_worktrees_isolated(self, tmp_path, monkeypatch):
        """23(K): three agents work simultaneously without touching each other."""
        import agent_worktree as aw
        repo = _make_tmp_git_repo(tmp_path)
        monkeypatch.setenv("AGENT_WORKTREE_ROOT", str(tmp_path / "wts3"))
        names = ["glm", "opus", "codex"]
        wts = [aw.create_worktree(n, repo_root=repo) for n in names]
        assert len({str(w) for w in wts}) == 3

        for n, wt in zip(names, wts):
            (wt / f"{n}_work.txt").write_text(f"by {n}\n", encoding="utf-8")
            # each agent commits only in its own worktree
            g = subprocess.run(["git", "-C", str(wt), "add", "-A"], capture_output=True, text=True)
            assert g.returncode == 0
            g = subprocess.run(["git", "-C", str(wt), "commit", "-m", f"agent {n} change"], capture_output=True, text=True)
            assert g.returncode == 0

        for n, wt in zip(names, wts):
            assert (wt / f"{n}_work.txt").exists()
            for other in names:
                if other != n:
                    assert not (wt / f"{other}_work.txt").exists(), "worktrees must be isolated"
        # main tree untouched
        assert not (repo / "glm_work.txt").exists()
        assert not (repo / "opus_work.txt").exists()
        assert not (repo / "codex_work.txt").exists()

    def test_create_blocked_when_unsafe(self, tmp_path, monkeypatch):
        """Worktree creation refuses when the main tree is unsafe (section 20)."""
        import agent_worktree as aw
        repo = _make_tmp_git_repo(tmp_path)
        (repo / "providers-state-export.json").write_text('{"a":1}', encoding="utf-8")
        monkeypatch.setenv("AGENT_WORKTREE_ROOT", str(tmp_path / "wts"))
        with pytest.raises(SystemExit):
            aw.create_worktree("bad", repo_root=repo)


# =============================================================================
# G: deploy_local updates code, preserves private runtime state
# =============================================================================
class TestDeployLocal:
    def test_deploy_updates_code_preserves_private_state(self, tmp_path, monkeypatch):
        """23(G): code revision deployed; private root untouched."""
        import deploy_local as dl
        repo = _make_tmp_git_repo(tmp_path)

        # feature branch merged to master first (trusted review flow)
        g = lambda cwd, *a: subprocess.run(["git", "-C", str(cwd), *a], capture_output=True, text=True)
        assert g(repo, "checkout", "-b", "feature").returncode == 0
        (repo / "app.py").write_text("VALUE = 42\n", encoding="utf-8")
        assert g(repo, "add", "-A").returncode == 0
        assert g(repo, "commit", "-m", "reviewed change").returncode == 0
        assert g(repo, "checkout", "master").returncode == 0

        # private runtime sentinel OUTSIDE the repository
        private_root = tmp_path / "private_root"
        private_root.mkdir()
        sentinel = private_root / "runtime_state.json"
        sentinel.write_text('{"untouched": true}', encoding="utf-8")
        monkeypatch.setenv("WATCHEDIT_DATA_DIR", str(private_root))
        monkeypatch.setattr(dl, "PRIVATE_RUNTIME_ROOT", private_root)

        ok = dl.deploy_local(source="feature", skip_tests=True, quiet=True, repo_root=repo)
        assert ok, "deploy must succeed"
        assert (repo / "app.py").read_text(encoding="utf-8") == "VALUE = 42\n"  # code updated
        assert sentinel.read_text(encoding="utf-8") == '{"untouched": true}'    # private state intact

        # rollback point recorded
        tags = g(repo, "tag", "-l", "predeploy/*").stdout.split()
        assert tags, "deploy must create a rollback tag"

    def test_deploy_aborts_when_unsafe(self, tmp_path):
        import deploy_local as dl
        repo = _make_tmp_git_repo(tmp_path)
        (repo / "data.sqlite").write_bytes(b"\x00sqlite")
        assert dl.deploy_local(skip_tests=True, quiet=True, repo_root=repo) is False


# =============================================================================
# Offline contracts (section 15)
# =============================================================================
class TestOfflineContracts:
    def test_fixture_contract_shapes(self):
        fx = json.loads((REPO_ROOT / "9router_WatchEdit" / "tests" / "fixtures"
                         / "offline_contracts_sanitized.json").read_text(encoding="utf-8"))
        for key in ("providers_sanitized", "models_sanitized", "combos_sanitized",
                    "probe_responses_sanitized", "credentials_placeholder"):
            assert key in fx, f"missing offline contract: {key}"
        responses = fx["probe_responses_sanitized"]
        for state in ("success", "auth", "balance", "rate_limit", "timeout", "model_missing"):
            assert state in responses
        # credentials in the contract are placeholders only
        blob = json.dumps(fx)
        for placeholder in ("<REDACTED_API_KEY>", "fake-access-token",
                            "fake-refresh-token", "example-client-secret"):
            assert placeholder in blob

    def test_fixture_scans_clean(self):
        from core.secret_scanner import scan_file
        p = REPO_ROOT / "9router_WatchEdit" / "tests" / "fixtures" / "offline_contracts_sanitized.json"
        assert scan_file(p, REPO_ROOT) == []
