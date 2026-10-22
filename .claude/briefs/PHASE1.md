# Phase 1 — task notes for the phase lead

Phase 1 = data collection, tasks P1.1–P1.7 (P1.8 is NOT in scope). Read docs/DATA.md fully
before briefing implementers; pass them the relevant sections by reference (§ numbers).

## Package placement (decided — # DEFAULT: where noted in code)
- `shared/github/` (exists, empty): GitHub REST client + raw.githubusercontent fetcher (P1.1). mypy strict.
- `shared/sanitise/` (exists, empty): sanitiser + `hash_identity` (P1.4). mypy strict.
- `services/collector/` (new uv member: `pyproject.toml`, `src/collector/`, `tests/`): repo-config
  loader and workflow detection (P1.2), backfill/daily/status CLI (P1.5, P1.6), export/import (P1.7).
  Console script `collector` so `uv run collector backfill --member test` works (DATA §3).
  Remove `services/collector` from the root workspace `exclude` and add it to mypy `files`.
- `scripts/rerun_check.py` (P1.3): a thin script that imports from `shared.github` and `collector`.

## Cross-cutting
- No real network in tests: respx with recorded-style JSON fixtures under each package's `tests/fixtures/`.
  Fixtures must look like real GitHub responses (right field names/shapes, Link headers, rate-limit headers).
- Inject clock and sleep into the client so rate-limit/backoff tests run instantly.
- Never log the token or Authorization header; never log unsanitised log text.
- GITHUB_TOKEN comes from the environment. `make` already loads `.env`; for direct runs document
  `uv run --env-file .env ...`. Don't add python-dotenv.
- Sync `httpx.Client` is fine (simplest; the collector is a sequential CLI) — # DEFAULT:.
- Runs are stored per attempt (DATA §3.2, D10). Upserts keyed by GitHub IDs (DATA §3.3).

## Task notes
- **P1.1** Link-header pagination; `x-ratelimit-remaining` < 200 → sleep until `x-ratelimit-reset`
  (threshold from config/env, default 200); 403/429 with `retry-after` → wait and retry; secondary
  rate limit 403 without retry-after → back off; exponential backoff with cap on 5xx and transport
  errors; date-window splitting for `actions/runs?created=a..b` when `total_count` ≥ 1000 (split in
  half recursively, down to a minimum window). File fetcher for `raw.githubusercontent.com/{o}/{r}/{sha}/{path}`
  sends the token, backs off on 429, returns None on 404. Accept: respx tests for pagination,
  rate-limit sleep, retry, window splitting.
