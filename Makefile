# Works with GNU make on Linux and on Windows (Git Bash). Only POSIX sh features are used.
# Load .env when present (CI has none and sets variables directly). Values are never echoed.
# DEFAULT: plain `include` of KEY=value lines; do not quote values or add `export` inside .env.
-include .env
export

.PHONY: webhooks check-hook check-dashboard setup up up-experts e2e down check migrate schemas export-data import-data migrate-testing

setup:
	uv sync --all-packages
	uv run pre-commit install

up:
	docker compose up -d --wait

# DEFAULT: `up` starts only Postgres + Redis; this also builds and starts the four experts
# (the "experts" profile in docker-compose.yml).
up-experts:
	docker compose --profile experts up -d --build --wait

# DEFAULT (P2.9): end-to-end test. Builds and starts postgres, redis, api and the Test expert
# (profile "e2e"; the gateway runs the mock provider), migrates the database from the host, runs the `e2e` tests, then always
# stops and removes only its own containers (api, testing-api, testing-worker, llm-gateway; the postgres/redis
# dev stack stays up) and keeps the test's exit code. E2E_REQUIRED=1 turns "stack unreachable" from
# a skip into a failure, so CI cannot pass without really running the test.
# EXPERT_BACKEND_URL makes the Test worker post its diagnosis to the api container.
e2e:
	@status=0; \
	EXPERT_BACKEND_URL=http://api:8000 docker compose --profile e2e up -d --build --wait \
		&& $(MAKE) migrate \
		&& E2E_REQUIRED=1 uv run pytest -m e2e services/api/tests/e2e \
		|| status=$$?; \
	if [ $$status -ne 0 ]; then docker compose --profile e2e logs --no-color; fi; \
	docker compose --profile e2e stop api testing-api testing-worker llm-gateway; \
	docker compose --profile e2e rm -f api testing-api testing-worker llm-gateway; \
	exit $$status

# DEFAULT: --profile experts so profile services are stopped too (a plain `down` skips them).
down:
	docker compose --profile experts --profile e2e down

check:
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy
	uv run pytest
	$(MAKE) check-hook
	$(MAKE) check-dashboard

# DEFAULT (P3.4): Node tests of the pre-push hook run with the built-in test runner (Node 22, no
# dependencies). Skipped with a notice when node is missing locally; CI installs Node 22 and sets
# REQUIRE_NODE=1 so it cannot be skipped there.
check-hook:
	@if command -v node >/dev/null 2>&1; then cd tools/pre-push-hook && node --test "test/*.test.mjs"; \
	elif [ -n "$(REQUIRE_NODE)" ]; then echo "check-hook: node is required"; exit 1; \
	else echo "check-hook: node not found, skipping hook tests"; fi

# DEFAULT (P3.5): dashboard lint, tests and build. Skipped with a notice when node is missing or
# dependencies are not installed (run `npm ci` in services/dashboard); with REQUIRE_NODE=1 (CI) it
# installs them and cannot be skipped.
check-dashboard:
	@if ! command -v node >/dev/null 2>&1; then \
		if [ -n "$(REQUIRE_NODE)" ]; then echo "check-dashboard: node is required"; exit 1; \
		else echo "check-dashboard: node not found, skipping"; fi; \
	elif [ -n "$(REQUIRE_NODE)" ] || [ -d services/dashboard/node_modules ]; then \
		cd services/dashboard && { [ -d node_modules ] || npm ci; } && npm run lint && npm test && npm run build; \
	else echo "check-dashboard: run npm ci in services/dashboard first, skipping"; fi

# DEFAULT (P3.2): relay GitHub App webhooks from smee.io to the local API. Needs Node (npx); leave it
# running in its own terminal next to the API. SMEE_URL comes from .env.
webhooks:
	@test -n "$(SMEE_URL)" || { echo "webhooks: set SMEE_URL in .env (see docs/GITHUB_APP.md)"; exit 1; }
	npx --yes smee-client --url "$(SMEE_URL)" --target "http://127.0.0.1:$(or $(API_PORT),8000)/webhooks/github"

# DEFAULT: alembic config lives with the migrations in shared/db; the URL is read from DATABASE_URL.
migrate:
	uv run alembic -c shared/db/alembic.ini upgrade head
	$(MAKE) migrate-testing

# DEFAULT (P4.2): the Test expert's testing schema has its own Alembic config and version table.
migrate-testing:
	uv run alembic -c experts/testing/alembic.ini upgrade head

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
