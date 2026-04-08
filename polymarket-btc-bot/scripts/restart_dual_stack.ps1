# Stop listeners on common bot ports, then start one paper structural arb.
# Run from repo root or anywhere (script cd's to polymarket-btc-bot).

$ErrorActionPreference = "SilentlyContinue"
$ports = @(8765, 8767, 8780)
foreach ($port in $ports) {
    Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue |
        ForEach-Object {
            $procId = $_.OwningProcess
            if ($procId -and $procId -ne 0) {
                Write-Host "Stopping PID $procId (port $port)"
                Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue
            }
        }
}
Start-Sleep -Seconds 2

$botRoot = Split-Path -Parent $PSScriptRoot
Set-Location $botRoot

Write-Host ""
Write-Host "Starting single paper structural arb..."
Write-Host ""

& "$botRoot\.venv\Scripts\python.exe" "$botRoot\scripts\start_paper_split.py" @args
