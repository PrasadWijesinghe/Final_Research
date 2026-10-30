# Starts everything PipelineIQ needs on this Windows computer, then checks that each part answers.
# Containers: Postgres, Redis, the Test expert, its worker and the LLM gateway.
# In the background: the API, the dashboard and the webhook relay. Logs and PIDs go to .run\.
# Safe to run twice: parts already running are left alone. Stop it all with scripts\stop.ps1.
# Usage: powershell -ExecutionPolicy Bypass -File scripts\start.ps1 [-Build]
# Values from .env are read here but never printed.
param([switch]$Build)
$ErrorActionPreference = "Continue"
Set-Location (Split-Path $PSScriptRoot -Parent)
$Run = Join-Path (Get-Location) ".run"
New-Item -ItemType Directory -Force $Run | Out-Null
$ApiUrl = "http://127.0.0.1:8000"
$DashUrl = "http://localhost:5173"
$script:Failed = $false

function Ok($t) { Write-Host "  [ok] $t" -ForegroundColor Green }
function Bad($t) { Write-Host "  [X]  $t" -ForegroundColor Red; $script:Failed = $true }
function Note($t) { Write-Host "  -    $t" }
function Step($t) { Write-Host "`n$t" -ForegroundColor White }
function EnvVal($key) {
    $line = Get-Content .env -ErrorAction SilentlyContinue | Where-Object { $_ -match "^$key=" } | Select-Object -Last 1
    if ($line) { ($line -replace "^$key=", "").Trim('"') } else { "" }
}
function Listening($port) { [bool](Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue) }
function Alive($part) {
    $f = Join-Path $Run "$part.pid"
    (Test-Path $f) -and [bool](Get-Process -Id (Get-Content $f) -ErrorAction SilentlyContinue)
}
# True once the URL answers (and contains $text, when given).
function WaitFor($url, $seconds, $text = "") {
    for ($i = 0; $i -lt $seconds; $i++) {
        try {
            $r = Invoke-WebRequest -UseBasicParsing -TimeoutSec 3 $url
            if (-not $text -or $r.Content -like "*$text*") { return $true }
        } catch {}
        Start-Sleep 1
    }
    return $false
}
# Start a program hidden in the background, with its output in .run\<part>.log, and remember its PID.
function StartBg($part, $exe, $argList, $dir = (Get-Location)) {
    $p = Start-Process -FilePath $exe -ArgumentList $argList -WorkingDirectory $dir -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $Run "$part.log") -RedirectStandardError (Join-Path $Run "$part.err.log")
    $p.Id | Set-Content (Join-Path $Run "$part.pid")
}
function RelayRunning { [bool](Get-CimInstance Win32_Process -Filter "Name='node.exe'" | Where-Object { $_.CommandLine -like "*smee-client --url*" }) }

Step "1. Checking this computer"
foreach ($tool in "docker", "uv", "node", "npm") {
    if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) { Bad "$tool is not installed" }
}
if ($script:Failed) { exit 1 }
docker info *> $null
if ($LASTEXITCODE -eq 0) { Ok "Docker is running" } else { Bad "Docker is not running: start Docker Desktop and try again"; exit 1 }
if (Test-Path .env) { Ok ".env found" } else { Bad ".env is missing (see docs\MOVING_MACHINES.md)"; exit 1 }
$key = EnvVal "GITHUB_APP_PRIVATE_KEY_PATH"
if ($key -and (Test-Path $key)) { Ok "GitHub App key found" } else { Note "GitHub App key not found at GITHUB_APP_PRIVATE_KEY_PATH: GitHub check runs and log downloads will fail" }
$smee = EnvVal "SMEE_URL"
if (-not $smee) { Note "SMEE_URL is not set in .env: the webhook relay will be skipped" }

