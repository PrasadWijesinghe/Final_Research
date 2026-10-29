# Plan

Tasks are small and testable. Do them in order unless the dependencies allow otherwise.
Tick `[x]` when the acceptance criteria are met **and** `make check` passes.

**★ = first session** (the foundation teammates need before they can start).

**Every task is done only when:** the code follows `docs/CONVENTIONS.md`, it has tests,
`make check` passes, any new default is marked `# DEFAULT:`, **the component's `README.md`
explains what was built in plain language** (see CONVENTIONS → Documentation), and
`docs/COMPONENTS.md` is up to date.

---

## Phase 0 — Foundation

- [x] **★ P0.1 Repo scaffold.** Create the folder structure from `docs/ARCHITECTURE.md` §8 (empty packages with `__init__.py` where needed), a short `README.md` (what it is, quick start, links to docs), `.gitignore` (`.env`, `data/`, `models/`, `mlruns/`, caches) and `.env.example` (`GITHUB_TOKEN`, `IDENTITY_SALT`, `DATABASE_URL`, `REDIS_URL`, `LLM_PROVIDER=mock`, `LLM_API_KEY`, `MEMBER`).
  *Accept:* tree matches §8; `.env.example` lists every variable used anywhere.
- [x] **★ P0.2 Tooling.** uv workspace (root `pyproject.toml` with members), ruff, mypy (Pydantic plugin), pytest config, `.pre-commit-config.yaml`, and a `Makefile` with `setup`, `up`, `down`, `check`, `migrate`, `export-data`, `import-data` (the last two can be stubs until P1.7).
  *Accept:* `make setup && make check` works on a clean clone.
- [x] **★ P0.3 CI.** `.github/workflows/ci.yml` runs ruff, mypy and pytest on PRs and on `main`.
  *Accept:* passes on the scaffold.
- [x] **★ P0.4 Docker Compose.** `pgvector/pgvector:pg17` and `redis:7` (pinned tags), named volumes, healthchecks, ports from `.env`. Init script enables the `vector` extension and creates schemas `raw`, `platform`, `dependency`, `build`, `testing`, `infra`.
  *Accept:* `make up` gives a healthy Postgres with the extension and schemas, plus Redis.
- [x] **★ P0.5 Contracts in place.** Keep `shared/contracts/` exactly as provided (v1.1). Make it importable as a workspace package; ensure its tests run under `make check`.
  *Accept:* all contract tests pass; `export_schemas` writes JSON Schemas.
- [x] **★ P0.6 Database layer.** `shared/db`: SQLAlchemy 2 models and Alembic migrations for the `raw` and `platform` tables in `docs/DATA.md` §4.
  *Accept:* `make migrate` creates all tables; a round-trip test inserts and reads one row per table.
- [x] **★ P0.7 Team files.** `CODEOWNERS` (GitHub usernames as `TODO` placeholders), PR template (task ID, acceptance criteria checked, `make check` passed), `docs/ONBOARDING.md` (clone → `.env` → `make setup` → `make up` → `make migrate` → first collector run → start your expert from the template), and `docs/AI_LOG.md` with its first entry.
  *Accept:* a teammate can follow `ONBOARDING.md` without asking questions.

## Phase 1 — Data collection

- [x] **★ P1.1 GitHub client.** `shared/github`: httpx client with token auth, pagination, rate-limit handling (read headers, sleep below 200 remaining, honour `retry-after`), exponential backoff on 5xx, and date-window splitting for queries that hit the 1,000-result cap. Includes a `raw.githubusercontent.com` file fetcher.
  *Accept:* unit tests with recorded responses (respx) cover pagination, rate-limit sleep, retry and window splitting.
- [x] **★ P1.2 Repo config.** Loader and schema validation for `config/repos/<member>.yaml`; workflow auto-detection rule from `docs/DATA.md` §2.
  *Accept:* invalid configs fail with clear messages; detection tested on sample workflow files.
- [x] **★ P1.3 Re-run check script.** `scripts/rerun_check.py --member test`: for each repo, the number of runs in the last 90 days, failure rate, share of runs with `run_attempt > 1`, the oldest run still available (actual retention), detected test workflows, and the licence reported by GitHub (flag any mismatch with the config). Outputs a Markdown table and CSV under `data/reports/`. Uses at most ~5 API calls per repo.
  *Accept:* runs on `config/repos/test.yaml` and produces the report. **This is the first thing Member 3 will run.**
- [x] **★ P1.4 Sanitiser.** `shared/sanitise`: detect-secrets plus the custom patterns in `docs/DATA.md` §8, `[REDACTED:<type>]` replacement with counts, and `hash_identity(login)` using `IDENTITY_SALT`.
  *Accept:* tests with fake secrets of every pattern; no false positives on 10 sample clean logs.
- [x] **★ P1.5 Collector backfill.** `services/collector`: the `backfill` command per `docs/DATA.md` §3 (runs in date windows, jobs with `filter=all`, steps, commit diffs, config file snapshots, logs per the log policy, sanitise then gzip). Idempotent upserts; resumable via `sync_state.backfill_cursor`.
  *Accept:* integration test against recorded responses for one repo; running twice changes nothing; interrupting and resuming completes correctly.
