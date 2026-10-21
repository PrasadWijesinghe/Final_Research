# Backend API

**In one sentence:** The backend service (FastAPI, port 8000) that the pre-push hook, the workers and the dashboard talk to. It asks the four experts for predictions, stores what happens, and keeps the history.

**Status:** working for pre-push predictions, history, reviews and API keys · **Owner:** shared · **Plan tasks:** P2.6 (router P2.7 not built yet)

## What it does

The backend is the one front door for the platform. A developer's pre-push hook sends the local changes here before a push. The backend builds a `CommitContext` (a standard description of the commit, see `shared/contracts`), asks all four experts for a risk score at the same time, and returns the four answers together. The backend also saves what it did, so the history can be shown later.

Other callers use the same service. Expert workers post their finished diagnoses here. The dashboard reads the risks, the history and a failure with its diagnoses, and it posts the developer's review decision. The backend forwards each review to the expert that wrote the diagnosis.

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

The in-memory tests use fake expert servers (respx routes), so they make no network calls. Use `127.0.0.1`, not `localhost`, on Windows. Otherwise each connection can wait about 2 seconds.

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

## Inputs and outputs

| | What | Where / format |
|---|---|---|
| In | Pre-push payload (local diff and config files) | JSON body of `POST /predict` |
| In | Expert answers | JSON list of `RiskOutput` from each expert's `/predict` |
| In | Diagnoses, review decisions, API key requests | JSON bodies, checked against `shared/contracts` |
| In | Settings | Environment variables (see Settings) |
| Out | `PredictResponse` | JSON: `context_id`, `head_sha`, `budget_ms`, `elapsed_ms`, and `results` (one entry per expert, with `state` `ready` or `not_ready`) |
| Out | Saved rows | `platform` tables in Postgres. Each contract is stored whole in a `payload` column |
| Out | Logs | Standard `logging` lines with extra fields. No keys or tokens are logged |

## Settings

| Setting | Where | Default | What it changes |
|---|---|---|---|
| `DATABASE_URL` | `.env` or environment | None (required) | Postgres connection. Without it, the first database call fails with a clear error |
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

**Not built yet:**

- The router and enqueueing of `RoutedFailure`s (P2.7) are not here. The store already has `save_routed_failure`, which the router will call. Until then, `GET /failures/{id}` only finds failures that someone saved by hand.
- `PREDICT_BUDGET_PR_MS` (2 minutes for PR notes) is read but nothing uses it yet. It waits for the webhook task (P3.1).
- The webhook and GitHub sign-in endpoints return 501.
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

## Related

- `docs/ARCHITECTURE.md` section 4 (the endpoint list) and section 3 (the budgets)
- `docs/PLAN.md` P2.6 (this task), P2.7 (router), P2.9 (end-to-end test), P3.1 (GitHub App), P3.5 (dashboard)
- `shared/contracts/README.md`: the models the API sends and receives
- `shared/db/README.md`: the `platform` tables and `DATABASE_URL`
- `services/features/README.md`: builds the `CommitContext` from the pre-push payload
- `experts/testing/README.md` and the other three expert guides: the services `/predict`, `/review` and `/health` are called on
- `shared/sanitise/README.md`: how logins are hashed for `user_hash`
- `docs/COMPONENTS.md`: the list of all components
