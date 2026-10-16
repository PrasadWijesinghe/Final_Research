# Database layer (shared/db)

**In one sentence:** Describes the PostgreSQL tables that the collector and the backend write to, and holds the scripts that create them.

**Status:** working · **Owner:** shared · **Plan tasks:** P0.6

## What it does

PipelineIQ keeps its data in one PostgreSQL database per member. The database has two groups of tables.

- The `raw` schema holds what the collector pulls from GitHub: repos, workflow runs, jobs, steps, commits, changed files, file snapshots and log references. It has 10 tables.
- The `platform` schema holds what the backend produces: commit contexts, predictions, routed failures, diagnoses, reviews, failure labels, API keys and LLM call records. It has 8 tables.

This folder does three jobs. It describes each table in Python code. It holds the migrations, which are numbered scripts that create or change tables in a fixed order, so every machine ends up with the same schema. It also gives the rest of the code one place to read the database address from.

The tables are described with SQLAlchemy. SQLAlchemy is a Python library that maps Python classes to database tables. Each class is called a model. The migrations use Alembic, the migration tool that goes with SQLAlchemy. A migration is a script that moves the database from one version to the next.

Each platform table keeps the whole contract message in a `payload` column, which is a JSON column. A few fields are copied into normal columns so they can be searched quickly.

## How it works

1. `models.py` describes each of the 18 tables as a Python class. SQLAlchemy uses these classes to read and write rows.
2. `session.py` reads `DATABASE_URL` from the environment and makes a connection engine. The address is never written into a file.
3. `alembic.ini` tells Alembic where the migration scripts live.
4. `migrations/env.py` runs when you migrate. It reads `DATABASE_URL` and connects to the database.
5. `migrations/versions/0001_raw_and_platform.py` is the first migration. It enables the `vector` extension, creates the `raw` and `platform` schemas, and then creates all 18 tables with their indexes.
6. Alembic records the version it reached in a table called `alembic_version`, in the `public` schema. The next run applies only the newer migrations.

```mermaid
flowchart LR
    A["make migrate"] --> B["alembic.ini"]
    B --> C["env.py"]
    C -->|"reads DATABASE_URL"| D[("Postgres")]
    C --> E["versions/0001 ..."]
    E -->|"creates tables"| D
    F["models.py"] -.->|"used by tests and code"| D
```

## How to run it

You need Docker Desktop running and `make setup` done once.

```bash
make up                 # start Postgres (pgvector) and Redis in Docker
export DATABASE_URL=postgresql+psycopg://pipelineiq:change-me@127.0.0.1:5432/pipelineiq
make migrate            # create the schemas and all 18 tables
uv run alembic -c shared/db/alembic.ini current    # check which version is applied
```

The `DATABASE_URL` above is the placeholder from `.env.example` and the Docker Compose defaults. Nothing in this repo reads `.env` for these commands, so export the variable in each new terminal.

Use `127.0.0.1`, not `localhost`. On Windows, `localhost` tries IPv6 first and each connection can stall for about 2 seconds.

What you should see when it works:

- `make migrate` finishes with no error. Alembic does not print progress lines, because `alembic.ini` logs at WARNING level and Alembic's progress messages are at INFO level.
- The `current` command prints `0001 (head)`.

If `DATABASE_URL` is not set, `make migrate` stops with: `DATABASE_URL is not set. Copy .env.example to .env and export it ...`

Migrations are not applied in offline mode. There is no way to produce a plain SQL script. See the limitations below.

## Inputs and outputs

| | What | Where / format |
|---|---|---|
| In | Table definitions | `shared/db/models.py` (Python classes) |
| In | Database address | `DATABASE_URL` environment variable |
| In | Migration scripts | `shared/db/migrations/versions/*.py` |
| Out | 10 raw tables | Schema `raw` in Postgres |
| Out | 8 platform tables | Schema `platform` in Postgres |
| Out | Current version marker | Table `public.alembic_version` |
| Out | `vector` extension | Enabled in the database by migration 0001 |

## Settings

| Setting | Where | Default | What it changes |
|---|---|---|---|
| `DATABASE_URL` | Environment variable (form shown in `.env.example`) | None, required | Which database migrations and tests connect to |
| `script_location` | `shared/db/alembic.ini` | `%(here)s/migrations` | Where Alembic finds the migration scripts. Works from any folder |
| Connection pooling | `migrations/env.py` | Off (`NullPool`) | Each migration run opens one connection and closes it when done |

## Example

The round-trip test in `shared/tests/test_db.py` shows the basic use.

- **Input:** a repo called `octo/roundtrip` with id 999000001. Then one workflow run for that repo, with `pr_numbers` set to `[1, 2]` and `created_at` set to 2026-01-02 03:04:05 UTC. The test adds the repo first, then the run, and flushes after each row.
- **Output:** when the run is read back, `pr_numbers` is `[1, 2]` and the timestamp matches. `source` is `collector`, even though the test did not set it. That value comes from the model's default. The test then rolls back, so nothing stays in the database.

## Adding a new migration

1. Change the model in `models.py` so it describes the new table or column.
2. Create `shared/db/migrations/versions/0002_<short_name>.py`. Copy the header and imports from `0001`. Set `revision = "0002"` and `down_revision = "0001"`.
3. Write `upgrade()` to make the change, and `downgrade()` to undo it.
4. Never edit `0001`, or any migration that someone has already applied. This is a rule in `docs/CONVENTIONS.md`.
5. Run `make migrate`. Then run the `current` command from above. It should print `0002 (head)`.
6. Run the tests (see below).

