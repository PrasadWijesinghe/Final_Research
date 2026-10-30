# Moving PipelineIQ to another computer

The code is on GitHub, so you get it back with one `git clone`. Some things are kept off GitHub on
purpose: passwords, the collected data, the trained models and the database. You copy those by hand.
Use an external drive or a private cloud folder. Never use email, chat or a public link.

## The quick way (scripts)

1. On the old Mac, run `tools/move/pack.sh`. It makes `~/pipelineiq-move.zip` with the minimum
   below (settings, key, models, database copy) and asks you for a password.
   Add `--lite` (`tools/move/pack.sh --lite`) for a ~3 MB database copy instead of ~930 MB: the
   system runs and the dashboard keeps its failures, diagnoses, reviews and the knowledge base,
   but the collected research data (~7.4 GB) is left out. Old commits lose their CI run status, and
   replaying old failures (testing guide step 7), retraining and the evaluation need the full copy.
2. Move the zip to the new computer. On Windows: right-click, **Extract All**, type the password.
3. In the extracted folder, run `powershell -ExecutionPolicy Bypass -File .\restore.ps1`.
   It gets the code, puts every file in the right place, loads the database and starts the
   containers. Or open Claude Code in that folder and say "follow SETUP_FOR_CLAUDE.md".
4. Delete the zip and the extracted folder afterwards; they contain secrets.

The rest of this page is the same thing done by hand.

## What to copy

| What | Where it is now | Size | Why it matters |
|---|---|---|---|
| Settings and passwords | `Final_Research/.env` | tiny | Database password, GitHub App details, DeepSeek key. Nothing starts without it |
| GitHub App key and hook settings | `~/.pipelineiq/` (`github-app.pem`, `config`) | tiny | Lets the system talk to GitHub as the App; the push check uses `config` |
| Trained models | `Final_Research/models/` | 10 MB | The model that gives risk scores and flaky/genuine answers |
| Training history | `Final_Research/mlruns/` | 2 MB | Records of every training run, for the thesis |
| Datasets | `Final_Research/data/datasets/` | 30 MB | What the models were trained on |
| Reports | `Final_Research/data/reports/` | 11 MB | Evaluation reports |
| Snapshots | `Final_Research/data/snapshots/` | 330 MB | Saved copies of collected data |
| Raw job logs | `Final_Research/data/logs/` | 13 GB | Original CI logs. Only needed to rebuild datasets |
| Exports | `Final_Research/data/exports/` | 26 GB | Large data exports. Only needed to rebuild datasets |
| The database | Docker, not a normal folder | varies | Collected runs, failures, diagnoses, reviews. See below |
| Test repo | `~/projects/piq-demo` | small | Already on GitHub; just clone it again |

**Minimum** to run the system on the new computer: `.env`, `~/.pipelineiq/`, `models/` and the
database. The data folders are only needed to retrain or rebuild datasets.

The keys in `.env` and `github-app.pem` are secrets. Keep them only on your own computers. Never
commit them to git.

## Step 1: Save on the old computer

1. Start Docker Desktop, then run `make up`.
2. Save the database to one file:

   ```bash
   docker compose exec -T postgres pg_dump -U pipelineiq -Fc pipelineiq > pipelineiq.dump
   ```

3. Copy these to your drive: `pipelineiq.dump`, `.env`, the folder `~/.pipelineiq/`, and the
   folders `models/`, `mlruns/` and `data/` (or only the parts of `data/` you need, see the table).
4. Make sure all your code changes are committed and pushed: `git status` should show nothing to
   commit.

## Step 2: Set up the new computer

1. Install Docker Desktop, Node 22 or newer, `uv` and git.
2. Get the code:

   ```bash
   git clone https://github.com/PrasadWijesinghe/Final_Research.git
   cd Final_Research
   git checkout feat/dashboard        # or main, whichever you work on
   ```

3. Put the copied files back in the same places:
   - `.env` in the `Final_Research` folder.
   - `~/.pipelineiq/` in your home folder, then `chmod 600 ~/.pipelineiq/*`.
   - `models/`, `mlruns/` and `data/` in the `Final_Research` folder.
4. In `.env`, check `GITHUB_APP_PRIVATE_KEY_PATH`. It must point to the `.pem` file on the new
   computer, because your user name or home folder may be different.
5. Install the packages:

   ```bash
   uv sync
   cd services/dashboard && npm install && cd ../..
   ```

6. Start the database and load your saved copy:

   ```bash
   make up
   docker compose exec -T postgres pg_restore -U pipelineiq -d pipelineiq --clean --if-exists < pipelineiq.dump
   ```

7. Start the rest and check it works, using steps 1 and 3 of `docs/TESTING_GUIDE.md`.
8. If you use the push check in other repos, run the hook installer again in each one:
   `node <path to Final_Research>/tools/pre-push-hook/src/install.mjs`.

## Things that don't move by copying

- **GitHub sign-in on the dashboard:** sign in again on the new computer.
- **Git login:** log in to GitHub again. If a push says `403`, see step 3.4 of the testing guide.
- **The webhook relay:** nothing to copy. It uses the `SMEE_URL` already in `.env`; just run
  `make webhooks`.
- **The GitHub App:** it lives on GitHub, so it keeps working. Nothing to reinstall.

## Quick check after moving

- `make check` passes (apart from the 9 known Infra test failures).
- http://127.0.0.1:8103/status shows the model version (`lightgbm-v4`), not `stub`.
- The dashboard's **Failures** page shows your old failures and reviews.
