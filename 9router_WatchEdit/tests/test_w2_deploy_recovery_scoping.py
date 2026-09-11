"""
W2-002 + W2-004 / SRC-001:R0007 + R0009 regressions — deploy_local recovery
semantics and repository-scoped process control.

W2-002: an abandoned-deploy marker that CANNOT be auto-recovered must abort
the deploy with STATUS_RECOVERY_REQUIRED, create no new predeploy tag, perform
no checkout/restart, and leave the original marker byte-for-byte intact.
Successful recovery must terminate the invocation with STATUS_RECOVERED
(separate transactional phases).

W2-004: stop/start process control must resolve against the REQUESTED
repo_root, never the module-global REPO_ROOT.
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
TOOLS = REPO_ROOT / "tools"
sys.path.insert(0, str(TOOLS))


def _git(repo: Path, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)


def _mini_repo(tmp_path: Path, name: str = "repo") -> Path:
    repo = tmp_path / name
    repo.mkdir()
    (repo / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    g = lambda *a: _git(repo, *a)
    assert g("init").returncode == 0
    g("config", "user.name", "t")
    g("config", "user.email", "t@local")
    g("add", "-A")
    g("commit", "-m", "base")
    return repo


def _plant_marker(repo: Path, state: str, original_ref: str) -> bytes:
    marker = repo / ".git" / "watchedit_deploy_marker.json"
    marker.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({
        "state": state, "started": "2026-09-04T00:00:00",
        "original_ref": original_ref, "original_sha": "deadbeef", "tag": "predeploy/old",
    }).encode("utf-8")
    marker.write_bytes(payload)
    return payload


# =============================================================================
# W2-002: recovery result semantics
# =============================================================================
class TestRecoverySemantics:
    def test_unrecoverable_marker_aborts_with_no_side_effects(self, tmp_path, capsys):
        import deploy_local as dl
        repo = _mini_repo(tmp_path)
        original_payload = _plant_marker(repo, "APPLYING", "does-not-exist")

        ok = dl.deploy_local(source=None, restart=False, skip_tests=True, quiet=False, repo_root=repo)

        assert ok is False
        assert dl.last_status() == "RECOVERY_REQUIRED"
        # original marker preserved byte-for-byte
        assert (repo / ".git" / "watchedit_deploy_marker.json").read_bytes() == original_payload
        # no new rollback tag
        assert _git(repo, "tag", "-l", "predeploy/*").stdout.split() == []
        # no checkout happened (HEAD still on master, file content unchanged)
        assert (repo / "app.py").read_text(encoding="utf-8") == "VALUE = 1\n"

    def test_malformed_marker_json_aborts_and_preserves_bytes(self, tmp_path):
        import deploy_local as dl
        repo = _mini_repo(tmp_path)
        marker = repo / ".git" / "watchedit_deploy_marker.json"
        marker.write_bytes(b"{ this is not json")
        before = marker.read_bytes()

        ok = dl.deploy_local(skip_tests=True, quiet=True, repo_root=repo)

        assert ok is False
        assert dl.last_status() == "RECOVERY_REQUIRED"
        assert marker.read_bytes() == before  # never rewritten, never deleted

    def test_missing_original_ref_aborts(self, tmp_path):
        import deploy_local as dl
        repo = _mini_repo(tmp_path)
        _plant_marker(repo, "APPLYING", "")

        ok = dl.deploy_local(skip_tests=True, quiet=True, repo_root=repo)
        assert ok is False
        assert dl.last_status() == "RECOVERY_REQUIRED"

    def test_successful_recovery_stops_invocation(self, tmp_path, capsys):
        import deploy_local as dl
        repo = _mini_repo(tmp_path)
        # create a real second branch to restore
        assert _git(repo, "checkout", "-b", "backup-branch").returncode == 0
        (repo / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-m", "second")
        assert _git(repo, "checkout", "master").returncode == 0
        # marker on master claims the deploy interrupted from backup-branch
        _plant_marker(repo, "APPLYING", "backup-branch")

        ok = dl.deploy_local(skip_tests=True, quiet=False, repo_root=repo)
        out = capsys.readouterr().out

        assert ok is False  # recovery is a separate phase; deploy does not continue
        assert dl.last_status() == "RECOVERED"
        assert "recovered" in out.lower()
        # marker consumed, original revision restored
        assert not (repo / ".git" / "watchedit_deploy_marker.json").exists()
        current = _git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        assert current == "backup-branch"
        # no predeploy tag created by the aborted invocation
        tags = _git(repo, "tag", "-l", "predeploy/*").stdout.split()
        assert tags == [] or all("predeploy/old" == t for t in tags)

    def test_stale_completed_marker_cleaned_and_redeploy_succeeds(self, tmp_path):
        import deploy_local as dl
        repo = _mini_repo(tmp_path)
        _plant_marker(repo, "COMPLETED", "master")
        ok = dl.deploy_local(skip_tests=True, quiet=True, repo_root=repo)
        assert ok is True
        assert dl.last_status() == "SUCCESS"
        # legacy stale marker consumed; the new invocation's own marker is
        # left as COMPLETED by design (it is the current deploy's own run record)
        info = json.loads((repo / ".git" / "watchedit_deploy_marker.json").read_text(encoding="utf-8"))
        assert info.get("state") == "COMPLETED"
        assert (repo / "app.py").read_text(encoding="utf-8") == "VALUE = 1\n"


# =============================================================================
# W2-004: repo-scoped process control
# =============================================================================
class TestRepoScopedProcessControl:
    def test_start_uses_requested_repo_paths(self, tmp_path, monkeypatch):
        import deploy_local as dl
        repo_a = _mini_repo(tmp_path, "repoA")
        repo_b = _mini_repo(tmp_path, "repoB")
        # both have START scripts so existence check passes
        for r in (repo_a, repo_b):
            (r / "START_WATCHEDIT.bat").write_text("@echo off\n", encoding="utf-8")

        captured = []
        monkeypatch.setattr(subprocess, "Popen",
                            lambda args, **kw: captured.append((args, kw)) or True)

        assert dl._start_instance(repo_a) is True
        args, kw = captured[0]
        flat = " ".join(str(a) for a in args)
        assert str(repo_a) in flat, "start command must reference the REQUESTED repo"
        assert str(repo_b) not in flat
        assert kw["cwd"] == str(repo_a), "cwd must be the REQUESTED repo"

    def test_start_missing_script_fails(self, tmp_path):
        import deploy_local as dl
        repo = _mini_repo(tmp_path)
        assert dl._start_instance(repo) is False

    def test_stop_query_contains_requested_repo(self, tmp_path, monkeypatch):
        import deploy_local as dl
        repo_a = tmp_path / "repoA"
        repo_a.mkdir()
        repo_b = tmp_path / "repoB"
        repo_b.mkdir()

        captured = {}
        class FakeResult:
            returncode = 0
            stdout = ""
            stderr = ""
        def fake_run(args, **kw):
            captured["args"] = args
            return FakeResult()
        monkeypatch.setattr(subprocess, "run", fake_run)

        dl._stop_running_instance(repo_a)
        flat = " ".join(str(a) for a in captured["args"])
        assert str(repo_a) in flat, "stop query must contain the REQUESTED repo path"
        assert str(repo_b) not in flat, "stop query must not contain a sibling repo path"
        assert "run.py" in flat and "9router_WatchEdit" in flat