There is no `script.py.mako` template in `migrations/`, so `alembic revision` does not generate the file for you. I did not run that command to confirm the exact error. Write the file by hand, or add the template as a follow-up. Autogenerate (`alembic revision --autogenerate`) is not reliable yet, because `include_schemas` is not set. See the limitations.

## Running the tests

The tests are in `shared/tests/test_db.py`. They fall into two groups.

- **No database needed:** `test_missing_database_url_gives_clear_error`, `test_every_model_table_is_in_raw_or_platform` (checks for 18 tables in two schemas) and `test_database_url_is_never_hard_coded`.
- **Need a live database (marked `integration`):** `test_upgrade_downgrade_upgrade`, `test_extension_and_schemas_exist` and `test_round_trip_one_row_per_table`. The root `conftest.py` skips these automatically when the database is not reachable.

```bash
make up
export DATABASE_URL=postgresql+psycopg://pipelineiq:change-me@127.0.0.1:5432/pipelineiq
uv run pytest shared/tests/test_db.py                  # all tests
uv run pytest shared/tests/test_db.py -m integration   # only the database tests
```

When the database is up, the integration tests run and pass. When it is down, they show as skipped with the reason "database in DATABASE_URL is unreachable (run `make up`)".

**Warning: the integration tests wipe the database they point at.** `test_upgrade_downgrade_upgrade` downgrades to `base`, which drops every `raw` and `platform` table, and then upgrades again. Any data in that database is lost. Only point `DATABASE_URL` at the local Docker database, and never at a database that holds data you need. `make export-data` is not built yet (it arrives in P1.7), so there is no backup command.

## Limitations and known issues

**Known behaviour**

- **No offline mode.** `env.py` only runs against a live database. You cannot produce a SQL script to run by hand.
- **Downgrade drops tables only.** Downgrade removes the 18 tables. It leaves the `raw` and `platform` schemas and the `vector` extension in place, so `downgrade base` is not a full reset.
- **No ORM relationships.** The models do not define links between classes. Insert parent rows before child rows: `repos` before `sync_state`, `workflows` and `workflow_runs`; `jobs` before `steps` and `logs`; `commits` before `commit_files`.
- **Platform tables have no foreign keys between them, on purpose.** Messages can arrive out of order, for example a diagnosis before its failure is stored. The database will accept that order. Code that needs an order must check it.
- **`alembic_version` lives in `public`.** No `version_table_schema` is set, so Alembic uses the default schema.
- **`alembic check` reports drift.** `include_schemas` is not set in `env.py`, so Alembic only compares the default schema and reports the `raw` and `platform` tables as differences. This is a known limitation. Write migrations by hand until it is fixed.
- **No `script.py.mako` template.** See "Adding a new migration".
- **Models and migration 0001 are written separately.** Migration 0001 repeats what the models say. Nothing checks them against each other, except the tests that touch the columns.
- **`api_keys.id` has no database default.** The default is set in Python only. A raw SQL insert must supply the id. The same applies to the UUID keys of the other platform tables, which come from the contract message.
- **Index list may be short.** `docs/DATA.md` section 4.2 lists many columns as "indexed" (for example `history_cutoff`, `event`, `alert` and `is_replay`). Migration 0001 creates only the composite indexes listed in the code. Someone should decide whether DATA.md means "stored as a column" or "indexed".

**Every `# DEFAULT:` in this folder**

1. `models.py`, top of file: every datetime is `timestamptz`, every list is a native Postgres array (`text[]` or `bigint[]`), and raw GitHub and contract payloads are `jsonb`.
2. `models.py`, raw schema: primary keys are the natural keys GitHub gives. Jobs are keyed by `job_id` alone, because job ids are unique across attempts.
3. `models.py`, platform schema: platform tables use the contract's own UUID as the key, and keep the whole message in `payload`.
4. `models.py`, `FailureLabelRow`: a surrogate bigint `id`, because the contract has no id of its own.
5. `models.py`, `LlmCall`: a surrogate bigint `id`, because `llm_calls` has no natural id.
6. `migrations/env.py`: offline (SQL script) mode is not supported.
7. `migrations/versions/0001_raw_and_platform.py`, `downgrade()`: schemas and the extension are not dropped, because other migrations and the init script share them.
8. `shared/tests/test_db.py`, round-trip test: the test uses odd ids far from real GitHub ids. Everything is rolled back anyway.

Adjacent defaults outside this folder, listed so nothing is missed:

- `Makefile`, `migrate` target: the Alembic config lives in `shared/db`, and the URL comes from `DATABASE_URL`.
- `Makefile`, `schemas` target: schemas are generated on demand and not committed.
- `shared/pyproject.toml`: packaging layout (the package folder is `./shared`).
- `conftest.py` (repo root): Postgres on the standard port when `DATABASE_URL` has no port.

## Related

- Plan task P0.6 in `docs/PLAN.md` (this component). P0.7 onboarding will point to `make migrate`.
- `docs/DATA.md` section 4: the table list this code follows.
- `docs/CONVENTIONS.md`: the rule for changing the database (new migration only).
- `docker/postgres/init/01-init.sql`: creates the `vector` extension and the schemas when the container first starts.
- `shared/contracts/`: the messages that the platform tables store in `payload`.
- `docker-compose.yml`: the Postgres and Redis containers used by `make up`.
