# PipelineIQ dashboard

A small web app (React + Vite + TypeScript) that shows PipelineIQ predictions and diagnoses and lets
a developer review them. It talks only to the main API (`services/api`), never to an expert directly.
The one exception is the older **Infra demo** page, which still talks to the Infra expert.

## Pages

The app uses hash routes (the part after `#` in the URL), so no server setup is needed.

| Route | What it shows |
|---|---|
| `#/` | Risk dashboard: recent runs, one risk cell per expert, top alerts, summary tiles |
| `#/history` | Table of runs: what was predicted vs what happened; repo filter |
| `#/commits/<sha>?repo=owner/name` | Every prediction for one commit (the pre-push hook links here) |
| `#/failures` | Failed jobs with their diagnoses and review state |
| `#/failures/<id>` | The routed failure, each expert diagnosis (cause, evidence, confidence, fix), past reviews and the review form |
| `#/settings` | Create, list and revoke your API keys; how to use one in `~/.pipelineiq/config` |
| `#/signin` | "Sign in with GitHub" |
| `#/infra` | The original Infra demo (works without signing in) |

Every page shows a loading state, an empty state and a clear error message (for example when the API is down).
If `/auth/me` answers 401 you see the sign-in page instead of the protected pages.

**Review form.** Choose accept, reject, modify or request revision. A **decision timer** starts when the
diagnosis is shown and runs on screen; its value is sent as `seconds_to_decide` in the `ReviewDecision`.
"Reject" always sends `cause_confirmed: false` (the contract requires it). "Modify" needs a corrected
remediation summary. "Request revision" needs a note. The reviewer is your `user_hash` from `/auth/me`.

## Run locally

```bash
cd services/dashboard
npm install
npm run dev
```

Open `http://127.0.0.1:5173`. Vite forwards `/api/...` to the main API at `http://127.0.0.1:8000`
(with the `/api` prefix removed) and `/infra-api/...` to the Infra expert at `http://127.0.0.1:8104`
(Infra demo only). Requests carry the login cookie (`credentials: "include"`).

In a container (nginx serves the build and does the same forwarding; hosts are `api:8000` and `infra-api:8104`):

```bash
docker compose --profile experts up --build dashboard
```

Then open `http://127.0.0.1:3000`. The `api` service is in the `e2e` profile of `docker-compose.yml`.

## Checks

```bash
npm run lint
npm test          # vitest: router, API client (401), review form + timer, API key shown once
npm run build
make check-dashboard   # the three above; also part of `make check`
```

## Generated contract types

`src/types/contracts.gen.ts` is generated from the contract JSON Schemas in `shared/contracts/`.
After a contract change, run `npm run gen:types` (it exports the schemas with `uv run python -m
shared.contracts.export_schemas`, then runs `json-schema-to-typescript`) and commit the result.
Never edit the file by hand. Shapes that only the API defines (runs, failure list items, API keys)
are written by hand in `src/types/api.ts`.

## Limitations and defaults

- Risk level (`low` / `medium` / `high`): `high` means the expert raised an alert; `medium` means at least half
  of the expert's threshold. This is a display choice only.
- The review form is shown only for diagnoses with status `diagnosed`.
- After sending a review the failure view reloads, so the new review shows under "Past reviews".
- No pagination: lists ask for the newest 25 to 100 items.

---

# Infra demo (page `#/infra`)

For **Connect repository**, branch monitoring and live prediction instructions, see
[the Infra live prediction guide](../../experts/infra/LIVE_PREDICTION.md). One repository branch
can be connected at a time. The local polling monitor reads Git revisions and calls the trained
Infra model. This flow does not require a webhook, router, RAG or LLM.

This focused demo displays the calibrated failure probability from the real Infra Expert. It can
either simulate a push with the form or automatically receive a real committed diff through the
local pre-push hook. It is intentionally labelled **research estimate** because the current model
has not passed unseen-repository validation.

## Run locally

Start the Infra API on port 8104, then:

```bash
cd services/dashboard
npm install
npm run dev
```

Open `http://127.0.0.1:5173/#/infra`. Vite proxies `/infra-api` to `http://127.0.0.1:8104`.

For a container demo, run from the repository root:

```bash
docker compose --profile experts up --build infra-api dashboard
```

Then open `http://127.0.0.1:3000`.

The form is useful for manual scenarios. The Connect control watches a real remote branch by
polling Git every five seconds. GitHub App/webhook integration remains planned for production.
The optional advisory pre-push connector below provides feedback before a push is transmitted.

## Real `git push` demonstration

Keep the Infra API and dashboard running. Install the demonstration hook into the local project you
want to push:

```powershell
& .venv\Scripts\python.exe scripts\install_infra_demo_hook.py C:\path\to\your-project
```

Then edit an infrastructure file, commit it and push normally:

```powershell
git add .
git commit -m "demo: introduce infrastructure failure"
git push
```

Before Git sends the commits, the hook reads the real committed diff and calls the packaged model.
The terminal prints the probability and the open dashboard detects the result within about two
seconds. The hook is advisory: if PipelineIQ is offline, it prints a warning and allows the push.

For a visible failing GitHub Actions run, use a disposable GitHub repository and deliberately point
a workflow job at a nonexistent container image. The model predicts before the push; GitHub then
provides the real failing run after the push. Do not use a production repository for this test.
