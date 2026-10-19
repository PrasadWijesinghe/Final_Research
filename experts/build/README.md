# Build expert

**In one sentence:** the Build and Code Quality expert. It will predict and explain failures in compiling, linting and build steps. Today it is a stub with fake answers.

**Status:** stub · **Owner:** Member 2 · **Plan tasks:** P2.1

## What it does

This is a copy of the expert template, set up for build failures. It has an API with `/predict`, `/review` and `/health`, and a worker that writes diagnoses for the `diagnose:build` queue. Both return valid messages with fixed or hash-based answers. The real logic comes later.

The shared rules, the endpoints and the steps to replace the stubs are in `experts/_template/README.md`. Read that first.

## Facts

| Name | Value |
|---|---|
| Package | `build_expert` (in `src/build_expert/`) |
| Port | 8102 (`EXPERT_PORT`) |
| Queue | `diagnose:build` |
| Detail model | `BuildDetails`, with `tool` set to `"other"` |
| Database schema | `build`. No tables yet. |
| `/predict` scope | Commit. One output per commit. |
| Docker | Image `pipelineiq-expert-build`. Compose services `build-api` and `build-worker`. |

## How to run it

```bash
# the whole repo
make check

# only this expert's tests
uv run pytest experts/build/tests
```

**Expected:** all tests pass. To start the stub in Docker, run `make up-experts`. That was not run in P2.1.

## Limitations and known issues

- Stub only. See `experts/_template/README.md` for the limits that all four experts share.
- `# DEFAULT:` in `src/build_expert/config.py`: `DEFAULT_PORT = 8102`, from the port table in `docs/experts/README.md`.
- The other `# DEFAULT:` choices (the Dockerfile versions, the 0.5 threshold, the 0.01 to 0.2 probability range, the confidence of 0.0, the 10 second backend timeout and the Redis fallback) are the same as in the template. They are listed in `experts/_template/README.md`.

## Related

- `experts/_template/README.md`: the template this copy came from.
- `docs/experts/README.md`: the expert interface.
- `docs/experts/build.md`: the design doc, written by the owner (not yet written).
- `docs/PLAN.md`, task P2.1.
