# Shared contracts — v1.1.0

The messages passed between the four experts and the shared services. Drafted from all
four proposals (C1 Dependency, C2 Build, C3 Test, C4 Infra). Changing anything here needs
approval from all four members (see `docs/CONVENTIONS.md`).

## The messages

| Message | Produced by | Read by | Travels by |
|---|---|---|---|
| `CommitContext` | `services/features` | all four experts | body of `POST /predict` |
| `RiskOutput` | each expert | backend → dashboard, PR note, pre-push hook | reply to `POST /predict`, as a JSON array (one per commit, or one per test job) |
| `RoutedFailure` | router (inside `services/api`) | one or two experts | Redis queue `diagnose:<expert>` |
| `Diagnosis` | each expert's worker | backend → dashboard | `POST /diagnoses` on the backend |
| `ReviewDecision` | dashboard | backend, then the diagnosing expert | `POST /reviews`, then `POST /review` on the expert |
| `VerifiedCase` | each expert | that expert's own vector collection | written directly, never sent |
| `FailureLabel` | labelling scripts / manual review | all four experts (training) | written to `platform.failure_labels` |

Serialise with `model.model_dump_json()`; parse with `Model.model_validate_json(raw)`.
A malformed message is rejected at the boundary.

## Rules built into the code

- Unknown fields are rejected; messages are immutable once created.
- **No look-ahead leakage:** experts may only use history created before `CommitContext.history_cutoff`.
- **No raw identities:** authors and reviewers are 64-character salted hashes.
- **Alerts must explain themselves:** an alert needs at least one `RiskFactor`, and each factor's direction must match its SHAP sign.
- **Probabilities are never normalised** across experts; they predict different events and may sum to more than 1.
- **The router sends to at most two experts**, never the same one twice.
- **Diagnoses must cite evidence:** at least one evidence line, and the remediation may only cite cases that were actually retrieved.
- **Experts can push back:** `status="misrouted"` with a `suggested_category`.
- **Knowledge-base write-back** needs a confirmed cause and a passing re-run (`verifying_run_passed` must be `True`).
- **Licence tracking:** every `VerifiedCase` records the repo licence, so non-permissive repos can be kept out of a commercial knowledge base.
- **Synthetic data is flagged:** fault-injected commits set `is_synthetic=True`.
- **Replay is invisible to GitHub:** `RoutedFailure.replay=True` (and `CommitContext.event="replay"`) must never post PR notes or checks.

## Each owner: confirm your part

| Owner | Check |
|---|---|
| Member 1 (Dependency) | `DependencyDetails.kind` values; `PACKAGE_MANIFEST` / `LOCKFILE` tagging; lockfiles stored as refs |
| Member 2 (Build) | `BuildDetails.tool` and `BuildLocation`; `BUILD_CONFIG` / `LINT_CONFIG` / `BUNDLER_CONFIG` tagging; `sub_risks` keys |
| Member 3 (Test) | `BehaviourDetails`; `TEST` / `TEST_CONFIG` tagging; job-scoped `RiskOutput` |
| Member 4 (Infra) | `InfraDetails.area` and sub-patterns; `WorkflowJob` fields (runner, permissions, container) |

## Using the contracts

```bash
uv run pytest shared/tests -q                 # contract tests, also worked examples
uv run python -m shared.contracts.export_schemas   # JSON Schemas for the TypeScript dashboard
```

```python
from shared.contracts import CommitContext, FileTag

ctx = CommitContext.model_validate_json(raw_json)
test_files = ctx.files_with(FileTag.TEST, FileTag.TEST_CONFIG)
```

## Changing a contract

Open a PR touching `shared/contracts/`, get approval from all four members, bump
`SCHEMA_VERSION` in `common.py` (major = breaking, minor = new optional field), and add a
line to the changelog below.

## Changelog

- **1.1.0** — `CommitContext.event` gains `"pre_push"` (local diff from the pre-push hook;
  no `pr_number` allowed). `RoutedFailure` gains `replay: bool = False`.
- **1.0.0** — first draft: seven messages.
