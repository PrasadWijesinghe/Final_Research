# Infrastructure expert

**In one sentence:** the Infrastructure and Deployment expert. It will predict and explain failures in deploy and environment steps. Today it is a stub with fake answers.

**Status:** stub · **Owner:** Member 4 · **Plan tasks:** P2.1

## What it does

This is a copy of the expert template, set up for infrastructure failures. It has an API with `/predict`, `/review` and `/health`, and a worker that writes diagnoses for the `diagnose:infra` queue. Both return valid messages with fixed or hash-based answers. The real logic comes later.

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

```bash
# the whole repo
make check

# only this expert's tests
uv run pytest experts/infra/tests
```

**Expected:** all tests pass. To start the stub in Docker, run `make up-experts`. That was not run in P2.1.

## Limitations and known issues

- Stub only. See `experts/_template/README.md` for the limits that all four experts share.
- `# DEFAULT:` in `src/infra_expert/config.py`: `DEFAULT_PORT = 8104`, from the port table in `docs/experts/README.md`.
- The other `# DEFAULT:` choices (the Dockerfile versions, the 0.5 threshold, the 0.01 to 0.2 probability range, the confidence of 0.0, the 10 second backend timeout and the Redis fallback) are the same as in the template. They are listed in `experts/_template/README.md`.

## Related

- `experts/_template/README.md`: the template this copy came from.
- `docs/experts/README.md`: the expert interface.
- `docs/experts/infra.md`: the design doc, written by the owner (not yet written).
- `docs/PLAN.md`, task P2.1.
