# PipelineIQ dashboard

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

Open `http://127.0.0.1:5173`. Vite proxies `/api` to `http://127.0.0.1:8104`.

### Dependency Expert log classifier

Train the verified log-only model once, then start its standalone API in a separate terminal. Text
and uploaded logs work without GitHub credentials; run URL analysis requires `GITHUB_TOKEN` in the
API process environment.

```powershell
.venv\Scripts\python.exe experts\dependency\research\prototype\train_log_only_model.py
.venv\Scripts\python.exe -m uvicorn main:app --app-dir services/dependency_api --host 127.0.0.1 --port 8001
```

Then start the dashboard with `npm run dev`. The Dependency Expert sends log text or run URLs to
`http://127.0.0.1:8001/api/v1/predict/dependency`. If the API is unreachable or returns a server
error, the page labels its sample fallback `OFFLINE / MOCK RESULT`; invalid input and unavailable
GitHub logs remain visible errors and do not receive a predicted class.

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
