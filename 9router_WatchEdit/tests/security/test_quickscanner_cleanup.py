"""Focused source contracts for the WPF scanner; no GUI or provider access."""
import re
from pathlib import Path

import pytest


SOURCE = (Path(__file__).resolve().parents[3] / "9RouterQuickScanner.ps1").read_text(encoding="utf-8-sig")


def function(name):
    match = re.search(rf"(?ms)^function {re.escape(name)}\b.*?^\}}", SOURCE)
    assert match, name
    return match.group()


def event(owner, name):
    match = re.search(rf"(?ms)^\${owner}\.Add_{name}\(\{{.*?^\}}\)", SOURCE)
    assert match, (owner, name)
    return match.group()


def test_cleanup_is_central_and_silent():
    cleanup = function("Clear-ScanSecretState")
    assert '$script:ScanApiKey = ""' in cleanup
    assert "$KeyBox.Clear()" in cleanup
    assert not re.search(r"Write-|Set-Status|Out-File|Add-Content|Set-Content", cleanup)
    # Only initialization and the central helper may blank the process key.
    assert SOURCE.count('$script:ScanApiKey = ""') == 2
    assert SOURCE.count("$KeyBox.Clear()") == 1
    assert not re.search(r"\$KeyBox\.Password\s*=", SOURCE)


def test_stop_always_disposes_before_clearing_even_when_idle():
    stop = function("Stop-CurrentScan")
    assert "return" not in stop
    assert stop.index("Dispose-CatalogTask") < stop.index("Clear-ScanSecretState")
    assert stop.index("Dispose-ProbeTasks") < stop.index("Clear-ScanSecretState")
    # No terminal scan path can merely flip the flag and forget the helper.
    assert SOURCE.count("$script:Scanning = $false") == 2  # initialization + STOP
    for name in ("Dispose-CatalogTask", "Dispose-ProbeTasks"):
        dispose = function(name)
        assert dispose.index(".Stop()") < dispose.index(".Dispose()")


@pytest.mark.parametrize("terminal_message", [
    "CATALOG ERROR  |  ",
    "CATALOG ERROR  |  Provider returned no catalog result.",
    "CATALOG ERROR  |  HTTP $statusCode  |  $detail",
    "DONE  |  Catalog contains no likely text models.",
])
def test_catalog_terminal_paths(terminal_message):
    body = function("Process-CatalogCompletion")
    # Each terminal branch must call STOP before reporting/returning.
    position = body.index('"' + terminal_message)
    branch = body[:position].rsplit("{", 1)[-1]
    assert "Stop-CurrentScan" in branch
    assert "return" in body[position:].split("}", 1)[0]


def test_no_models_and_normal_completion():
    pool = function("Start-ProbePool")
    empty = pool.split("if ($script:TotalModels -eq 0) {", 1)[1].split("}", 1)[0]
    assert empty.index("Stop-CurrentScan") < empty.index("return")
    completion = function("Process-ProbeCompletions")
    assert re.search(r"\$script:Tasks.Count -eq 0\s*\)\s*\{\s*Stop-CurrentScan", completion)
    # In-progress probes, including individual failed probes, retain the key
    # until the whole batch finishes or is explicitly stopped.
    assert completion.count("Stop-CurrentScan") == 1


def test_partial_startup_and_unexpected_scan_failures():
    pool = function("Start-ProbePool")
    assert pool.index("$script:Tasks.Add($task)") < pool.index("$ps.AddArgument($script:ScanApiKey)")
    assert pool.index("$script:Tasks.Add($task)") < pool.index("$ps.BeginInvoke()")
    start = function("Start-CatalogScan")
    assert re.search(r"try\s*\{\s*Start-CatalogScanCore\s*\}\s*catch\s*\{\s*Stop-CurrentScan", start)
    tick = event("timer", "Tick")
    assert "Process-CatalogCompletion" in tick and "Process-ProbeCompletions" in tick
    assert re.search(r"catch\s*\{\s*Stop-CurrentScan", tick)


def test_stop_and_shutdown_events():
    assert "Stop-CurrentScan -UserRequested $true" in event("StopButton", "Click")
    assert "Stop-CurrentScan" in event("CloseButton", "Click")
    assert "Stop-CurrentScan" in event("window", "Closing")
    assert re.search(r"finally\s*\{\s*Stop-CurrentScan\s*\$timer.Stop\(\)", SOURCE)


def test_remote_provider_support_remains():
    resolver = function("Resolve-ApiBase")
    assert "loopback" not in resolver.lower()
    assert '"https://$value"' in resolver
    assert "$ps.AddArgument($script:ScanApiKey)" in function("Start-ProbePool")
    assert "$script:CatalogPowerShell.AddArgument($script:ScanApiKey)" in function("Start-CatalogScanCore")
