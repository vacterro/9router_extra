param(
    [Parameter(Mandatory = $true)]
    [string]$SourceDir,

    [Parameter(Mandatory = $true)]
    [string]$BackupParent,

    [string]$BackupName = ("9router_backup_{0}" -f (Get-Date -Format "yyyy-MM-dd_HHmmss")),

    [ValidateRange(1, 20)]
    [int]$Keep = 3,

    [ValidateRange(1, 10240)]
    [int]$MaxTotalMiB = 512
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$sourceFull = [IO.Path]::GetFullPath($SourceDir)
$parentFull = [IO.Path]::GetFullPath($BackupParent)
$backupFull = [IO.Path]::GetFullPath((Join-Path $parentFull $BackupName))
$parentPrefix = $parentFull.TrimEnd([IO.Path]::DirectorySeparatorChar, [IO.Path]::AltDirectorySeparatorChar) + [IO.Path]::DirectorySeparatorChar

if (-not (Test-Path -LiteralPath $sourceFull -PathType Container)) {
    throw "9router data directory does not exist: $sourceFull"
}
if (-not $backupFull.StartsWith($parentPrefix, [StringComparison]::OrdinalIgnoreCase)) {
    throw "Backup target must stay inside its parent directory: $backupFull"
}
if (Test-Path -LiteralPath $backupFull) {
    throw "Backup target already exists: $backupFull"
}

New-Item -ItemType Directory -Path $backupFull -Force | Out-Null
$incompleteMarker = Join-Path $backupFull ".incomplete"
Set-Content -LiteralPath $incompleteMarker -Value "backup in progress" -Encoding utf8

$included = [Collections.Generic.List[string]]::new()
try {
    # Mutable state needed to restore providers, connections and local identity.
    foreach ($directoryName in @("db", "auth")) {
        $sourcePath = Join-Path $sourceFull $directoryName
        if (Test-Path -LiteralPath $sourcePath -PathType Container) {
            Copy-Item -LiteralPath $sourcePath -Destination $backupFull -Recurse -Force
            $included.Add($directoryName)
        }
    }

    foreach ($fileName in @("jwt-secret", "machine-id", "model-catalog.json", "model-catalog-raw.json")) {
        $sourcePath = Join-Path $sourceFull $fileName
        if (Test-Path -LiteralPath $sourcePath -PathType Leaf) {
            Copy-Item -LiteralPath $sourcePath -Destination $backupFull -Force
            $included.Add($fileName)
        }
    }

    [ordered]@{
        schemaVersion = 1
        createdAt = (Get-Date).ToUniversalTime().ToString("o")
        source = $sourceFull
        included = @($included)
        excludedRegenerable = @("source", "runtime", "logs", "*.tgz")
    } | ConvertTo-Json -Depth 3 | Set-Content -LiteralPath (Join-Path $backupFull "backup-manifest.json") -Encoding utf8

    Remove-Item -LiteralPath $incompleteMarker -Force

    # Retain only the newest completed backups. In-progress directories are
    # never candidates, and every deletion is verified to remain under parent.
    $completed = Get-ChildItem -LiteralPath $parentFull -Directory -Filter "9router_backup_*" -ErrorAction SilentlyContinue |
        Where-Object { -not (Test-Path -LiteralPath (Join-Path $_.FullName ".incomplete")) } |
        Sort-Object LastWriteTime -Descending
    foreach ($oldBackup in @($completed | Select-Object -Skip $Keep)) {
        $oldFull = [IO.Path]::GetFullPath($oldBackup.FullName)
        if (-not $oldFull.StartsWith($parentPrefix, [StringComparison]::OrdinalIgnoreCase)) {
            throw "Refusing to prune path outside backup parent: $oldFull"
        }
        if ($oldFull -eq $backupFull) {
            throw "Refusing to prune the backup created by this run: $oldFull"
        }
        Remove-Item -LiteralPath $oldFull -Recurse -Force
    }

    # A count limit alone does not clean up legacy multi-GiB backups. After a
    # valid new backup exists, also cap total completed-backup disk usage while
    # always preserving the newest completed backup.
    $retained = @(Get-ChildItem -LiteralPath $parentFull -Directory -Filter "9router_backup_*" -ErrorAction SilentlyContinue |
        Where-Object { -not (Test-Path -LiteralPath (Join-Path $_.FullName ".incomplete")) } |
        Sort-Object LastWriteTime -Descending)
    $sizes = @{}
    $totalBytes = 0L
    foreach ($item in $retained) {
        $itemBytes = (Get-ChildItem -LiteralPath $item.FullName -File -Recurse -Force -ErrorAction Stop | Measure-Object Length -Sum).Sum
        if ($null -eq $itemBytes) { $itemBytes = 0L }
        $sizes[$item.FullName] = [long]$itemBytes
        $totalBytes += [long]$itemBytes
    }
    $maxBytes = [long]$MaxTotalMiB * 1MB
    for ($index = $retained.Count - 1; $index -ge 1 -and $totalBytes -gt $maxBytes; $index--) {
        $oldFull = [IO.Path]::GetFullPath($retained[$index].FullName)
        if (-not $oldFull.StartsWith($parentPrefix, [StringComparison]::OrdinalIgnoreCase)) {
            throw "Refusing to prune path outside backup parent: $oldFull"
        }
        if ($oldFull -eq $backupFull) { continue }
        $totalBytes -= $sizes[$retained[$index].FullName]
        Remove-Item -LiteralPath $oldFull -Recurse -Force
    }

    Write-Output $backupFull
} catch {
    # Keep the marker so a later run will never mistake a partial copy for a
    # completed backup or delete it automatically.
    throw
}
