# Backend API

**In one sentence:** The backend service (FastAPI, port 8000) that the pre-push hook, the workers and the dashboard talk to. It asks the four experts for predictions, stores what happens, keeps the history, and sorts failed CI jobs to the expert that should look at them (the router).

**Status:** working for pre-push predictions, history, reviews and API keys (P2.6). The router (P2.7) is written and has unit tests, but nothing calls it yet · **Owner:** shared · **Plan tasks:** P2.6, P2.7

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
| `POST /webhooks/github` | GitHub (later) | None | Returns 501 until P3.1 |
| `GET` or `POST /auth/github/...` | Dashboard (later) | None | Returns 501 until P3.1 |
| `GET /health` | Anyone | None | Backend status plus each expert's health |

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

Nothing calls the router yet. The function `route_failed_job` is ready for two callers:

- P2.8, the replay command. It will pass in rules it has already loaded.
- P3.1, the GitHub webhook.

Until one of them calls it, `GET /failures/{id}` only finds failures that someone saved by hand.

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
| `PREDICT_BUDGET_PR_MS` | `.env` | `120000` | Read into settings but not used yet (see Limits) |
| `EXPERT_CALL_TIMEOUT_SECONDS` | `.env` | `2` | Timeout for `/health` probes and `/review` forwarding |
| `EXPERT_DEPENDENCY_URL` | `.env` | `http://127.0.0.1:8101` | Where the Dependency expert listens |
| `EXPERT_BUILD_URL` | `.env` | `http://127.0.0.1:8102` | Where the Build expert listens |
| `EXPERT_TEST_URL` | `.env` | `http://127.0.0.1:8103` | Where the Test expert listens |
| `EXPERT_INFRA_URL` | `.env` | `http://127.0.0.1:8104` | Where the Infra expert listens |
| `API_ADMIN_TOKEN` | `.env` | Empty | Protects `/api-keys`. Empty means key management is off (503) |
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
- Nothing calls the router yet. P2.8 and P3.1 will call `route_failed_job`.
- Repeated routing of the same CI job creates a new `RoutedFailure` and a new job each time. The job id only stops the same `RoutedFailure` from being queued twice.
- The save and the enqueue are two steps, not one transaction. If Redis fails after the save, the failure is stored but not queued, and the error is raised. Running the routing again stores a second row, because each routing makes a new `failure_id`.
- If two experts are chosen and the second enqueue fails, the first job is already on its queue.
- `min_score` must be above zero. The code does not check this. With `min_score` at 0 or below, a failed job where no rule matches gives a zero score for the winner, and the margin check divides by that zero. The shipped value is 1.0.
- The code does not check that weights are positive.
- A rules file that is missing, not valid YAML, or names an unknown expert raises `RouterConfigError` on load. Nothing loads the rules at API startup, so the running API is not affected until a caller routes a job.
- When `route_failed_job` is called without rules, it reads the rules file on every call. P2.8 and P3.1 should pass rules they have already loaded.
- The matching only sees the failed step names and the log excerpt (the tail of the failed step, up to 20,000 characters), not the full log.
- The patterns in `config/router_rules.yaml` are marked as draft. Each expert owner refines their own block. The margin and `min_score` are guesses until labelled failures exist (open question O7).
- The fixtures in `tests/fixtures/router_failures.json` are sanitised and hand-written. They are not recorded real CI logs. The file says so in its `_note`. Real failure logs should replace some of them later.
- The router tests use fakeredis, not a real Redis. Real RQ behaviour, such as how long a finished job's record is kept, is not tested here.
- The test that checks the worker job paths looks for the worker files. It does not import them. The worker functions themselves are not run by these tests.
- A job with no failed step names is rejected by the `FailedJob` validation (at least one failed step is required).

**Not built yet:**

- The webhook and GitHub sign-in endpoints return 501.
- `PREDICT_BUDGET_PR_MS` (2 minutes for PR notes) is read but nothing uses it yet. It waits for the webhook task (P3.1).
- There is no Dockerfile and no entry in `docker-compose.yml` for the API. The brief places that in P2.9. Note that P2.9 in `docs/PLAN.md` is the end-to-end test, so the owner of the Dockerfile is not yet clear in the plan.

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

## Related

- `docs/ARCHITECTURE.md` section 4 (the endpoint list) and section 3 (the budgets)
- `docs/PLAN.md` P2.6 (backend API), P2.7 (router, this section), P2.8 (replay, which will call the router), P2.9 (end-to-end test), P3.1 (GitHub App and webhook), P3.5 (dashboard)
- `docs/DECISIONS.md` D19 (the rules router)
- `config/router_rules.yaml`: the rules, weights, `min_score` and margin
- `shared/contracts/README.md`: the models the API sends and receives, including `RoutedFailure`
- `shared/db/README.md`: the `platform` tables and `DATABASE_URL`
- `services/features/README.md`: builds the `CommitContext` from the pre-push payload
- `experts/testing/README.md` and the other three expert guides: the services `/predict`, `/review` and `/health` are called on, and each worker reads its `diagnose:<expert>` queue
- `shared/sanitise/README.md`: how logins are hashed for `user_hash`
- `docs/COMPONENTS.md`: the list of all components
