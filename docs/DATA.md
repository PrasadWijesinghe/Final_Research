# Data

## 1. Constraints that shape everything

- **GitHub keeps only 90 days.** Since 1 October 2026, workflow runs, checks and statuses on
  public repos follow the Actions retention setting, capped at 90 days, and deletion is
  permanent. Anything not collected within 90 days is lost.
- **Rate limit:** 5,000 REST requests per hour per personal token. Each member uses **their
  own token for their own repos**. Never pool tokens to get around the limit.
- **Result caps:** paginated workflow-run queries return at most 1,000 results, and counts
  above 2,500 show as "2500+". Always query runs in **date windows** small enough to stay
  under 1,000 results (split a window in half if it hits the cap).

## 2. Repo lists

Each member keeps a list in `config/repos/<expert>.yaml`:

```yaml
member: test                # dependency | build | test | infra
repos:
  - full_name: Automattic/mongoose
    licence: MIT
    workflows: [".github/workflows/test.yml"]   # optional; empty = auto-detect
    enabled: true
```

Auto-detecting workflows: keep a workflow if any job in it runs the member's category
(for the Test expert: a job whose steps run `npm test`, `jest`, `mocha`, `vitest`, or a
`test` script). Store the result so detection runs once per workflow file version.

## 3. The collector

A CLI in `services/collector`:

```bash
uv run collector backfill --member test      # first pull: the last 90 days
uv run collector daily --member test         # top-up with catch-up
uv run collector status --member test        # what's collected, gaps, rate limit
```

### 3.1 API calls

| Purpose | Call |
|---|---|
| Runs in a window | `GET /repos/{o}/{r}/actions/runs?created=<from>..<to>&per_page=100` (optionally `&workflow_id=`) |
| Jobs of every attempt | `GET /repos/{o}/{r}/actions/runs/{run_id}/jobs?filter=all&per_page=100` (each job has `run_attempt`) |
| One attempt's run metadata | `GET /repos/{o}/{r}/actions/runs/{run_id}/attempts/{n}` (only when `run_attempt > 1`) |
| Job log | `GET /repos/{o}/{r}/actions/jobs/{job_id}/logs` (follows a redirect) |
| Commit diff | `GET /repos/{o}/{r}/commits/{sha}` (files with patches; paginated for very large commits) |
| PR base diff | `GET /repos/{o}/{r}/compare/{base}...{head}` |
| File at a commit | `https://raw.githubusercontent.com/{o}/{r}/{sha}/{path}` (not counted in the REST quota; send the token anyway and back off on 429) |

### 3.2 What it fetches

- **Every run attempt** in the member's selected workflows, **stored as its own row**. A re-run is a new row, not an update.
- **Every job and step** of each attempt.
- **Logs:** all **failed** jobs, plus **passing jobs in the member's category workflows** (for the Test expert, passing test jobs too, to get per-test history).
- **Commit diffs** for each distinct `head_sha`.
- **Config file contents** at each `head_sha`, for the paths tagged in §5 (cached by sha + path).

### 3.3 Algorithm

1. **Backfill:** walk back from today to 90 days ago in date windows, fetching everything above. Resume safely if interrupted.
2. **Daily:** fetch runs created since `sync_state.last_daily_sync_at`, **plus re-scan runs created in the last 14 days**, because re-runs of older runs appear later and are exactly the flaky signal.
3. **Idempotent:** all writes are upserts keyed by GitHub IDs. Running twice changes nothing.
4. **Rate limits:** read `x-ratelimit-remaining` and `x-ratelimit-reset` on every response. Below 200 remaining, sleep until reset. On 403/429 with `retry-after`, wait and retry. Exponential backoff on 5xx.
5. **Sanitise every log before writing it** (§8), then gzip it to `data/logs/{owner}/{repo}/{run_id}/{attempt}/{job_id}.log.gz`.
6. **Missed days are fine** as long as the collector runs at least every week or two; catch-up covers the gap. Schedule it with cron (Linux/macOS) or Task Scheduler (Windows).

## 4. Database

One PostgreSQL database per member (Docker, `pgvector/pgvector:pg17`). Migrations with Alembic in `shared/db/`.

### 4.1 `raw` schema (written by the collector)

| Table | Key columns |
|---|---|
| `repos` | `id`, `full_name`, `member`, `licence`, `default_branch`, `enabled` |
| `sync_state` | `repo_id`, `backfill_done_at`, `backfill_cursor`, `last_daily_sync_at` |
| `workflows` | `repo_id`, `path`, `name`, `relevant` (bool), `detected_at_sha` |
| `workflow_runs` | **PK (`run_id`, `attempt`)**, `repo_id`, `workflow_path`, `head_sha`, `head_branch`, `event`, `status`, `conclusion`, `created_at`, `run_started_at`, `updated_at`, `actor_hash`, `pr_numbers`, `source` (`collector` or `app`), `raw` (jsonb) |
| `jobs` | `job_id`, `run_id`, `attempt`, `name`, `job_key` (mapped from the workflow file when possible), `status`, `conclusion`, `started_at`, `completed_at`, `runner_os`, `labels`, `raw` |
| `steps` | `job_id`, `number`, `name`, `status`, `conclusion`, `started_at`, `completed_at` |
| `commits` | `sha`, `repo_id`, `parent_shas`, `author_hash`, `committed_at`, `message` (sanitised) |
| `commit_files` | `sha`, `repo_id`, `path`, `previous_path`, `status`, `additions`, `deletions`, `patch` (nullable), `patch_truncated` |
| `file_snapshots` | `repo_id`, `sha`, `path`, `content` or `storage_path`, `fetched_at` |
| `skipped_runs` | `repo_id`, `run_id`, `attempt`, `reason`, `skipped_at`: runs skipped after persistent GitHub 5xx errors, retried by later backfill and daily runs (D39) |
| `logs` | `job_id`, `storage_path`, `bytes`, `sha256`, `redactions`, `status` (`ok`, `expired`, `missing`), `fetched_at` |

