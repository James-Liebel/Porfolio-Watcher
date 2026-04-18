# Stop all listeners on common bot ports, kill stray python processes for this repo,
# then start exactly one LIVE structural arb (start_live_arb.py --yes).
# Run from repo root or anywhere (script cd's to polymarket-btc-bot).

$ErrorActionPreference = "SilentlyContinue"

Get-CimInstance Win32_Process |
    Where-Object {
        $_.Name -eq 'python.exe' -and
        $_.CommandLine -like '*polymarket-btc-bot*' -and (
            $_.CommandLine -like '* -m src*' -or
            $_.CommandLine -like '*start_live_arb*' -or
            $_.CommandLine -like '*start_paper_arb*'
        )
    } |
    ForEach-Object {
        Write-Host "Stopping stray bot PID $($_.ProcessId)"
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
    }

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
Write-Host "Starting single LIVE structural arb..."
Write-Host ""

& "$botRoot\.venv\Scripts\python.exe" "$botRoot\scripts\start_live_arb.py" --yes @args
