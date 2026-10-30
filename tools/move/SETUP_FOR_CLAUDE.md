# Setting up PipelineIQ on this computer (instructions for Claude Code)

The person moved PipelineIQ here from another computer. This folder (`pipelineiq-move`) holds:

| File | What it is | Goes to |
|---|---|---|
| `dot-env` | The project's `.env` (secrets) | `<repo>/.env` |
| `pipelineiq-home/` | `github-app.pem` (GitHub App key) and `config` (pre-push hook settings) | `~/.pipelineiq/` |
| `models/` | Trained models | `<repo>/models/` |
| `pipelineiq.dump` | Database copy (`pg_dump -Fc`; a `--lite` copy has empty `raw.*` research tables and test results) | restored into the `postgres` container |
| `restore.ps1` | Does all of the below on Windows | |

## Rules

- Never print, cat, grep or paste the contents of `dot-env`, `.env`, `github-app.pem` or `config`.
  Copy them as files only.
- Never commit any of them. They are already in `.gitignore` or outside the repo.
- Ask before deleting anything that already exists in the target places. Back it up first.

## Steps

1. Check that git, Docker Desktop (running), `uv` and Node 22+ are installed. If one is missing,
   tell the person what to install; don't install system software without asking.
2. On Windows, run `powershell -ExecutionPolicy Bypass -File .\restore.ps1` from this folder.
   Add `-RepoPath <path>` if the repo should live somewhere other than
   `~/projects/Final_Research`, and `-Branch main` if they want main. If it fails, read the error,
   fix the cause, and run it again; every step can safely run twice.
3. On macOS or Linux, do the same by hand:
   - `git clone https://github.com/PrasadWijesinghe/Final_Research.git` and `git checkout feat/dashboard`.
   - Copy the files to the places in the table, then `chmod 600 ~/.pipelineiq/*`.
   - In `<repo>/.env`, set the line `GITHUB_APP_PRIVATE_KEY_PATH=` to the full path of
     `~/.pipelineiq/github-app.pem` (edit that one line without printing the file).
   - `make up`, then
     `docker compose cp pipelineiq.dump postgres:/tmp/pipelineiq.dump` and
     `docker compose exec -T postgres sh -c 'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists --no-owner /tmp/pipelineiq.dump'`.
   - `uv sync --all-packages` (not `--all-extras`: the embedding model is optional), `npm install` in `services/dashboard`,
     `docker compose --profile experts up -d --build testing-api testing-worker llm-gateway`.
4. Start the API (`uv run --env-file .env uvicorn api.app:app --host 127.0.0.1 --port 8000`) and
   the dashboard (`npm run dev` in `services/dashboard`), each in its own terminal.

## Check it worked

- `http://127.0.0.1:8103/status` shows `"model_version":"lightgbm-v4"`, not `stub`.
- `http://127.0.0.1:8200/health` shows provider `deepseek`.
- `http://localhost:5173`, **Failures** page lists the old failures and reviews.
- `make check` passes apart from 9 known Infra test failures (`make` needs Git Bash or WSL on Windows).

Then tell the person to delete the zip and this folder, since they contain secrets.
More detail: `docs/MOVING_MACHINES.md` and `docs/TESTING_GUIDE.md` in the repo.
