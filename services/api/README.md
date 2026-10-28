# Backend API

**In one sentence:** The backend service (FastAPI, port 8000) that the pre-push hook, the workers and the dashboard talk to. It asks the four experts for predictions, stores what happens, keeps the history, and sorts failed CI jobs to the expert that should look at them (the router).

**Status:** working for pre-push predictions, history, reviews and API keys (P2.6). The router (P2.7) has unit tests and is called by the replay CLI (P2.8). The replay CLI has unit tests but has not been run end to end. The Dockerfile and the end-to-end test (P2.9) are written but have not been run · **Owner:** shared · **Plan tasks:** P2.6, P2.7, P2.8, P2.9

## What it does

The backend is the one front door for the platform. A developer's pre-push hook sends the local changes here before a push. The backend builds a `CommitContext` (a standard description of the commit, see `shared/contracts`), asks all four experts for a risk score at the same time, and returns the four answers together. The backend also saves what it did, so the history can be shown later.

Other callers use the same service. Expert workers post their finished diagnoses here. The dashboard reads the risks, the history and a failure with its diagnoses, and it posts the developer's review decision. The backend forwards each review to the expert that wrote the diagnosis.

The backend has a second job, the router. When a CI job fails, the router decides which expert should diagnose it, saves that decision, and puts the failure on the expert's work queue. See the Router section below.

The backend never changes user code and never blocks a push on its own. It only reports. The GitHub webhook and GitHub sign-in endpoints exist but return "501 Not Implemented" until Phase 3.

## How it works

1. The hook sends a pre-push payload with a Bearer API key (a password-like token in the `Authorization` header).
2. The backend hashes the key and looks up the hash in the `platform` tables. An unknown or revoked key gets a 401.
3. The backend builds the `CommitContext` from the payload. The author is the key owner's hashed identity, never a name sent in the payload.
4. It saves the context.
5. It calls all four experts at the same time. Each call gets the same budget (10 seconds by default) and a header `X-Deadline-Ms` that tells the expert how long it has.
6. Each answer is checked. It must be a list of risks, for this context, from this expert. Anything else counts as "not ready" with a reason.
7. It saves the ready risks and returns all four experts' results, ready or not.

```mermaid
flowchart LR
    Hook["Pre-push hook"] -->|"POST /predict with key"| API["Backend API"]
    Dash["Dashboard"] -->|"risks, history, reviews"| API
    Workers["Expert workers"] -->|"POST /diagnoses"| API
    API -->|"4 calls in parallel"| Experts["Four experts (/predict)"]
    API -->|"saves and reads"| DB[("Postgres, platform schema")]
    API -->|"router: queues failed jobs"| Redis[("Redis queues diagnose:*")]
```

Each expert is a separate service in `experts/`. The backend calls them over HTTP. It never imports their code.

## How to run it

These commands run from the repository root. Start Postgres and Redis first.

```bash
make setup                     # install packages (once)
make up                        # start Postgres and Redis in Docker
uv run --env-file .env uvicorn api.app:app --host 127.0.0.1 --port 8000
```

There is no `make` target for the API yet. The `--env-file .env` part is needed so that `DATABASE_URL` is set. The Makefile loads `.env` for its own targets, but a plain `uv run` does not.

When it works, uvicorn prints `Uvicorn running on http://127.0.0.1:8000`. Then open `http://127.0.0.1:8000/health`. With no experts running, you get `"status": "degraded"` and each expert shows `"unreachable"`. That is the correct answer for that situation.

To run the tests:

```bash
uv run pytest services/api              # in-memory tests, no database needed
make up                                  # optional: start Postgres
DATABASE_URL=postgresql+psycopg://pipelineiq:change-me@127.0.0.1:5432/pipelineiq uv run pytest services/api
```

The in-memory tests use fake expert servers (respx routes), so they make no network calls. The router tests use a fake Redis (fakeredis), so they need no real Redis either. Use `127.0.0.1`, not `localhost`, on Windows. Otherwise each connection can wait about 2 seconds.

### Tests that skip on your machine

`services/api/tests/test_backend_sql.py` has one test. It saves and reads every kind of row in real Postgres. It is marked `integration`. If the database in `DATABASE_URL` cannot be reached, pytest skips it and prints the reason `database in DATABASE_URL is unreachable (run make up)`. A skip is not a pass. Run `make up` and set `DATABASE_URL` to run it. This guide does not yet record a run of it.

The API SQL tests (`test_backend_sql.py` and `test_replay_source_sql.py`) do not use your main database. The `engine` fixture in `services/api/tests/api_db.py` creates a throwaway database named `<db>_api_test` (for example `pipelineiq_api_test`), runs the migrations in it, and drops it when the module's tests finish. It drops any existing database with that name first, so do not keep other data under that name.

## End-to-end test (P2.9)

This test checks the failure path from start to finish, with real Postgres, real Redis and the Test expert running in a container. It does not use GitHub. The test plays the webhook itself.

**Status:** written, not run. None of this section has been run on a machine yet.

### Where it lives

- The test is `services/api/tests/e2e/test_failure_flow_e2e.py`. Its guard, `services/api/tests/e2e/conftest.py`, decides whether to run it or skip it.
- Both use the `e2e` marker, which is registered in the root `pyproject.toml`.
- `services/api/Dockerfile` builds the API image. The Test expert image comes from `experts/testing/Dockerfile`.
- The containers are in the `e2e` profile of `docker-compose.yml`. The `make e2e` target in `Makefile` runs the whole thing.

### What it does

1. The test takes the first fixture case that expects only the Test expert. At the time of writing that is `test-1-jest` in `services/api/tests/fixtures/router_failures.json`. Its failed step is "Run tests" and its log is a Jest failure.
2. It builds a failed CI job with fresh ids and calls `route_failed_job`. This runs on the host.
3. The router saves a `RoutedFailure` in Postgres. Then it puts one job on the Redis queue `diagnose:test`.
4. The Test worker container takes the job. Its diagnosis is a stub. The worker posts the `Diagnosis` to `http://api:8000/diagnoses`, the API container, over the Compose network.
5. The test polls `GET /failures/{id}` on the API once a second, for up to 60 seconds. It stops when a diagnosis appears.
6. It checks that the failure was sent to `test` only, and that the diagnosis has the same failure id and comes from `test`.

If no diagnosis appears in 60 seconds, the test fails. It prints the RQ job status. A status of `queued` means no worker took the job.

