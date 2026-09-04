"""
Master end-to-end invariant test (campaign section 28).

SAFE repo + external private runtime with synthetic canaries + three agent
worktrees. Full flow: verify -> worktree -> develop -> commit -> review ->
scan -> tests -> merge -> deploy -> smoke -> rollback -> re-deploy.

Asserts throughout:
  A. no private-runtime canary ever appears inside the source repo
  B. no canary inside any agent worktree
  C. no canary in Git commits
  D. no canary in logs (captured output)
  E. no canary in receipts
  F. no canary in diagnostic bundles
  G. private runtime hashes unchanged across normal code deploy
  H. external agent changes still develop/test/deploy normally
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from tests.security.canaries import (
    all_canaries, canary_access_token, canary_api_key, canary_refresh_token,
)
from tests.security.helpers import REPO_ROOT, TOOLS, git, hash_tree, make_mini_repo

sys.path.insert(0, str(TOOLS))
sys.path.insert(0, str(REPO_ROOT / "9router_WatchEdit"))

PRIVATE_CONTENTS = {
    "credentials.dat": "CRED-" + "9rTEST2026",
    "provider_state.dat": "PROV-" + "9rTEST2026",
    "data.sqlite": "SQLite format 3\x00PRIVATE-9rTEST2026",
    "oauth_state.dat": "OAUTH-" + "9rTEST2026",
    "settings.json": '{"trusted_os_unlock": false, "note": "PRIVATE-9rTEST2026"}',
}


@pytest.fixture
def world(tmp_path, monkeypatch):
    """Mini safe repo + private runtime (with canaries) + three worktrees."""
    import agent_worktree as aw
    import deploy_local as dl

    repo = make_mini_repo(tmp_path, "world")
    private = tmp_path / "private_runtime"
    private.mkdir()
    for fname, content in PRIVATE_CONTENTS.items():
        (private / fname).write_text(content, encoding="utf-8")
    # high-entropy canaries in the private runtime (never in the repo)
    (private / "credentials.dat").write_text(
        (private / "credentials.dat").read_text(encoding="utf-8") + "\n" + canary_api_key(),
        encoding="utf-8")
    (private / "oauth_state.dat").write_text(
        (private / "oauth_state.dat").read_text(encoding="utf-8") + "\n" + canary_access_token(),
        encoding="utf-8")

    monkeypatch.setenv("AGENT_WORKTREE_ROOT", str(tmp_path / "wts"))
    monkeypatch.setenv("WATCHEDIT_DATA_DIR", str(private))
    monkeypatch.setattr(dl, "PRIVATE_RUNTIME_ROOT", private)

    wts = {n: aw.create_worktree(n, repo_root=repo) for n in ("glm", "opus", "codex")}
    return type("World", (), {"repo": repo, "private": private, "wts": wts,
                              "tmp_path": tmp_path, "dl": dl, "aw": aw})()


def _assert_no_canaries(where: Path, label: str):
    for p in Path(where).rglob("*"):
        if not p.is_file() or ".git" in p.parts:
            continue
        blob = p.read_text(encoding="utf-8", errors="replace")
        for canary in all_canaries():
            assert canary not in blob, f"CANARY LEAK ({label}): {canary[:20]}... in {p}"


class TestMasterE2E:
    def test_full_flow_invariants(self, world, capsys, monkeypatch):
        repo, private, wts = world.repo, world.private, world.wts

        # 1. VERIFY_AGENT_SAFE on the safe main tree
        from verify_agent_safe import verify_agent_safe
        assert verify_agent_safe(repo) == []

        # 2-4. three agents develop + commit independently
        for n, wt in wts.items():
            (wt / f"{n}_feature.py").write_text(f"VALUE_{n} = 1\n", encoding="utf-8")
            assert git(wt, "add", "-A").returncode == 0
            assert git(wt, "commit", "-m", f"agent {n} feature").returncode == 0

        # B. worktrees stay canary-free
        for wt in wts.values():
            _assert_no_canaries(wt, "worktree")

        # 5. review each worktree
        for n in wts:
            rc = world.aw.review_worktree(n, repo_root=repo, run_tests=True)
            assert rc == 0, f"clean agent {n} must review successfully"

        # 6. independent scans of each worktree
        for wt in wts.values():
            assert verify_agent_safe(wt) == []

        # 7. unit tests inside a worktree
        import subprocess
        tr = subprocess.run([sys.executable, "-m", "pytest", "-q", "tests/test_ok.py"],
                            cwd=str(wts["glm"]), capture_output=True, text=True, timeout=120)
        assert tr.returncode == 0

        # 8. merge one agent branch
        branch = git(wts["glm"], "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        assert git(repo, "merge", branch).returncode == 0
        assert (repo / "glm_feature.py").exists(), "H: agent change must land"

        # 9. deploy (CODE only)
        private_before = hash_tree(private)
        assert world.dl.deploy_local(skip_tests=True, quiet=True, repo_root=repo) is True
        # G. private runtime untouched
        assert hash_tree(private) == private_before

        # 10. smoke verification: the REAL app must boot LOCKED. It uses its own
        # fresh data dir — the app legitimately writes its own settings during
        # a boot, which is distinct from the operator's private runtime state.
        smoke_data = world.tmp_path / "smoke_data"
        monkeypatch.setenv("WATCHEDIT_DATA_DIR", str(smoke_data))
        ok, detail = world.dl._smoke_test(REPO_ROOT)
        assert ok, f"real-app smoke failed: {detail}"
        monkeypatch.setenv("WATCHEDIT_DATA_DIR", str(world.private))
        assert hash_tree(private) == private_before

        # 11. rollback test: inject failure mid-deploy of another branch
        repo_before = hash_tree(repo)
        monkeypatch.setattr(world.dl, "_smoke_test", lambda *a, **k: (False, "injected"))
        branch2 = git(wts["opus"], "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        assert world.dl.deploy_local(source=branch2, skip_tests=True, quiet=True, repo_root=repo) is False
        assert hash_tree(repo) == repo_before, "rollback must restore exact state"
        assert hash_tree(private) == private_before, "rollback must never touch private state"
        monkeypatch.undo()

        # 12. re-deploy succeeds
        assert world.dl.deploy_local(skip_tests=True, quiet=True, repo_root=repo) is True
        assert hash_tree(private) == private_before

        # A. source repo never received canaries
        _assert_no_canaries(repo, "source repo")

        # C. no canary in ANY git commit content
        log = git(repo, "log", "-p", "--all").stdout
        for canary in all_canaries():
            assert canary not in log, f"CANARY COMMITTED: {canary[:20]}..."

        # D. no canary in captured logs of the entire flow
        captured = capsys.readouterr()
        for canary in all_canaries():
            assert canary not in captured.out + captured.err

        # E. receipts (flow output saved to disk) scan clean
        receipts = world.tmp_path / "receipts"
        receipts.mkdir(exist_ok=True)
        (receipts / "flow.log").write_text(
            captured.out + captured.err + log, encoding="utf-8")
        from core.secret_scanner import scan_tree
        assert scan_tree(receipts) == []

        # F. diagnostic bundle from this world is canary-free
        from core.diagnostics import export_diagnostic_bundle
        bundle = export_diagnostic_bundle(
            recent_errors=[f"Authorization: Bearer {canary_access_token()}",
                           f"refresh failure {canary_refresh_token()}"],
            out_dir=world.tmp_path / "diag")
        blob = bundle.read_text(encoding="utf-8")
        for canary in all_canaries():
            assert canary not in blob
        assert scan_tree(world.tmp_path / "diag") == []
