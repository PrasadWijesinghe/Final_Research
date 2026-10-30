#!/usr/bin/env bash
# Starts everything PipelineIQ needs on this computer, then checks that each part answers.
# Containers: Postgres, Redis, the Test expert, its worker and the LLM gateway.
# In the background: the API, the dashboard and the webhook relay. Logs and PIDs go to .run/.
# Safe to run twice: parts already running are left alone. Stop it all with scripts/stop.sh.
# Usage: scripts/start.sh [--build]   (--build rebuilds the expert images first)
# Values from .env are read here but never printed.
set -uo pipefail

cd "$(dirname "$0")/.."
RUN=.run
mkdir -p "$RUN"
API_URL=http://127.0.0.1:8000
DASH_URL=http://localhost:5173
build=""
[[ "${1:-}" == "--build" ]] && build="--build"
failed=0

ok() { printf '  \033[32m✔\033[0m %s\n' "$1"; }
bad() { printf '  \033[31m✘\033[0m %s\n' "$1"; failed=1; }
note() { printf '  - %s\n' "$1"; }
step() { printf '\n\033[1m%s\033[0m\n' "$1"; }
envval() { grep -E "^$1=" .env 2>/dev/null | tail -1 | cut -d= -f2- | sed -e 's/^"//' -e 's/"$//'; }
listening() { lsof -nP -iTCP:"$1" -sTCP:LISTEN >/dev/null 2>&1; }
alive() { [[ -f "$RUN/$1.pid" ]] && kill -0 "$(cat "$RUN/$1.pid")" 2>/dev/null; }
# wait_for URL SECONDS [TEXT]: true once URL answers (and contains TEXT, when given).
wait_for() {
  local i body
  for ((i = 0; i < $2; i++)); do
    if body=$(curl -fsS --max-time 3 "$1" 2>/dev/null) && [[ -z "${3:-}" || "$body" == *"$3"* ]]; then
      return 0
    fi
    sleep 1
  done
  return 1
}

step "1. Checking this computer"
for tool in docker uv node npm npx curl lsof; do
  command -v "$tool" >/dev/null || { bad "$tool is not installed"; }
done
((failed)) && exit 1
docker info >/dev/null 2>&1 && ok "Docker is running" || { bad "Docker is not running: start Docker Desktop and try again"; exit 1; }
[[ -f .env ]] && ok ".env found" || { bad ".env is missing (see docs/MOVING_MACHINES.md)"; exit 1; }
key=$(envval GITHUB_APP_PRIVATE_KEY_PATH)
key="${key/#\~/$HOME}"
if [[ -n "$key" && -f "$key" ]]; then ok "GitHub App key found"; else note "GitHub App key not found at GITHUB_APP_PRIVATE_KEY_PATH: GitHub check runs and log downloads will fail"; fi
smee=$(envval SMEE_URL)
[[ -n "$smee" ]] || note "SMEE_URL is not set in .env: the webhook relay will be skipped"

step "2. Starting the containers"
if docker compose up -d --wait postgres redis >"$RUN/compose.log" 2>&1; then ok "Postgres and Redis"; else bad "Postgres or Redis did not start (see $RUN/compose.log)"; exit 1; fi
if uv run --env-file .env alembic -c shared/db/alembic.ini upgrade head >"$RUN/migrate.log" 2>&1 &&
  uv run --env-file .env alembic -c experts/testing/alembic.ini upgrade head >>"$RUN/migrate.log" 2>&1; then
  ok "Database tables up to date"
else
  bad "Database update failed (see $RUN/migrate.log)"
fi
# shellcheck disable=SC2086
if docker compose --profile experts up -d $build testing-api testing-worker llm-gateway >>"$RUN/compose.log" 2>&1; then
  ok "Test expert, worker and LLM gateway"
else
  bad "Expert containers did not start (see $RUN/compose.log)"
fi

step "3. Starting the API, dashboard and relay"
if listening 8000; then
  note "API: something is already running on port 8000, leaving it"
else
  nohup uv run --env-file .env uvicorn api.app:app --host 127.0.0.1 --port 8000 >"$RUN/api.log" 2>&1 &
  echo $! >"$RUN/api.pid"
  ok "API started (log: $RUN/api.log)"
fi
if listening 5173; then
  note "Dashboard: something is already running on port 5173, leaving it"
else
  [[ -d services/dashboard/node_modules ]] || (cd services/dashboard && npm install >"../../$RUN/npm-install.log" 2>&1)
  (cd services/dashboard && exec nohup npm run dev -- --strictPort) >"$RUN/dashboard.log" 2>&1 &
  echo $! >"$RUN/dashboard.pid"
  ok "Dashboard started (log: $RUN/dashboard.log)"
fi
if [[ -z "$smee" ]]; then
  note "Relay skipped (no SMEE_URL)"
elif alive relay || pgrep -f 'smee-client --url' >/dev/null; then
  # Two relays would deliver every webhook twice.
  note "Relay: one is already running, leaving it"
else
  nohup npx --yes smee-client --url "$smee" --target "$API_URL/webhooks/github" >"$RUN/relay.log" 2>&1 &
  echo $! >"$RUN/relay.pid"
  ok "Relay started (log: $RUN/relay.log)"
fi

step "4. Checking every part answers"
wait_for "$API_URL/health" 40 && ok "API          $API_URL" || bad "API is not answering (see $RUN/api.log)"
if wait_for http://127.0.0.1:8103/status 60 lightgbm; then
  ok "Test expert  real model loaded"
elif wait_for http://127.0.0.1:8103/status 1; then
  bad "Test expert answers but has no model (stub): is models/testing/ in place?"
else
  bad "Test expert is not answering (docker compose logs testing-api)"
fi
if wait_for http://127.0.0.1:8200/health 40; then
  provider=$(curl -fsS --max-time 3 http://127.0.0.1:8200/health | grep -oE '"provider" *: *"[a-z]+"' | grep -oE '"[a-z]+"$' | tr -d '"')
  ok "LLM gateway  provider ${provider:-unknown}"
else
  bad "LLM gateway is not answering (docker compose logs llm-gateway)"
fi
[[ "$(docker compose ps --status running -q testing-worker 2>/dev/null)" ]] && ok "Worker       running" || bad "Worker is not running (docker compose logs testing-worker)"
wait_for "$DASH_URL" 40 && ok "Dashboard    $DASH_URL" || bad "Dashboard is not answering (see $RUN/dashboard.log)"
if [[ -n "$smee" ]]; then
  if alive relay; then
    for ((i = 0; i < 20; i++)); do grep -q Connected "$RUN/relay.log" 2>/dev/null && break; sleep 1; done
    grep -q Connected "$RUN/relay.log" 2>/dev/null && ok "Relay        connected" || bad "Relay did not connect (see $RUN/relay.log)"
  else
    pgrep -f 'smee-client --url' >/dev/null && ok "Relay        running (started elsewhere)" || bad "Relay is not running"
  fi
fi

echo
if ((failed)); then
  printf '\033[31mSome parts failed.\033[0m Fix them and run scripts/start.sh again.\n'
  exit 1
fi
printf '\033[32mAll running.\033[0m Open %s  ·  Stop everything: scripts/stop.sh\n' "$DASH_URL"