```mermaid
sequenceDiagram
    participant T as Test (host, pytest)
    participant DB as Postgres
    participant R as Redis
    participant W as Test worker (container)
    participant A as API (container)
    T->>DB: route_failed_job saves RoutedFailure
    T->>R: one job on queue diagnose:test
    R->>W: worker takes the job
    W->>A: POST /diagnoses
    A->>DB: saves Diagnosis
    T->>A: GET /failures/id, every second for up to 60 s
    A-->>T: failure with its diagnosis
```

The diagnosis is a stub, so the test checks the links between the records, not the quality of the answer.

### How CI runs it

In `.github/workflows/ci.yml`, in job `check` (30-minute job limit):

1. `make check` runs first. The e2e test skips there (see the next part).
2. The step "End-to-end test" runs `make e2e`. This step has a 15-minute limit.
3. If that step fails, a last step prints the Compose logs.

`make e2e` sets `E2E_REQUIRED=1`. With that set, a skip becomes a failure, so CI cannot pass without the test running.

### Plain `make check` skips it

`make check` runs everything under `services/api/tests`, so it also collects this test. The guard skips the test when nothing answers `/health` at `E2E_API_URL` (default `http://127.0.0.1:8000`). The skip reason says to run `make e2e`.

Be careful: anything that answers on port 8000 counts as the API. A local uvicorn (from "How to run it") does. The test then runs, and it fails after 60 seconds if no Test worker posts a diagnosis. Stop the local server before you run `make check`.

### Run it locally (only when Docker works)

`make e2e` is heavy. It builds two images, `pipelineiq-api` and `pipelineiq-expert-testing`. The first build downloads base images and installs every package, so expect several minutes. The build time has not been measured.

Before you start:

- Start Docker Desktop. Run `make` from Git Bash, because the Makefile needs `sh`.
- Stop anything on port 8000, such as a local uvicorn. Otherwise the `api` container cannot bind the port.
- Check that `.env` has `DATABASE_URL`, `REDIS_URL` and `POSTGRES_PASSWORD`. The test and `make migrate` read the first two from your shell.

```bash
make e2e
```

It runs these steps in order:

1. It starts Postgres, Redis, the API and the Test expert (its API and worker) in Docker.
2. It runs `make migrate` against `DATABASE_URL`.
3. It runs `pytest -m e2e services/api/tests/e2e` with `E2E_REQUIRED=1`.
4. If anything failed, it prints the Compose logs.
5. It stops and removes `api`, `testing-api` and `testing-worker`, then exits with the test's exit code.

Postgres and Redis keep running. Stop them with `make down`.

Expected when it works: pytest reports `1 passed` and nothing is skipped. This is what we expect, not something we have seen.

Two side effects to know about:

- If you ran `make up-experts` earlier, `make e2e` recreates `testing-api` and `testing-worker`, because their environment changes. At the end they are removed. Run `make up-experts` again to get the Test expert back.
- The test writes its rows to your main database and does not delete them. It also puts a job on your real `diagnose:test` queue. See "Limitations" below.

### The API image

`services/api/Dockerfile` uses the repo root as its build context:

```bash
docker build -f services/api/Dockerfile .
```

1. **Builder stage.** It uses Python 3.12.8 (slim, Debian bookworm) and uv 0.13.1. It copies the `shared`, `services` and `experts` folders, because uv needs to see every workspace member to read the lockfile. It installs only `pipelineiq-api` into `/opt/venv`.
2. **Runtime stage.** This is a fresh Python image. It copies only `/opt/venv`, not the source. It runs as a non-root user named `api` (uid 10001).
3. **Config.** It copies `config/` twice. The API reads `/app/config` through `ROUTER_RULES_PATH` and `SANITISE_PATTERNS_FILE`. `shared/file_tags.py` has no environment override. It looks in `site-packages/config` next to the installed `shared` package, so the second copy goes there. The `python3.12` in that path is tied to the base image.
4. **Start.** It runs `uvicorn api.app:app` on port `${API_PORT}` (8000), listening on all interfaces inside the container. Compose publishes the port only on `127.0.0.1:8000`.

`.dockerignore` keeps `.env`, `data/`, `docs/` and the dashboard out of the build.

### Environment variables

| Variable | Read by | Default | What it changes |
|---|---|---|---|
| `E2E_API_URL` | `tests/e2e/conftest.py` (host) | `http://127.0.0.1:8000` | Where the test finds the API. A trailing slash is removed |
| `E2E_REQUIRED` | `tests/e2e/conftest.py` | Unset (skip) | `1` turns every skip into a failure. `make e2e` sets it |
| `EXPERT_BACKEND_URL` | `docker-compose.yml`, passed to the Test worker as `BACKEND_URL` | Empty (stub, no POST) | Where the Test worker posts diagnoses. `make e2e` sets it to `http://api:8000` |

The host also needs `DATABASE_URL` and `REDIS_URL`. All three variables are listed, commented out, in `.env.example`.

## Endpoints

| Method and path | Who calls it | Auth | What it does |
|---|---|---|---|
| `POST /predict` | Pre-push hook | Bearer API key | Builds the context, asks four experts, saves, returns four results |
| `POST /diagnoses` | Expert workers | None (see Limits) | Saves a finished `Diagnosis` |
| `POST /reviews` | Dashboard | None (see Limits) | Saves a `ReviewDecision` and forwards it to the diagnosing expert |
| `GET /commits/{sha}/risks` | Dashboard | None (see Limits) | Stored risks for a commit. Replay results only with `include_replay=true` |
| `GET /runs` | Dashboard | None (see Limits) | History: predictions next to the run outcome. Newest first |
| `GET /failures/{id}` | Dashboard | None (see Limits) | A routed failure with its diagnoses and reviews |
| `POST /api-keys` | Admin | Admin token | Issues a key. The plain key is returned once |
| `DELETE /api-keys/{id}` | Admin | Admin token | Revokes a key (204, or 404 if unknown) |
| `POST /webhooks/github` | GitHub App | `X-Hub-Signature-256` | Checks the signature (401 if wrong, 503 if no secret is set), answers 202 and handles the event in the background. See "GitHub App webhooks (P3.1)" |
| `GET` or `POST /auth/github/...` | Dashboard (later) | None | Returns 501 (GitHub sign-in is left for the dashboard task, P3.5) |
| `GET /health` | Anyone | None | Backend status plus each expert's health |

