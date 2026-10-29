# Test expert (testing)

**In one sentence:** the Test and App Behaviour expert. It predicts how likely a test job is to fail, using a trained LightGBM model, and explains failures in test runs. Its prediction works per test job, not per commit.

**Status:** `/predict` serves the trained model v4 (`lightgbm-v4`) with a stub fallback. Log parsers, results extraction, features, labels, datasets and models are built. Models v5 and v6 are candidates, not served (v6 is not better than v4, see `reports/v6_vs_v4.md`). The diagnosis worker is still a stub · **Owner:** Member 3 · **Plan tasks:** P2.1, P4.1 to P4.7 (partly)

> **Read this first.** The "What it does" and "How the test-job logic works" sections describe the original P2.1 stub. The stub still runs as the fallback. The trained model is described in "Serving the trained model" and the sections after it.

## What it does

This is a copy of the expert template, set up for test failures. It has an API with `/predict`, `/review` and `/health`, and a worker that writes diagnoses. Both return valid messages with fixed or hash-based answers.

The one real difference from the other experts is in `/predict`. It looks for test jobs in the commit and returns one prediction per test job. If it finds none, it returns one commit-scoped prediction, like the other experts do.

The shared rules, the endpoints and the steps to replace the stubs are in `experts/_template/README.md`. Read that first.

## How the test-job logic works

1. For each job in `workflow_jobs`, the stub looks at its `run:` commands.
2. A job counts as a test job if one command matches the heuristic below.
3. Each test job gets one `RiskOutput` with `scope` set to `job`, and `workflow_path` and `job_key` copied from that job.
4. If no job counts, or there are no jobs, the stub returns one commit-scoped output instead.
5. Every output in one commit gets the same probability, because it comes from the hash of `head_sha` only.

The heuristic matches these commands:

- `npm test`, `npm run test`, `yarn test` or `pnpm test`, with an optional suffix such as `test:unit`.
- `npx jest`, `npx mocha` or `npx vitest`.
- The bare words `jest`, `mocha` or `vitest` anywhere in a command.

Python test runs such as `pytest` do not match. A job like that falls back to the commit-scoped output.

The fixture `commit_context_multi_job.json` has a `unit` job in `ci.yml`, an `integration` job in `e2e.yml` and a lint job. The tests check that the prediction returns one job-scoped output for `unit` and one for `integration`. A commit with only the lint job gets one commit-scoped output.

## Facts

| Name | Value |
|---|---|
| Package | `testing_expert` (in `src/testing_expert/`) |
| Port | 8103 (`EXPERT_PORT`) |
| Queue | `diagnose:test`. Not `diagnose:testing`, because the expert's enum value is `test`. |
| Detail model | `BehaviourDetails`. `verdict` is `"unclear"`, `framework` is `"other"`, `failing_tests` is the name of the first failed step, and `suggest_rerun` is false. |
| Database schema | `testing`. No tables yet. |
| `/predict` scope | Job, for each test job. Commit when there are none. |
| Docker | Image `pipelineiq-expert-testing`. Compose services `testing-api` and `testing-worker`. |

## How to run it

```bash
# the whole repo
make check

# only this expert's tests
uv run pytest experts/testing/tests
```

**Expected:** all tests pass. To start the stub in Docker, run `make up-experts`. That was not run in P2.1.

## Test results extraction (P4.2, v1 + full scope)

What it does: reads job logs from `data/logs/`, runs the log parsers, and stores which tests ran
and how they ended in `testing.test_results` (with a stable id per test in `testing.test_ids`).
`testing.log_parses` has one row per log read: the framework, whether parsing worked, a short failure
reason, and the granularity the reporter gave (`test`, `file` or `none`).

Two scopes:

- **v1 (default mode):** failed job attempts, plus every other attempt of the same job on the same commit.
- **Full scope (`--all`):** EVERY stored job log, passing or failing, that is not yet in `log_parses`.
  A log with no Jest, Mocha or Vitest run is recorded as "not a test job" and writes no rows.

Rows are `test` granularity (one test) or `file` granularity (the reporter printed only files, or a
whole file failed to run). Each row says which. Mocha lists every test but not its file; Jest and
Vitest usually list files only, and a failing test is also named in the failure details.

How it stays fast and safe: parsing runs in several processes (`--workers`), results are written in
batches of 200 jobs, each batch with `COPY` into temporary tables followed by set-based inserts. A job that already has a `log_parses` row is skipped, so an interrupted
run resumes where it stopped, and re-writing a job replaces its earlier rows (idempotent).
`--sample N` reads a random N logs, for a throughput trial.

