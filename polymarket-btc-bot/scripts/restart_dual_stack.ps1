# Stop listeners on agent + advisor ports, then start two $100 traders + LLM advisor.
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
Write-Host "Starting dual agents + advisor (Ollama on 11434 is left running)..."
Write-Host ""

& "$botRoot\.venv\Scripts\python.exe" "$botRoot\scripts\run_two_structural_agents.py" @args