## GitHub App webhooks (P3.1)

### What it does

GitHub sends an event to `POST /webhooks/github` each time something happens in a repo where the
PipelineIQ App is installed. The API proves the event is from GitHub, answers at once, and does the
real work afterwards (GitHub gives up after 10 seconds).

### How it works

1. **Signature.** GitHub signs the raw body with the webhook secret and sends the result in
   `X-Hub-Signature-256`. The API recomputes it and compares in constant time. A wrong or missing
   signature gets 401 and nothing else happens. The body and the secret are never logged.
2. **Installation token.** To read the repo, the API signs a short-lived JWT with the App's private
   key (RS256), swaps it for an installation access token, and keeps that token until two minutes
   before it expires (`shared/github/app_auth.py`).
3. **Per event:**
   - `push` (a branch, not a delete or tag) and `pull_request` (opened, synchronize, reopened,
     ready_for_review): builds the `CommitContext` with the live builder, saves it, asks the four
     experts with the 2 minute budget and saves their predictions, then posts or updates the PipelineIQ check run (see "PR check run" below).
   - `workflow_run` and `workflow_job`: stored in `raw.workflow_runs`, `raw.jobs` and `raw.steps`
     with `source="app"`. Like the collector, the actor is hashed, `actor`/`head_commit`/repository
     blocks are dropped from the stored JSON, and the run title is passed through the sanitiser.
   - A `workflow_job` that completed with `failure`: the API fetches the job log, sanitises it,
     writes it under `DATA_DIR/logs/...`, adds a `raw.logs` row, and hands the failed job to the
     router, which queues it for the experts.
4. Errors in the background step are logged and never returned: a failing GitHub call must not make
   GitHub retry the delivery.

### Limits

- `job_key` is left empty for App-sourced jobs (the collector fills it from the workflow file).
- Duplicate deliveries are not detected. A repeated push predicts again; stored rows are upserted.
- Sign in with GitHub (`/auth/github/*`) still returns 501.

### Tests

`tests/test_webhooks.py` sends fake payloads signed with a fake secret, with GitHub mocked by
respx. `tests/test_backend_sql.py::test_sql_raw_store_writes_app_rows` checks `source="app"` on a
real database. `shared/tests/test_github_app_auth.py` covers the JWT, the token cache and the
signature check. To try it for real, see `docs/ONBOARDING.md` step 10.

## PR check run (P3.3)

### What it does

After a push or pull request, the API shows the result on GitHub as a check run named
**PipelineIQ**. The summary lists every expert's score, whether it is an alert, up to three plain
reasons, and which experts have not answered yet. It also links to the dashboard commit page
(`DASHBOARD_URL/#/commits/<sha>`). The check never blocks a merge.

### How it works

1. After the experts answer, the API builds the check text (`api/checks.py`).
2. If nothing is ready yet, the check is created as `in_progress`. Otherwise it is created
   `completed`: `neutral` when there is an alert or an expert is still missing, `success` when all
   four answered and none alerts. It is never `failure`.
3. GitHub's check id is saved per repo and commit in `platform.check_runs`
   (migration 0004). A later push of the same commit updates the same check.
4. Late experts: the API asks the experts that were not ready again after 30 s, 90 s, 5 min and
   15 min (`CHECK_RETRY_DELAYS_SECONDS`). Each new answer is saved and updates the same check
   (a PATCH, not a new check). The last round finishes the check even if experts are still silent,
   so it never stays `in_progress` forever.
5. A GitHub error is logged (the error type only, never the body or the token) and ignored. The
   predictions are kept either way. Check calls are not retried, so a hiccup cannot create two
   checks.
6. Replay and the pre-push hook never reach this code. A test checks that `api.replay` does not
   load it.

The GitHub App needs **Checks: Read and write** (see `docs/GITHUB_APP.md`).

### Limits

- Late results come from asking `/predict` again, because experts have no way to call us back yet.
  If the API restarts during the 15 minute window, the check stays as it was.
- Only the first 12 predictions per expert are listed (test jobs can be many), highest first.
- A pull request check is attached to the head commit, so it shows on the PR and on the push.

Tests: `tests/test_checks.py` (GitHub mocked by respx).

## Router (P2.7)

### What it does

When a CI job fails, someone has to decide which expert should look at it. The router makes that decision with rules. Each expert has its own rules in `config/router_rules.yaml`. A rule is a text pattern (a regular expression, such as `npm ERR!`) with a weight. A pattern that matches a failed step name or a line of the log adds its weight to that expert's score.

The router then sends the failure to the top expert. When the runner-up is close behind, it sends the failure to both. The result is a `RoutedFailure` (the shared contract for a failed job). The router saves it and puts one job on each chosen expert's queue. The expert's worker picks the job up and writes a diagnosis.

If no expert scores high enough, the failure is "unknown". Unknown failures are only written to the log. See "Unknown failures" below.

### How it scores

1. There are two kinds of rule. Step-name rules check the names of the failed steps (for example "Run tests"). Log rules check each line of the log excerpt. The excerpt is the sanitised tail of the failed step, up to 20,000 characters.
2. Each rule counts once. If a pattern matches ten lines, it still adds its weight only once.
3. An expert's score is the sum of the weights of its rules that matched.
4. The router scores all four experts this way.

### How it decides

1. Take the highest score. If it is below `min_score` (1.0 in the shipped rules), the failure is unknown. Stop.
2. Otherwise the top expert is chosen.
3. The runner-up (the second-highest expert) is also chosen when both of these hold:
   - Its score is at least `min_score`.
   - The gap is small enough. The gap is measured against the winner: `(top - runner-up) / top` must be at most `two_expert_margin` (0.25 in the shipped rules). For example, 4 against 3 is a gap of 0.25, which is exactly at the limit, so both experts are chosen.
4. At most two experts are chosen, and never the same expert twice.
5. The confidence is the winner's share of all the scores, at most 1.0. It is a simple ratio, not a calibrated probability.

```mermaid
flowchart LR
    Job["Failed job"] --> Score["Score all four experts"]
    Score --> Top{"Top score at least min_score?"}
    Top -->|"no"| Unknown["Unknown: log only"]
    Top -->|"yes"| Second{"Runner-up at least min_score and gap within margin?"}
    Second -->|"yes"| Both["Top two experts"]
    Second -->|"no"| One["Top expert only"]
    Both --> Build["Build RoutedFailure"]
    One --> Build
    Build --> Save["Save to store"]
    Save --> Enqueue["One job per expert on its queue"]
```

