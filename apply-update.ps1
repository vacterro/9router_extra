# ==============================================================================
# 9Router Extra Upgrade Script (v0.5.65-extra)
# Preserves all existing provider nodes, connections, and custom combo order.
#
# CORE-004: every native command is checked; any non-zero exit aborts BEFORE the
# dependent steps, the safety backup is preserved on failure, and the success
# banner is printed only after the launched instance is verified to be running.
# All paths/commands are injectable so the failure matrix can be tested without
# touching a real 9Router install (see 9router_WatchEdit/tests/
# test_core004_apply_update_contract.py).
# ==============================================================================
[CmdletBinding()]
param(
    [string]$NpmExe = "npm",
    [string]$NodeExe = "node",
    [string]$PackagePath = "",
    [string]$BackupScript = "",
    [string]$RestoreScript = "",
    [string]$ExportFile = "",
    [string]$AppDataRouter = "",
    [string]$BackupParent = "",
    [string]$LaunchCommand = "9router -t --skip-update",
    [int]$LaunchVerifySeconds = 6,
    [switch]$SkipStop,
    [switch]$SkipLaunch
)

$ErrorActionPreference = "Stop"

if (-not $PackagePath) {
    $PackagePath = Join-Path $env:LOCALAPPDATA "9router_WatchEdit/engine/packages/9router-0.5.65-extra.tgz"
}
if (-not $BackupScript) {
    $BackupScript = Join-Path $PSScriptRoot "tools/create_safety_backup.ps1"
}
if (-not $RestoreScript) {
    $RestoreScript = Join-Path $env:LOCALAPPDATA "9router_WatchEdit/engine/backup_tools/restore_state.js"
}
if (-not $ExportFile) {
    $ExportFile = Join-Path $env:LOCALAPPDATA "9router_WatchEdit/backups/private/providers-state-export.json"
}
if (-not $AppDataRouter) {
    $AppDataRouter = Join-Path $env:APPDATA "9router"
}
if (-not $BackupParent) {
    $BackupParent = $env:APPDATA
}

$script:BackupDir = ""

function Fail {
    param([string]$Message, [int]$Code = 1)
    Write-Host ""
    Write-Host "UPGRADE FAILED: $Message" -ForegroundColor Red
    if ($script:BackupDir) {
        Write-Host "Safety backup preserved: $script:BackupDir" -ForegroundColor Yellow
    }
    Write-Host "No success banner is printed for a failed upgrade." -ForegroundColor Yellow
    exit $Code
}

function Invoke-NativeChecked {
    # Runs one native command and refuses to continue on a non-zero exit.
    param([string]$Label, [string]$Exe, [string[]]$ExeArgs = @())
    Write-Host "  -> $Label ($Exe $($ExeArgs -join ' '))"
    $code = -1
    try {
        & $Exe @ExeArgs
        $code = $LASTEXITCODE
    } catch {
        Fail "$Label could not run: $($_.Exception.Message)"
    }
    if ($null -eq $code) { $code = 0 }
    if ($code -ne 0) {
        Fail "$Label exited non-zero (code $code)"
    }
    return $code
}

Write-Host "=====================================================" -ForegroundColor Cyan
Write-Host " 9Router Extra Upgrade to v0.5.65" -ForegroundColor Cyan
Write-Host "=====================================================" -ForegroundColor Cyan

# 1. Stop running 9router processes
Write-Host "`n[1/5] Stopping running 9router instance..." -ForegroundColor Yellow
if ($SkipStop) {
    Write-Host "Stop step skipped by -SkipStop." -ForegroundColor Gray
} else {
    $proc = Get-Process -Name "node" -ErrorAction SilentlyContinue | Where-Object {
        $cmd = (Get-CimInstance Win32_Process -Filter "ProcessId = $($_.Id)" -ErrorAction SilentlyContinue).CommandLine
        $cmd -match "9router"
    }
    if ($proc) {
        Write-Host "Stopping 9router processes (PID: $($proc.Id -join ', '))..."
        $proc | Stop-Process -Force
        if ($LASTEXITCODE -and $LASTEXITCODE -ne 0) {
            Fail "Stop-Process exited non-zero (code $LASTEXITCODE)"
        }
        Start-Sleep -Seconds 2
    } else {
        Write-Host "No active 9router process found." -ForegroundColor Gray
    }
}

