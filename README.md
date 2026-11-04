# PipelineIQ

PipelineIQ is a research platform for predicting and diagnosing failures in Node.js GitHub Actions pipelines. It brings four specialised components into one dashboard.

## Components

| Component | Focus |
| --- | --- |
| Dependency and Runtime | Package dependencies and Node.js compatibility |
| Build and Code Quality | Compilation, linting and bundling |
| Test and Application Behaviour | Test failures and application behaviour |
| Infrastructure and Deployment | Workflows, runners, containers and deployment |

## How it works

1. Connect a GitHub repository and branch.
2. Detect changes after a push and estimate failure risk.
3. Display risk estimates and available configuration findings.
4. The planned diagnosis flow analyses failed-run logs and retrieves relevant fixes for developer review.

**Status:** Research prototype. The Infra live monitor is experimental; its prediction accuracy is not established. SHAP, RAG and LLM diagnosis are not connected to the current Infra demo.

## Technology

- **Frontend:** React, TypeScript and Vite
- **Backend:** Python and FastAPI
- **Machine learning:** scikit-learn and XGBoost
- **Shared services:** PostgreSQL and Redis
- **Development:** uv, Docker Compose and Make

## Project structure

```text
experts/     # Dependency, build, testing and infrastructure components
services/    # Dashboard, APIs and supporting services
shared/      # Shared contracts and utilities
scripts/     # Development and monitoring tools
docs/        # Setup, architecture and project plans
```

## Getting started

Install the prerequisites described in [Onboarding](docs/ONBOARDING.md), then run from the repository root:

```bash
make setup    # Install dependencies and configure hooks
make up       # Start PostgreSQL and Redis
make migrate  # Apply database migrations
make check    # Run project checks
```


For the Infra repository-monitoring demo, see [Live prediction setup](experts/infra/LIVE_PREDICTION.md).

## Documentation

- [Onboarding](docs/ONBOARDING.md)
- [Architecture](docs/ARCHITECTURE.md)
- [Component status](docs/COMPONENTS.md)
- [Data and datasets](docs/DATA.md)
- [Development conventions](docs/CONVENTIONS.md)
- [Project plan](docs/PLAN.md)
- [Infra research](experts/infra/research/README.md)
