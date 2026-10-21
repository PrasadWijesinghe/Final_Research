# Phase 2 — task notes for the phase lead

Phase 2 = core services, tasks P2.2–P2.9 (P2.1 is done: `experts/_template` + four stubs).
Read docs/ARCHITECTURE.md §3–5, docs/DATA.md §4–5, docs/experts/README.md and shared/contracts/
before briefing implementers; pass them the relevant sections by reference.

## Constraints (all of Phase 2)
- **Never start Docker or build images** — it freezes the user's laptop. Integration/compose tests
  are written and marked (`integration`, or a new `e2e` marker) so they auto-skip locally; CI runs
  them (`make up` then `make check` in .github/workflows/ci.yml — extend CI if a task needs more,
  e.g. `make up-experts` for P2.9). List every test that did not run in each task report.
- No real network in tests (respx / httpx ASGITransport / fakeredis). No LLM calls (LLM_PROVIDER=mock).
  No model downloads in tests.
- Contracts are frozen. Everything crossing a service boundary is a `shared.contracts` model.
- `services/<name>/` = uv member with `pyproject.toml`, `src/<pkg>/`, `tests/`; remove from root
  workspace `exclude`, add to mypy `files`/`mypy_path` (follow services/collector).
- **Services never import experts** (and experts never import services). Tests that need an expert
  use respx routes returning contract-valid JSON, or the E2E stack in CI.
- New env vars go in `.env.example`; new config in `config/`.

## Package placement (decided — mark # DEFAULT: in code where the docs are silent)
- `services/features/` (pkg `features`): file tagger (P2.2), CommitContext builders + workflow parser
  (P2.3), replay CLI `replay` (P2.8) — ARCHITECTURE §2/§3.4 put replay here.
- `services/llm_gateway/` (pkg `llm_gateway`, FastAPI, port 8200): P2.4.
- `shared/embeddings/` (exists, empty): P2.5.
- `services/api/` (pkg `api`, FastAPI, port 8000): backend API (P2.6) + router (P2.7).

