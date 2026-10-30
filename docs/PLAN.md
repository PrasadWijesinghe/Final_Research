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
- [x] **P1.8 Data-quality report.** Per repo: runs, attempts, failed jobs, logs fetched / expired / missing, redaction counts.
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

- [x] **P3.1 GitHub App.** Registration guide in `docs/` (permissions: Actions read, Contents read, Checks write, Pull requests write, Metadata read; events: push, pull_request, workflow_run, workflow_job); webhook signature verification; installation tokens; storing App-sourced data in `raw` with `source="app"`.
  *Accept:* recorded webhook payloads drive the live flows in tests.
- [x] **P3.2 Webhooks on a laptop.** smee.io client setup (and a Cloudflare Tunnel fallback) in `ONBOARDING.md`.
- [x] **P3.3 PR check.** Post a check run with alerts, and update it when late results arrive.
  *Accept:* tested against recorded API calls; replay never posts.
- [x] **P3.4 Pre-push hook.** `tools/pre-push-hook`: Node 22 script plus installer (husky or direct `.git/hooks`). Sends the diff and the tagged config files, reads the API key from env or `~/.pipelineiq`, 10 s timeout, prints risks, asks "Push anyway?" only if interactive, and always allows the push on error.
  *Accept:* tests for the risky, clean, timeout and backend-down cases.
- [x] **P3.5 Dashboard MVP.** React + Vite + TypeScript, styled to match the prototype (PipelineIQ): sign-in with GitHub, risk dashboard, pipeline history, diagnosis and review (accept / reject / modify / request revision, with a decision timer), settings (API keys). Types generated from the contract JSON Schemas.
  *Accept:* every page works against the stub-backed backend.
- [ ] **P3.6 Review loop.** `/reviews` → expert `/review` → `VerifiedCase` only after a confirmed cause and a later passing run.
  *Accept:* tested for both the pass and no-pass paths.

## Phase 4 — Test expert (Member 3)

Design: `docs/experts/test.md`.

- [x] **P4.1 Log parsers** for Jest, Mocha and Vitest, plus a stack-trace parser, with real sanitised fixtures (≥10 per framework).
- [ ] **P4.2 Test results extraction** into `testing.test_results`, at test or file granularity. *v1 done 2026-10-28 (cut scope, see `.claude/briefs/PHASE4_V1.md`); full scope in v2. Full log parse built 2026-10-29; awaiting kappa round.*
- [ ] **P4.3 Labelling** per §3, written as `FailureLabel`s; a manual-review sample exporter; a kappa calculator. *v1 done 2026-10-28 (cut scope, see `.claude/briefs/PHASE4_V1.md`); full scope in v2.*
- [ ] **P4.4 Feature pipeline** per §4, one module per group, used by both training and serving. *v1 done 2026-10-28 (cut scope, see `.claude/briefs/PHASE4_V1.md`); full scope in v2. v2 groups and per-test history built 2026-10-29; awaiting kappa round.*
- [ ] **P4.5 Dataset builder** through replay → `data/datasets/testing/vN/` with manifest. *v1 done 2026-10-28 (cut scope, see `.claude/briefs/PHASE4_V1.md`); full scope in v2. Datasets v2 to v4 built 2026-10-29; not frozen, awaiting kappa round.*
- [ ] **P4.6 Baselines** per §5. *v1 done 2026-10-28 (cut scope, see `.claude/briefs/PHASE4_V1.md`); full scope in v2.*
- [ ] **P4.7 Model training:** candidates, temporal and leave-one-repo-out validation, calibration, threshold, MLflow tracking, model bundle export. *v1 done 2026-10-28 (cut scope, see `.claude/briefs/PHASE4_V1.md`); full scope in v2. Models v2 to v6 built 2026-10-29 (v4 served; v5 and v6 candidates, not better); awaiting kappa round.*
- [x] **P4.8 SHAP explanations** into `RiskFactor`s using `features/descriptions.yaml`. *Done 2026-10-29: `explain.py`; the v4 genuine and flaky boosters' contributions are weighted by their share of the combined risk.*
- [x] **P4.9 Triage:** stack-vs-diff overlap, error types, same-SHA evidence, and a classifier with a confidence floor. *Done 2026-10-29: `triage/`, bundle `models/testing/triage/v1`, `reports/triage_v1.md`, wired into the diagnose path (narrative text still the stub, P4.11). Same-SHA evidence is the strongest signal and is partly circular with the rule labels; human kappa round still pending; weak transfer to unseen repos (see the report).*
- [x] **P4.10 Knowledge base:** mine cases per §9, tag them, embed them, and fill `testing.kb_cases`. *Done 2026-10-30: `kb/` (mine, tags, store, retrieve, evaluate, CLI), migration t0004, `config/test_kb.yaml`, 1,233 cases (602 genuine, 631 flaky) embedded with granite, hybrid retrieval wired into the diagnosis (`retrieved_cases`), `reports/kb_v1.md`. Labels, tags and fixes are rule-based and unchecked by a human; the failure text barely predicts the fix category.*
- [x] **P4.11 Diagnosis worker:** prompt file, gateway call, `BehaviourDetails`, citation rules, misrouted handling. *Done 2026-10-30 with the mock gateway only: `rag/` (prompt file, gateway client, template, misroute rules), `config/test_diagnosis.yaml`, one KB case per fix commit in the diagnosis path. With the mock provider the text comes from the deterministic template (`llm_model` says placeholder); the real-model path is tested against a fake gateway only and the prompt has not been tried on a real model. A real provider was added later the same day (DeepSeek, decision D53: `LLM_PROVIDER=deepseek`, key in `LLM_API_KEY`, default model `deepseek-flash`); it is tested with fake responses only, and answer quality has not been rated or the prompt tried on the real model.*
- [x] **P4.12 Swap the stub** for the real expert, keeping the stub tests green. *Done 2026-10-30: the model, triage, knowledge base and diagnosis were already real in code; this task made them the default in the running stack. New `llm-gateway` Compose service (mock provider), worker wired to it, to the host API (`EXPERT_BACKEND_URL`) and to `data/logs`; `GET /status` and a worker start banner show real vs fallback; fixed a worker deadlock (model loaded before the RQ fork) and a missing log-parsing config in Docker; `scripts/demo_route_failure.py` replays a real failure. Verified live with the mock LLM on two replayed failures; stub tests unchanged. A real LLM provider (O5) is still pending.*
- [x] **P4.13 Dependency prototype packaging (Member 1).** Move the existing data extraction, feature engineering, model training, and local API prototype into the Dependency expert's project structure. Keep generated datasets and model artifacts under ignored `data/` and `models/` paths; document prototype limitations and do not replace the contract-based `/predict` stub until evaluation and integration tasks are complete.
  *Accept:* no prototype source remains at the repository root; scripts use explicit project-relative or CLI paths, contain no hard-coded secrets, and have focused tests; `make check` passes.