- [x] **★ P1.6 Collector daily and status.** `daily` (new runs since last sync, plus a 14-day re-scan for new attempts) and `status` (counts, gaps, rate limit). Doc section in `ONBOARDING.md` on scheduling with cron or Task Scheduler.
  *Accept:* a new attempt of an old run is picked up by the re-scan in tests.
- [x] **★ P1.7 Export and import.** `make export-data` → `data/exports/<member>-<date>.tar.gz` (pg_dump custom format + logs + `manifest.json` with checksums); `make import-data FILE=…` restores into a running stack.
  *Accept:* export on one database, import into a fresh one, row counts and log checksums match.
- [ ] **P1.8 Data-quality report.** Per repo: runs, attempts, failed jobs, logs fetched / expired / missing, redaction counts.
  *Accept:* report generated from the test fixtures.

## Phase 2 — Core services

- [x] **★ P2.1 Expert template and stubs.** `experts/_template` per `docs/experts/README.md`, then the four stub experts (`dependency`, `build`, `testing`, `infra`) with the stub behaviour described there, each with Dockerfile and tests. Add them to Docker Compose.
  *Accept:* each stub's `/predict` returns a valid list of `RiskOutput`s for a fixture `CommitContext`; each worker turns a fixture `RoutedFailure` into a valid `Diagnosis`.
- [x] **P2.2 File tagger.** `services/features` reads `config/file_tags.yaml` and tags files per `docs/DATA.md` §5, including size limits.
  *Accept:* table-driven tests for every tag, multi-tag files and truncation.