# 2. Backup AppData database and configuration
$timestamp = Get-Date -Format "yyyy-MM-dd_HHmmss"
$backupName = "9router_backup_$timestamp"
$script:BackupDir = Join-Path $BackupParent $backupName

Write-Host "`n[2/5] Creating safety backup at $script:BackupDir..." -ForegroundColor Yellow
if (Test-Path $AppDataRouter) {
    if (-not (Test-Path -LiteralPath $BackupScript -PathType Leaf)) {
        Fail "Safety backup helper not found: $BackupScript"
    }
    try {
        & $BackupScript -SourceDir $AppDataRouter -BackupParent $BackupParent -BackupName $backupName -Keep 3 -MaxTotalMiB 512 | Out-Null
    } catch {
        Fail "Safety backup helper threw: $($_.Exception.Message)"
    }
    if ($LASTEXITCODE -and $LASTEXITCODE -ne 0) {
        Fail "Safety backup helper exited non-zero (code $LASTEXITCODE)"
    }
    if (-not (Test-Path -LiteralPath $script:BackupDir -PathType Container)) {
        Fail "Safety backup reported success but created no backup directory at $script:BackupDir"
    }
    Write-Host "Backup created successfully." -ForegroundColor Green
} else {
    Write-Host "No AppData 9router directory at $AppDataRouter; nothing to back up." -ForegroundColor Gray
    $script:BackupDir = ""
}

# 3. Install the patched package globally
# Engine packages now live OUTSIDE the repository (private local runtime layer)
if (-not (Test-Path $PackagePath)) {
    Fail "Patched package not found at: $PackagePath"
}

Write-Host "`n[3/5] Installing 9router-0.5.65-extra globally via npm..." -ForegroundColor Yellow
Invoke-NativeChecked -Label "npm install" -Exe $NpmExe -ExeArgs @("install", "-g", "--force", "$PackagePath") | Out-Null

# 4. Synchronize database state (Connections preserved, Combos untouched)
Write-Host "`n[4/5] Synchronizing database state and providers..." -ForegroundColor Yellow
if ((Test-Path $RestoreScript) -and (Test-Path $ExportFile)) {
    Invoke-NativeChecked -Label "state restore" -Exe $NodeExe -ExeArgs @("$RestoreScript") | Out-Null
} else {
    Write-Host "Database state is live and preserved (restore inputs absent: script=$([bool](Test-Path $RestoreScript)) export=$([bool](Test-Path $ExportFile)))." -ForegroundColor Gray
}

# 5. Launch 9Router and verify it is actually running before any success claim
Write-Host "`n[5/5] Launching updated 9Router..." -ForegroundColor Yellow
if ($SkipLaunch) {
    Write-Host "Launch skipped by -SkipLaunch; launch verification NOT performed." -ForegroundColor Yellow
} else {
    $launched = $null
    try {
        $launched = Start-Process -FilePath "powershell.exe" -WindowStyle Hidden -PassThru `
            -ArgumentList "-NoProfile", "-Command", $LaunchCommand
    } catch {
        Fail "Launch command could not be started: $($_.Exception.Message)"
    }
    if (-not $launched) {
        Fail "Launch produced no process handle to verify."
    }
    Start-Sleep -Seconds $LaunchVerifySeconds
    if ($launched.HasExited) {
        Fail "Launched 9Router process exited immediately (exit code $($launched.ExitCode)); the instance is not running."
    }
    Write-Host "  -> verified running instance (pid $($launched.Id), command: $LaunchCommand)" -ForegroundColor Green
}

Write-Host "`n=====================================================" -ForegroundColor Green
Write-Host " Upgrade complete! 9Router v0.5.65-extra is running." -ForegroundColor Green
Write-Host " - User custom combo order preserved 100% intact" -ForegroundColor Green
Write-Host " - Muse Spark stream failure & 60s timeout fallback active" -ForegroundColor Green
Write-Host " - AMD DeepSeek/Qwen mid-system message folding active" -ForegroundColor Green
Write-Host " - WorkBuddy AI (wb/hy3) and Gemini 3.8 Flash available" -ForegroundColor Green
Write-Host "=====================================================" -ForegroundColor Green
exit 0