Step "2. Starting the containers"
docker compose up -d --wait postgres redis *> (Join-Path $Run "compose.log")
if ($LASTEXITCODE -eq 0) { Ok "Postgres and Redis" } else { Bad "Postgres or Redis did not start (see .run\compose.log)"; exit 1 }
uv run --env-file .env alembic -c shared/db/alembic.ini upgrade head *> (Join-Path $Run "migrate.log")
$m1 = $LASTEXITCODE
uv run --env-file .env alembic -c experts/testing/alembic.ini upgrade head *>> (Join-Path $Run "migrate.log")
if ($m1 -eq 0 -and $LASTEXITCODE -eq 0) { Ok "Database tables up to date" } else { Bad "Database update failed (see .run\migrate.log)" }
$composeArgs = @("compose", "--profile", "experts", "up", "-d")
if ($Build) { $composeArgs += "--build" }
& docker @composeArgs testing-api testing-worker llm-gateway *>> (Join-Path $Run "compose.log")
if ($LASTEXITCODE -eq 0) { Ok "Test expert, worker and LLM gateway" } else { Bad "Expert containers did not start (see .run\compose.log)" }

Step "3. Starting the API, dashboard and relay"
if (Listening 8000) {
    Note "API: something is already running on port 8000, leaving it"
} else {
    StartBg "api" "uv" "run --env-file .env uvicorn api.app:app --host 127.0.0.1 --port 8000"
    Ok "API started (log: .run\api.log)"
}
if (Listening 5173) {
    Note "Dashboard: something is already running on port 5173, leaving it"
} else {
    if (-not (Test-Path services\dashboard\node_modules)) { Push-Location services\dashboard; npm install *> (Join-Path $Run "npm-install.log"); Pop-Location }
    StartBg "dashboard" "npm.cmd" "run dev -- --strictPort" (Resolve-Path services\dashboard)
    Ok "Dashboard started (log: .run\dashboard.log)"
}
if (-not $smee) {
    Note "Relay skipped (no SMEE_URL)"
} elseif ((Alive "relay") -or (RelayRunning)) {
    # Two relays would deliver every webhook twice.
    Note "Relay: one is already running, leaving it"
} else {
    StartBg "relay" "npx.cmd" "--yes smee-client --url $smee --target $ApiUrl/webhooks/github"
    Ok "Relay started (log: .run\relay.log)"
}

Step "4. Checking every part answers"
if (WaitFor "$ApiUrl/health" 40) { Ok "API          $ApiUrl" } else { Bad "API is not answering (see .run\api.log and .run\api.err.log)" }
if (WaitFor "http://127.0.0.1:8103/status" 60 "lightgbm") { Ok "Test expert  real model loaded" }
elseif (WaitFor "http://127.0.0.1:8103/status" 1) { Bad "Test expert answers but has no model (stub): is models\testing\ in place?" }
else { Bad "Test expert is not answering (docker compose logs testing-api)" }
if (WaitFor "http://127.0.0.1:8200/health" 40) {
    $provider = try { (Invoke-RestMethod -TimeoutSec 3 "http://127.0.0.1:8200/health").provider } catch { "unknown" }
    Ok "LLM gateway  provider $provider"
} else { Bad "LLM gateway is not answering (docker compose logs llm-gateway)" }
if (docker compose ps --status running -q testing-worker) { Ok "Worker       running" } else { Bad "Worker is not running (docker compose logs testing-worker)" }
if (WaitFor $DashUrl 40) { Ok "Dashboard    $DashUrl" } else { Bad "Dashboard is not answering (see .run\dashboard.log)" }
if ($smee) {
    if (Alive "relay") {
        $log = Join-Path $Run "relay.log"
        for ($i = 0; $i -lt 20 -and -not (Select-String -Quiet Connected $log -ErrorAction SilentlyContinue); $i++) { Start-Sleep 1 }
        if (Select-String -Quiet Connected $log -ErrorAction SilentlyContinue) { Ok "Relay        connected" } else { Bad "Relay did not connect (see .run\relay.log)" }
    } elseif (RelayRunning) { Ok "Relay        running (started elsewhere)" } else { Bad "Relay is not running" }
}

Write-Host ""
if ($script:Failed) { Write-Host "Some parts failed. Fix them and run scripts\start.ps1 again." -ForegroundColor Red; exit 1 }
Write-Host "All running. Open $DashUrl  -  Stop everything: scripts\stop.ps1" -ForegroundColor Green