- **P1.2** Pydantic schema for `config/repos/<member>.yaml` (DATA §2). Look at all four files in
  config/repos/. `member` ∈ dependency|build|test|infra and must match the filename; `full_name`
  `owner/repo`; `licence` str; `workflows` optional list (empty = auto-detect); `enabled` default true;
  extra keys (framework, flaky, note, commercial_kb, …) allowed and preserved. Duplicate repos → error.
  Clear error messages naming file, repo and field. Workflow auto-detection rule (DATA §2): put the
  per-member detection patterns in a config file (e.g. `config/workflow_detection.yaml`) — Test:
  a job whose `run:` steps call `npm test`, `npm run test…`, `yarn test`, `pnpm test`, `npx jest|mocha|vitest`,
  `jest`, `mocha`, `vitest`. Other members have no rule yet → treat all workflows as relevant and say so
  (# DEFAULT:). Parse workflow YAML safely (yaml.safe_load; `on:` parses as True — handle it).
  Detection results are stored once per workflow file version (`raw.workflows.detected_at_sha`) —
  P1.2 provides the pure function; storing happens in P1.5. Accept: invalid configs fail with clear
  messages; detection tested on sample workflow files (fixtures modelled on real repos' workflows).
- **P1.3** `scripts/rerun_check.py --member test` — budget ≈5 REST calls per repo:
  1 `GET /repos/{r}` (licence spdx_id, default branch, archived);
  2 `GET .../actions/runs?created=>=<today-90d>&per_page=100` → total_count + a 100-run sample
    (failure rate and share with run_attempt>1 computed on the sample, labelled as such);
  3 `GET .../actions/runs?status=failure&created=...&per_page=1` → failure count for the true rate;
  4 the last page of the unfiltered runs listing (per_page=1, page=total_count, capped by the 1000-result
    limit — if capped, say "older than N" honestly) → oldest run still available (actual retention);
  5 `GET .../actions/workflows` → workflow paths; then fetch each workflow file through
    raw.githubusercontent.com (not REST quota) and apply P1.2 detection.
  Flag licence mismatch between config and GitHub's spdx_id. Skip disabled repos unless `--all`.
  Output Markdown table + CSV under `data/reports/` (`rerun_check-<member>-<YYYY-MM-DD>.{md,csv}`),
  and print the report path. Count actual API calls per repo and put it in the report. Robust to a
  repo that 404s or has Actions disabled (row with an error note, continue). Tests with respx only.
  **When P1.3 is committed, report to the orchestrator before continuing** only if the orchestrator
  asked you to; otherwise continue to P1.4.
- **P1.4** DATA §8: detect-secrets plus custom regexes (GitHub tokens `gh[pousr]_…` incl. fine-grained
  `github_pat_…`, npm tokens `npm_…`, AWS access keys `AKIA…`/secret keys, JWTs, PEM private-key blocks,
  `Authorization:` header values, URLs with `user:pass@`). Replace with `[REDACTED:<type>]`, return text
  + per-type counts. Patterns in config if practical. `hash_identity(login)` = sha256(SALT + login) hex
  with SALT from `IDENTITY_SALT` (error if unset; never default a salt). Accept: tests with fake secrets
  of every pattern (build fake tokens by string concatenation so the repo itself doesn't trip secret
  scanners); no false positives on 10 realistic clean CI logs (npm install output, jest/mocha/vitest
  output with hashes, SHAs, UUIDs, base64-ish cache keys, timestamps).
- **P1.5** DATA §3: `backfill` per repo in date windows back to 90 days, jobs with `filter=all`, steps,
  `/attempts/{n}` metadata when run_attempt>1, commit diffs per distinct head_sha (patches >1 MB dropped,
  `patch_truncated`), config file snapshots at head_sha for tagged paths (use the tag patterns in
  config/file_tags.yaml; lockfiles stored by reference), logs per the log policy (all failed jobs +
  passing jobs in the member's relevant workflows), sanitise then gzip to
  `data/logs/{owner}/{repo}/{run_id}/{attempt}/{job_id}.log.gz` (expired/missing log → status row).
  Actor/author logins hashed. Idempotent upserts; resumable via `sync_state.backfill_cursor`.
  Accept: integration test against recorded responses for one repo; running twice changes nothing
  (row counts + file checksums); interrupting (simulated exception mid-run) and resuming completes.
  Map job names to YAML job keys when possible, else null.
- **P1.6** `daily` (runs created since `last_daily_sync_at` + 14-day re-scan for new attempts) and
  `status` (per repo counts, gaps by day, backfill state, current rate limit via `GET /rate_limit`).
  Add an ONBOARDING section on scheduling with cron and Windows Task Scheduler (exact commands).
  Accept: a new attempt of an old run is picked up by the re-scan in tests.
- **P1.7** `make export-data` → `data/exports/<member>-<date>.tar.gz` containing a pg_dump custom-format
  dump (run `pg_dump` INSIDE the postgres container via `docker compose exec -T`, no local client
  needed), the logs folder and `manifest.json` (member, date, git commit, row counts per table,
  sha256 of the dump and of every log file). `make import-data FILE=…` restores into a running stack
  (pg_restore inside the container; refuse to overwrite a non-empty DB without `FORCE=1`) and verifies
  checksums. Accept: export from one DB, import into a fresh one, row counts and log checksums match
  (integration test may use a second database in the same Postgres server).