## Task notes
- **P2.2 File tagger.** Read `config/file_tags.yaml`; tags per DATA §5 (each tag independent, a file can
  have several, `other` only when nothing matched; `test_config` includes `vite.config.*` only when the
  content has a `test:` block — so the tagger takes optional content). `source` = JS/TS not `test`.
  Size limits: patches > 1 MB dropped (`patch=None`, context marked truncated); lockfiles always by
  reference. Use the contract's `FileTag` enum. Compare with services/collector/src/collector/file_tags.py
  (collector already has some tag logic for snapshots): make ONE implementation — move the shared tag
  logic so both use it (features may import collector? No: put the pure tagger in `shared/` only if both
  need it, else collector imports features' tagger via a shared module — decide, mark # DEFAULT:).
  Accept: table-driven tests for every tag, multi-tag files, truncation.
- **P2.3 CommitContext builders.** Three builders producing identical structures: **live** (GitHub API via
  shared.github), **replay** (raw tables; `history_cutoff` = the run's `created_at`; only rows created
  before the cutoff), **pre-push** (local diff payload from the hook — define the payload schema as a
  Pydantic model in services/features, NOT in contracts; the API in P2.6 accepts it). Workflow YAML parser
  → contract `WorkflowJob` (job key, runs-on, Node versions incl. matrix, permissions, container, `run:`
  commands). Accept: the same commit built live and by replay gives equal contexts apart from `event` and
  `history_cutoff` — test with respx fixtures for live and an in-memory/SQL fixture for replay (SQL
  version integration-marked).
- **P2.4 LLM gateway.** `POST /complete` `{expert, prompt_version, messages, json_schema}` → validated JSON.
  Provider interface; mock provider returns schema-valid fixtures (e.g. generated from the schema or
  canned per expert). Request cache (key = hash of the normalised request; store in Redis or Postgres —
  pick the simplest testable one). Per-expert monthly spending cap from config (e.g. `config/llm.yaml`,
  # DEFAULT: values); a call over the cap is refused (HTTP 429/402 with a clear body). Log every call to
  `platform.llm_calls` (model, prompt_version, tokens, cost, cache_hit, latency). JSON-schema validation of
  the answer with ONE retry on invalid output. LLM_API_KEY only here, never logged. No real provider yet
  (O5: provider not chosen) — only the interface + mock. Accept: all behaviour tested with the mock; over-cap refused.
- **P2.5 Embeddings and hybrid search.** `shared/embeddings`: granite-embedding-small-english-r2 (384-d)
  via sentence-transformers, **lazy-loaded**; make sentence-transformers/torch an OPTIONAL extra
  (`pipelineiq-shared[embeddings]`) so `make setup` stays light, and use a deterministic fake embedder in
  tests (# DEFAULT:). Log normalisation (strip timestamps, ANSI, hex ids, paths → placeholders). Per-expert
  pgvector collections: a helper that creates/uses `<expert_schema>.<collection>` with `vector(384)` +
  `tsvector`; hybrid search = Postgres full-text + vector, merged by reciprocal rank fusion. Check the
  embedding dimension against the table. Accept: top-k on a small fixture set (RRF unit-tested in pure
  Python; the SQL path integration-marked); dimension check tested.
- **P2.6 Backend API.** Endpoints in ARCHITECTURE §4; `/webhooks/github` and `/auth/github/*` return 501.
  `POST /predict` (pre-push payload + `Authorization: Bearer <key>`, keys stored hashed in
  `platform.api_keys`; `POST /api-keys` returns the plain key once) → build CommitContext (P2.3 pre-push
  builder) → parallel fan-out to the four experts (URLs from env/config) with the **10 s** budget for
  pre-push and **2 min** for PR notes, sending `X-Deadline-Ms`; late experts → "not ready". Saves
  contracts to `platform` (commit_contexts, predictions, diagnoses, reviews). `POST /diagnoses`,
  `POST /reviews` (forward to the expert's `/review`), `GET /commits/{sha}/risks`, `GET /runs`,
  `GET /failures/{id}`, `GET /health`. Accept: `POST /predict` with a fixture pre-push payload returns
  four experts' results within budget (experts mocked with respx returning contract-valid outputs; one
  slow expert shows "not ready").
- **P2.7 Router.** In services/api, reads `config/router_rules.yaml` (patterns on failed step name and log
  lines, weights, `two_expert_margin`). Score → top-1, or top-2 when the margin is small; never the same
  expert twice; at most two. Enqueue `RoutedFailure`s on RQ queues `diagnose:<Expert value>` — note the
  Test expert's queue is `diagnose:test`. Accept: 20 fixture failures (5 per category) route correctly;
  close cases go to two experts. Fixtures: realistic sanitised step names + log excerpts.
- **P2.8 Replay CLI.** `uv run replay --member test --from YYYY-MM-DD --to YYYY-MM-DD`: for each stored run
  in the window, build the replay CommitContext (P2.3), call the experts' `/predict`, store predictions
  with `is_replay=true`; for failed jobs, route (P2.7) and store `RoutedFailure` with `replay=true`
  (diagnoses come back through the queue/workers → POST /diagnoses with the replay flag). **Never touches
  GitHub** — enforce structurally (no GitHub client constructed; a test proves no HTTP to api.github.com).
  Accept: a replay run produces stored predictions and diagnoses with replay flags (DB part integration-marked).
- **P2.9 End-to-end test.** Fixture failure → router → Redis queue → stub worker → `POST /diagnoses` →
  visible via `GET /failures/{id}`. Runs in CI with Docker Compose: add the backend (and whatever it needs)
  to docker-compose.yml (profile, like the experts), add an `e2e` marker + a Makefile target, and extend
  .github/workflows/ci.yml so CI runs it. Locally it auto-skips. Keep the stack minimal (postgres, redis,
  api, testing expert worker is enough).

## Clarification (after batch A)
- `services/features` is a library used by the backend and replay (ARCHITECTURE §2), so
  `services/api` MAY import `features` (PrePushPayload, builders, tagger). Services still never
  import experts, and experts never import services.
- Batch A left: no Dockerfile/compose entry for the LLM gateway (add one in P2.9 only if the E2E
  stack needs it); gateway env vars LLM_GATEWAY_PORT/URL are unused until something calls it.
