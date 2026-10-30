# Stops everything scripts\start.ps1 started: the relay, dashboard and API, then the containers.
# Data is kept (containers are stopped, not removed).
# Usage: powershell -ExecutionPolicy Bypass -File scripts\stop.ps1
Set-Location (Split-Path $PSScriptRoot -Parent)
$Run = Join-Path (Get-Location) ".run"

foreach ($part in "relay", "dashboard", "api") {
    $f = Join-Path $Run "$part.pid"
    $procId = if (Test-Path $f) { Get-Content $f } else { $null }
    if ($procId -and (Get-Process -Id $procId -ErrorAction SilentlyContinue)) {
        # /T stops everything it started too (npm and uv start child processes).
        taskkill /PID $procId /T /F *> $null
        Write-Host "  stopped $part"
    } else {
        Write-Host "  $part was not started by scripts\start.ps1 (if it runs in a window, press Ctrl+C there)"
    }
    Remove-Item $f -ErrorAction SilentlyContinue
}

docker compose --profile experts --profile e2e stop *> $null
if ($LASTEXITCODE -eq 0) { Write-Host "  stopped the containers (data kept)" }
