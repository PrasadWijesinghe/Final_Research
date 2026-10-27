# Dependency expert

**In one sentence:** the Dependency and Runtime expert. It will predict and explain failures caused by packages and runtime versions. Today it is a stub with fake answers.

**Status:** stub, with an experimental prediction prototype · **Owner:** Member 1 · **Plan tasks:** P2.1, P4.13

## What it does

This is a copy of the expert template, set up for dependency failures. It has an API with `/predict`, `/review` and `/health`, and a worker that writes diagnoses for the `diagnose:dependency` queue. Both return valid messages with fixed or hash-based answers. The real logic comes later.

The shared rules, the endpoints and the steps to replace the stubs are in `experts/_template/README.md`. Read that first.

The prototype trainer and standalone demo API are under `research/prototype/`; the dataset
extractor and feature conversion code live in the expert package. Generated JSON/CSV and model
files are local artifacts under the ignored `data/` and `models/` folders. This is an experiment
only: it does not replace the contract-based `/predict` endpoint, and its labels and model
evaluation are not production-certified. See `research/README.md` before running it.

The random control scan logs repository, run and job IDs without logging job-log contents. It uses
a 15-second request timeout with no retries; a failed request is recorded and skipped, not labeled.
The scan writes a valid partial result if interrupted or if another error occurs. Check the
result's `scan_status` before using it; see `research/README.md` for details.
Job logs over 5 MiB are not sanitised or matched and cannot be selected as controls. The current
shared client streams limited responses and stops reading when the content length or received
bytes exceed the cap.
Once random controls are manually reviewed and marked final, the master label audit includes
them from `random_control*.json`; pending controls are not used as training labels.

## Facts

| Name | Value |
|---|---|
| Package | `dependency_expert` (in `src/dependency_expert/`) |
| Port | 8101 (`EXPERT_PORT`) |
| Queue | `diagnose:dependency` |
| Detail model | `DependencyDetails`, with `kind` set to `"other"` |
| Database schema | `dependency`. No tables yet. |
| `/predict` scope | Commit. One output per commit. |
| Docker | Image `pipelineiq-expert-dependency`. Compose services `dependency-api` and `dependency-worker`. |

## How to run it

```bash
# the whole repo
make check

# only this expert's tests
uv run pytest experts/dependency/tests
```

**Expected:** all tests pass. To start the stub in Docker, run `make up-experts`. That was not run in P2.1.

## Limitations and known issues

- Stub only. See `experts/_template/README.md` for the limits that all four experts share.
- The prediction prototype uses a small set of dependency-file-change features and workflow
  conclusions. A failed workflow may have an unrelated cause, so its labels are not yet suitable
  evidence of dependency-caused failure.
- The standalone prototype API has a different request/response shape from the expert contract.
  Do not use it in the backend or pre-push flow.
- `# DEFAULT:` in `src/dependency_expert/config.py`: `DEFAULT_PORT = 8101`, from the port table in `docs/experts/README.md`.
- The other `# DEFAULT:` choices (the Dockerfile versions, the 0.5 threshold, the 0.01 to 0.2 probability range, the confidence of 0.0, the 10 second backend timeout and the Redis fallback) are the same as in the template. They are listed in `experts/_template/README.md`.

## Related

- `experts/_template/README.md`: the template this copy came from.
- `docs/experts/README.md`: the expert interface.
- `docs/experts/dependency.md`: the design doc, written by the owner (not yet written).
- `docs/PLAN.md`, task P2.1.
