"""
AutoVault Task Scheduler INTEGRATION test (Windows only, real scheduler).

Creates a TEMPORARY uniquely-named task, verifies registration / principal /
trigger / StartWhenAvailable / single-instance policy / absolute paths /
manual start + exit code reporting, then DELETES it. The production task
("9Router AutoVault Backup") is never touched (different name).
"""
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "tools"))

pytestmark = [
    pytest.mark.skipif(sys.platform != "win32", reason="Windows Task Scheduler only"),
    pytest.mark.integration,
]

import autovault as av  # noqa: E402

PROD_TASK = "9Router AutoVault Backup"


def _ps(script: str) -> str:
    r = subprocess.run(["powershell", "-NoProfile", "-Command", script],
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    return r.stdout


def test_task_scheduler_integration():
    name = f"9Router AutoVault IT {uuid.uuid4().hex[:8]}"
    assert PROD_TASK not in name
    # harmless action: python -c exits 7, no secrets, no state touched
    code = "import sys; sys.exit(7)"
    exe = sys.executable
    ps_reg = "\n".join([
        "$ErrorActionPreference = 'Stop'",
        "$a = New-ScheduledTaskAction -Execute '" + exe + "' -Argument '-c \"import sys; sys.exit(7)\"'",
        "$t = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(2) "
        "-RepetitionInterval (New-TimeSpan -Hours 6) -RepetitionDuration (New-TimeSpan -Days 3650)",
        "$s = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew "
        "-ExecutionTimeLimit (New-TimeSpan -Hours 1) -Hidden",
        "$s.RestartCount = 2",
        "$p = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited",
        "Register-ScheduledTask -TaskName '" + name + "' -Action $a -Trigger $t -Settings $s -Principal $p | Out-Null",
        "Write-Output OK",
    ])
    assert "OK" in _ps(ps_reg)
    try:
        # registration + principal + trigger + settings + absolute path
        detail = _ps(
            "$t = Get-ScheduledTask -TaskName '" + name + "';"
            "$t.Actions[0].Execute;"
            "$t.Triggers[0].Repetition.Interval;"
            "$t.Settings.StartWhenAvailable;"
            "$t.Settings.MultipleInstances;"
            "$t.Settings.ExecutionTimeLimit;"
            "$t.Principal.LogonType;"
            "$t.Principal.RunLevel;"
            "$t.State"
        )
        lines = [l.strip() for l in detail.splitlines() if l.strip()]
        assert len(lines) == 8, lines
        assert Path(lines[0]).is_absolute() and lines[0].lower() == exe.lower(), lines
        assert lines[1] == "PT6H", lines
        assert lines[2] == "True", f"StartWhenAvailable off: {lines}"
        assert lines[3] == "IgnoreNew", lines
        assert lines[4] in ("PT1H", "01:00:00"), lines
        assert lines[5] == "Interactive" and lines[6] == "Limited", lines
        assert lines[7] == "Ready", lines

        # manual start -> correct exit/result reporting
        _ps("Start-ScheduledTask -TaskName '" + name + "'")
        deadline = time.monotonic() + 90
        last = None
        while time.monotonic() < deadline:
            last = _ps("(Get-ScheduledTaskInfo -TaskName '" + name + "').LastTaskResult").strip()
            if last not in ("267009", ""):  # 267009 = still running
                break
            time.sleep(2)
        assert last == "7", f"expected exit code 7, got {last!r}"
    finally:
        _ps("Unregister-ScheduledTask -TaskName '" + name + "' -Confirm:$false")
    assert "GONE" in _ps(
        "try { Get-ScheduledTask -TaskName '" + name + "' -ErrorAction Stop; 'PRESENT' } catch { 'GONE' }"
    )


def test_register_idempotent_no_duplicates():
    name = f"9Router AutoVault IT {uuid.uuid4().hex[:8]}"
    try:
        assert av.register_task(sys.executable, str(REPO_ROOT / "tools" / "autovault.py"), 6, task_name=name)
        assert av.register_task(sys.executable, str(REPO_ROOT / "tools" / "autovault.py"), 6, task_name=name)
        count = _ps("@(Get-ScheduledTask -TaskName '" + name + "' -ErrorAction SilentlyContinue).Count").strip()
        assert count in ("", "1"), f"duplicate tasks: {count!r}"
    finally:
        av.remove_task(task_name=name)
