# PipelineIQ

**PipelineIQ** is a 4-member research platform that predicts and diagnoses failures in Node.js GitHub Actions pipelines. Before developers push code, experts estimate the risk of build, test, dependency and infrastructure failures—explained with SHAP. After failures occur, an LLM-powered diagnosis engine, grounded in verified past fixes through retrieval-augmented generation, identifies the root cause. Developers review and confirm fixes, which feed back into the experts' knowledge base over time.

## Quick start

```bash
make setup    # Install dependencies and set up git hooks
make up       # Start PostgreSQL and Redis
make migrate  # Apply database migrations
make check    # Run linters, type checks, and tests
```

For detailed setup steps including GitHub App configuration and scheduled collection, see [docs/ONBOARDING.md](docs/ONBOARDING.md).

## Documentation

- [PLAN.md](docs/PLAN.md) — tasks and acceptance criteria
- [CONVENTIONS.md](docs/CONVENTIONS.md) — code style, testing, and naming
- [ARCHITECTURE.md](docs/ARCHITECTURE.md) — services, flows, endpoints, and data models
- [DATA.md](docs/DATA.md) — collection, database schema, tagging, and datasets
- [COMPONENTS.md](docs/COMPONENTS.md) — what's built, with status
- [DECISIONS.md](docs/DECISIONS.md) — architectural choices and reversals
- [experts/README.md](docs/experts/README.md) — expert interface and template
- [shared/contracts/README.md](shared/contracts/README.md) — message contracts v
