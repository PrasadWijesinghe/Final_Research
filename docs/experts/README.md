# Experts

Four experts, one per failure category, all with the **same interface** so the backend
treats them identically.

| Expert | Folder | Package | Port | Owner | Design doc |
|---|---|---|---|---|---|
| Dependency & Runtime | `experts/dependency` | `dependency_expert` | 8101 | Member 1 | `docs/experts/dependency.md` (owner writes) |
| Build & Code Quality | `experts/build` | `build_expert` | 8102 | Member 2 | `docs/experts/build.md` (owner writes) |
| Test & App Behaviour | `experts/testing` | `testing_expert` | 8103 | Member 3 | `docs/experts/test.md` |
| Infrastructure & Deployment | `experts/infra` | `infra_expert` | 8104 | Member 4 | `docs/experts/infra.md` (owner writes) |

## The interface

| Part | Contract in → out |
|---|---|
| `POST /predict` | `CommitContext` → JSON array of `RiskOutput` (one per commit, or one per test job for the Test expert) |
| `POST /review` | `ReviewDecision` → `204` |
| `GET /health` | → `{status, model_version, kb_cases}` |
| Worker on Redis queue `diagnose:<expert>` | `RoutedFailure` → `Diagnosis`, sent to the backend's `POST /diagnoses` |

Timeouts: `/predict` must answer within **10 s** when called for the pre-push hook and within
**2 minutes** for PR notes. The backend passes `X-Deadline-Ms` so the expert knows which.
An expert that can't finish in time returns `status="degraded"` with whatever it has.

## Folder layout (copy `experts/_template/`)

```
experts/<name>/
├── pyproject.toml
├── Dockerfile
├── src/<package>/
│   ├── api.py           # FastAPI app: /predict, /review, /health
│   ├── worker.py        # RQ worker for diagnose:<expert>
│   ├── predict.py       # CommitContext → RiskOutput (stub until the model exists)
│   ├── diagnose.py      # RoutedFailure → Diagnosis (stub until real)
│   ├── features/        # feature code, shared by training and serving
│   ├── model/           # training, calibration, explanation
│   ├── rag/             # knowledge base, retrieval, prompts/
│   └── db.py            # this expert's own schema
├── migrations/          # Alembic, for this expert's schema only
└── tests/
```

## Stub behaviour

Every expert starts as a stub so the whole flow works before anyone has a model:

- `/predict` returns a valid `RiskOutput`: `status="ok"`, a low deterministic probability
  (derived from a hash of `head_sha`, so it's stable), `threshold=0.5`, `alert=false`, and
  `model_version="stub"`. Commit-scoped for dependency, build and infra; job-scoped for
  test (one output per test job in `workflow_jobs`, or commit-scoped if none is found).
- The worker returns a valid `Diagnosis` built from fixed text (no LLM call; the gateway
  comes later in P2.4), with one evidence line taken from the log excerpt, no retrieved
  cases, and `details` filled with that expert's detail model. It posts to the backend's
  `POST /diagnoses` when `BACKEND_URL` is set, and otherwise just returns it.
- `/review` accepts and logs the decision.

## Starting your expert

1. Copy `experts/_template/` to your folder, rename the package, set your port.
2. Write `docs/experts/<yours>.md`: labels, features, model plan, diagnosis details. Use `docs/experts/test.md` as an example.
3. Fill `config/repos/<yours>.yaml`, your rows of `config/file_tags.yaml` and your patterns in `config/router_rules.yaml`.
4. Run `uv run collector backfill --member <yours>`.
5. Replace the stub functions one at a time. The stub's tests keep passing as long as the contracts hold.
