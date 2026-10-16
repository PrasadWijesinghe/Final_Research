# Conventions

## Tooling

| Area | Choice |
|---|---|
| Python | 3.12 |
| Packages | uv, as a workspace: root `pyproject.toml` lists `shared`, `services/*`, `experts/*` as members. One `uv.lock` |
| Lint + format | ruff (line length 100) |
| Types | mypy (strict on `shared/`, standard elsewhere), with the Pydantic plugin |
| Tests | pytest |
| Hooks | pre-commit: ruff, ruff-format, mypy on changed files, end-of-file and trailing-whitespace fixers |
| Frontend / hook | Node 22, pnpm, TypeScript strict, ESLint, Prettier |
| Containers | Docker Compose; pin image tags (no `latest`) |

`make check` runs ruff, mypy and pytest. It must pass before a task counts as done.

## Commands

```bash
make setup          # uv sync + pre-commit install
make up / make down # start / stop Postgres (pgvector) and Redis
make migrate        # apply database migrations
make check          # ruff + mypy + pytest
make export-data    # dump your database + logs for sharing or backup
make import-data FILE=...
```

## Design rules

These come from the architecture and apply wherever they're relevant:

- **No look-ahead leakage.** Any query for history must only use records created before the context's `history_cutoff`.
- **Replay and pre-push never touch GitHub.** No PR notes, checks or comments from either.
- **The system advises, never acts.** It never modifies user code, never blocks a push by itself and never auto-applies a fix.
- **Sanitise before storing.** Logs are scrubbed before they reach disk, the database or the LLM; identities are hashed with `IDENTITY_SALT`.

## Code rules

- **Boundaries use contracts.** Anything crossing a service boundary is a `shared.contracts` model. Never pass raw dicts between services.
- **No cross-expert imports.** `experts/<a>` never imports `experts/<b>`. Shared logic goes in `shared/`, after team agreement.
- **One home for feature code.** Each expert's features live in `src/<pkg>/features/`. Training scripts and the service both import from there.
- **No `test_*` files and no `Test*` classes under `src/`.** pytest would collect them.
- **Config over constants.** Repo lists, tagging patterns, routing rules, thresholds and timeouts live in `config/` or `.env`, never hard-coded.
- **Secrets only in `.env`.** Commit `.env.example` with placeholder values. Never log tokens, keys or unsanitised log text.
- **Type hints everywhere.** Public functions get a one-line docstring.
- **Logging:** standard `logging` with structured `extra={...}` fields; no `print` in services.
- **Time:** always timezone-aware UTC (`datetime.now(timezone.utc)`).
- **Defaults you invent** get a `# DEFAULT:` comment explaining the choice, so the team can review them.

## Documentation

Every component (each service, shared package, expert, and the hook) has a `README.md`
in its own folder that explains **what it does and how it works in plain language**.

- **Write it with the code**, in the same task, from `docs/templates/COMPONENT_README.md`.
- **Update it whenever behaviour changes.** A stale guide is worse than none.
- **Write for a teammate who hasn't seen the code:** short sentences, numbered steps, one real example, no code dumps. Explain any technical term the first time it appears.
- **Add a Mermaid diagram** when there are more than three steps or more than two parts talking to each other.
- **List limitations honestly,** including every `# DEFAULT:` choice made in that component.
- **Add or update the component's row** in `docs/COMPONENTS.md`.

## Tests

- Unit tests next to each package in `tests/`.
- **Fixtures are real, sanitised data:** recorded GitHub API responses (JSON) and real failure logs in `tests/fixtures/`.
- **No network in tests.** GitHub calls use recorded responses (respx for httpx); the LLM uses `LLM_PROVIDER=mock`.
- Integration tests use `docker compose` services and replay fixture runs end to end.

## Git

- `main` is protected. Work on branches named `<member>/<short-desc>` (e.g. `test/log-parsers`, `shared/collector-daily`).
- Merge through pull requests with at least one approval. CI must be green.
- Commit messages follow Conventional Commits: `feat(collector): …`, `fix(api): …`, `docs: …`.
- `CODEOWNERS`: each member owns `experts/<theirs>/` and `config/repos/<theirs>.yaml`. **All four** own `shared/contracts/`. Shared services need one approval from anyone.

## Changing shared things

- **Contracts:** PR with a version bump and changelog line in `shared/contracts/README.md`; all four approve.
- **Database:** new Alembic migration only; never edit an applied one.
- **Decisions:** any new decision or reversal gets an entry in `docs/DECISIONS.md` (number, decision, reason, date).
