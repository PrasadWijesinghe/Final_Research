# Dependency prediction experiment

**In one sentence:** This folder holds Member 1's early data and model experiment; it is not
the production Dependency expert.

**Status:** Experimental · **Owner:** Member 1 · **Plan task:** P4.13

## What it does

The experiment collects recent workflow runs from the repositories in
`config/repos/dependency.yaml`, records dependency-file changes, converts those records into a
small CSV, and trains a baseline model. The dataset and model are generated files, so they live
under the ignored `data/` and `models/` folders and are not committed.

The official expert endpoint remains the contract-based stub. The stand-alone API in this
folder accepts a different input and returns a different response; it is for local experiments
only.

## How it works

1. The extractor uses the configured Dependency repositories and shared GitHub client.
2. The feature converter counts package manifests and lockfiles for each run.
3. The trainer compares simple Logistic Regression and XGBoost baselines and saves a local
   model.
4. The optional stand-alone API demonstrates that model, separately from the expert service.

```mermaid
flowchart LR
    A[Dependency repo config] --> B[Shared GitHub client]
    B --> C[Local run dataset]
    C --> D[Feature CSV]
    D --> E[Prototype model]
    E --> F[Local demo API]
```

## How to run it

Choose repositories from `config/dependency_repository_groups.yaml` with `--group`, specify a
repository directly with `--repo`, or fill in `config/repos/dependency.yaml` and use that
curated list. The groups are research candidates, not an approved or automatically enabled
collection list. Do not put a token in either config file or in source code; provide
`GITHUB_TOKEN` through the private `.env`.

```bash
uv run --env-file .env --package pipelineiq-expert-dependency python -m dependency_expert.prototype_dataset --group backend_frameworks
# Collect just one newly selected repository:
uv run --env-file .env --package pipelineiq-expert-dependency python -m dependency_expert.prototype_dataset --repo expressjs/express
# Add more repositories by repeating --repo:
uv run --env-file .env --package pipelineiq-expert-dependency python -m dependency_expert.prototype_dataset --repo expressjs/express --repo nestjs/nest
uv run --package pipelineiq-expert-dependency python -m dependency_expert.features.prototype
uv run --package pipelineiq-expert-dependency --extra prototype python experts/dependency/research/prototype/model_trainer.py
```

Use `python -m dependency_expert.prototype_dataset --help` to see all options, or `--group` with
one of `backend_frameworks`, `frontend_and_build_tools`, `database_and_orms`,
`utils_and_testing`, `personal_and_misc`, `New_one`, or `top_nodejs_repos`. You can also add
the repo to `config/repos/dependency.yaml` and run the extractor without `--group` or `--repo`.
The extractor appends only unseen repository/run/attempt records to the existing JSON dataset,
so new records join the existing 293 instead of replacing them. The commands write
`dependency_runs_dataset.json`, `dependency_features.csv`, and
`xgboost_model.pkl` under the ignored `data/` and `models/` folders. The extractor needs
enabled repositories in the config; the model trainer needs enough records and both outcome
labels in its training subset.

To start the separate demo API after training:

```bash
uv run --package pipelineiq-expert-dependency --extra prototype python experts/dependency/research/prototype/app.py
```

It listens on localhost port 5000. It does not implement the expert contract.

## Inputs and outputs

| | What | Where / format |
|---|---|---|
| In | Repositories and workflow history | `config/repos/dependency.yaml`, GitHub API |
| In | Workflow outcome and changed dependency files | Local JSON dataset |
| Out | Experimental feature rows | `data/datasets/dependency/prototype/dependency_features.csv` |
| Out | Trained baseline | `models/dependency/prototype/xgboost_model.pkl` |

## Settings

| Setting | Where | Default | What it changes |
|---|---|---|---|
| GitHub token | `.env` as `GITHUB_TOKEN` | None | Authenticates the shared GitHub client |
| Collection window | CLI `--days` | 90 days | Limits runs fetched by the prototype |
| Dataset/model paths | CLI options or module defaults | Ignored local folders | Selects local inputs and outputs |

## Example

A run that changed `package.json` and `package-lock.json` becomes one feature row with
`has_package_json_change=1` and `has_lockfile_change=1`. Its label is based on the workflow
conclusion, not on a verified root cause.

## Limitations and known issues

- This is not a production model, and its labels do not prove that a dependency change caused a
  workflow failure.
- The small feature set, repository sampling and current validation do not establish model
  quality. Treat training metrics as exploratory only.
- The old local JSON/CSV/model files are ignored artifacts. Review and sanitize data before
  sharing it; do not commit these generated files.
- The demo API uses a standalone request/response shape and is not connected to backend fan-out,
  the pre-push hook, or the expert's contract endpoint.
- `# DEFAULT:` the prototype looks back 90 days, matching the collection window in
  `docs/DATA.md`.

## Related

- [Dependency expert](../README.md)
- [Dependency expert design](../../../docs/experts/dependency.md)
- [Plan](../../../docs/PLAN.md), task P4.13