**What is not stored (a choice).** Storing every passing test of every passing log would be about 130
million rows (Mocha prints 1,000+ tests per run). A passing or skipped *test-level* row is kept only
when that test has failed somewhere in the data (`prune_passing` in `extract.py`). File-level rows and
all failed rows are always kept. A test that never failed has a fail rate of 0 whether or not its passes
are stored, so the history features lose nothing they could use.

Run it (database and logs must be present):

```
make migrate-testing    # creates the testing tables and indexes (migrations t0001 to t0003)
uv run --env-file .env python -m testing_expert.extract --member test --all            # full scope
uv run --env-file .env python -m testing_expert.extract --member test --all --sample 2000   # trial
uv run --env-file .env python -m testing_expert.extract --member test                  # v1 scope
```

**Real run (2026-10-29, 10-core Mac).** Trial: 2,000 random logs at 157 logs/s. Full run: 269,564 logs in
980 s (275 logs/s, about 16 minutes), no parse failures. Together with the v1 run all 288,330 stored logs
are parsed: 204,849 are test jobs (Jest 57,636, Mocha 97,168, Vitest 49,339, other 706) and 83,481 are not
test jobs. The table now holds 17.1 million result rows (13.4M test level, 3.7M file level, 4.9 GB with
indexes, 58,490 distinct test ids).

**Coverage of the 212,000 labelled attempts of dataset v3:**

| | attempts | share |
|---|---|---|
| no stored log (mostly passing jobs whose log was not collected) | 43,527 | 20.5% |
| log read, not a test runner | 2,181 | 1.0% |
| test job, reporter listed nothing usable (`none`) | 37,597 | 17.7% |
| test job with parsed results | 128,695 | 60.7% |
| of those: per test | 74,574 | 57.9% of parsed |
| of those: per file | 54,121 | 42.1% of parsed |

By framework (parsed results, per test / per file / none): Mocha 53,354 / 0 / 18,265; Jest 3,609 / 38,806 /
11,157; Vitest 16,922 / 15,315 / 8,175. "None" means the job's CI used a reporter or flags that print no
per-test or per-file lines (a summary only), so no results can be recovered from the log.

## Pre-run features (P4.4, v1 + v2 + per-test history)