Job names are often templated (`test (20)`), so map them to the workflow's YAML job key
by parsing the workflow file at that sha. Leave `job_key` null when unsure.

### 4.2 `platform` schema (written by the backend)

Every contract message is stored as `payload jsonb`, plus a few indexed columns.

| Table | Indexed columns |
|---|---|
| `commit_contexts` | `context_id`, `repo`, `head_sha`, `event`, `history_cutoff` |
| `predictions` | `prediction_id`, `context_id`, `expert`, `probability`, `alert`, `is_replay` |
| `routed_failures` | `failure_id`, `run_id`, `attempt`, `job_id`, `replay` |
| `diagnoses` | `diagnosis_id`, `failure_id`, `expert`, `status` |
| `reviews` | `decision_id`, `diagnosis_id`, `decision` |
| `failure_labels` | `run_id`, `attempt`, `job_id`, `category`, `sub_label`, `labelled_by`, `is_final` |
| `api_keys` | `id`, `user_hash`, `key_hash`, `created_at`, `revoked_at` |
| `llm_calls` | `expert`, `model`, `prompt_version`, `input_tokens`, `output_tokens`, `cost_usd`, `cache_hit`, `latency_ms` |

Replay rows are marked (`is_replay` / `replay`) and excluded from live views.

### 4.3 Expert schemas

`dependency`, `build`, `testing`, `infra`: owned by each expert and defined in that
expert's own migrations. Each holds the expert's features, per-test or per-file results,
datasets and its pgvector collection of `VerifiedCase`s (`vector(384)`).

## 5. File tagging

The features service tags every changed file once. Patterns live in `config/file_tags.yaml`.
Each tag is checked independently, so a file can carry several tags. `other` applies only
when no other tag matched.

| Tag | Patterns |
|---|---|
| `test` | `**/*.test.*`, `**/*.spec.*`, `**/__tests__/**`, `test/**`, `tests/**`, `e2e/**` |
| `test_config` | `**/jest.config.*`, `**/vitest.config.*`, `**/.mocharc*`, `**/playwright.config.*`, plus `vite.config.*` when it contains a `test:` block |
| `source` | `**/*.{js,jsx,ts,tsx,mjs,cjs}` that is not `test` |
| `package_manifest` | `**/package.json` |
| `lockfile` | `**/package-lock.json`, `**/yarn.lock`, `**/pnpm-lock.yaml` |
| `build_config` | `**/tsconfig*.json`, `**/babel.config.*`, `**/.babelrc*` |
| `lint_config` | `**/eslint.config.*`, `**/.eslintrc*`, `**/.prettierrc*` |
| `bundler_config` | `**/webpack.config.*`, `**/vite.config.*`, `**/rollup.config.*`, `**/esbuild.*` |
| `workflow` | `.github/workflows/*.yml`, `.github/workflows/*.yaml` |
| `dockerfile` | `**/Dockerfile*`, `**/docker-compose*.yml`, `**/.dockerignore` |
| `deploy_config` | `vercel.json`, `netlify.toml`, `fly.toml`, `k8s/**`, `helm/**` |
| `docs` | `**/*.md`, `docs/**` |
| `other` | anything that matched nothing else |

Limits: patches over **1 MB** are dropped (`patch=None`) and the context is marked
`truncated`. Lockfiles are always stored by reference, never inline.

## 6. Labels

Every failed job attempt gets one shared `FailureLabel` (see the contract). Each member
writes rules for their own category. If two members' rules claim the same job, both rows
are kept with `is_final=false` and the job goes to manual review; the reviewed row gets
`labelled_by="manual"` and `is_final=true`. Expert-specific labelling rules live in each
expert's doc (Test: `docs/experts/test.md`).

## 7. Datasets

A dataset is a **frozen snapshot** built from the raw tables by replay:

```
data/datasets/<expert>/v1/
├── rows.parquet
└── manifest.json   # build date, history cutoff, repos, row counts per label, git commit, sha256
```

Raw collection keeps running; experiments always name the dataset version they used.

## 8. Privacy and security

- **Sanitiser (`shared/sanitise`):** detect-secrets plus custom patterns (GitHub tokens `gh[pousr]_…`, npm tokens, AWS keys, JWTs, private-key blocks, `Authorization:` headers, URLs with credentials). Replace each with `[REDACTED:<type>]` and count redactions. Unit-test it with fake secrets.
- **Hashing:** `sha256(SALT + login)`, with `SALT` from `.env` (`IDENTITY_SALT`). The whole team uses the same salt, shared privately and never committed.
- **Licences:** each repo's licence is recorded, and each `VerifiedCase` carries it. Cases from non-permissive repos are excluded from any commercial knowledge base.
- **Synthetic data:** fault-injected commits are flagged `is_synthetic` and reported separately.

## 9. Sharing data between members

```bash
make export-data       # → data/exports/<member>-<date>.tar.gz : pg_dump (custom format) + logs + manifest
make import-data FILE=data/exports/build-2026-11-02.tar.gz
```

What the archive holds, the import checks and the options: `services/collector/README.md`, "Export and import".

Never copy Docker volume folders between machines: PostgreSQL data folders aren't portable
across versions or machine types. The export archive doubles as the **weekly backup**
(upload it to Google Drive or OneDrive).