### From a decision to a queued job

1. The router builds a `RoutedFailure`. It holds the predicted category, the confidence, the experts in rank order, and the job details. The `replay` flag is copied from the failed job.
2. The router saves the `RoutedFailure` to the store first. That way `GET /failures/{id}` can find it.
3. The router enqueues one job for each chosen expert.

A queue is a list of jobs kept in Redis (an in-memory database). This code uses RQ (a Python library for Redis queues). Each expert's worker watches one queue:

| Expert | Queue | Job function (dotted path) |
|---|---|---|
| Dependency | `diagnose:dependency` | `dependency_expert.worker.process_failure` |
| Build | `diagnose:build` | `build_expert.worker.process_failure` |
| Test | `diagnose:test` | `testing_expert.worker.process_failure` |
| Infra | `diagnose:infra` | `infra_expert.worker.process_failure` |

Some details of the job:

- The job function is stored as a text path, for example `testing_expert.worker.process_failure`. The API never imports expert code. The worker process imports the function when it runs the job.
- The job payload is the `RoutedFailure`, converted to JSON. The worker checks it against the contract again before it uses it.
- The job id is `<failure_id>-<expert>`. Putting the same `RoutedFailure` on the same queue twice adds nothing, as long as the first job's record is still in Redis.
- Routing the same CI job again is a different thing. Each routing creates a new `failure_id`, so it creates a new job. The job id does not stop a repeated CI run from being queued twice.

### Unknown failures

No rule reached `min_score`, so the router does not store the failure and does not enqueue it. It writes a log line with the run id and job id only.

The reason is the shared contract. A `RoutedFailure` must name at least one expert in `sent_to`, and the contracts are frozen. An unknown failure has no expert, so it cannot be stored as a `RoutedFailure`. The config file's header says unknown failures are "shown on the dashboard". That is not true yet. To show them, the team needs a contract change, for example allowing an empty `sent_to` or adding a separate record for unknown failures. That change is still to be proposed.

### Who calls the router

The replay command calls `route_failed_job` (see "Replay CLI" below). It loads the rules once per run and passes them in. The GitHub webhook (P3.1) will be the second caller. Until the webhook exists, the only failures that reach `GET /failures/{id}` are the ones the replay command routed, or ones someone saved by hand.

### Example

The test `close-1-build-vs-dependency` is a "close case". The failed step is named "Build". The log has three lines that matter:

- `error vite@5.0.4: The engine "node" is incompatible ...` matches a dependency log rule (weight 2.5).
- `src/main.ts(1,8): error TS2307: Cannot find module ...` matches a build log rule (weight 3.0).
- `npm ERR! code ELIFECYCLE` matches a dependency log rule (weight 2.0).

The step name "Build" matches a build step-name rule (weight 2.0).

Worked out by hand from the rules file, the scores are Build 5.0 and Dependency 4.5. The gap is 0.5 / 5.0 = 0.10, which is within 0.25. So the failure goes to both experts, Build first. The test checks that the two experts are `build` and `dependency`, in that order. The confidence is 5.0 / 9.5, about 0.53.

### Tests

`services/api/tests/test_router.py` has 41 tests. None are skipped, and none need a real Redis or a database.

- 24 cases come from `tests/fixtures/router_failures.json`: 5 each for dependency, build, test and infra, 2 close cases that go to two experts, and 2 unknown cases that go nowhere.
- 17 more check the margin, the two-expert limit, `min_score`, the rule counting, the rules file errors, the queue names, the job payload, the repeat check, the Redis URL and the "stored, then queued" and "unknown is not stored" paths.

### Settings

| Setting | Where | Default | What it changes |
|---|---|---|---|
| `REDIS_URL` | `.env` or environment | `redis://127.0.0.1:6379/0` | Which Redis holds the queues. Blank counts as unset |
| `ROUTER_RULES_PATH` | `.env` or environment | Unset, so `config/router_rules.yaml` at the repo root | Another rules file to load. Read when the rules are loaded |
| `two_expert_margin` | `config/router_rules.yaml` | `0.25` | How close the runner-up must be to join the winner |
| `min_score` | `config/router_rules.yaml` | `1.0` | The score an expert needs to be chosen at all |

## Replay CLI (P2.8)

### What it does

The replay command runs old CI runs through the platform again. It takes runs that the collector already stored, rebuilds the description of each commit from the stored data, asks the experts for predictions, and sends failed jobs through the router. It is how the team builds history for training and evaluation. You run it by hand. Nothing runs it on a schedule.

Replay never talks to GitHub. It reads only the `raw` tables in Postgres and the log files on disk. A test checks that importing the module does not load the GitHub client. Another test checks that no request reaches a GitHub host.

Replay results are kept apart from live results. Each replayed prediction is marked `is_replay`. Each replayed failure is marked `replay`. The live endpoints leave these out unless you ask for them.

### Flags

```bash
uv run --env-file .env --package pipelineiq-api replay --member test --from 2026-03-01 --to 2026-03-31
```

| Flag | Required | What it means |
|---|---|---|
| `--member` | Yes | `dependency`, `build`, `test`, `infra` or `all`. A single member asks only that member's expert, and replays only runs of repos whose stored member is that member. `all` replays every stored repo and asks all four experts |
| `--from` | Yes | First day to replay, as `YYYY-MM-DD`, in UTC |
| `--to` | Yes | Last day to replay, as `YYYY-MM-DD`, in UTC. The day is included |
| `--data-dir` | No | Folder that holds the stored log files. Default: the `DATA_DIR` setting, else `data` in the folder you run the command from |

A bad date, an unknown member, or `--to` before `--from` exits with code 2 and writes a message to the screen.

The module docstring shows `uv run replay`. The root project does not depend on `pipelineiq-api`, so `--package pipelineiq-api` may be needed. The command has not been run end to end yet. Check it on the first real run. Postgres must be running (`make up`). Redis is used only when a failed job is routed.

### How it works

