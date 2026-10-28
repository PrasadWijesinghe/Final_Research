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
