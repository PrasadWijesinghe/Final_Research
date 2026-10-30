#!/usr/bin/env bash
# Packs what PipelineIQ needs on a new computer into one password-protected zip:
# .env, ~/.pipelineiq (GitHub App key and hook config), models/ and a copy of the database.
# The zip holds secrets: keep it on your own drive, never in git, chat or email.
# Usage: tools/move/pack.sh [--lite] [output.zip]   (default: ~/pipelineiq-move.zip)
#   --lite  keeps every table but leaves out the collected research data (~7.4 GB): the system runs,
#           the dashboard keeps its failures, diagnoses and reviews, and the knowledge base stays.
#           Retraining, the evaluation and history for the 18 collected repos need the full copy.
set -euo pipefail

lite=0
if [[ "${1:-}" == "--lite" ]]; then
  lite=1
  shift
fi
repo="$(cd "$(dirname "$0")/../.." && pwd)"
out="${1:-$HOME/pipelineiq-move.zip}"

# Tables whose rows --lite leaves out (their layout is still copied). Nothing else points at them.
LITE_SKIP=(
  raw.commit_files raw.steps raw.jobs raw.file_snapshots raw.logs raw.workflow_runs raw.commits
  testing.test_results testing.labels testing.log_parses testing.test_ids
)
dump_args=()
if ((lite)); then
  for t in "${LITE_SKIP[@]}"; do dump_args+=("--exclude-table-data=$t"); done
fi
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
pkg="$work/pipelineiq-move"
mkdir -p "$pkg/pipelineiq-home"

cd "$repo"
echo "Starting the database..."
docker compose up -d --wait postgres
if ((lite)); then echo "Saving the database (lite: no research data)..."; else echo "Saving the database (this can take a few minutes)..."; fi
docker compose exec -T postgres sh -c 'pg_dump -U "$POSTGRES_USER" -Fc "$@" "$POSTGRES_DB"' sh ${dump_args[@]+"${dump_args[@]}"} > "$pkg/pipelineiq.dump"

cp .env "$pkg/dot-env"
cp -R "$HOME/.pipelineiq/." "$pkg/pipelineiq-home/"
cp -R models "$pkg/models"
cp tools/move/restore.ps1 tools/move/SETUP_FOR_CLAUDE.md "$pkg/"

echo "Zipping. Choose a password; you type it again on the new computer."
rm -f "$out"
(cd "$work" && zip -qr -e "$out" pipelineiq-move)
chmod 600 "$out"
echo "Done: $out ($(du -h "$out" | cut -f1))"
