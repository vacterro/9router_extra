"""CORE-004 (T-5 / SRC-001:R0004) — apply-update.ps1 exit-code contract.

The defect: the updater shelled out to `npm install -g`, `node` restore,
`Stop-Process` and `Start-Process` without ever checking `$LASTEXITCODE`, then
printed an unconditional success banner even after killing the router.

Contract proven here, with injected failures, on real Windows PowerShell:
  * npm non-zero              -> non-zero exit, no success banner, dependency
                                 steps skipped, safety backup preserved;
  * restore (node) failure    -> non-zero exit, no success banner, backup kept;
  * backup helper failure     -> non-zero exit, no banner, install never runs;
  * launch exits immediately  -> non-zero exit, no banner (launch is verified);
  * complete success          -> banner only after the launch verification.

Every injected path lives in a tmp sandbox, so no real 9Router install, backup
or process is touched.
"""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "apply-update.ps1"
POWERSHELL = shutil.which("powershell") or shutil.which("pwsh")
BANNER = "Upgrade complete!"

pytestmark = pytest.mark.skipif(
    POWERSHELL is None or not SCRIPT.is_file(),
    reason="Windows PowerShell and apply-update.ps1 are required",
)


def _fake_cmd(path: Path, exit_code: int, marker: Path | None = None) -> Path:
    lines = ["@echo off"]
    if marker is not None:
        lines.append(f'echo ran>"{marker}"')
    lines.append(f"exit /b {exit_code}")
    path.write_text("\n".join(lines) + "\n", encoding="ascii")
    return path


def _fake_backup_script(path: Path, exit_code: int = 0, create: bool = True) -> Path:
    lines = [
        "param([string]$SourceDir,[string]$BackupParent,[string]$BackupName,[int]$Keep,[int]$MaxTotalMiB)",
    ]
    if create:
        lines += [
            "$d = Join-Path $BackupParent $BackupName",
            "New-Item -ItemType Directory -Force -Path $d | Out-Null",
            'Set-Content -Path (Join-Path $d "data.sqlite") -Value "state"',
        ]
    lines.append(f"exit {exit_code}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _sandbox(tmp_path: Path, npm_exit: int = 0, node_exit: int = 0, backup_exit: int = 0) -> dict:
    appdata_router = tmp_path / "appdata" / "9router"
    (appdata_router / "db").mkdir(parents=True)
    (appdata_router / "db" / "data.sqlite").write_text("state", encoding="utf-8")
    backups = tmp_path / "backups"
    backups.mkdir()
    package = tmp_path / "9router-0.5.65-extra.tgz"
    package.write_text("package", encoding="utf-8")
    restore = tmp_path / "restore_state.js"
    restore.write_text("// fake restore", encoding="utf-8")
    export = tmp_path / "providers-state-export.json"
    export.write_text("{}", encoding="utf-8")
    return {
        "appdata": appdata_router,
        "backups": backups,
        "package": package,
        "npm": _fake_cmd(tmp_path / "fake_npm.cmd", npm_exit, tmp_path / "npm.marker"),
        "node": _fake_cmd(tmp_path / "fake_node.cmd", node_exit, tmp_path / "node.marker"),
        "backup": _fake_backup_script(tmp_path / "fake_backup.ps1", backup_exit),
        "restore": restore,
        "export": export,
        "npm_marker": tmp_path / "npm.marker",
        "node_marker": tmp_path / "node.marker",
    }


def _args(env: dict, launch: str | None = None) -> list[str]:
    args = [
        "-NpmExe", str(env["npm"]),
        "-NodeExe", str(env["node"]),
        "-PackagePath", str(env["package"]),
        "-BackupScript", str(env["backup"]),
        "-AppDataRouter", str(env["appdata"]),
        "-BackupParent", str(env["backups"]),
        "-RestoreScript", str(env["restore"]),
        "-ExportFile", str(env["export"]),
        "-SkipStop",
    ]
    if launch is None:
        args.append("-SkipLaunch")
    else:
        args += ["-LaunchCommand", launch, "-LaunchVerifySeconds", "1"]
    return args


def _run(env: dict, launch: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(SCRIPT), *_args(env, launch)],
        capture_output=True,
        text=True,
        timeout=300,
    )


def _backup_dirs(env: dict) -> list[Path]:
    return sorted(env["backups"].glob("9router_backup_*"))


def test_npm_failure_aborts_nonzero_without_banner_and_preserves_backup(tmp_path):
    env = _sandbox(tmp_path, npm_exit=7)
    result = _run(env)

    assert result.returncode != 0, result.stdout + result.stderr
    assert BANNER not in result.stdout
    assert "UPGRADE FAILED" in result.stdout
    assert _backup_dirs(env), "safety backup must be preserved on failure"
    assert env["npm_marker"].exists(), "npm install did run (it is the injected failure)"
    assert not env["node_marker"].exists(), "dependent restore step must not run after npm failure"


def test_restore_failure_aborts_nonzero_without_banner(tmp_path):
    env = _sandbox(tmp_path, node_exit=3)
    result = _run(env)

    assert result.returncode != 0, result.stdout + result.stderr
    assert BANNER not in result.stdout
    assert "UPGRADE FAILED" in result.stdout
    assert env["npm_marker"].exists()
    assert env["node_marker"].exists()
    assert _backup_dirs(env)


def test_backup_helper_failure_aborts_before_install(tmp_path):
    env = _sandbox(tmp_path, backup_exit=1)
    result = _run(env)

    assert result.returncode != 0, result.stdout + result.stderr
    assert BANNER not in result.stdout
    assert not env["npm_marker"].exists(), "install must never run when the backup failed"
    assert not env["node_marker"].exists()


def test_immediate_exit_launch_aborts_without_banner(tmp_path):
    env = _sandbox(tmp_path)
    result = _run(env, launch="exit 0")

    assert result.returncode != 0, result.stdout + result.stderr
    assert BANNER not in result.stdout
    assert "exited immediately" in result.stdout
    assert env["npm_marker"].exists() and env["node_marker"].exists()


def test_complete_success_banners_only_after_launch_verification(tmp_path):
    env = _sandbox(tmp_path)
    result = _run(env, launch="Start-Sleep -Seconds 5")

    assert result.returncode == 0, result.stdout + result.stderr
    assert BANNER in result.stdout
    assert "verified running instance" in result.stdout


def test_static_contract_every_native_call_is_checked_and_banner_is_last(tmp_path):
    text = SCRIPT.read_text(encoding="utf-8")

    # every native invocation goes through the checked helper
    assert "function Invoke-NativeChecked" in text
    assert 'Invoke-NativeChecked -Label "npm install"' in text
    assert 'Invoke-NativeChecked -Label "state restore"' in text
    assert not re.search(r"^\s*npm install -g", text, re.M), "raw npm call bypasses the checker"
    assert not re.search(r"^\s*node \"", text, re.M), "raw node call bypasses the checker"
    assert text.count("$LASTEXITCODE") >= 4, "each native stage must inspect $LASTEXITCODE"

    # the launched process must be verified before any success claim
    assert "-PassThru" in text
    assert re.search(r"if \(\$launched\.HasExited\) \{\s*Fail", text), "launch liveness must gate the banner"
    banner_index = text.index(BANNER)
    verify_index = text.index("HasExited")
    assert verify_index < banner_index, "success banner must be printed after launch verification"

    # failure must preserve the backup and never claim success
    assert "Safety backup preserved" in text