1. The command reads the flags. It stops if `--to` is before `--from`.
2. It turns the days into a time window. The window starts at midnight UTC on `--from`. It ends at midnight UTC on the day after `--to`.
3. It lists the stored runs created in the window, oldest first. For a single member, it keeps only runs of that member's repos.
4. For each run, it rebuilds the commit's `CommitContext` from the raw tables. The builder is in `services/features` (its guide has a Replay section). If stored data is missing, the run counts as skipped, and the command moves on.
5. It saves the context with the event `replay`.
6. It asks the chosen experts for predictions at the same time. They share one budget for the run: the PR-notes budget of 2 minutes.
7. It saves each ready answer with `is_replay = true`. Answers that are not ready are counted, not saved.
8. For each failed job of the run, it adds the context id, sets `replay = true`, and routes the job with the router. The router saves the failure and queues it.
9. It moves to the next run. At the end it prints one summary line.

```mermaid
flowchart TD
    CLI["replay command (flags)"] --> List["List stored runs in the window"]
    List --> Build["Rebuild CommitContext from raw tables"]
    Build -->|"data missing"| Skip["Count as skipped"]
    Build --> Ctx["Save context (event = replay)"]
    Ctx --> Fan["Ask chosen experts in parallel"]
    Fan --> Pred["Save ready answers with is_replay = true"]
    Pred --> Jobs["Failed jobs with a stored log"]
    Jobs --> Route["Route with the router, save with replay = true"]
    Route --> Queue[("Redis diagnose:* queues")]
    Queue --> Workers["Expert workers"]
    Workers -->|"POST /diagnoses later"| API["Backend API"]
```

The command prints one line, for example:

```text
replayed 2 runs (1 skipped): 2 predictions, 1 failures routed, 0 unrouted, 0 expert answers missing
```

### Replay flags on the data

| Where | Replay value | How it is set |
|---|---|---|
| Predictions (`platform.predictions`) | `is_replay = true` | Set by the replay command. `/predict` always saves `false` |
| Routed failures (`platform.routed_failures`) | `replay = true` | Copied from the failed job. The queued job payload also has `replay: true` |
| Contexts (`platform.commit_contexts`) | `event = "replay"` | Set by the context builder. These do not appear in `GET /runs` |
| Diagnoses (`platform.diagnoses`) | No flag of its own | Expert workers post diagnoses later through `POST /diagnoses`. A diagnosis points to its failure by `failure_id`, so it is replay when its failure is. `GET /failures/{id}` shows the failure and its diagnoses together |

### Example

The test `test_replay_stores_flagged_predictions_and_routed_failures` uses four stored runs. The data is in memory, not in Postgres:

- Runs 1 and 2 fall inside the window. Run 1 has one failed job. Its failed step is `npm test`, and its log says `FAIL test/a.test.ts`.
- Run 3 falls after the window, so it is not listed.
- Run 4 has no commit row, so its context cannot be built. It is skipped.

The test expects these results:

- 2 runs replayed, 1 skipped.
- 2 predictions, one from the test expert for each replayed run.
- 1 failure routed to the test expert, with `replay = true`. It is queued on `diagnose:test`.
- 0 failures unrouted.

### Tests

- `services/api/tests/test_replay.py` has 6 tests. They use in-memory data, a fake Redis (fakeredis) and fake expert servers (respx). They need no database and no Redis, so they run on any machine. They were written with this change but I did not run them while writing this guide, so no pass is recorded here.
- `services/api/tests/test_replay_source_sql.py` has 1 test. It is marked `integration`. It inserts a repo, a run, a job, a step and a log row into Postgres, reads them back through `SqlReplaySource`, then deletes them. It only covers the SQL source. It does not cover the rest of the command. If the database in `DATABASE_URL` cannot be reached, pytest skips it. A skip is not a pass. Run `make up` and set `DATABASE_URL` to run it. It has never been run.

### Limits and known issues

- **Re-running a window duplicates rows.** Each run gets a new context id, new prediction ids and new failure ids. Nothing checks for an earlier replay of the same run. Delete the old rows by hand if you need to.
- **`--member` does not limit routing.** The member limits which runs are read and which expert answers. Every failed job of those runs goes through the router. The router can send a job to any expert, so a failure found while replaying `--member test` can be queued for the build expert.
- **Queued jobs reach the real workers.** Routed failures are put on the live `diagnose:*` queues. The expert workers pick them up and do their work, including any LLM calls they make. Replay therefore has real effects outside the database.
- **Unrouted failures are only counted.** When no rule matches, the failure is not stored and not queued. The only trace is the `unrouted` number in the summary.
- **Only `conclusion = failure` jobs are replayed.** A job that was cancelled or timed out as a whole is never replayed.
- **Failed jobs without a stored log are skipped.** A job is also skipped when none of its steps failed, or when its log is missing, not marked `ok`, or cannot be read from disk. The reason is written at INFO level, which the command does not show by default.
- **Skipped runs show only as a count.** The reasons are written as warning log lines. The summary line does not show them.
- **One member per repo.** `raw.repos.member` holds one value per repo. If two members' repo lists both contain a repo, only the stored member's replay picks it up.
- **Runs run one after another.** Inside one run, the experts are asked at the same time. The next run starts only when all of them answer or the 2-minute budget ends. A slow expert can add up to 2 minutes to each run.
- **A dead expert does not stop the replay.** Its answer is counted under "expert answers missing". The context and any routed failures are still saved.
- **Not tested locally:** the `main()` path from the command line to the printed line, and the database path of the command end to end. The parser, the replay logic and the SQL source's read path are covered (the SQL path only by the integration test above).
- **Choices with no `# DEFAULT:` marker, to review:**
  - `--to` is included by adding one day to the window end.
  - Runs with the same `created_at` are ordered by run id.
  - A run with no stored workflow path has no failed jobs. It is not an error.
  - The context is saved even when no expert answers. Predictions are saved only when at least one answer is ready.

**Every `# DEFAULT:` in `services/api/src/api/replay.py`:**

| Line | Choice | Why |
|---|---|---|
| 10 (module docstring) | The CLI lives in `services/api`, not `services/features` | `features` is a library that `api` imports. A CLI in `features` would need `api` (fan-out, router, store), which is a loop. See the flag under `services/features/README.md` |
| 58 | The stored log excerpt is its last 20,000 characters | The contract's maximum size for `log_excerpt`. The end of a log holds the failure |
| 98 | A member's name is its expert's value; `all` calls all four experts | Matches `config/repos/<expert>.yaml` and the `MEMBER` setting |
| 249 | Replay uses the PR-notes budget (2 minutes) rather than the 10-second pre-push budget | Nobody is waiting on a replay |
| 302 | Logs are read from `DATA_DIR`, else `data` in the current folder | The comment says this is the collector's rule. It is not: the collector uses `<checkout>/data`, not the current folder. Needs a decision |

