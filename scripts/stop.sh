#!/usr/bin/env bash
# Stops everything scripts/start.sh started: the relay, dashboard and API, then the containers.
# Data is kept (containers are stopped, not removed). Usage: scripts/stop.sh
set -uo pipefail

cd "$(dirname "$0")/.."
RUN=.run

# Stop a process and everything it started (npm and uv start child processes).
kill_tree() {
  local child
  for child in $(pgrep -P "$1" 2>/dev/null); do kill_tree "$child"; done
  kill "$1" 2>/dev/null
}

for part in relay dashboard api; do
  pidfile="$RUN/$part.pid"
  if [[ -f "$pidfile" ]] && kill -0 "$(cat "$pidfile")" 2>/dev/null; then
    kill_tree "$(cat "$pidfile")"
    echo "  stopped $part"
  else
    echo "  $part was not started by scripts/start.sh (if it runs in a terminal, press Ctrl+C there)"
  fi
  rm -f "$pidfile"
done

docker compose --profile experts --profile e2e stop >/dev/null 2>&1 && echo "  stopped the containers (data kept)"
