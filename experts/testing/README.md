# Test expert (testing)

**In one sentence:** the Test and App Behaviour expert. It will predict and explain failures in test runs. Today its endpoints are a stub with fake answers; the log parsers (P4.1) are real. Its prediction works per test job, not per commit.

**Status:** endpoints are a stub, log parsers built · **Owner:** Member 3 · **Plan tasks:** P2.1, P4.1

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

## Test results extraction (P4.2, v1)

What it does: reads job logs from `data/logs/`, runs the log parsers, and stores which tests ran
and how they ended in `testing.test_results` (with a stable id per test in `testing.test_ids`).

Which logs (the v1 cut): failed job attempts, plus every other attempt or run of the same job
(same repo, same commit, same job name) that has a log. A failed job only counts as a test job when
the parsers recognise Jest, Mocha or Vitest in its log. Passing logs in general come in v2.

Rows are `test` granularity (one test) or `file` granularity (the reporter only printed files, or
a whole file failed to run). Each row says which. A Jest log usually gives both: file rows from the
reporter and test rows from the failure details.

Run it (database and logs must be present; safe to run again, it skips jobs already done):

```
make migrate-testing    # creates the testing tables (also part of `make migrate`)
uv run --env-file .env python -m testing_expert.extract --member test
```

`testing.log_parses` has one row per log read, with the framework, whether parsing worked, and a
short failure reason, so you can see what was tried and what went wrong.

First run on the real data (2026-10-28, 2.5 minutes): 15,553 failed jobs plus 1,213 same-commit
sibling jobs read, 10,856 of them test jobs, no parse failures, 2.79M test rows and 223k file rows.

## Pre-run features (P4.4, v1)

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
| Run setup | `run_setup.py` | runner OS, matrix size, pull request or push, default branch |
| Repo | `repo.py` | average test-job duration, test framework |

History is kept per job key (the workflow's YAML key, or the job name without its `(matrix)` part),
not per test (v1 cut). A past job only counts if it finished strictly before the cutoff. Tunables
are in `config/test_features.yaml`. `features/descriptions.yaml` has one plain-language sentence
per feature for the later SHAP explanations. Not in v1: the tree-sitter group and the code vector.

Two entry points, same code underneath: `compute_features(conn, run_id, attempt, job_id)` for one
job (serving) and `compute_features_batch(conn, keys)` for many (the dataset builder). Both read
the `raw` tables only.

Try it on one real repo (read-only):

```
uv run --env-file .env python -m testing_expert.features --repo vitest-dev/vitest
```

**Expected:** a line like `31587 rows in 3.8s`, then one sample row. All test-like job attempts
of all repos (285,665 rows) take about 28 seconds.

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
| `unclear` | anything else, or the failed job has no log |
| `not_test` | cancelled, or the failed job's log shows no test runner |

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

Known limits: only the failing test file is used for the genuine rule, because P4.2 did not store stack frames.
A single failure with no re-run counts as "kept failing". Commits that never ran this job are invisible to the
7-day window. A test series found by the loose P4.2 heuristic can pull in non-test jobs.

## Limitations and known issues

- Stub only. See `experts/_template/README.md` for the limits that all four experts share.
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

## Related

- `experts/_template/README.md`: the template this copy came from.
- `docs/experts/README.md`: the expert interface.
- `docs/experts/test.md`: the Test expert's design doc.
- `shared/contracts/README.md`: `RiskOutput`, `BehaviourDetails` and `WorkflowJob`.
- `docs/PLAN.md`, task P2.1.