### Settings

| Setting | Where | Default | What it changes |
|---|---|---|---|
| `DATA_DIR` | `.env` or environment | Empty: `./data` in the current folder | Where the stored log files are read. Overridden by `--data-dir` |
| `DATABASE_URL` | `.env` or environment | None (required) | The database the replay reads from and writes to |
| `REDIS_URL` | `.env` or environment | `redis://127.0.0.1:6379/0` | Redis for the queues. Used only when a failed job is routed |
| `PREDICT_BUDGET_PR_MS` | `.env` or environment | `120000` | The total time the experts get for one replayed run. Slow experts are cut off at this limit |

## Inputs and outputs

| | What | Where / format |
|---|---|---|
| In | Pre-push payload (local diff and config files) | JSON body of `POST /predict` |
| In | Expert answers | JSON list of `RiskOutput` from each expert's `/predict` |
| In | Diagnoses, review decisions, API key requests | JSON bodies, checked against `shared/contracts` |
| In | Failed CI job for the router (`FailedJob`) | Python call to `route_failed_job`. No HTTP endpoint yet |
| In | Settings | Environment variables (see Settings) |
| Out | `PredictResponse` | JSON: `context_id`, `head_sha`, `budget_ms`, `elapsed_ms`, and `results` (one entry per expert, with `state` `ready` or `not_ready`) |
| Out | Saved rows | `platform` tables in Postgres. Each contract is stored whole in a `payload` column |
| Out | Routed failures | `RoutedFailure` saved in `platform.routed_failures`, and one job per chosen expert on its Redis queue |
| Out | Logs | Standard `logging` lines with extra fields. No keys or tokens are logged |

## Settings

| Setting | Where | Default | What it changes |
|---|---|---|---|
| `DATABASE_URL` | `.env` or environment | None (required) | Postgres connection. Without it, the first database call fails with a clear error |
| `REDIS_URL` | `.env` or environment | `redis://127.0.0.1:6379/0` | Redis for the router's expert queues. See the Router section |
| `ROUTER_RULES_PATH` | `.env` or environment | Unset: `config/router_rules.yaml` | Another rules file for the router. See the Router section |
| `PREDICT_BUDGET_PRE_PUSH_MS` | `.env` | `10000` | Total time for one `/predict` (10 s). Also sent to experts as `X-Deadline-Ms` |
| `PREDICT_BUDGET_PR_MS` | `.env` | `120000` | The total time for the experts on one replayed run (the replay CLI uses it). Also the budget for the experts on a push or pull request webhook |
| `EXPERT_CALL_TIMEOUT_SECONDS` | `.env` | `2` | Timeout for `/health` probes and `/review` forwarding |
| `EXPERT_DEPENDENCY_URL` | `.env` | `http://127.0.0.1:8101` | Where the Dependency expert listens |
| `EXPERT_BUILD_URL` | `.env` | `http://127.0.0.1:8102` | Where the Build expert listens |
| `EXPERT_TEST_URL` | `.env` | `http://127.0.0.1:8103` | Where the Test expert listens |
| `EXPERT_INFRA_URL` | `.env` | `http://127.0.0.1:8104` | Where the Infra expert listens |
| `API_ADMIN_TOKEN` | `.env` | Empty | Protects `/api-keys`. Empty means key management is off (503) |
| `GITHUB_WEBHOOK_SECRET` | `.env` | Empty | Secret that signs GitHub webhooks. Empty means `/webhooks/github` answers 503 |
| `GITHUB_APP_ID`, `GITHUB_APP_PRIVATE_KEY_PATH` | `.env` | Empty | The App id and the path of its `.pem`. Needed to get installation tokens |
| `CHECK_RETRY_DELAYS_SECONDS` | `.env` | `30,90,300,900` | When to ask late experts again for the PR check. Empty list means never |
| `DASHBOARD_URL` | `.env` | `http://localhost:5173` | Base of the link in the check's details |
| `DATA_DIR` | `.env` | `./data` | Where logs fetched by webhooks are written (same as the collector) |
| `API_PORT` | `.env.example` only | `8000` | Documents the port. No code reads it; the port comes from the uvicorn command |

Settings are read once, when the app module is imported. Change `.env` and restart the process.

## Example

The test `test_slow_expert_is_not_ready_and_others_still_answer` shows the budget at work. The test sets the budget to 300 ms. The Infra expert answers after 5 seconds.

1. The hook sends the fixture payload with a valid key.
2. Dependency, Build and Test answer in time with one risk each.
3. The Infra call is cut off at 300 ms.
4. The response is still HTTP 200. The Infra entry says `"state": "not_ready"` and `"reason": "timeout"`, with an empty `risks` list.
5. Three predictions are saved. The Infra result is not saved, because only ready results are.

The test fails if the whole call takes 3 seconds or more, which shows the slow expert is cut off and not waited for.

## Limitations and known issues

**Authentication gaps (on purpose, until Phase 3):**

- `GET /runs`, `GET /commits/{sha}/risks`, `GET /failures/{id}` and `POST /reviews` have no authentication. Anyone who can reach the port can read them or post a review. They stay open until the GitHub sign-in (P3.1) and the dashboard (P3.5) add real checks.
- `POST /diagnoses` has no authentication. It is meant for expert workers on the internal network, like the LLM gateway. Add a shared worker token before any public deployment.
- The first API key is made by hand. Call `POST /api-keys` with `API_ADMIN_TOKEN` as the Bearer token and a `user_hash`. The `user_hash` must be 64 lowercase hex characters: the salted hash of the developer's GitHub login, made the same way the sanitiser hashes logins (see `shared/sanitise/README.md`). This stays until GitHub sign-in exists.
- A key is shown once, in the response to `POST /api-keys`. Only its SHA-256 hash is stored. A lost key cannot be recovered; issue a new one and revoke the old.
- No rate limiting. No CORS settings either. The dashboard will need CORS when it is built (P3.5).

**Router (P2.7):**

