# Sets up PipelineIQ on a Windows computer from the folder made by tools/move/pack.sh.
# Run it from inside the extracted pipelineiq-move folder:
#   powershell -ExecutionPolicy Bypass -File .\restore.ps1
# Options: -RepoPath C:\path\to\Final_Research   -Branch main
param(
    [string]$RepoPath = "$HOME\projects\Final_Research",
    [string]$Branch = "feat/dashboard"
)
$ErrorActionPreference = "Stop"
$Here = $PSScriptRoot

function Step($text) { Write-Host "`n== $text" -ForegroundColor Cyan }
function Need($cmd, $hint) {
    if (-not (Get-Command $cmd -ErrorAction SilentlyContinue)) { throw "$cmd is missing. $hint Then run this script again." }
}

Step "Checking the tools you need"
Need git "Install Git for Windows: https://git-scm.com/download/win"
Need docker "Install Docker Desktop: https://www.docker.com/products/docker-desktop/"
Need uv "Install uv: https://docs.astral.sh/uv/getting-started/installation/"
Need node "Install Node.js 22 or newer: https://nodejs.org/"
docker info *> $null
if ($LASTEXITCODE -ne 0) { throw "Docker Desktop is not running. Start it, wait until it says Running, then run this script again." }

Step "Getting the code into $RepoPath"
if (-not (Test-Path "$RepoPath\.git")) {
    # Keep Linux line endings: the Docker images run shell scripts from the repo.
    git clone -c core.autocrlf=input https://github.com/PrasadWijesinghe/Final_Research.git $RepoPath
}
git -C $RepoPath checkout $Branch
if ($LASTEXITCODE -ne 0) { throw "Could not switch to branch $Branch." }

Step "Putting the settings, key and models in place"
$PiqHome = Join-Path $HOME ".pipelineiq"
New-Item -ItemType Directory -Force $PiqHome | Out-Null
Copy-Item "$Here\pipelineiq-home\*" $PiqHome -Force
# Only you may read the key and the hook config.
icacls $PiqHome /inheritance:r /grant:r "$($env:USERNAME):(OI)(CI)F" | Out-Null

$EnvFile = Join-Path $RepoPath ".env"
if (Test-Path $EnvFile) { Copy-Item $EnvFile "$EnvFile.bak" -Force; Write-Host "Old .env kept as .env.bak" }
# The key path in .env points at the old computer; point it at the key on this one.
$Pem = (Join-Path $PiqHome "github-app.pem") -replace '\\', '/'
$lines = Get-Content "$Here\dot-env" | ForEach-Object {
    if ($_ -match '^GITHUB_APP_PRIVATE_KEY_PATH=') { "GITHUB_APP_PRIVATE_KEY_PATH=$Pem" } else { $_ }
}
[IO.File]::WriteAllText($EnvFile, ($lines -join "`n") + "`n")  # no BOM, Linux line endings

$Models = Join-Path $RepoPath "models"
New-Item -ItemType Directory -Force $Models | Out-Null
Copy-Item "$Here\models\*" $Models -Recurse -Force

Push-Location $RepoPath
try {
    Step "Starting the database and loading your saved copy"
    docker compose up -d --wait postgres redis
    if ($LASTEXITCODE -ne 0) { throw "The database did not start. See: docker compose logs postgres" }
    docker compose cp "$Here\pipelineiq.dump" postgres:/tmp/pipelineiq.dump
    docker compose exec -T postgres sh -c 'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists --no-owner /tmp/pipelineiq.dump'
    # pg_restore also exits non-zero on harmless warnings, so this only warns.
    if ($LASTEXITCODE -ne 0) { Write-Warning "pg_restore reported warnings; check the dashboard still shows your failures." }
    docker compose exec -T postgres rm -f /tmp/pipelineiq.dump

    Step "Installing packages"
    uv sync --all-packages  # no embedding model: the Docker worker does not use one
    Push-Location services\dashboard
    npm install
    Pop-Location

    Step "Building and starting the test expert and the LLM gateway"
    docker compose --profile experts up -d --build testing-api testing-worker llm-gateway
    if ($LASTEXITCODE -ne 0) { throw "The expert containers did not start. See: docker compose logs testing-api" }
}
finally { Pop-Location }

Step "Done"
Write-Host @"
Start the API and the dashboard, each in its own terminal, from $RepoPath :
  uv run --env-file .env uvicorn api.app:app --host 127.0.0.1 --port 8000
  cd services\dashboard; npm run dev
For webhooks from GitHub (own terminal):
  npx --yes smee-client --url <SMEE_URL from .env> --target http://127.0.0.1:8000/webhooks/github
Check: http://127.0.0.1:8103/status shows lightgbm-v4, and http://localhost:5173 shows your failures.
You can now delete the pipelineiq-move folder and the zip; they contain secrets.
"@
