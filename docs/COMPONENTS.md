# Components

A plain-language guide to every part of the system. Each component has a `README.md` in its
own folder, written from `docs/templates/COMPONENT_README.md`. Start here to understand
what exists and how far along it is.

| Component | What it does | Guide | Status |
|---|---|---|---|
| Contracts | The messages services send each other | `shared/contracts/README.md` | Working (v1.1) |
| Database layer | Describes the raw and platform tables and creates them with Alembic migrations | `shared/db/README.md` | Working (P0.6) |
| GitHub client | Reads the GitHub REST API and raw files with rate-limit waits, retries and date-window splitting | `shared/github/README.md` | Working (P1.1) |
| Collector | Loads and validates each member's repo list, decides which workflow files are relevant, backfills the last 90 days of runs, jobs, logs, commits and config files, keeps them up to date with a daily sync, reports what is stored, gaps and the rate limit (`status`), and bundles the database and files into one archive that can be imported elsewhere (`make export-data`, `make import-data`) | `services/collector/README.md` | Working (P1.2, P1.5 backfill, P1.6 daily and status, P1.7 export and import). Database integration tests not yet run (no Docker when written) |
| Re-run check script | Checks each member's repos on GitHub: runs and failure rate over 90 days, re-run share, real retention, test workflows and licence match, written to a Markdown and CSV report | `scripts/README.md` | Working (P1.3) |
| Sanitiser | Removes secrets (tokens, keys, passwords, auth headers) from CI logs and counts the removals; hashes author logins with a team salt | `shared/sanitise/README.md` | Working (P1.4) |
| Features service | Gives each changed file its tags (such as `test`, `lockfile`, `workflow`) from `config/file_tags.yaml` and drops patches that are too big to store. Builds the `CommitContext` for a live commit (GitHub API), a stored CI run (replay, from the raw tables) or an unpushed commit (pre-push payload), with one shared assembly step, so the three agree | `services/features/README.md` | Working (P2.2 file tagger, P2.3 CommitContext builders). The SQL replay test (integration) has never been run |
| LLM gateway | The only door to the language model. Takes a request from an expert, returns an answer checked against the requested JSON Schema (one retry if invalid), caches identical requests, enforces a monthly spending cap per expert and logs every paid or cached call to `platform.llm_calls`. Only the mock provider exists (decision O5 open) | `services/llm_gateway/README.md` | Working with the mock provider only (P2.4). The SQL log test (integration) has never been run |
| Expert template | The starting point for all four experts: a stub API (`/predict`, `/review`, `/health`), a stub diagnose worker, a Dockerfile and tests. Copied, then renamed, for each expert | `experts/_template/README.md` | Stub (P2.1). Tests pass; Docker not built or run |
| Dependency expert (stub) | Stub of the Dependency and Runtime expert (port 8101, queue `diagnose:dependency`). Owner Member 1 | `experts/dependency/README.md` | Stub (P2.1). Tests pass; Docker not built or run |
| Build expert (stub) | Stub of the Build and Code Quality expert (port 8102, queue `diagnose:build`). Owner Member 2 | `experts/build/README.md` | Stub (P2.1). Tests pass; Docker not built or run |
| Test expert (stub) | Stub of the Test and App Behaviour expert (port 8103, queue `diagnose:test`). Its `/predict` is per test job. Owner Member 3 | `experts/testing/README.md` | Stub (P2.1). Tests pass; Docker not built or run |
| Infra expert (stub) | Stub of the Infrastructure and Deployment expert (port 8104, queue `diagnose:infra`). Owner Member 4 | `experts/infra/README.md` | Stub (P2.1). Tests pass; Docker not built or run |

<!-- Claude Code: add a row whenever a component gets its README, and keep Status current. -->
