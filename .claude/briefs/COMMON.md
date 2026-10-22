# Common brief — every PipelineIQ agent reads this first

## Environment
- Repo: `F:\Final Research` (Git Bash path `/f/Final Research`). Windows 10. Use the Bash tool (Git Bash).
- At the start of EVERY Bash call run:
  `export PATH="$HOME/.local/bin:/c/Users/HP/AppData/Local/Microsoft/WinGet/Packages/ezwinports.make_Microsoft.Winget.Source_8wekyb3d8bbwe/bin:$PATH"`
  (uv 0.13 and GNU make 4.4 live there; Python 3.12 is uv-managed.)
- Docker Desktop is running. `make up` / `make down` start/stop Postgres (pgvector) + Redis.
- Git branch: `shared/foundation`. Never push. Never switch branches.

## Hard rules (from CLAUDE.md, non-negotiable)
1. Never edit `shared/contracts/`. If something needs a contract change, STOP and report it.
2. Experts import only from `shared/`, never from each other.
3. No secrets in code, git, logs, fixtures or LLM prompts. Never print or cat `.env`. Fake tokens in tests must be obviously fake.
4. `make check` must pass before a task is done.
5. No real network calls in tests (respx for httpx; LLM_PROVIDER=mock).
6. Any choice the docs don't cover: simplest option, marked `# DEFAULT: <one-line reason>`.
7. If docs conflict or don't cover something important: STOP and report, don't guess.
8. Commit messages: Conventional Commits referencing the task ID, e.g. `feat(github): rate-limited client (P1.1)`.
   NO `Co-Authored-By` trailer, NO Claude session links.

## Docs (open only what the task needs)
CLAUDE.md, docs/PLAN.md (tasks + acceptance), docs/CONVENTIONS.md (read before coding),
docs/ARCHITECTURE.md, docs/DATA.md, shared/contracts/, docs/experts/README.md,
docs/templates/COMPONENT_README.md, docs/COMPONENTS.md, docs/DECISIONS.md.

## State of the repo (decisions already made — follow them)
- Ticked tasks in docs/PLAN.md are done; `git log` shows one commit per task.
- uv workspace: root `pyproject.toml` is virtual; members `shared`, `services/*`, `experts/*`.
  uv errors on member folders without a pyproject, so empty folders are listed in root `[tool.uv.workspace] exclude`.
  **When you add `<folder>/pyproject.toml`, remove that folder from `exclude`.**
- mypy `files = [...]` in root pyproject lists only folders with Python code. **When you add a Python package elsewhere, append its folder.** Strict on `shared.*`, standard elsewhere.
- `shared` is packaged by `shared/pyproject.toml` (hatchling force-include). Add shared's runtime deps there (`uv add --package pipelineiq-shared ...`); dev/test deps in root dev group (`uv add --dev ...`). Always keep `uv.lock` updated via uv commands.
- New service/expert packages: `<folder>/pyproject.toml` + `src/<package>/` + `tests/`. No `test_*` files or `Test*` classes under `src/`.
- Docker Compose (`docker-compose.yml`): pgvector/pgvector:0.8.1-pg17 + redis:7.4.6, ports on 127.0.0.1, init script `docker/postgres/init/01-init.sql` creates the `vector` extension and schemas raw, platform, dependency, build, testing, infra. CI runs `make up` then `make check`.
- Tests marked `@pytest.mark.integration` auto-skip when the DB in DATABASE_URL is unreachable (root conftest.py). To run them locally: `make up` and export
  `DATABASE_URL=postgresql+psycopg://pipelineiq:change-me@127.0.0.1:5432/pipelineiq` and `REDIS_URL=redis://127.0.0.1:6379/0` (compose defaults). Use 127.0.0.1, not localhost: on Windows localhost tries IPv6 first and each connection stalls ~2 s.
- `.env.example` must list every env variable the code reads. Add new ones there (placeholders only).
- Python style: type hints everywhere, one-line docstring on public functions, `logging` with `extra={...}`, no `print` in services, UTC-aware datetimes, config in `config/` or `.env` not constants.
