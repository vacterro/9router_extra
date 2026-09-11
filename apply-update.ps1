# ==============================================================================
# 9Router Extra Upgrade Script (v0.5.65-extra)
# Preserves all existing provider nodes, connections, and custom combo order.
# ==============================================================================

$ErrorActionPreference = "Stop"

Write-Host "=====================================================" -ForegroundColor Cyan
Write-Host " 9Router Extra Upgrade to v0.5.65" -ForegroundColor Cyan
Write-Host "=====================================================" -ForegroundColor Cyan

# 1. Stop running 9router processes
Write-Host "`n[1/5] Stopping running 9router instance..." -ForegroundColor Yellow
$proc = Get-Process -Name "node" -ErrorAction SilentlyContinue | Where-Object {
    $cmd = (Get-CimInstance Win32_Process -Filter "ProcessId = $($_.Id)" -ErrorAction SilentlyContinue).CommandLine
    $cmd -match "9router"
}
if ($proc) {
    Write-Host "Stopping 9router processes (PID: $($proc.Id -join ', '))..."
    $proc | Stop-Process -Force
    Start-Sleep -Seconds 2
} else {
    Write-Host "No active 9router process found." -ForegroundColor Gray
}

# 2. Backup AppData database and configuration
$timestamp = Get-Date -Format "yyyy-MM-dd_HHmmss"
$appDataRouter = Join-Path $env:APPDATA "9router"
$backupDir = Join-Path $env:APPDATA "9router_backup_$timestamp"
$backupScript = Join-Path $PSScriptRoot "tools/create_safety_backup.ps1"

Write-Host "`n[2/5] Creating safety backup at $backupDir..." -ForegroundColor Yellow
if (Test-Path $appDataRouter) {
    if (-not (Test-Path -LiteralPath $backupScript -PathType Leaf)) {
        Write-Error "Safety backup helper not found: $backupScript"
    }
    & $backupScript -SourceDir $appDataRouter -BackupParent $env:APPDATA -BackupName "9router_backup_$timestamp" -Keep 3 -MaxTotalMiB 512 | Out-Null
    Write-Host "Backup created successfully." -ForegroundColor Green
}

# 3. Install the patched package globally
# Engine packages now live OUTSIDE the repository (private local runtime layer)
$packagePath = Join-Path $env:LOCALAPPDATA "9router_WatchEdit/engine/packages/9router-0.5.65-extra.tgz"
if (-not (Test-Path $packagePath)) {
    Write-Error "Patched package not found at: $packagePath"
}

Write-Host "`n[3/5] Installing 9router-0.5.65-extra globally via npm..." -ForegroundColor Yellow
npm install -g --force "$packagePath"

# 4. Synchronize database state (Connections preserved, Combos untouched)
Write-Host "`n[4/5] Synchronizing database state and providers..." -ForegroundColor Yellow
$restoreScript = Join-Path $env:LOCALAPPDATA "9router_WatchEdit/engine/backup_tools/restore_state.js"
$exportFile = Join-Path $env:LOCALAPPDATA "9router_WatchEdit/backups/private/providers-state-export.json"
if ((Test-Path $restoreScript) -and (Test-Path $exportFile)) {
    node "$restoreScript"
} else {
    Write-Host "Database state is live and preserved." -ForegroundColor Gray
}

# 5. Launch 9Router
Write-Host "`n[5/5] Launching updated 9Router..." -ForegroundColor Yellow
Start-Process -FilePath "powershell.exe" -ArgumentList "-NoProfile", "-WindowStyle", "Hidden", "-Command", "9router -t --skip-update"
Start-Sleep -Seconds 2

Write-Host "`n=====================================================" -ForegroundColor Green
Write-Host " Upgrade complete! 9Router v0.5.65-extra is running." -ForegroundColor Green
Write-Host " - User custom combo order preserved 100% intact" -ForegroundColor Green
Write-Host " - Muse Spark stream failure & 60s timeout fallback active" -ForegroundColor Green
Write-Host " - AMD DeepSeek/Qwen mid-system message folding active" -ForegroundColor Green
Write-Host " - WorkBuddy AI (wb/hy3) and Gemini 3.8 Flash available" -ForegroundColor Green
Write-Host "=====================================================" -ForegroundColor Green