- [x] **P4.14 Dependency repository group loader (Member 1).** Add `scripts/load_groups.py` to merge one selected candidate group (or all groups explicitly) from `config/dependency_repository_groups.yaml` into `config/repos/dependency.yaml`, preserving existing repo settings and marking new repos disabled until their licence and selection are reviewed.
  *Accept:* tests cover group selection, all-groups selection, preserving existing config and safe defaults; no network access is required.
- [x] **P4.15 Dependency candidate dataset refresh (Member 1).** Keep only runs where a dependency manifest or lockfile changed in the prototype JSON dataset; regenerate its matching feature CSV after collection. Do not create redundant candidate JSON/CSV copies.
  *Accept:* existing JSON rows without dependency-file changes are removed; collection and offline feature generation maintain matching JSON/CSV rows without network access.
  *Accept:* offline tests prove collection refreshes both derived files, excludes records without dependency-file changes, and preserves the raw records.
- [x] **P4.16 Dependency failure sample labels (Member 1).** Select a reproducible sample of failed dependency-change runs, fetch failed-job logs through the shared GitHub client, sanitise logs before classification, and save labels plus evidence without retaining log text. Treat regex matches as heuristic candidates, not verified ground truth; missing/expired logs remain undetermined.
  *Accept:* tests cover sampling, run-attempt matching, pagination, log sanitisation, heuristic and undetermined outcomes, and output safety without network access.
- [x] **P4.17 Dependency prototype data audit and baseline evaluation (Member 1).** Audit the local feature CSV for schema/data-quality issues and evaluate an exploratory workflow-failure baseline with a deterministic repository-held-out split. Report class balance and baseline metrics; do not describe workflow failure as dependency-caused failure or use the result in production.
  *Accept:* tests cover malformed data, duplicate handling, deterministic repository-disjoint splits, class imbalance and metrics; the documented CLI writes a reproducible report without network access; `make check` passes.
- [x] **P4.18 Bounded dependency-log candidate scan (Member 1).** Extend the existing failed-run labeler with a deterministic mode that scans at most 100 distinct repository/commit pairs per batch, fetches and sanitises their failed-job logs, and writes only heuristic dependency-error candidates with safe metadata to a separate local output. Keep heuristic matches explicitly unverified; never persist raw logs or auto-label records as ground truth.
  *Accept:* tests cover distinct-commit selection, the scan bound, deterministic output, expanded package-manager error patterns, sanitisation, candidate-only output, and no-network operation; document invocation and limits; `make check` passes.
- [x] **P4.19 Distinct dependency-candidate batches (Member 1).** Extend the bounded candidate scanner to exclude every repository/commit pair already scanned in prior candidate batch files, including non-matching runs, and exclude manually reviewed candidate commits. Record the scanned repository/commit keys in each batch output so the next batch is reproducible and cannot repeat prior work.
  *Accept:* tests prove prior scan history and reviewed candidates are excluded across separate output files, the current output does not exclude itself, and each batch records all scanned commit keys; document Batch 2 invocation and limits.
