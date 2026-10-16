# Works with GNU make on Linux and on Windows (Git Bash). Only POSIX sh features are used.
.PHONY: setup up down check migrate schemas export-data import-data

setup:
	uv sync --all-packages
	uv run pre-commit install

up:
	docker compose up -d --wait

down:
	docker compose down

check:
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy
	uv run pytest

migrate:
	@if [ -f alembic.ini ]; then uv run alembic upgrade head; \
	else echo "No alembic.ini yet: the database layer arrives in P0.6."; exit 1; fi

# DEFAULT: schemas are generated on demand and not committed (see .gitignore)
schemas:
	uv run python -m shared.contracts.export_schemas

export-data:
	@echo "export-data: not implemented until P1.7"
	@exit 1

import-data:
	@echo "import-data: not implemented until P1.7 (usage: make import-data FILE=...)"
	@exit 1
