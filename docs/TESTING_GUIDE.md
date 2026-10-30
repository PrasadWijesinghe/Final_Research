# Testing PipelineIQ end to end

A short walkthrough to try the whole system on your laptop: pre-push risk, a real GitHub repo,
failure diagnosis, reviews and the PR check. Run every command from the repo folder unless it says
otherwise. Never paste keys or secrets into chat or issues.

## 0. What you need

- Docker Desktop running, Node 22+, `uv`, and a filled `.env` (see `docs/ONBOARDING.md`).
- The PipelineIQ GitHub App registered, with its values in `.env` (see `docs/GITHUB_APP.md`).
- For real diagnosis text: `LLM_PROVIDER=deepseek` and `LLM_API_KEY=...` in `.env`.
  Without them the text comes from a template (everything else still works).

## 1. Start everything

```bash
make up                                                              # Postgres and Redis
docker compose --profile experts up -d --build testing-api testing-worker llm-gateway
uv run --env-file .env uvicorn api.app:app --host 127.0.0.1 --port 8000   # API (own terminal)
cd services/dashboard && npm run dev                                 # dashboard (own terminal)
```

Check:

- http://127.0.0.1:8103/status shows `"mode":"real"` and `"model_version":"lightgbm-v4"`.
- http://127.0.0.1:8200/health shows the provider (`deepseek` or `mock`).
- http://localhost:5173 opens the dashboard. Sign in with GitHub.

## 2. Pre-push risk check

1. In the dashboard, **Settings**, create an API key and save it in `~/.pipelineiq/config`:

   ```
   api_key=piq_...
   api_url=http://127.0.0.1:8000
   dashboard_url=http://localhost:5173
   ```

   Then `chmod 600 ~/.pipelineiq/config`.
2. In the repo you want to check, install the hook once:

   ```bash
   node /path/to/Final_Research/tools/pre-push-hook/src/install.mjs
   ```

3. Push. The terminal prints a `PipelineIQ:` line. It asks "Push anyway?" only when a job's risk is
   above the threshold. From a GUI or an AI agent you get a desktop notification instead.

Quick test without a repo:

```bash
curl -s -X POST localhost:8103/predict -H 'content-type: application/json' \
  --data @experts/testing/tests/fixtures/commit_context_multi_job.json | python3 -m json.tool
```

## 3. Connect a GitHub repo

Use a small Node.js repo you own, with a GitHub Actions workflow that runs `npm test`
(`~/projects/piq-demo` is one, with a deliberately flaky test). Public repos are best: Actions
minutes are free.

1. **Start the relay** (own terminal, leave it running). GitHub can't reach `localhost`, so smee
   forwards webhooks to the API:

   ```bash
   make webhooks
   ```

   Wait for `Connected`.
2. **Check the App settings** on GitHub (Settings, Developer settings, GitHub Apps, PipelineIQ):
   **Webhook** is active and its URL equals `SMEE_URL` in `.env`.
3. **Install the App on the repo:** **Install App**, **Configure**, **Only select repositories**,
   pick the repo, **Save**.
4. **Push to the repo.** If git says `403` for another account, put your username in the URL:

   ```bash
   git remote set-url origin https://<you>@github.com/<you>/<repo>.git
   ```

## 4. Watch a failure get diagnosed

Push a commit (an empty one is fine):

```bash
git commit --allow-empty -m "pipelineiq test" && git push
```

| When | Where | What you see |
|---|---|---|
| At once | Your terminal | The pre-push `PipelineIQ:` line |
| Seconds | Relay terminal | `POST` lines (push, workflow_run, workflow_job) |
| 10 to 30 s | GitHub, the commit's checks | A **PipelineIQ** check (neutral, "no alerts" or alerts) |
| 1 to 3 min | GitHub, **Actions** | The CI jobs; the flaky test fails some of the time |
| After a failed job | Dashboard, **Failures** | The failure, with its diagnosis |

Open the failure. The **test expert** card shows:

- **Triage:** genuine, flaky or unclear, how sure, and whether to re-run.
- **Root cause and suggested fix** (from DeepSeek, or the template).
- **Evidence** (log, history, diff) and **similar past cases**.

## 5. Review a diagnosis

On the failure page, under **Your review**:

1. Pick **Accept**, **Reject**, **Modify** or **Request revision**.
2. Tick **I agree with the root cause** only if it is right. Add a comment if useful.
3. **Submit review.** It appears under **Past reviews**, with the time you took to decide.

Reviews are stored for evaluation. They do not retrain any model or call the LLM.

## 6. Pull request check

1. In the test repo, create a branch, change something, push it and open a PR.
2. The PR's checks list shows **PipelineIQ**. Click it for the per-job risk and reasons. It is
   updated when late expert answers arrive, and it never blocks a merge.

## 7. Replay real failures (no GitHub needed)

Send a failure from the collected dataset through the same path:

```bash
PYTHONPATH=. uv run --env-file .env python scripts/demo_route_failure.py --list
PYTHONPATH=. uv run --env-file .env python scripts/demo_route_failure.py --job-id <id>
```

It prints the failure and a dashboard link.

## If something goes wrong

| Problem | Fix |
|---|---|
| No failure appears in the dashboard | Is the relay running? GitHub, App settings, **Advanced**, **Recent deliveries**: redeliver the missed ones |
| API answers `401` to webhooks | `GITHUB_WEBHOOK_SECRET` in `.env` differs from the App's secret. Fix it and restart the API |
| Diagnosis says "Stub expert" | The log has no test runner the expert knows (Jest, Mocha, Vitest, node:test), or the worker has no model. Check `docker compose logs testing-worker` |
| Diagnosis text has a "template" note | The LLM is the mock, or DeepSeek failed (key, balance, timeout). Check `docker compose logs llm-gateway` |
| `git push` gives `403` | Wrong GitHub account; see step 3.4 |
| Dashboard data missing after a reboot | Start step 1 again |

Stop everything with `make down` (and Ctrl+C the API, dashboard and relay terminals).
