# Infrastructure expert

**In one sentence:** the Infrastructure and Deployment expert estimates the probability that a
push will fail for infrastructure reasons.

**Status:** integrated research model · **Owner:** Member 4 · **Plan tasks:** P2.1

## What it does

The `/predict` endpoint converts a shared `CommitContext` into the same pre-execution fields used
during training and runs the packaged, calibrated `v4-no-history` random-forest model. It returns a
valid `RiskOutput` that the dashboard can display. `/review` and the diagnosis worker still use the
original scaffold behaviour; RAG/LLM diagnosis is not connected yet.

The shared rules, the endpoints and the steps to replace the stubs are in `experts/_template/README.md`. Read that first.

## Facts

| Name | Value |
|---|---|
| Package | `infra_expert` (in `src/infra_expert/`) |
| Port | 8104 (`EXPERT_PORT`) |
| Queue | `diagnose:infra` |
| Detail model | `InfraDetails`, with `area` set to `"other"` |
| Database schema | `infra`. No tables yet. |
| `/predict` scope | Commit. One output per commit. |
| Docker | Image `pipelineiq-expert-infra`. Compose services `infra-api` and `infra-worker`. |

## How to run it

For the Infra-only live repository monitor, follow [LIVE_PREDICTION.md](LIVE_PREDICTION.md).
The dashboard can connect one GitHub repository branch, detect new remote revisions by polling,
and display predictions from the packaged model. RAG and LLM are not required.

```bash
# the whole repo
make check

# only this expert's tests
uv run pytest experts/infra/tests
```

**Expected:** all tests pass. To start the model and dashboard in Docker, run:

```bash
docker compose --profile experts up --build infra-api dashboard
```

Open `http://127.0.0.1:3000`.

## Limitations and known issues

- Prediction is a **research estimate**, not a production gate. The saved model has zero recall at
  its selected threshold in leave-one-repository-out validation.
- An October 2026 data audit fixed missing event flags and aligned several live workflow signals
  with the historical collector. This changes live input extraction for the existing v4 artifact;
  it does not retrain that artifact. The new v5 experiment still missed 179 of 184 held-out
  failure rows and was not promoted. See `research/DATASET_REPAIR_REPORT.md`.
- The training examples describe single workflow runs; live commits can aggregate multiple
  workflows. Neither the image's availability nor live runner/network health is a model input.
- The dashboard supports both a manual form and the local real-`git push` hook. Automatic GitHub
  webhook triggering still needs the planned GitHub App/collector work for production. The local
  hook in `scripts/infra_pre_push.py` works without the router or database and never blocks a push.
- The `v4-no-history` model is used for serving because the shared contract intentionally does not
  embed historical failure rates.
- `# DEFAULT:` in `src/infra_expert/config.py`: `DEFAULT_PORT = 8104`, from the port table in `docs/experts/README.md`.
- The other `# DEFAULT:` choices (the Dockerfile versions, the 0.5 threshold, the 0.01 to 0.2 probability range, the confidence of 0.0, the 10 second backend timeout and the Redis fallback) are the same as in the template. They are listed in `experts/_template/README.md`.

## Model research workspace

The owner's experiments remain in `research/`. The canonical feature transformations now live in
`src/infra_expert/features/model_features.py` so training and serving cannot silently disagree.
The selected no-history artifact is packaged under `src/infra_expert/model/`; the larger datasets,
reports and alternate models remain ignored research outputs.

## Related

- `experts/_template/README.md`: the template this copy came from.
- `docs/experts/README.md`: the expert interface.
- `docs/experts/infra.md`: the design doc, written by the owner (not yet written).
- `docs/PLAN.md`, task P2.1.
- `research/README.md`: isolated model-development workflow and current data limitations.
