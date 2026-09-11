"""
CORE-001 / SRC-001:R0001 regressions — no repository-relative private-state
fallback when WATCHEDIT_DATA_DIR / LOCALAPPDATA / APPDATA are missing or hostile.
All checks run in subprocesses so import-time module state is isolated.

Design contract: when no valid private root exists, config reports
PRIVATE_STORAGE_AVAILABLE=False and derives every private path under an
os.devnull sentinel — absolute operations (mkdir/write) fail with OSError,
exists() is False for derived files, and nothing may appear in the source tree.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
WATCHEDIT = REPO_ROOT / "9router_WatchEdit"
STRIP = ("WATCHEDIT_DATA_DIR", "LOCALAPPDATA", "APPDATA")


def _base_env(extra: dict = None) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in STRIP}
    env["PYTHONDONTWRITEBYTECODE"] = "1"  # keep __pycache__ out of tree-diff assertions
    if extra:
        env.update(extra)
    return env


REPRO = r'''
import json, os, sys
from pathlib import Path
repo = Path(sys.argv[1]).resolve()
os.chdir(sys.argv[2])
sys.path.insert(0, str(repo / "9router_WatchEdit"))
import config
import core.security as sec
import core.secret_store as ss
out = {
    "cwd": str(Path.cwd()),
    "LOCALAPPDATA_DIR": str(config.LOCALAPPDATA_DIR),
    "sentinel": str(config.LOCALAPPDATA_DIR) == os.devnull,
    "PRIVATE_STORAGE_AVAILABLE": config.PRIVATE_STORAGE_AVAILABLE,
    "APPDATA_ROUTER": str(config.APPDATA_ROUTER),
    "settings_file_absolute": config.LOCAL_SETTINGS_FILE.is_absolute(),
    "machine_id_exists": config.MACHINE_ID_FILE.exists(),
    "sm_storage": sec.SecurityManager().storage_available,
    "store_set_refused": False,
    "store_get_none": None,
    "store_has_false": None,
    "store_dir_created": False,
}
store = ss.DPAPIFileStore()
try:
    store.set_secret("probe", "x")
except Exception:
    out["store_set_refused"] = True
out["store_get_none"] = store.get_secret("probe") is None
out["store_has_false"] = store.has_secret("probe") is False
out["store_dir_created"] = store.secure_dir.is_absolute() and store.secure_dir.exists()
# Exercise SecurityManager write path (must degrade, never crash, never create)
sec.SecurityManager()._write_local_settings({"probe": 1})
print(json.dumps(out))
'''

TREE_GUARD = r'''
import json, os, sys
from pathlib import Path
repo = Path(sys.argv[1]).resolve()
os.chdir(sys.argv[2])
sys.path.insert(0, str(repo / "9router_WatchEdit"))
before = sorted(str(p) for p in Path.cwd().rglob("*"))
import config, core.security, core.secret_store  # noqa: F401
import core.history, core.combo_manager  # noqa: F401
# Exercise the writers that would silently land in CWD if paths were relative
from core.history import HealthCache
from core.combo_manager import PresetManager
HealthCache().save()
PresetManager().save()
after = sorted(str(p) for p in Path.cwd().rglob("*"))
new = [p for p in after if p not in set(before)]
print(json.dumps({"new_paths": new}))
'''


def _run(script: str, cwd: Path, extra_env: dict = None) -> dict:
    res = subprocess.run(
        [sys.executable, "-c", script, str(REPO_ROOT), str(cwd)],
        capture_output=True, text=True, timeout=120, env=_base_env(extra_env),
    )
    assert res.returncode == 0, (res.stdout or "")[-500:] + (res.stderr or "")[-500:]
    return json.loads(res.stdout.strip().splitlines()[-1])


@pytest.mark.parametrize("cwd", [REPO_ROOT, WATCHEDIT])
def test_missing_env_degrades_to_unavailable_sentinel(cwd):
    out = _run(REPRO, cwd)
    assert out["PRIVATE_STORAGE_AVAILABLE"] is False
    assert out["sentinel"] is True, "LOCALAPPDATA_DIR must be the devnull sentinel, not a real path"
    assert out["settings_file_absolute"] is False
    assert out["machine_id_exists"] is False
    assert out["sm_storage"] is False, "SecurityManager must report storage unavailable"
    assert out["store_set_refused"] is True, "DPAPIFileStore must refuse to store"
    assert out["store_get_none"] is True
    assert out["store_has_false"] is True
    assert out["store_dir_created"] is False, "no secure dir may be created"


@pytest.mark.parametrize("cwd", [REPO_ROOT, WATCHEDIT])
def test_missing_env_no_files_created_in_tree(cwd):
    out = _run(TREE_GUARD, cwd)
    assert out["new_paths"] == [], f"imports/writes created paths under {cwd}: {out['new_paths']}"


def test_relative_watchedit_data_dir_rejected(tmp_path):
    out = _run(REPRO, tmp_path, extra_env={"WATCHEDIT_DATA_DIR": "relative_private_root"})
    assert out["PRIVATE_STORAGE_AVAILABLE"] is False
    assert out["sentinel"] is True
    assert not (tmp_path / "relative_private_root").exists(), "relative override must create nothing"


def test_watchedit_data_dir_inside_repo_rejected():
    hostile = WATCHEDIT / "tests" / "_hostile_private_root"
    hostile.mkdir(exist_ok=True)
    try:
        out = _run(REPRO, REPO_ROOT, extra_env={"WATCHEDIT_DATA_DIR": str(hostile)})
        assert out["PRIVATE_STORAGE_AVAILABLE"] is False, "repo-inside WATCHEDIT_DATA_DIR must be rejected"
        assert not any(hostile.iterdir()), "rejected root must stay empty"
    finally:
        hostile.rmdir()


def test_valid_localappdata_still_works(tmp_path):
    """Happy path: normal LOCALAPPDATA keeps private storage available."""
    code = (
        "import json, os, sys;"
        "os.environ['LOCALAPPDATA'] = sys.argv[2];"
        "os.environ.pop('WATCHEDIT_DATA_DIR', None);"
        "os.environ.pop('APPDATA', None);"
        "sys.path.insert(0, sys.argv[1] + '/9router_WatchEdit');"
        "import config;"
        "print(json.dumps({'avail': config.PRIVATE_STORAGE_AVAILABLE,"
        "'la': str(config.LOCALAPPDATA_DIR)}))"
    )
    res = subprocess.run(
        [sys.executable, "-c", code, str(REPO_ROOT), str(tmp_path)],
        capture_output=True, text=True, timeout=120,
        env={**_base_env(), "LOCALAPPDATA": str(tmp_path)},
    )
    assert res.returncode == 0, (res.stderr or "")[-500:]
    out = json.loads(res.stdout.strip().splitlines()[-1])
    assert out["avail"] is True
    assert Path(out["la"]) == Path(tmp_path) / "9router_WatchEdit"


def test_agent_safe_gate_still_yes():
    sys.path.insert(0, str(REPO_ROOT / "tools"))
    try:
        import verify_agent_safe as vas
        assert vas.format_result(vas.verify_agent_safe(REPO_ROOT)) == "AGENT SAFE: YES"
    finally:
        sys.path.pop(0)