- Unknown failures are logged only. They are not stored and not queued. See "Unknown failures" above. Showing them on the dashboard needs a contract change, which is still to be proposed.
- The replay CLI calls the router. P3.1 (the webhook) will call it too.
- Repeated routing of the same CI job creates a new `RoutedFailure` and a new job each time. The job id only stops the same `RoutedFailure` from being queued twice.
- The save and the enqueue are two steps, not one transaction. If Redis fails after the save, the failure is stored but not queued, and the error is raised. Running the routing again stores a second row, because each routing makes a new `failure_id`.
- If two experts are chosen and the second enqueue fails, the first job is already on its queue.
- `min_score` must be above zero. The code does not check this. With `min_score` at 0 or below, a failed job where no rule matches gives a zero score for the winner, and the margin check divides by that zero. The shipped value is 1.0.
- The code does not check that weights are positive.
- A rules file that is missing, not valid YAML, or names an unknown expert raises `RouterConfigError` on load. Nothing loads the rules at API startup, so the running API is not affected until a caller routes a job.
- When `route_failed_job` is called without rules, it reads the rules file on every call. The replay CLI passes in rules it loaded once. P3.1 should do the same.
- The matching only sees the failed step names and the log excerpt (the tail of the failed step, up to 20,000 characters), not the full log.
- The patterns in `config/router_rules.yaml` are marked as draft. Each expert owner refines their own block. The margin and `min_score` are guesses until labelled failures exist (open question O7).
- The fixtures in `tests/fixtures/router_failures.json` are sanitised and hand-written. They are not recorded real CI logs. The file says so in its `_note`. Real failure logs should replace some of them later.
- The router tests use fakeredis, not a real Redis. Real RQ behaviour, such as how long a finished job's record is kept, is not tested here.
- The test that checks the worker job paths looks for the worker files. It does not import them. The worker functions themselves are not run by these tests.
- A job with no failed step names is rejected by the `FailedJob` validation (at least one failed step is required).

**Replay CLI (P2.8):**

- Its limits (duplicate rows on re-run, routing ignores `--member`, queued jobs reach real workers, and more) are in the "Replay CLI (P2.8)" section above.

**End-to-end test (P2.9):**

- **Nothing here has been run.** The Dockerfile, the `e2e` compose profile, `make e2e`, the CI step and the test were written without a run. The first CI run will be the first real one.
- **The test writes to the main database.** It uses `DATABASE_URL` directly and does not delete its rows. Each run leaves one routed failure and one diagnosis behind. Unlike the SQL tests, it does not use a throwaway database.
- **It uses the real queue.** It puts a job on the `diagnose:test` queue of the Redis in `REDIS_URL`. Any other Test worker on that Redis would take the job too.
- **One path only.** It covers one Test-expert failure, with the stub worker. It does not cover the other three experts, the real webhook (P3.1), the LLM, or the quality of a diagnosis.
- **Port 8000 is fixed.** The compose `api` service sets `API_PORT: "8000"` and publishes `127.0.0.1:8000`. The `API_PORT` value in `.env` does not change either one.
- **Waits are fixed.** The 60-second wait and the 1-second poll are not settings.
- **The CI logs step comes too late for the API.** It runs after `make e2e` has removed the API and Test containers, so it shows mostly Postgres and Redis. `make e2e` prints the full logs first, while the containers still exist.
- **The CI step has its own limit.** The "End-to-end test" step fails after 15 minutes, even though the job allows 30.
- **Choices with no `# DEFAULT:` marker yet:** the `<db>_api_test` name and the drop-first behaviour in `api_db.py`, the fixed `127.0.0.1:8000` port and the `api` uid 10001 and `API_PORT=8000` in the Dockerfile, and the 15-minute CI step limit.

**Every `# DEFAULT:` added or changed for P2.9** (grep of `services/api/Dockerfile`, `Makefile`, `docker-compose.yml`, `tests/e2e`, `api_db.py` and `pyproject.toml`):

| Where | Choice | Why |
|---|---|---|
| `services/api/Dockerfile` line 5 | Python 3.12.8 on Debian bookworm, pinned | No moving tag, so the image does not change under us |
| `services/api/Dockerfile` line 8 | uv 0.13.1, pinned | The version the lockfile was written with |
| `services/api/Dockerfile` line 34 | `config/` copied twice (see "The API image") | The code finds config in two ways. The second copy is for `shared/file_tags.py` |
| `docker-compose.yml` line 82 | The API healthcheck uses `python`, not `curl` | `python` is always in the image. `/health` answers 200 even when experts are down |
| `docker-compose.yml` line 119 | `testing-api` and `testing-worker` also belong to the `e2e` profile | So `make e2e` has a Test expert to diagnose the job |
| `docker-compose.yml` line 130 | `BACKEND_URL` comes from `EXPERT_BACKEND_URL`, empty by default | Empty keeps the stub behaviour. `make e2e` sets it |
| `Makefile` line 21 | The `e2e` target always stops its own containers, keeps the test's exit code, sets `E2E_REQUIRED=1`, and leaves Postgres and Redis up | Cleans up after a failure, and CI cannot pass by skipping |
| `Makefile` line 38 | `down` also stops the `e2e` profile | A plain `down` skips profile services. The comment still mentions only `experts` |
| `tests/e2e/conftest.py` line 10 | Default API URL `http://127.0.0.1:8000` | The compose `api` service publishes that port |
| `tests/e2e/test_failure_flow_e2e.py` line 30 | 60 s wait, 1 s poll | The stub worker takes about 1 s |
| `tests/e2e/test_failure_flow_e2e.py` line 41 | Ids come from the clock | Repeated runs on one database do not collide |
| `pyproject.toml` line 52 | F811 is ignored for `test_*_sql.py` | Those tests import the `engine` fixture from `api_db.py` (new helper) |

**Not built yet:**

- The webhook and GitHub sign-in endpoints return 501.
- `PREDICT_BUDGET_PR_MS` (2 minutes for PR notes) is used by the replay CLI only. The webhook task (P3.1) will use it for PR notes.
- The API has a Dockerfile and a Compose service in the `e2e` profile (P2.9). Neither has been built or run yet. `make up-experts` does not start the API. Only `make e2e` does.

**Behaviour to know about:**

- `elapsed_ms` in `/predict` includes building and saving the context, not only the time the experts take. It is measured from the start of the request.
- The replay flag is on `routed_failures`, not on `diagnoses`. Diagnoses inherit it through their `failure_id`. Predictions carry their own `is_replay` flag, and `/predict` always saves with `is_replay` false.
- Contexts saved with `event = "replay"` do not appear in `GET /runs`.
- A `/predict` that is partly "not ready" still returns HTTP 200. Only a bad payload (422) or a missing or bad key (401) stops it.
- The default store is created once per process (one database engine). Database calls run in worker threads so they do not block the event loop.
- `GET /health` returns `degraded` when any expert is down. The HTTP status is still 200.