- [x] **P4.20 Dependency ground-truth audit table (Member 1).** Include attempt in generated feature rows and the workflow-failure audit schema; consolidate final run-level labels from local review JSON files, preserve source records, detect conflicting labels by `(repository, run_id, attempt)`, and export a deduplicated audit plus a training table excluding `uncertain` and `mixed_failure_causes`.
  *Accept:* tests cover attempt propagation, source-file discovery from labeled, candidate, and random-control JSON files, final-label filtering, duplicate keys, label conflicts, preserved audit records, and clean class counts; pending random controls are ignored until final review; document outputs and limitations; `make check` passes.
- [x] **P4.21 Random non-matching failure control sample (Member 1).** Add a deterministic, bounded mode that selects failed runs not previously scanned/reviewed, fetches and sanitises their failed-job logs, and saves only runs with zero dependency-pattern matches as an unverified random control sample. Record all scanned repository/commit keys so future batches do not repeat work; never store raw logs or auto-label controls.
  *Accept:* tests cover deterministic bounded selection, exclusion of prior scan and reviewed histories, zero-pattern filtering, streamed oversized-log rejection before sanitisation, unavailable logs, sanitized candidate-only output, a 15-second/zero-retry control request policy, transport-error skip-and-continue, safe progress logs, and valid partial output after interruption or unexpected errors; document invocation and limitations; `make check` passes.

## Phase 4B — Build expert (Member 2)

Design: `docs/experts/build.md`.

- [x] **B1 Repo list and collection rules:** `config/repos/build.yaml` (20 repos, reserves, licence-risky), Build rows of `config/workflow_detection.yaml`, `daily --logs failed` (D46), refined Build router patterns.
  *Accept:* config loads; detection and daily tests pass; real build logs route to Build.
- [x] **B2 Build screen:** `scripts/build_screen.py` (real 90-day failure counts, build share, backfill cost).
  *Accept:* tested on canned GitHub answers.
- [x] **B2b Run the build screen** with a real token and trim the repo list (27 Oct 2026: 11 repos kept, suggestion rule fixed).
- [ ] **B2c Backfill** the 11 repos (`backfill --logs failed`), then `daily --logs failed` every day.
- [x] **B3 Log parsers** for tsc, ESLint, Prettier and bundlers (Vite, webpack, esbuild, Rollup, Next.js) with real tool-output fixtures.
- [x] **B4 Labelling** per §3, written as `FailureLabel`s (`platform.failure_labels`, `is_final=false`).
- [ ] **B4b Manual label check:** sample per sub-label and per reason, second labeller, kappa. Tool ready (`build-label-check`, 28 Oct); the two people's check is pending.
- [x] **B5 Feature pipeline** per §4: groups A–D, one module per group, `descriptions.yaml`.
- [x] **B6 Dataset builder** (`build-dataset`) → `data/datasets/build/vN/` with `rows.parquet`, `fix_candidates.parquet` and manifest.
  *Accept:* in-memory and Postgres builds give identical rows; no look-ahead test; frozen versions.
- [x] **B7 Baselines** per §6 (last outcome; warning/error threshold rule).
- [ ] **B8 Model training:** GBT and code-embedding variant, temporal and leave-one-repo-out validation, calibration, threshold, export. GBT part done (`build-train`, 28 Oct): splits, calibration, thresholds, ablation, SHAP summary, report, export; code-embedding variant pending.
- [x] **B9 SHAP explanations** into `RiskFactor`s (28 Oct).
- [ ] **B10 Knowledge base** from reviewed fix candidates (`build.kb_cases`). Built and loaded from mined candidates (`build-kb`, 28 Oct); the human review of the cases is pending.
- [x] **B11 Diagnosis worker** with `BuildDetails`, citations and misrouted handling (28 Oct; LLM rewrite optional, off until a provider is chosen).
- [x] **B12 Swap the stub** for the real expert, keeping the stub tests green (28 Oct: the stub still answers when no model bundle is configured).

## Phase 5 — Evaluation

- [ ] **P5.1** Evaluation harness on replay, with the metrics from `docs/experts/test.md` §5.
- [ ] **P5.2** Per-failure triage vs per-test labelling (headline result).
- [ ] **P5.3** Feature ablation, including the code vector.
- [ ] **P5.4** Retrieval comparison (BM25, MiniLM, granite, EmbeddingGemma, a code model).
- [ ] **P5.5** Rules router vs trained router (team-wide).
- [ ] **P5.6** Time-to-diagnosis study support. **Blocked until ethics clearance.**
- [ ] **P5.7** Replication package: datasets, configs, prompts, model cards, scripts.