- [x] **P2.3 CommitContext builders.** Three builders producing identical structures: **live** (GitHub API), **replay** (raw tables, `history_cutoff` = the run's `created_at`) and **pre-push** (a local diff payload from the hook). Includes the workflow YAML parser → `WorkflowJob` (job key, runs-on, Node versions, permissions, container, `run:` commands).
  *Accept:* the same commit built live and by replay gives equal contexts, apart from `event` and `history_cutoff`.
- [x] **P2.4 LLM gateway.** `services/llm_gateway`: provider interface; mock provider returning schema-valid fixtures; request cache; per-expert monthly spending cap; `platform.llm_calls` logging; JSON-schema output with one validation retry.
  *Accept:* all behaviour tested with the mock; a call over the cap is refused.
- [x] **P2.5 Embeddings and hybrid search.** `shared/embeddings`: granite small via sentence-transformers (lazy-loaded), log normalisation, per-expert pgvector collections, and hybrid search (full-text + vector, reciprocal rank fusion).
  *Accept:* top-k results on a small fixture set; embedding dimension is checked against the table.
- [x] **P2.6 Backend API.** `services/api` with the endpoints in `docs/ARCHITECTURE.md` §4 (webhook and auth endpoints may return 501 until Phase 3). Parallel fan-out to experts with the 10 s / 2 min budgets and `X-Deadline-Ms`; saves contracts to `platform`.
  *Accept:* `POST /predict` with a fixture pre-push payload returns four experts' results within budget, using the stubs.
- [x] **P2.7 Router.** Rules engine in `services/api` reading `config/router_rules.yaml` (patterns on the failed step name and log lines, with weights), top-2 routing when the score margin is small, enqueueing `RoutedFailure`s on `diagnose:<expert>`.
  *Accept:* routes 20 fixture failures (5 per category) correctly; close cases go to two experts.
- [x] **P2.8 Replay CLI.** `uv run replay --member test --from … --to …`: builds contexts from raw data, calls the experts, stores results flagged as replay, and never touches GitHub.
  *Accept:* a replay run produces stored predictions and diagnoses with replay flags set.
- [x] **P2.9 End-to-end test.** Fixture failure → router → queue → stub worker → `/diagnoses` → visible through `GET /failures/{id}`.
  *Accept:* runs in CI with Docker Compose.

## Phase 3 — User-facing

- [ ] **P3.1 GitHub App.** Registration guide in `docs/` (permissions: Actions read, Contents read, Checks write, Pull requests write, Metadata read; events: push, pull_request, workflow_run, workflow_job); webhook signature verification; installation tokens; storing App-sourced data in `raw` with `source="app"`.
  *Accept:* recorded webhook payloads drive the live flows in tests.
- [ ] **P3.2 Webhooks on a laptop.** smee.io client setup (and a Cloudflare Tunnel fallback) in `ONBOARDING.md`.
- [ ] **P3.3 PR check.** Post a check run with alerts, and update it when late results arrive.
  *Accept:* tested against recorded API calls; replay never posts.
- [ ] **P3.4 Pre-push hook.** `tools/pre-push-hook`: Node 22 script plus installer (husky or direct `.git/hooks`). Sends the diff and the tagged config files, reads the API key from env or `~/.pipelineiq`, 10 s timeout, prints risks, asks "Push anyway?" only if interactive, and always allows the push on error.
  *Accept:* tests for the risky, clean, timeout and backend-down cases.
- [ ] **P3.5 Dashboard MVP.** React + Vite + TypeScript, styled to match the prototype (PipelineIQ): sign-in with GitHub, risk dashboard, pipeline history, diagnosis and review (accept / reject / modify / request revision, with a decision timer), settings (API keys). Types generated from the contract JSON Schemas.
  *Accept:* every page works against the stub-backed backend.
- [ ] **P3.6 Review loop.** `/reviews` → expert `/review` → `VerifiedCase` only after a confirmed cause and a later passing run.
  *Accept:* tested for both the pass and no-pass paths.

## Phase 4 — Test expert (Member 3)

Design: `docs/experts/test.md`.

- [ ] **P4.1 Log parsers** for Jest, Mocha and Vitest, plus a stack-trace parser, with real sanitised fixtures (≥10 per framework).
- [ ] **P4.2 Test results extraction** into `testing.test_results`, at test or file granularity.
- [ ] **P4.3 Labelling** per §3, written as `FailureLabel`s; a manual-review sample exporter; a kappa calculator.
- [ ] **P4.4 Feature pipeline** per §4, one module per group, used by both training and serving.
- [ ] **P4.5 Dataset builder** through replay → `data/datasets/testing/vN/` with manifest.
- [ ] **P4.6 Baselines** per §5.
- [ ] **P4.7 Model training:** candidates, temporal and leave-one-repo-out validation, calibration, threshold, MLflow tracking, model bundle export.
- [ ] **P4.8 SHAP explanations** into `RiskFactor`s using `features/descriptions.yaml`.
- [ ] **P4.9 Triage:** stack-vs-diff overlap, error types, same-SHA evidence, and a classifier with a confidence floor.
- [ ] **P4.10 Knowledge base:** mine cases per §9, tag them, embed them, and fill `testing.kb_cases`.
- [ ] **P4.11 Diagnosis worker:** prompt file, gateway call, `BehaviourDetails`, citation rules, misrouted handling.
- [ ] **P4.12 Swap the stub** for the real expert, keeping the stub tests green.

## Phase 4B — Build expert (Member 2)

Design: `docs/experts/build.md`.

- [x] **B1 Repo list and collection rules:** `config/repos/build.yaml` (20 repos, reserves, licence-risky), Build rows of `config/workflow_detection.yaml`, `daily --logs failed` (D42), refined Build router patterns.
  *Accept:* config loads; detection and daily tests pass; real build logs route to Build.
- [x] **B2 Build screen:** `scripts/build_screen.py` (real 90-day failure counts, build share, backfill cost).
  *Accept:* tested on canned GitHub answers.
- [x] **B2b Run the build screen** with a real token and trim the repo list (27 Oct 2026: 11 repos kept, suggestion rule fixed).
- [ ] **B2c Backfill** the 11 repos (`backfill --logs failed`), then `daily --logs failed` every day.
- [x] **B3 Log parsers** for tsc, ESLint, Prettier and bundlers (Vite, webpack, esbuild, Rollup, Next.js) with real tool-output fixtures.
- [x] **B4 Labelling** per §3, written as `FailureLabel`s (`platform.failure_labels`, `is_final=false`).
- [ ] **B4b Manual label check:** sample per sub-label and per reason, second labeller, kappa.
- [x] **B5 Feature pipeline** per §4: groups A–D, one module per group, `descriptions.yaml`.
- [x] **B6 Dataset builder** (`build-dataset`) → `data/datasets/build/vN/` with `rows.parquet`, `fix_candidates.parquet` and manifest.
  *Accept:* in-memory and Postgres builds give identical rows; no look-ahead test; frozen versions.
- [ ] **B7 Baselines** per §6 (last outcome; warning/error threshold rule).
- [ ] **B8 Model training:** GBT and code-embedding variant, temporal and leave-one-repo-out validation, calibration, threshold, export.
- [ ] **B9 SHAP explanations** into `RiskFactor`s.
- [ ] **B10 Knowledge base** from reviewed fix candidates (`build.kb_cases`).
- [ ] **B11 Diagnosis worker** with `BuildDetails`, citations and misrouted handling.
- [ ] **B12 Swap the stub** for the real expert, keeping the stub tests green.

## Phase 5 — Evaluation

- [ ] **P5.1** Evaluation harness on replay, with the metrics from `docs/experts/test.md` §5.
- [ ] **P5.2** Per-failure triage vs per-test labelling (headline result).
- [ ] **P5.3** Feature ablation, including the code vector.
- [ ] **P5.4** Retrieval comparison (BM25, MiniLM, granite, EmbeddingGemma, a code model).
- [ ] **P5.5** Rules router vs trained router (team-wide).
- [ ] **P5.6** Time-to-diagnosis study support. **Blocked until ethics clearance.**
- [ ] **P5.7** Replication package: datasets, configs, prompts, model cards, scripts.