**Every `# DEFAULT:` choice in this component:**

| Where | Choice | Why |
|---|---|---|
| `app.py` line 3 | No `from __future__ import annotations` in `app.py` | FastAPI needs real annotation objects to resolve `Depends(...)` in the local auth helpers |
| `app.py` line 117 (`/runs`) | Newest 50 contexts by default, capped at 200 per request | A history page should not load everything |
| `app.py` line 126 (`/diagnoses`) | Unauthenticated, for internal workers | Same model as the LLM gateway. Revisit before any public deployment |
| `config.py` line 10 | Local expert ports 8101 to 8104 | Ports from `docs/experts/README.md`. Compose overrides them with service names |
| `config.py` line 42 | 2 s timeout for `/health` and `/review` forwarding | A dead expert must not hang these calls |
| `config.py` line 46 | One admin token for key management; empty means off (503) | Until GitHub sign-in exists. Never open by accident |
| `auth.py` line 9 | Keys start with `piq_` | Makes a leaked key easy to spot in secret scanners |
| `auth.py` line 21 | Unsalted SHA-256 for key hashes | Keys are 256-bit random values, not passwords. A plain hash allows a simple indexed lookup |
| `schemas.py` line 44 | `user_hash` is 64 hex characters (a SHA-256 hash of the salted login) | Matches the sanitiser's hash format |
| `router.py` line 137 | Ties keep the order of the rules file | `sorted()` is stable, and the order is simple to explain |
| `router.py` line 145 | The runner-up must also reach `min_score` | One weak match should not start a second diagnosis |
| `router.py` line 149 | Confidence is the winner's share of the total score | A simple number from 0 to 1. Not a calibrated probability |
| `failure_queue.py` line 16 | Redis fallback `redis://127.0.0.1:6379/0` when `REDIS_URL` is unset | Same local Redis as `.env.example` and the expert workers |
| `failure_queue.py` line 45 | Job id is `<failure_id>-<expert>` | A failure put on the same queue twice does not create a second job |
| `config/router_rules.yaml` line 5 | `two_expert_margin: 0.25` | A guess. Tune it once labelled failures exist (open question O7) |
| `config/router_rules.yaml` line 6 | `min_score: 1.0` | Not marked `# DEFAULT:` in the file yet. Treat as a default until the team reviews it |
| `.env.example` line 86 | `ROUTER_RULES_PATH` unset means `config/router_rules.yaml` | The rules file lives at the repo root |
| `services/api/src/api/replay.py` (5 choices) | Listed in full in the "Replay CLI (P2.8)" section | Replay is the only caller of the router so far |

## Dashboard sign-in, own keys and failure list (P3.5)

People sign in with GitHub so the dashboard can show their data.

1. `GET /auth/github/login` sends the browser to GitHub. It also sets a short-lived signed cookie holding a random `state`, so the callback can tell the request really started here.
2. GitHub sends the browser back to `GET /auth/github/callback`. The API checks the `state`, trades the code for a GitHub token, reads the profile once (`GET https://api.github.com/user`) and throws the token away. It is never stored or logged.
3. The API then sets the `piq_session` cookie: a signed (HMAC-SHA256 with `SESSION_SECRET`), HttpOnly, SameSite=Lax cookie that lasts 7 days and holds the GitHub id, login, avatar and `user_hash` (`sha256(IDENTITY_SALT + login)`, the same hash the rest of the platform uses). The browser goes on to `DASHBOARD_URL`.
4. `GET /auth/me` returns `{login, avatar_url, user_hash}` or 401. `POST /auth/logout` returns 204 and clears the cookie.

If `GITHUB_APP_CLIENT_ID`, `GITHUB_APP_CLIENT_SECRET` or `SESSION_SECRET` is empty, sign-in answers 503. It is never open.

With a session, a developer manages their own pre-push keys: `GET /me/api-keys` (ids and dates only), `POST /me/api-keys` (201 with the plain key, shown once) and `DELETE /me/api-keys/{id}` (204; 404 for someone else's key). The admin `/api-keys` endpoints are unchanged.

`GET /failures?repo=&limit=50` (max 200) lists routed failures, newest first (replay failures are left out): `failure_id`, `repo`, `head_sha`, `run_id`, `job_name`, `expert` (the first expert it was routed to), `created_at`, `diagnosis_count`, `latest_review` (`accept`, `reject`, `modify`, `request_revision` or `null`).

The dashboard calls the API through `/api/*` (the proxy removes the prefix), so the cookies are same-origin and no CORS setup is needed.

New settings: `SESSION_SECRET`, `API_PUBLIC_URL` (default `http://localhost:8000`), `DASHBOARD_URL` (default `http://localhost:5173`), `SESSION_COOKIE_SECURE` (default `true`; set `false` for plain-http localhost), `SESSION_TTL_SECONDS` (default 7 days).

Choices not in the docs: stdlib HMAC cookie instead of a new dependency; the `state` cookie lasts 10 minutes; `/failures` hides replay failures. Tests: `services/api/tests/test_sign_in.py`.


## Related

- `docs/ARCHITECTURE.md` section 4 (the endpoint list) and section 3 (the budgets)
- `docs/PLAN.md` P2.6 (backend API), P2.7 (router, this section), P2.8 (replay CLI, see "Replay CLI"), P2.9 (end-to-end test), P3.1 (GitHub App and webhook), P3.5 (dashboard)
- `services/features/README.md`: the replay context builder (Replay section) that the replay CLI calls
- `docs/DECISIONS.md` D19 (the rules router)
- `config/router_rules.yaml`: the rules, weights, `min_score` and margin
- `shared/contracts/README.md`: the models the API sends and receives, including `RoutedFailure`
- `shared/db/README.md`: the `platform` tables and `DATABASE_URL`
- `services/features/README.md`: builds the `CommitContext` from the pre-push payload
- `experts/testing/README.md` and the other three expert guides: the services `/predict`, `/review` and `/health` are called on, and each worker reads its `diagnose:<expert>` queue
- `shared/sanitise/README.md`: how logins are hashed for `user_hash`
- `docs/COMPONENTS.md`: the list of all components
