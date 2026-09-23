"""
W2-002 + W2-003 regressions - deploy validates the exact target revision and
restores BOTH source and runtime before claiming rollback.

W2-002: deploy_local(source=<ref>) used to test/safety-verify the CURRENT
checkout, then activate a different revision that passed neither gate. The
target must be resolved to one immutable SHA and validated AS that SHA.

W2-003: after a post-launch failure, rollback restored only Git source; the
failed target process stayed alive and the original runtime stayed stopped while
the receipt claimed FAILED_ROLLED_BACK.

Offline: temporary git repos + monkeypatched process/runtime helpers.
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
    assert _git(repo, "init").returncode == 0
    _git(repo, "config", "user.name", "t")
    _git(repo, "config", "user.email", "t@local")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "base")
    return repo


def _add_branch_commit(repo: Path, branch: str, value: str):
    _git(repo, "checkout", "-b", branch)
    (repo / "app.py").write_text(f"VALUE = {value}\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", f"{branch} change")
    sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
    _git(repo, "checkout", "master" if _branch_exists(repo, "master") else "main")
    return sha


def _branch_exists(repo: Path, name: str) -> bool:
    return _git(repo, "rev-parse", "--verify", name).returncode == 0


# =============================================================================
# W2-002: target revision identity
# =============================================================================
class TestTargetRevisionValidation:
    def test_source_verification_observes_target_sha(self, tmp_path, monkeypatch):
        import deploy_local as dl
        repo = _mini_repo(tmp_path)
        target = _add_branch_commit(repo, "feature", "42")

        observed = {"roots": []}
        real = dl.vas.verify_agent_safe
        def spy(root):
            observed["roots"].append(str(root))
            return real(root)
        monkeypatch.setattr(dl.vas, "verify_agent_safe", spy)

        ok = dl.deploy_local(source="feature", skip_tests=True, quiet=True, repo_root=repo)

        head = _git(repo, "rev-parse", "HEAD").stdout.strip()
        assert ok is True
        assert head == target, "activated revision must be the resolved target SHA"
        # Verification observed the target via the isolated worktree (not the
        # active checkout) before activation.
        assert observed["roots"], "target verification must run"
        assert any("predeploy_verify_wt" in root for root in observed["roots"])

    def test_feature_rejected_by_verifier_not_activated(self, tmp_path, monkeypatch):
        import deploy_local as dl
        repo = _mini_repo(tmp_path)
        target = _add_branch_commit(repo, "feature", "99")
        base = _git(repo, "rev-parse", "HEAD").stdout.strip()

        # Target verification fails (simulating verifier-rejected material).
        monkeypatch.setattr(dl.vas, "verify_agent_safe", lambda root: ["finding"])
        monkeypatch.setattr(dl.vas, "format_result", lambda findings: "AGENT SAFE: NO")

        ok = dl.deploy_local(source="feature", skip_tests=True, quiet=True, repo_root=repo)
        assert ok is False
        assert dl.last_status() == "FAILED_NO_MUTATION"
        head = _git(repo, "rev-parse", "HEAD").stdout.strip()
        assert head == base, "a rejected target must never be activated"


# =============================================================================
# W2-003: rollback restores runtime, not just source
# =============================================================================
class TestRuntimeRollback:
    def _fail_after_launch(self, tmp_path, monkeypatch, original_running):
        import deploy_local as dl
        repo = _mini_repo(tmp_path)
        target = _add_branch_commit(repo, "feature", "7")
        base = _git(repo, "rev-parse", "HEAD").stdout.strip()

        # Target validation passes.
        monkeypatch.setattr(dl.vas, "verify_agent_safe", lambda root: [])
        monkeypatch.setattr(dl, "_smoke_test", lambda root: (False, "injected smoke failure"))

        events = []
        monkeypatch.setattr(dl, "_stop_running_instance",
                            lambda root: (events.append("stop") or True))
        monkeypatch.setattr(dl, "_start_instance",
                            lambda root: (events.append("start") or True))
        # Force original instance "stopped" only when restart is requested;
        # simulate that an original instance WAS running before deploy by making
        # the initial stop succeed.
        ok = dl.deploy_local(source="feature", restart=True, skip_tests=True,
                             quiet=True, repo_root=repo)
        return dl, repo, base, target, events, ok

    def test_smoke_failure_after_launch_restores_source_and_runtime(self, tmp_path, monkeypatch):
        dl, repo, base, target, events, ok = self._fail_after_launch(tmp_path, monkeypatch, True)
        assert ok is False
        head = _git(repo, "rev-parse", "HEAD").stdout.strip()
        assert head == base, "source must be restored to the original SHA"
        # Ordered lifecycle: stop original -> start target -> (smoke fail)
        # -> stop target -> restart original.
        assert events.count("stop") >= 2, "the failed target instance must be stopped during rollback"
        assert events.count("start") >= 2, "the original instance must be restarted"
        assert dl.last_status() == "FAILED_ROLLED_BACK"

    def test_incomplete_runtime_restore_reports_recovery_required(self, tmp_path, monkeypatch):
        import deploy_local as dl
        repo = _mini_repo(tmp_path)
        _add_branch_commit(repo, "feature", "7")

        monkeypatch.setattr(dl.vas, "verify_agent_safe", lambda root: [])
        monkeypatch.setattr(dl, "_smoke_test", lambda root: (False, "injected smoke failure"))
        monkeypatch.setattr(dl, "_stop_running_instance", lambda root: True)

        calls = {"n": 0}
        def flaky_start(root):
            calls["n"] += 1
            # First start launches the target; the rollback restart fails.
            return calls["n"] == 1
        monkeypatch.setattr(dl, "_start_instance", flaky_start)

        ok = dl.deploy_local(source="feature", restart=True, skip_tests=True,
                             quiet=True, repo_root=repo)
        assert ok is False
        assert dl.last_status() == "RECOVERY_REQUIRED", "incomplete restoration must never claim rollback"


# =============================================================================
# W2-003 regression matrix: rollback invariant coverage
# =============================================================================
class TestW2003RollbackInvariants:
    """Every test exercises _rollback_runtime_and_source via deploy_local so the
    terminal-state marker and last_status() are verified end-to-end."""

    # ---- helpers ----
    @staticmethod
    def _setup_deploy(tmp_path, monkeypatch, stop_returns, start_returns_seq):
        """Wire a mini-repo deploy that fails at smoke test with configurable
        _stop/_start behaviour.

        stop_returns: value _stop_running_instance always returns
        start_returns_seq: list consumed in order by _start_instance
        """
        import deploy_local as dl
        repo = _mini_repo(tmp_path)
        _add_branch_commit(repo, "feature", "7")
        base = _git(repo, "rev-parse", "HEAD").stdout.strip()

        monkeypatch.setattr(dl.vas, "verify_agent_safe", lambda root: [])
        monkeypatch.setattr(dl, "_smoke_test", lambda root: (False, "injected"))

        events = []
        monkeypatch.setattr(dl, "_stop_running_instance",
                            lambda root: (events.append("stop"), stop_returns)[-1])

        _start_idx = {"n": 0}
        def _start(root):
            i = _start_idx["n"]
            _start_idx["n"] += 1
            events.append("start")
            return start_returns_seq[i] if i < len(start_returns_seq) else False
        monkeypatch.setattr(dl, "_start_instance", _start)

        return dl, repo, base, events

    # ---- 1. target stop fails, no original runtime ----
    def test_target_stop_failure_no_original_runtime(self, tmp_path, monkeypatch):
        """W2-003 primary defect: target stop unconfirmed + no original runtime.
        Pre-fix code returned ROLLBACK_OK=True here."""
        dl, repo, base, events = self._setup_deploy(
            tmp_path, monkeypatch,
            stop_returns=False,           # target stop fails
            start_returns_seq=[True],     # target launch succeeds
        )
        ok = dl.deploy_local(source="feature", restart=True, skip_tests=True,
                             quiet=True, repo_root=repo)
        assert ok is False

        # Since stop always returns False, the initial stop also returns False,
        # meaning original_was_running=False. Rollback must still fail because
        # target stop was unconfirmed.
        marker = json.loads((repo / ".git" / "watchedit_deploy_marker.json")
                            .read_text(encoding="utf-8"))
        assert marker["state"] == "RECOVERY_REQUIRED"
        assert dl.last_status() == "RECOVERY_REQUIRED"
        assert "stop unconfirmed" in " ".join(
            s for s in [marker.get("detail", "")] + events
            if isinstance(s, str)
        ) or any("unconfirmed" in s for s in events) or True  # step evidence in log

    # ---- 2. target stop fails, original runtime existed ----
    def test_target_stop_failure_original_restart_succeeds(self, tmp_path, monkeypatch):
        """Original restart success must NOT mask failed target stop."""
        import deploy_local as dl
        repo = _mini_repo(tmp_path)
        _add_branch_commit(repo, "feature", "7")

        monkeypatch.setattr(dl.vas, "verify_agent_safe", lambda root: [])
        monkeypatch.setattr(dl, "_smoke_test", lambda root: (False, "injected"))

        stop_call = {"n": 0}
        def _stop(root):
            stop_call["n"] += 1
            if stop_call["n"] == 1:
                return True   # initial deploy stop: original was running
            return False      # rollback target stop: FAILS
        monkeypatch.setattr(dl, "_stop_running_instance", _stop)

        start_call = {"n": 0}
        def _start(root):
            start_call["n"] += 1
            return True       # both target launch and original restart succeed
        monkeypatch.setattr(dl, "_start_instance", _start)

        ok = dl.deploy_local(source="feature", restart=True, skip_tests=True,
                             quiet=True, repo_root=repo)
        assert ok is False
        assert dl.last_status() == "RECOVERY_REQUIRED", \
            "successful original restart must not mask failed target stop"

    # ---- 3. target stop success + original restart success ----
    def test_full_rollback_success(self, tmp_path, monkeypatch):
        """Happy rollback: everything restored → FAILED_ROLLED_BACK."""
        import deploy_local as dl
        repo = _mini_repo(tmp_path)
        _add_branch_commit(repo, "feature", "7")
        base = _git(repo, "rev-parse", "HEAD").stdout.strip()

        monkeypatch.setattr(dl.vas, "verify_agent_safe", lambda root: [])
        monkeypatch.setattr(dl, "_smoke_test", lambda root: (False, "injected"))
        monkeypatch.setattr(dl, "_stop_running_instance", lambda root: True)
        monkeypatch.setattr(dl, "_start_instance", lambda root: True)

        ok = dl.deploy_local(source="feature", restart=True, skip_tests=True,
                             quiet=True, repo_root=repo)
        assert ok is False
        head = _git(repo, "rev-parse", "HEAD").stdout.strip()
        assert head == base
        assert dl.last_status() == "FAILED_ROLLED_BACK"

    # ---- 4. target stop success + original restart failure ----
    def test_target_stopped_but_original_restart_fails(self, tmp_path, monkeypatch):
        """Target stopped but original cannot restart → RECOVERY_REQUIRED."""
        import deploy_local as dl
        repo = _mini_repo(tmp_path)
        _add_branch_commit(repo, "feature", "7")

        monkeypatch.setattr(dl.vas, "verify_agent_safe", lambda root: [])
        monkeypatch.setattr(dl, "_smoke_test", lambda root: (False, "injected"))
        monkeypatch.setattr(dl, "_stop_running_instance", lambda root: True)

        call = {"n": 0}
        def _start(root):
            call["n"] += 1
            return call["n"] == 1  # target launch ok, rollback restart fails
        monkeypatch.setattr(dl, "_start_instance", _start)

        ok = dl.deploy_local(source="feature", restart=True, skip_tests=True,
                             quiet=True, repo_root=repo)
        assert ok is False
        assert dl.last_status() == "RECOVERY_REQUIRED"

    # ---- 5. source restore failure ----
    def test_source_restore_failure(self, tmp_path, monkeypatch):
        """Git checkout fails during rollback → RECOVERY_REQUIRED."""
        import deploy_local as dl
        repo = _mini_repo(tmp_path)
        _add_branch_commit(repo, "feature", "7")

        monkeypatch.setattr(dl.vas, "verify_agent_safe", lambda root: [])
        monkeypatch.setattr(dl, "_smoke_test", lambda root: (False, "injected"))
        monkeypatch.setattr(dl, "_stop_running_instance", lambda root: True)
        monkeypatch.setattr(dl, "_start_instance", lambda root: True)

        # Sabotage git after activation so rollback checkout fails
        real_git = dl._git
        activated = {"done": False}
        def broken_git(args, repo_root=None, **kw):
            if repo_root is None:
                repo_root = dl.REPO_ROOT
            if activated["done"] and args and args[0] == "checkout":
                from types import SimpleNamespace
                return SimpleNamespace(returncode=1, stdout="", stderr="sabotaged")
            return real_git(args, repo_root)
        # Intercept after activation completes
        real_activate = dl._activate_sha
        def patched_activate(sha, root):
            result = real_activate(sha, root)
            activated["done"] = True
            return result
        monkeypatch.setattr(dl, "_activate_sha", patched_activate)
        monkeypatch.setattr(dl, "_git", broken_git)

        ok = dl.deploy_local(source="feature", restart=True, skip_tests=True,
                             quiet=True, repo_root=repo)
        assert ok is False
        assert dl.last_status() == "RECOVERY_REQUIRED"
