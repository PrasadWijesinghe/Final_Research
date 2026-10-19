# Works with GNU make on Linux and on Windows (Git Bash). Only POSIX sh features are used.
# Load .env when present (CI has none and sets variables directly). Values are never echoed.
# DEFAULT: plain `include` of KEY=value lines; do not quote values or add `export` inside .env.
-include .env
export

.PHONY: setup up up-experts down check migrate schemas export-data import-data

setup:
	uv sync --all-packages
	uv run pre-commit install

up:
	docker compose up -d --wait

# DEFAULT: `up` starts only Postgres + Redis; this also builds and starts the four experts
# (the "experts" profile in docker-compose.yml).
up-experts:
	docker compose --profile experts up -d --build --wait

# DEFAULT: --profile experts so profile services are stopped too (a plain `down` skips them).
down:
	docker compose --profile experts down

check:
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy
	uv run pytest

# DEFAULT: alembic config lives with the migrations in shared/db; the URL is read from DATABASE_URL.
migrate:
	uv run alembic -c shared/db/alembic.ini upgrade head

# DEFAULT: schemas are generated on demand and not committed (see .gitignore)
schemas:
	uv run python -m shared.contracts.export_schemas

# DEFAULT: MEMBER comes from .env (loaded above) or `make export-data MEMBER=build`.
export-data:
	@test -n "$(MEMBER)" || { echo "export-data: set MEMBER in .env or run: make export-data MEMBER=<member>"; exit 1; }
	uv run collector export --member "$(MEMBER)"

# usage: make import-data FILE=data/exports/build-2026-11-02.tar.gz [FORCE=1]
import-data:
	@test -n "$(FILE)" || { echo "import-data: usage: make import-data FILE=<archive.tar.gz> [FORCE=1]"; exit 1; }
	uv run collector import "$(FILE)" $(if $(filter 1,$(FORCE)),--force)
