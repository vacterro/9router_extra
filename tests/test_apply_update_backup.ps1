$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$helper = Join-Path (Split-Path -Parent $PSScriptRoot) "tools/create_safety_backup.ps1"
$testRoot = Join-Path ([IO.Path]::GetFullPath($env:TEMP)) ("9router-safety-backup-test_{0}" -f [guid]::NewGuid())
$source = Join-Path $testRoot "9router"
$backupParent = Join-Path $testRoot "backups"

function Assert-True([bool]$Condition, [string]$Message) {
    if (-not $Condition) { throw "ASSERTION FAILED: $Message" }
}

try {
    foreach ($path in @(
        (Join-Path $source "db"),
        (Join-Path $source "auth"),
        (Join-Path $source "source/.next"),
        (Join-Path $source "source/.next-cli-build"),
        (Join-Path $source "source/node_modules"),
        (Join-Path $source "runtime/node_modules"),
        (Join-Path $source "logs"),
        $backupParent
    )) { New-Item -ItemType Directory -Path $path -Force | Out-Null }

    Set-Content -LiteralPath (Join-Path $source "db/data.sqlite") -Value "database"
    Set-Content -LiteralPath (Join-Path $source "auth/cli-secret") -Value "secret"
    Set-Content -LiteralPath (Join-Path $source "jwt-secret") -Value "jwt"
    Set-Content -LiteralPath (Join-Path $source "machine-id") -Value "machine"
    Set-Content -LiteralPath (Join-Path $source "model-catalog.json") -Value "{}"
    Set-Content -LiteralPath (Join-Path $source "model-catalog-raw.json") -Value "{}"
    Set-Content -LiteralPath (Join-Path $source "source/.next/cache.bin") -Value "regenerable"
    Set-Content -LiteralPath (Join-Path $source "source/.next-cli-build/build.bin") -Value "regenerable"
    Set-Content -LiteralPath (Join-Path $source "source/node_modules/package.bin") -Value "regenerable"
    Set-Content -LiteralPath (Join-Path $source "runtime/node_modules/package.bin") -Value "regenerable"
    Set-Content -LiteralPath (Join-Path $source "logs/router.log") -Value "regenerable"
    Set-Content -LiteralPath (Join-Path $source "9router-old.tgz") -Value "regenerable"

    $oldest = Join-Path $backupParent "9router_backup_2026-01-01_000000"
    $newer = Join-Path $backupParent "9router_backup_2026-01-02_000000"
    $incomplete = Join-Path $backupParent "9router_backup_2026-01-03_000000"
    foreach ($path in @($oldest, $newer, $incomplete)) { New-Item -ItemType Directory -Path $path -Force | Out-Null }
    [IO.File]::WriteAllBytes((Join-Path $oldest "legacy.bin"), [byte[]]::new(700KB))
    [IO.File]::WriteAllBytes((Join-Path $newer "legacy.bin"), [byte[]]::new(700KB))
    Set-Content -LiteralPath (Join-Path $incomplete ".incomplete") -Value "partial"
    (Get-Item -LiteralPath $oldest).LastWriteTime = [datetime]"2026-01-01"
    (Get-Item -LiteralPath $newer).LastWriteTime = [datetime]"2026-01-02"

    $created = & $helper -SourceDir $source -BackupParent $backupParent -BackupName "9router_backup_2026-01-04_000000" -Keep 3 -MaxTotalMiB 1

    Assert-True (Test-Path -LiteralPath (Join-Path $created "db/data.sqlite")) "database was not copied"
    Assert-True (Test-Path -LiteralPath (Join-Path $created "auth/cli-secret")) "auth was not copied"
    Assert-True (Test-Path -LiteralPath (Join-Path $created "jwt-secret")) "jwt-secret was not copied"
    Assert-True (-not (Test-Path -LiteralPath (Join-Path $created "source"))) "source build tree was copied"
    Assert-True (-not (Test-Path -LiteralPath (Join-Path $created "runtime"))) "runtime dependencies were copied"
    Assert-True (-not (Test-Path -LiteralPath (Join-Path $created "logs"))) "logs were copied"
    Assert-True (-not (Test-Path -LiteralPath (Join-Path $created "9router-old.tgz"))) "package archive was copied"
    Assert-True (Test-Path -LiteralPath (Join-Path $created "backup-manifest.json")) "manifest is missing"
    Assert-True (-not (Test-Path -LiteralPath (Join-Path $created ".incomplete"))) "completed backup kept incomplete marker"
    Assert-True (-not (Test-Path -LiteralPath $oldest)) "oldest completed backup was not pruned by total-size budget"
    Assert-True (Test-Path -LiteralPath $newer) "newer retained backup was pruned"
    Assert-True (Test-Path -LiteralPath $incomplete) "incomplete backup was pruned"
    Assert-True (Test-Path -LiteralPath $created) "new backup was pruned"

    $completedCount = @(Get-ChildItem -LiteralPath $backupParent -Directory -Filter "9router_backup_*" |
        Where-Object { -not (Test-Path -LiteralPath (Join-Path $_.FullName ".incomplete")) }).Count
    Assert-True ($completedCount -eq 2) "retention should keep exactly two completed backups"
    Write-Host "PASS: safety backup copies mutable state only and keeps bounded completed backups"
} finally {
    $tempFull = [IO.Path]::GetFullPath($env:TEMP).TrimEnd([IO.Path]::DirectorySeparatorChar) + [IO.Path]::DirectorySeparatorChar
    $testFull = [IO.Path]::GetFullPath($testRoot)
    if ($testFull.StartsWith($tempFull, [StringComparison]::OrdinalIgnoreCase) -and (Test-Path -LiteralPath $testFull)) {
        Remove-Item -LiteralPath $testFull -Recurse -Force
    }
}
