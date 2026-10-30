# Dependency log prediction API

This local research service loads `models/dependency/xgboost_log_only.joblib` and exposes a
prediction endpoint for sanitized log text or a failed GitHub Actions run URL. It is separate from
the shared platform API on port 8000 and runs on port 8001.

## Train and evaluate the model

From the repository root, using the repository virtual environment:

```powershell
.venv\Scripts\python.exe experts\dependency\research\prototype\train_log_only_model.py
```

The script evaluates all 14 boolean features from
`extract_sanitized_log_features()` with five-fold `GroupKFold`, holding out whole repositories.
It writes the CV report to `data/datasets/dependency/evaluation_report_log_only.json` and then
fits the inference artifact on all 140 complete verified records. The generated model and dataset
outputs are git-ignored.

## Start the service

The service reads `GITHUB_TOKEN` or `GH_TOKEN` from the shell environment. If neither is set, it
also checks those two entries in the repository-root `.env` file for local development. The token
stays on the server and is never returned to the browser. A token is recommended for reliable API
rate limits and is required for private repositories.

```powershell
.venv\Scripts\python.exe -m uvicorn main:app --app-dir services/dependency_api --host 127.0.0.1 --port 8001
```

The React dashboard calls `http://127.0.0.1:8001/api/v1/predict/dependency`. CORS is limited to
the local dashboard origins on ports 5173 and 5174 (localhost and 127.0.0.1). `GET /health`
reports whether the model artifact is available.

## Request and response

Provide exactly one input:

```json
{"log_text":"npm error ERESOLVE unable to resolve dependency tree"}
```

or:

```json
{"run_url":"https://github.com/owner/repository/actions/runs/123456789"}
```

Run URLs must identify a completed failed Actions run. The service downloads logs for failed jobs
through the shared GitHub client, enforces a 5 MiB limit, sanitizes the log in memory, and never
stores raw log content. Missing, unavailable, or oversized logs return an error without assigning
a failure class.

The JSON response matches `services/dashboard/src/dependency/types.ts`. The confidence is the
selected class probability from the research XGBoost model and is exploratory; it is not a
calibrated guarantee. If the backend is unreachable, the UI marks its sample fallback as
`OFFLINE / MOCK RESULT` and states that it is not a prediction for the submitted log.
