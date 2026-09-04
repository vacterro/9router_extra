"""Shared helpers for the adversarial security campaign tests."""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
TOOLS = REPO_ROOT / "tools"

sys.path.insert(0, str(TOOLS))
sys.path.insert(0, str(REPO_ROOT / "9router_WatchEdit"))

from tests.security.canaries import (  # noqa: E402
    canary_access_token, canary_api_key, canary_client_secret, canary_jwt,
    canary_password, canary_refresh_token, canary_sk,
)


def git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True)


def make_mini_repo(tmp_path: Path, name: str = "gitrepo") -> Path:
    """Miniature git repo with helper tools, scanner core and a passing test.

    Used for worktree/deploy/patch campaign scenarios without touching the
    real repository."""
    repo = tmp_path / name
    repo.mkdir(parents=True)
    (repo / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    (repo / ".gitignore").write_text(
        "__pycache__/\n*.pyc\n.pytest_cache/\n", encoding="utf-8")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_ok.py").write_text(
        "def test_ok():\n    assert 1 + 1 == 2\n", encoding="utf-8")

    (repo / "tools").mkdir()
    for fn in ("verify_agent_safe.py", "agent_worktree.py", "pre_commit_secret_check.py",
               "deploy_local.py", "apply_agent_patch.py"):
        src = TOOLS / fn
        if src.exists():
            shutil.copy2(src, repo / "tools" / fn)
    core = repo / "9router_WatchEdit" / "core"
    core.mkdir(parents=True)
    (repo / "9router_WatchEdit" / "__init__.py").write_text("", encoding="utf-8")
    (core / "__init__.py").write_text("", encoding="utf-8")
    shutil.copy2(REPO_ROOT / "9router_WatchEdit" / "core" / "secret_scanner.py",
                 core / "secret_scanner.py")

    g = lambda *a: git(repo, *a)
    g("init")
    g("config", "user.name", "campaign")
    g("config", "user.email", "campaign@local")
    g("add", "-A")
    g("commit", "-m", "base")
    return repo


def mini_canary_repo(tmp_path: Path, name: str, canary: str, filename: str = "leaked.txt",
                     content: str = None) -> Path:
    """Mini repo containing one synthetic canary file (clean except the canary)."""
    repo = make_mini_repo(tmp_path, name)
    body = content if content is not None else f"note: {canary}\n"
    (repo / filename).write_text(body, encoding="utf-8")
    return repo


def hash_tree(root: Path) -> dict:
    """SHA-256 map of every file under root (for rollback exactness tests)."""
    import hashlib
    out = {}
    for p in sorted(Path(root).rglob("*")):
        if p.is_file() and ".git" not in p.parts:
            out[str(p.relative_to(root)).replace("\\", "/")] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out
