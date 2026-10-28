# Phase 4 v1 brief: Test expert, first trained model today

Read `.claude/briefs/COMMON.md` for the hard rules and repo conventions, BUT its
"Environment" section is outdated (Windows). Use this instead:

## Environment (Mac)
- Repo: `/Users/skyfall/projects/Final_Research`, macOS, zsh. `uv` and `make` are on PATH.
- Docker is up: `pipelineiq-postgres-1`, `pipelineiq-redis-1`. Data is LIVE in Postgres
  (23,943 runs, 471,155 jobs, ~288k logs under `data/logs/`, 13 GB, gzip, sanitised).
- DB URL for scripts/integration tests: read `DATABASE_URL` from `.env` (never print `.env`).
- Branch `test/collection-setup`. Never push, never switch branches.
- Other agents work in the same tree at the same time. **Commit only your own files**
  (`git add <explicit paths>`, never `git add -A`). If `make check` fails only in another
  agent's in-progress files, say so in your report instead of touching them.
- Commit message: Conventional Commits with the task ID and a `v1` note, e.g.
  `feat(testing): failed-log test results extraction (P4.2 v1)`. No Co-Authored-By, no session links.

## Goal
A first calibrated pre-run model for the Test expert TODAY. Speed over completeness.
Spec: `docs/experts/test.md` (§3 labels, §4 features, §5 model, §12 tables), `docs/DATA.md`
(§4 tables, §6 labels, §7 datasets). Existing parsers: `experts/testing/src/testing_expert/parsing/` (P4.1, read its README).

## v1 cuts (agreed with Member 3, mark each in code with `# DEFAULT: v1 cut, ...`)
- P4.2: parse only logs of FAILED test-job attempts plus every other attempt/run of the same
  job on the same head_sha. Passing logs in general come in v2.
- P4.3: rules `pass`, `flaky` (same SHA), `flaky` silent (low), `genuine`, `unclear`, not-test.
  Manual-sample exporter + kappa calculator built, but the human labelling happens later.
- P4.4: no tree-sitter group, no code vector. History at JOB-KEY granularity
  (job_key, fall back to job name), not per test.
- P4.6: baselines majority, same-as-last-outcome, re-run heuristic only.
- P4.7: logistic regression + LightGBM only.

## Ownership (avoid collisions)
- P4.2 owns `experts/testing/migrations/` and creates ALL `testing` schema tables in
  `docs/experts/test.md` §12 in one Alembic migration (kb_cases may hold the vector column now).
- P4.4 owns `experts/testing/src/testing_expert/features/`. Pure functions over plain inputs plus
  one loader module reading `raw` tables; respects `history_cutoff` strictly (no leakage).
  Same code used for training and serving.
- Everything must be runnable on the full real data in reasonable time (minutes, not hours):
  use set-based SQL, batching, multiprocessing for log parsing.

## Done means
Tests (unit + integration where it touches the DB), `make check` green, component README section
updated in plain language, task NOT ticked in PLAN.md (the orchestrator does that), and a short
report: what you built, the command to run it, and the real-data numbers from running it once.

## Dataset v1 schema (P4.5 writes it, P4.6/P4.7 read it)
`data/datasets/testing/v1/rows.parquet` + `manifest.json` (DATA.md §7: build date, repos,
row counts per label, git commit, sha256, feature list, the v1 cuts).
One row per labelled test-job attempt with label in {pass, flaky, genuine} (exclude unclear, not_test).
Columns:
- keys: `run_id`, `attempt`, `job_id`, `repo` (full_name), `created_at` (UTC timestamp = history_cutoff), `series_key`
- target: `label` in {`pass`, `genuine_fail`, `flaky_fail`}; `label_confidence` (`high`/`low`); `rule_name`;
  `genuine_strict` (bool: genuine with >= 2 failing attempts on that SHA; false otherwise)
- features: every name in `testing_expert.features.feature_names()` with the `cs_ stl_ tc_ td_ h_ rs_ rp_` prefixes;
  string features stay strings (the trainer encodes them).
Train variants: "all" (every row), "high" (drop label_confidence=low), "strict" (genuine only if genuine_strict).