What it does: turns one test job attempt into a row of numbers the model can read, using only what
was known before the run started (`history_cutoff` = the run's `created_at`). Code in
`src/testing_expert/features/`, one module per group:

| Group | Module | Examples |
|---|---|---|
| Change size | `change_size.py` | files, lines added and deleted, source / test / config split |
| Source-test link | `source_test_link.py` | source changed with no test changed |
| Test config | `config_change.py` | jest / vitest / mocha / playwright config changed |
| Test dependencies | `dependencies.py` | package.json or lockfile changed, test-library version bumped |
| History | `history.py` | fail rate over 7 / 30 / 90 days, recency-weighted flip rate, failures in a row, re-runs |
| History by runner OS | `history.py` (`h_os_*`) | the same history numbers, counting only earlier runs on the same OS as this job (linux / windows / macos / unknown) |
| What changed in tests | `test_changes.py` (`tw_*`) | new async tests, un-awaited promises, `setTimeout`, real timers, `Date.now()`, `Math.random()`, unmocked network calls, changed timeouts, `.only` / `.skip`, file-level shared state, setup with no `after*` cleanup |
| Run setup | `run_setup.py` | runner OS, matrix size, pull request or push, default branch |
| Repo | `repo.py` | average test-job duration, test framework |
| History per test / file | `history_tests.py` (`ht_*`) | failing and flaky tests of the job over 30 / 90 days, last run's failing tests, fail rate and last outcome of the test files this commit changes |

History is kept per job key (the workflow's YAML key, or the job name without its `(matrix)` part),
not per test (v1 cut). A past job only counts if it finished strictly before the cutoff. Tunables
are in `config/test_features.yaml`. `features/descriptions.yaml` has one plain-language sentence
per feature for the later SHAP explanations. Still not built: the optional code vector.

**`tw_*` (what changed in tests).** Counts of risky constructs the commit ADDED in its changed
JavaScript / TypeScript test files, found with tree-sitter. The database keeps only each file's
patch (the diff), not the file before and after. So each hunk is rebuilt as the new text (unchanged
lines plus added lines), parsed on its own, and a pattern counts only if it starts on an added line.
It is best-effort: a hunk can start in the middle of a block, so edge cases can be missed. A commit
with no test file gets zeros; a commit whose test files have no usable patch (missing, over 200,000
characters, GitHub-truncated) gets empty values (unknown), never fake zeros. `tw_test_files_parsed`
says how many files were read. Network calls only count when the file shows no mock (`jest.mock`,
`nock`, `msw`, ...). `tw_missing_cleanup` counts setups (timers, listeners, servers, spies, env or
global writes) in a file whose patch has no `afterEach` / `afterAll` hook. On the real data, 47% of
test-job rows have a parsed test diff, 53% changed no test file, 0.1% are unknown. One parse per
commit, shared by all its jobs.

**`h_os_*` (history by runner OS).** Same 13 numbers as `h_*`, but within the job key AND the same
OS. The OS comes from `raw.jobs.runner_os`, else the `runs-on` labels, else the job name; unresolved
`${{ matrix.os }}` names become `unknown` (its own bucket). Only attempts finished before the
cutoff count.

**`ht_*` (history per test and per file, 15 features).** Built from the parsed test results (P4.2).
Same series as `h_*` (one job key), and only attempts that finished strictly before the cutoff count.
Job level: `ht_parsed_runs_30d/90d` (earlier runs with readable results), `ht_last_run_failed_tests`
(tests that failed in the job's latest earlier run), `ht_persistent_failing_tests` (failed in both of the
last two runs), `ht_failed_tests_30d/90d` (distinct failing tests), `ht_flaky_tests_30d/90d` (distinct tests
that both failed and passed in the window). Touched files: for the test files this commit changes,
`ht_touched_test_files`, `ht_touched_files_with_history`, `ht_touched_fail_rate` (failed runs divided by
runs seen), `ht_touched_max_flip_rate`, `ht_touched_last_failed`, `ht_touched_failures_90d`.
**Granularity is recorded** in `ht_test_level` (1 = the job's reporter lists individual tests, as Mocha
does; 0 = files only, as Jest and Vitest usually do). Per-file history needs file names, so Mocha jobs
(test names only) get job-level numbers but no "touched file" numbers. Passing tests that never failed
are not stored (see P4.2), so there is no "number of tests run" feature, and per-file rates use only
file-level rows. Missing history is empty (`None`), counts are 0. Caveat from the data: the Jest repo's
own test suite deliberately runs failing fixture tests, so its `ht_` numbers are large; the model sees that
as a repo trait. Serving computes the same numbers with `loader.load_test_history`, one series only.

Two entry points, same code underneath: `compute_features(conn, run_id, attempt, job_id)` for one
job (serving) and `compute_features_batch(conn, keys)` for many (the dataset builder). Both read
the `raw` tables only.

Try it on one real repo (read-only):

```
uv run --env-file .env python -m testing_expert.features --repo vitest-dev/vitest
```

**Expected:** a line like `31587 rows in 3.8s`, then one sample row. All test-like job attempts
of all repos (285,665 rows) take about 110 seconds with the v2 groups (about 28 s in v1); the `ht_` group
adds about 9 minutes on the full dataset because it reads 17 million result rows (the v4 dataset build takes about 12 minutes).

Tree-sitter is pinned to `tree-sitter==0.25.2`: version 0.26.0 crashed the Python process
(segfault) on real patches.

Known limits: matrix cells of one job key share one history (a Windows cell sees Linux outcomes);
"test job" for the repo-level duration is a name match (`test|spec|jest|mocha|vitest|e2e|unit`);
the matrix size is the number of jobs with the same key in the run, not the parsed YAML matrix;
the framework comes from sparse `package.json` snapshots, else a word in the job key.

## Labelling (P4.3, v1)

What it does: gives every test job attempt one label, following `docs/experts/test.md` section 3. Labels go to
`testing.labels` and, for failed attempts, are copied into `platform.failure_labels` as `FailureLabel` rows
(category `test`).

Which attempts: a job counts as a test job when its series (repo, workflow file, job name) had at least one
attempt that the P4.2 parsers recognised as a test job. This is needed because passing logs were not parsed in v1.
Skipped jobs are left out.

| Label | Rule |
|---|---|
| `pass` (high) | attempt 1 passed and no attempt of that job on the same commit failed |
| `flaky` (high) | failed, and another attempt or run of the same job passed on the same commit |
| `flaky` silent (low) | failed, never passed on that commit, and a child commit that changed only docs passed |
| `genuine` (high) | failed, never passed on that commit, and a commit within 7 days on the same branch that touched the failing test file made the job pass |
| `genuine` low (v3, `persistent_failure_streak`) | failed, never passed on that commit, the log shows a test runner, and the job had failed on 3 or more consecutive earlier attempts spread over 2 or more other commits |
| `unclear` | anything else, or the failed job has no log |
| `not_test` | cancelled, or the failed job's log shows no test runner |

The streak rule (added for dataset v3) exists because a job that is simply broken, failing commit after commit with no
fix that touches the failing test file, used to land in `unclear` and drop out of training. It is low confidence
(nothing confirmed a fix), so the "high" training variant leaves it out. `# DEFAULT:` in `labels/rules.py` explains
the reading of "kept failing" (constants `STREAK_MIN`, `STREAK_MIN_OTHER_SHAS`). Real data: 492 attempts moved from
`unclear` to `genuine`. Most long failure "streaks" in the data are matrix legs or re-runs of one commit, not
consecutive commits, so only streaks over 2 or more other commits count.

A pass that follows a failure on the same commit is a rerun pass and gets no label. Failed attempts that
another category also claimed get `is_final = false`. `pass` and `not_test` are not copied to
`platform.failure_labels`, which is for failed jobs.

Run it (safe to run again; it replaces the earlier rule labels in one transaction, about 30 seconds):

```
make migrate-testing    # adds the unique index (migration t0002)
uv run --env-file .env python -m testing_expert.labels --member test
```

It prints the label counts per rule and per repo and writes the manual-review sample to `data/reports/`:
`test_labels_sample.csv` (what the reviewer sees: repo, commit, job, failing tests, log path, empty
`human_label` and `ai_prelabel` columns) and `test_labels_sample_key.csv` (the rule labels, kept apart so the
reviewer is blind). About 50 rows per label (flaky split by confidence), fixed seed. After the reviewer fills
`human_label` (pass, flaky, genuine, unclear, not_test):

```
uv run python -m testing_expert.labels --kappa data/reports/test_labels_sample.csv   # Cohen's kappa
uv run python -m testing_expert.labels --kappa FILE --rater ai_prelabel               # same for the AI column
```

Known limits: a failed job whose log shows no Jest, Mocha or Vitest (for example `node:test` or Playwright) is `not_test`, so
a broken job on an unparsed runner is still left out. Only the failing test file is used for the genuine rule, because P4.2 did not store stack frames.
A single failure with no re-run counts as "kept failing". Commits that never ran this job are invisible to the
7-day window. A test series found by the loose P4.2 heuristic can pull in non-test jobs.

## Dataset builder (P4.5, v1 and v2)

What it does: freezes the labelled attempts and their pre-run features into one table that the baselines and
the model train on. Output: `data/datasets/testing/<version>/rows.parquet` and `manifest.json` (build date,
git commit, repos, row counts per label, feature list, the v1 cuts, sha256 of the parquet file).

One row per attempt labelled `pass`, `flaky` or `genuine` (unclear and not_test are left out). Columns: keys
(`run_id`, `attempt`, `job_id`, `repo`, `created_at`, `series_key`), target (`label` = `pass` / `genuine_fail` /
`flaky_fail`, `label_confidence`, `rule_name`, `genuine_strict`) and every feature from `feature_names()`.
`genuine_strict` is true when the job failed in at least two attempts on that commit.
Train variants: all rows, "high" (drop low confidence), "strict" (genuine only if `genuine_strict`).

How: the builder reads `raw` and `testing.labels` directly and calls the same feature code the live expert
uses, with each row's history cutoff set to its run's `created_at`. It does not go through the replay CLI;
the features are the same. A finished version is frozen: the command refuses to overwrite it without `--force`.

```
uv run --env-file .env python -m testing_expert.dataset --version v1    # about 45 seconds
uv run --env-file .env python -m testing_expert.dataset --version v2    # about 90 seconds
uv run --env-file .env python -m testing_expert.dataset --version v3    # about 3 minutes
uv run --env-file .env python -m testing_expert.dataset --version v4    # about 12 minutes
```

The version is an argument. `v1` keeps its original 41 features (so it stays reproducible after new feature
groups were added). `v2` has the same rows and labels plus two new feature groups, 68 features in all:
`tw_*` (14, the tree-sitter test-change group; empty means unknown, 0 means the commit changes no test) and
`h_os_*` (13, job history counted only on the same runner OS). Its manifest says `"frozen": false`: labels are
still rule labels, so v2 is not frozen until the human kappa round (P4.3) is done. `v3` has the same 68 features and
rows as v2 plus the 492 streak rows (212,000 rows; genuine 2,324, flaky 823), after the P4.3 streak rule; it is also
`"frozen": false`, with a `label_changes` note in the manifest. `v4` has the same 212,000 rows and labels as v3 (nothing was rewritten in the database) plus the 15 `ht_*`
features, 83 in all; it is `"frozen": false` with a `kappa_note` (the human label check is still pending). v1 to v3 files are untouched
(v2 and v3 are built without `ht_`). Remaining cuts, listed in the
manifest: no daily collector (one-off data, no newer runs), no new repos (same 16), P4.2 and P4.3 still at v1 scope.

## Serving the trained model (P4.12, partial)

`POST /predict` now scores with the trained model instead of the stub. The stub is still there as a fallback.

**What happens on a request**
1. The service loads a model bundle once (a folder like `models/testing/v2/` with the model, calibrator, feature list and threshold).
2. It finds the test jobs in the commit's workflow files (same test-job check as the stub). No test job: it scores one commit-level row.
3. It builds the same features used for training. The test-change (`tw_*`), change-size, config and run-setup features come from the commit context itself, including the diffs of changed test files. History features (`h_*`, `h_os_*`, and `ht_*` for bundles that use them) come from the database when `DATABASE_URL` works and the repository is known.
4. An unknown repository, or no database, means "cold start": history is missing and the model handles that. If `DATABASE_URL` is set but the database fails, the output is marked `degraded`.
5. The answer is a normal `RiskOutput`: calibrated probability of failing, the bundle's threshold, the alert flag and `model_version` (for example `lightgbm-v2`). Each test job gets its own output; the highest one is the commit's headline risk.

**Picking a bundle.** Set `TESTING_MODEL_BUNDLE` to a bundle folder, or to a folder of versions (`models/testing`), in which case the newest `v<N>` is used. If it is not set, the newest bundle under `models/testing/` is used. If nothing can be loaded, the service logs a warning and answers with the stub (`model_version` "stub"). It never crashes because of the model. Only LightGBM bundles are served (v1 to v3, and the v4 two-model kind). Docker mounts `models/` and `config/` read-only into `testing-api`, so restart the container to pick up a new version.

**Interim reasons.** Until SHAP reasons exist (P4.8), the "top factors" are the LightGBM per-feature contributions to the fail score, largest first (at most 5, never zero), worded with `features/descriptions.yaml`. For `tw_*` factors the file and line of the first matching added line are attached when the diff shows one.

**Known limits.** Loading the per-test history (`ht_*`) for one job series used to take up to about 4 seconds for big matrix jobs (it streams every stored result row), and serving paid that for every request even though v4 does not read `ht_*`. Now the history is loaded only if the bundle has an `ht_` column (`Bundle.history_mode`: `none`, `job` or `full`), and a bundle that needs only the job-level numbers uses `loader.load_job_test_history`, three small queries that take 0.8 to 1.8 s on the biggest series tested (same numbers as the full loader; see the v6 section). A repository with no history scores low by design: the model leans on history, so a brand-new repo gets little signal beyond the diff. Per-job outputs instead of one max output follow the contract (one prediction per test job).

Code: `src/testing_expert/serving.py`. Tests: `tests/test_serving.py` (tiny trained bundle in a temp folder; `tests/conftest.py` forces stub mode elsewhere).

## Model v3: label fix, new calibration, monotone constraints (P4.7 v3)

Three fixes on top of v2, measured one by one in `reports/v3_vs_v2.md` (run: `uv run --env-file .env python -m testing_expert.model.compare_v3`,
about 10 minutes; it also writes the bundle `models/testing/v3/`, v1 and v2 stay as they are).

1. **Labels** (dataset v3, above). Long failure streaks are now genuine failures instead of dropped rows.
2. **Calibration.** v2 used isotonic regression, which turns every low raw score into exactly 0.0 when no failure fell
   in that score range of the fit slice (5,579 of 42,300 test rows had P(fail) = 0.0). `model/metrics.py` now fits four
   calibrators per class: `isotonic`, `sigmoid` (Platt), `beta` (3-parameter) and `isotonic_floor`, and the new ones never
   output below 0.0001 per fail class (`MIN_PROB`, `# DEFAULT:`). The comparison picks by Brier score and ECE (the
   average gap between predicted and observed fail rate), not PR-AUC; v3 uses `beta`. Old bundles (v1, v2) still load: the
   calibrator file has a `kind` field, and old files without it are read as isotonic.
3. **Monotone constraints.** `MONOTONE_FEATURES` in `model/models.py` lists the features whose direction is obvious
   (more recent failures, a longer failure streak, a higher fail rate or flip rate, more re-runs, more `setTimeout`,
   real timers, `Math.random`, `Date.now`, unmocked network, unawaited promises, `.only` added, missing cleanup never
   lower the risk; more days since the last failure never raise it), each with a `# DEFAULT:` comment. LightGBM gives all
   classes of a multiclass model the same constraint, which would also push P(pass) up, so v3 is two-stage: a
   fail-vs-pass booster with the constraints (`model.txt`) and an unconstrained genuine-vs-flaky booster
   (`split_model.txt`). `serving.py` loads `split_model.txt` when it is there (the only serving change). The "top
   factors" of a v3 answer are the fail booster's contributions.

Result in short: the calibration fix removes the hard zeros at the same Brier score; the constraints give a tested
guarantee but no clear accuracy gain; the label fix gives more training signal. **Streaks are still under-scored**
(the model says about 2% where 13% fail, and "same as last outcome" beats it on those rows), because the calibration
slice contains streak rows that rarely failed. Read the report before trusting v3 over v2. Tests: `tests/test_model_v3.py`,
streak cases in `tests/test_labels.py`.

## Model v4: recent-history calibration, separate genuine and flaky models (P4.7 v4)

Same dataset (v3) and the same time splits as v3. Measured in `reports/v4_vs_v3.md` (run:
`uv run python -m testing_expert.model.compare_v4 --export`, about 5 minutes; it also writes the bundle
`models/testing/v4/`). v1 to v3 bundles stay as they are and still load.

1. **Calibration over more history.** v3 fitted the calibrator on the 10% calibration slice only. v4 also uses
   out-of-fold scores over the train period (each block is scored by a model trained only on earlier blocks), weights
   rows by recency (half-life 30 days) and fits a beta map per event (`fit_binary_calibrator` in `model/metrics.py`).
   Overall calibration improves (test ECE 0.0127 to 0.0071 on the same booster). It does not fix streak rows by itself:
   their failure rate swings from 2% to 15% between periods, so no pre-test choice can match the test period.
2. **Two separate models.** `model/models.py` trains genuine-vs-rest and flaky-vs-rest as independent boosters
   (`SeparateBooster`, kind `lightgbm_sep`), each with its own monotone constraints (`GENUINE_MONOTONE`,
   `FLAKY_MONOTONE`, each `# DEFAULT:`), balanced class weights, calibrator and threshold. P(fail) = 1 - (1 - pg)(1 - pf);
   `sub_risks` carries both class probabilities. The `RiskOutput` contract says `alert == probability >= threshold`, so
   the alert uses the combined score against one threshold; the per-class thresholds are in `threshold.json`.
3. **Serving.** `serving.py` also loads `kind: lightgbm_sep` bundles (`model_genuine.txt`, `model_flaky.txt`, a
   calibrator file with one map per class). That is the only serving change.

Result in short: v4 is better calibrated (ECE 0.0118 to 0.0066) and ranks genuine failures a little better (PR-AUC +0.012,
a real but small gain); the any-fail gain (+0.006) is within the noise; leave-one-repo-out is mixed. **Streaks are still
under-scored** (mean P about 3% where 13% fail) and "same as last outcome" still beats the model on those rows. Tests:
`tests/test_calibration_v4.py`, `tests/test_model_v4.py`.

## Model v5 candidate: per-test history (P4.5 v4, P4.7 v5)

Dataset v4 (212,000 rows, v3 labels, 83 features) adds the `ht_*` group. Compared with model v4 on the
same rows and time splits, with the same recipe, in `reports/v5_vs_v4.md` (run:
`uv run python -m testing_expert.model.compare_v5 --export --json data/reports/v5_results.json`, about 20
minutes; add `--from-json FILE --notes NOTES.json` to re-render the report).

Result in short: **not better, so v4 is still the model in service.** Any-fail PR-AUC 0.189 to 0.193 (the
interval includes zero), genuine PR-AUC falls 0.106 to 0.096, flaky rises 0.191 to 0.207, leave-one-repo-out
is a wash (0.205 against 0.201). Only the job-level `ht_` features carry signal (any-fail +0.018 with an
interval above zero); the touched-test-file features add noise. That sub-group was picked after looking at
the test slice, so it needs a validation-based re-check before shipping. The v5 bundle sits in
`models/testing_candidates/v5/` so that a service restart cannot pick it up by accident.

Tests: `tests/test_features_history_tests.py` (pure), `tests/test_features_db.py` (no leakage, real
Postgres), `tests/test_model_v5.py`, `tests/test_extract.py` (batching, idempotency, pruning).

**Where `--export` writes.** `compare_v5.py` writes its bundle to `models/testing_candidates/v5`, which the service
never reads. To ship v5, move the folder to `models/testing/v5` and rebuild `testing-api`; the service serves the
newest `v<N>` in `models/testing/`.

## Model v6 candidate: the "still failing" rule, a calibrated threshold (P4.7 v6)

**In one line: built and measured, not better than v4, so v4 is still served.** Report:
`reports/v6_vs_v4.md` (run: `uv run --env-file .env python -m testing_expert.model.compare_v6 --export models/testing_candidates/v6 --json data/reports/v6_results.json`, about 20 minutes; add `--from-json FILE --notes NOTES.json` to re-render the report). The bundle is in `models/testing_candidates/v6/`.

Three changes on top of v4, each decided without looking at the test slice:

1. **The "still failing" rule** (`model/rule.py`, in plain words). If this job's previous run failed, the model is not allowed to say less than what usually happens next. "Previous run" means the same job on the same runner OS when it has run there before, otherwise the job's overall history. The rule looks up how often rows with the same number of failures in a row went on to fail (buckets: 1, 2, 3 to 9, 10 or more; counted on train and calibration rows only) and the final probability is the larger of the model's and the rule's. In serving it adds the first reason, for example "This job already failed 74 times in a row on Windows; the failure is likely not caused by this push.", and a `still_failing` entry in `sub_risks`. The contract is untouched. The numbers are in a `rule.json` file in the bundle; a bundle without that file behaves exactly like v4.
2. **Threshold from the calibration slice.** The default is the point with the highest F1 on the 21,186 calibration rows. The curve, "precision at least 0.30" and "top 5% of pushes" are in the report. The test slice never chose a threshold.
3. **Job-level `ht_` features: left out.** Decided on the calibration slice and on out-of-fold scores of the train period, where they do slightly worse (any-fail PR-AUC -0.009 and -0.005). They would fit the time budget (0.8 to 1.8 s with the new fast loader), but they do not earn their place.

**What the measurement says.** The rule's probabilities are low (about 5 to 11% of such rows fail, 11.8% on the test slice), below the alert threshold 0.169, so it never raises an alert by itself at the default threshold; it also flattens the model's ordering inside the rows it touches, so any-fail PR-AUC falls from 0.189 to 0.170. Calibration gets slightly better (ECE 0.0066 to 0.0054). F1 at the default threshold is the same as v4 (0.108). Leave-one-repo-out is slightly positive for the rule (macro PR-AUC 0.198 to 0.208). The strong "same as last outcome" baseline (precision 0.56, recall 0.64 on streak rows) uses the previous run's *label*, which is only known later, so it cannot be reproduced from history available before a run; the deployable version ("previous run failed") is right only 13% of the time on those rows. The looser "top 5% of pushes" threshold (0.0769) gives test precision 0.157 and recall 0.306 (about twice the F1 of the default); choosing it is a product decision about how many alerts per 100 pushes the team accepts. On rows whose previous run passed, v4 and v6 are the same model: precision 0.72 and recall 0.06 at the default threshold.

**To ship it later** (if the team decides): move `models/testing_candidates/v6` to `models/testing/v6` and rebuild with `docker compose --profile experts up -d --build testing-api`. `serving.py` already reads `rule.json`, so nothing else changes. Tests: `tests/test_model_v6.py` (rule, operating points, ht decision, serving with a rule), `tests/test_features_db.py` (fast loader gives the same numbers as the full one).

**`# DEFAULT:` choices in v6.** Rule buckets 1, 2, 3 to 9, 10 or more and a minimum of 50 rows per bucket (`model/rule.py`); max-F1 on calibration as the default threshold, "precision >= 0.30" and "top 5%" as the other operating points, a 20-row support floor for threshold choice (`model/compare_v6.py`); the ht_ include rule (higher any-fail PR-AUC on the calibration slice and on out-of-fold train blocks, both); leave-one-repo-out uses the main run's calibrators; the job-level loader's 90-day window (`features/loader.py`).

## Limitations and known issues

- The diagnosis worker is still a stub. Its `/predict` is trained (see "Serving the trained model"). See `experts/_template/README.md` for the limits that all four experts share.
- The test-job heuristic is simple on purpose. It can give false matches. A command like `npm install --save-dev jest` counts as a test job, because the bare word `jest` matches. It can also miss test runners it does not list. The real model replaces it.
- `# DEFAULT:` in `src/testing_expert/predict.py`: the heuristic above is the definition of a test job.
- `# DEFAULT:` in `src/testing_expert/predict.py`: every job in one commit gets the same stub probability.
- `# DEFAULT:` in `src/testing_expert/config.py`: `DEFAULT_PORT = 8103`, from the port table in `docs/experts/README.md`.
- The other `# DEFAULT:` choices (the Dockerfile versions, the 0.5 threshold, the 0.01 to 0.2 probability range, the confidence of 0.0, the 10 second backend timeout and the Redis fallback) are the same as in the template. They are listed in `experts/_template/README.md`.

## Log parsing

The package `testing_expert.parsing` reads the log of a failed job. For Jest, Mocha and Vitest logs it returns the failing tests (full name, file, error, stack frames), the per-test results the reporter printed, and the runner's summary. Any other log, such as a build error or a `node:test`, TAP or Playwright run, gets framework `other`.

- Full guide: `src/testing_expert/parsing/README.md`.
- Fixtures: real, sanitised logs in `tests/fixtures/logs/`, one folder per framework, each with a hand-checked expected result.
- Rebuild the fixtures from the local database (read-only): `uv run --env-file .env python -m testing_expert.parsing.make_fixtures [--suggest]`
- Check the parsers on about 2,000 real failed-job logs (read-only, not part of `make check`): `uv run --env-file .env python -m testing_expert.parsing.survey`

## Baselines and model training (P4.6 and P4.7, v1 and v2)

The package `testing_expert.model` trains the pre-run model on `data/datasets/testing/<version>/` (v1 or v2) and compares it with three simple baselines: always "pass", "same as the series' previous run", and a re-run heuristic ("flaky when 20% or more of the last 30 days' runs were re-runs"). Models are logistic regression and LightGBM, both with class weights. Probabilities are calibrated with isotonic regression on a calibration slice. A P(fail) threshold is chosen for precision 0.8 with the most recall.

- Run everything (about 8 minutes on the full data): `uv run --env-file .env python -m testing_expert.model train --dataset v1` (`--no-loro`, `--variants all`, `--no-mlflow` make it faster).
- Splits: temporal (earliest 70% train, 10% calibration, last 20% test) and leave-one-repo-out (repos with at least 20 non-pass rows). Variants: all, high (no low-confidence labels), strict (genuine only if failed 2 or more times on the SHA).
- Outputs: `models/testing/<version>/` (model, calibrator, features, threshold, card; v2 sits next to v1), `data/reports/testing_model_<version>.md`, MLflow runs in `mlruns/mlflow.db` (view with `mlflow ui --backend-store-uri sqlite:///mlruns/mlflow.db`). All are git-ignored.
- Missing values: LightGBM reads empty (None) features, such as unknown `tw_*`, as native missing. Logistic regression fills them with the training median and adds a missing-indicator column (`# DEFAULT:` in `model/models.py`).
- Compare two dataset versions on identical rows and splits, with ablations (minus `tw_`, minus `h_os_`) and a paired bootstrap of the PR-AUC difference (about 15 minutes): `uv run --env-file .env python -m testing_expert.model.compare --base v1 --new v2`. It writes `experts/testing/reports/v2_vs_v1.md`.
- On macOS LightGBM needs `brew install libomp`.
- Calibration: the calibration slice is split in time order. The first half fits the isotonic maps, the second half picks the P(fail) threshold (precision 0.8 with the most recall, else best F0.5). The second half holds few failures, so the threshold is noisy; read the test-slice precision and recall in the report.

## Related

- `experts/_template/README.md`: the template this copy came from.
- `docs/experts/README.md`: the expert interface.
- `docs/experts/test.md`: the Test expert's design doc.
- `shared/contracts/README.md`: `RiskOutput`, `BehaviourDetails` and `WorkflowJob`.
- `docs/PLAN.md`, task P2.1.
