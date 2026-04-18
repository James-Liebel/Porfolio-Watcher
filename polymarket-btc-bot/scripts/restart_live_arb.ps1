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

$py = Join-Path $botRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) {
    Write-Host ('[X] Missing ' + $py + ' - create venv or fix path.')
    exit 1
}

Write-Host ""
Write-Host "Opening a new window for the live bot (gates + child process)..."
Write-Host ""

# New window so logs stay visible; avoids the launcher looking like it 'did nothing' when run from a script host.
$tail = ''
if ($args.Count -gt 0) {
    $tail = ' ' + ($args -join ' ')
}
$innerCmd = "Set-Location -LiteralPath '$botRoot'; & '$py' '$botRoot\scripts\start_live_arb.py' --yes$tail"
$argList = @(
    '-NoExit'
    '-NoProfile'
    '-ExecutionPolicy', 'Bypass'
    '-Command'
    $innerCmd
)
Start-Process -FilePath "powershell.exe" -WorkingDirectory $botRoot -ArgumentList $argList
Write-Host '[OK] Launched. Watch the new PowerShell window for [OK] Started live arb / gate errors.'
