# Onboarding

This guide takes you from an empty folder to a working local setup. Follow the steps in order.
Where a step says **Expected**, check that you see the same result before you go on.

Run every command in **Git Bash**, from the repo folder, unless a step says otherwise. Git Bash is
a Linux-style terminal that comes with Git for Windows. Do not use PowerShell or Command Prompt
for these commands. The `Makefile` only works in a Linux-style shell such as Git Bash.

Words used in this guide:

- **Terminal:** the window where you type commands.
- **Environment variable:** a named setting that programs read, such as `DATABASE_URL`.
- **Docker** runs programs in isolated boxes called containers. **Docker Compose** starts several
  containers from one file, `docker-compose.yml`.
- **PostgreSQL** (Postgres) is the database. **Redis** is a fast store that holds queued jobs.
- **Migration:** a script that creates or changes database tables in a fixed order.
- **make** runs named command groups from the `Makefile`. For example, `make check`.
- **uv** is the Python tool. It installs Python 3.12 and the project's packages.

## 1. Install the prerequisites

Install these once per computer.

- **Git and Git Bash.** On Windows, install "Git for Windows" from git-scm.com. Git Bash comes
  with it. On macOS, run `xcode-select --install`. On Linux, run `sudo apt install git`.
  Check: `git --version`
- **uv.** On Windows, open PowerShell and run `winget install --id astral-sh.uv -e`. On macOS, run
  `brew install uv`. On Linux, run `curl -LsSf https://astral.sh/uv/install.sh | sh`.
  Check: `uv --version`
- **GNU make.** On Windows, run `winget install ezwinports.make`. It does not need admin rights.
  On macOS, `make` comes with the Xcode command line tools. On Linux, run `sudo apt install make`.
  Check: `make --version`
- **Docker Desktop.** On Windows and macOS, install it from docker.com and start it. On Linux,
  install Docker Engine and the Docker Compose plugin for your distribution.
  Check: `docker compose version`

Two notes:

- On Windows, **close Git Bash and open it again** after installing uv or make. A new terminal
  picks up the new PATH, which is the list of folders the terminal searches for programs.
- Docker Desktop must be **running** before you run `make up`. Wait until it says it is running.

## 2. Clone the repo

```bash
git clone <repo-url> "Final Research"
cd "Final Research"
```

Replace `<repo-url>` with the team's GitHub URL.

**Expected:** the folder contains `README.md` and `Makefile`.

Start on the branch you were given, or on `main`. Make your own branch before you change code:

```bash
git checkout -b <member>/<short-desc>     # for example: test/log-parsers
```

## 3. Create and fill your `.env` file

`.env` holds your private settings. Git ignores it, so it is never uploaded. Never commit it,
paste it into chat, or copy it into a log.

```bash
cp .env.example .env
```

Open `.env` in any text editor and fill in these lines:

- **`GITHUB_TOKEN`:** your own GitHub token. Create a **fine-grained personal access token** in
  GitHub under Settings, then Developer settings, then Personal access tokens. Give it
  **read-only access to public repositories**. Each member uses their own token. Never share a
  token or pool tokens.
- **`IDENTITY_SALT`:** the team's shared salt. The team shares it privately. Everyone must use
  the **same** value. The collector mixes this salt into a hash of each author's login, so
  people's data can only be matched when the salts are identical.
- **`MEMBER`:** your expert. One of `dependency`, `build`, `test` or `infra`. Step 8 uses the
  same name in its `--member` flag.

Leave the other lines as they are. `LLM_PROVIDER=mock` means no real AI model is called.
`LLM_API_KEY` and `BACKEND_URL` can stay empty for now.

### How `.env` is read

`.env` is not read by every tool. Know which tool reads it:

- **Docker Compose (`make up`) reads `.env` by itself.** It uses the `POSTGRES_*` and `*_PORT`
  values.
- **`make` reads `.env` by itself.** It skips the file when it does not exist, and never prints the
  values. So `make migrate` and `make check` see `DATABASE_URL` and `REDIS_URL` without any
  `export`. Keep `.env` lines in plain `KEY=value` form: no quotes, no `export`, and Unix line
  endings (LF).
- **`uv`, `pytest` and `alembic` run directly do not read `.env`.** They only see variables that are
  exported in the current terminal. Use the `make` targets, or load the file first:

```bash
set -a; . ./.env; set +a
```

`set -a` marks every variable as exported, `. ./.env` reads the file, and `set +a` turns the
marking off again. The command prints nothing. If `.env` was saved with Windows line endings
(CRLF), each value ends with a hidden carriage return, and `DATABASE_URL` breaks with an
unclear error. Save `.env` with LF line endings.

If you changed `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB` or `POSTGRES_PORT`, put the same
values into `DATABASE_URL`. Do the same for `REDIS_URL` and `REDIS_PORT`.

## 4. Install the project: `make setup`

```bash
make setup
```

This runs two things:

- `uv sync --all-packages` creates the `.venv` folder and installs every package at the exact
  versions in `uv.lock`. The first run can take several minutes, because uv may download
  Python 3.12.
- `pre-commit install` adds a Git hook. The hook runs the linters and type checker before each
  commit.

**Expected:** the command ends without an error.

## 5. Start Postgres and Redis: `make up`

```bash
make up
```

Docker starts two containers:

- **Postgres 17** with the **pgvector** extension, which lets the database search by vector
  similarity. On first start, a script creates the schemas `raw`, `platform`, `dependency`,
  `build`, `testing` and `infra`.
- **Redis 7.4**, which holds queued jobs.

The first run downloads the container images, which can take several minutes. Both containers
only listen on `127.0.0.1`, so nothing outside your computer can reach them.

**Expected:** the command ends after both containers are healthy. To check, run
`docker compose ps`. Both rows show `healthy`.

Stop the containers with `make down`. Your data stays in Docker volumes, so `make up` brings it back.

## 6. Create the tables: `make migrate`

This step reads `DATABASE_URL` from your `.env` file (see step 3).

```bash
make migrate
```

This runs Alembic, the migration tool, with `shared/db/alembic.ini`. It creates the 18 tables in
the `raw` and `platform` schemas. When it works there is no error and no Alembic output.

**Expected:** no error. To check which version is applied, run:

```bash
uv run alembic -c shared/db/alembic.ini current
```

It prints `0002 (head)`. (Run directly, this command needs `DATABASE_URL` exported; see step 3.)

If you see `DATABASE_URL is not set`, check that `.env` exists and has a `DATABASE_URL` line (step 3).

## 7. Run the checks: `make check`

```bash
make check
```

This runs four tools in order:

1. `ruff check` looks for code problems.
2. `ruff format --check` checks the code formatting.
3. `mypy` checks the types.
4. `pytest` runs the tests.

**Expected:** all four pass.

Some tests need the database. They are marked "integration". When `DATABASE_URL` is not set, or
the database is down, these tests are **skipped** and the skip reason is shown. A skip is not a
failure.

The database tests never touch your real database. They create a separate throwaway database named
after yours with `_test` added (for example `pipelineiq_test`), run their migrations there, and
drop it when the tests finish. It is safe to run `make check` at any time, also after you have
collected data.

## 8. Collect data: backfill, status, daily

Do steps 4 to 6 first. `make setup` installs the collector (it needs uv and Git), `make up` starts
Postgres, and `make migrate` creates the tables. The collector writes to that database, so Docker
Desktop must be running whenever you run it.

1. Fill in the repo list for your member, in `config/repos/<member>.yaml`. The format is in
   `docs/DATA.md`, section 2.
2. Make sure `GITHUB_TOKEN`, `IDENTITY_SALT` and `DATABASE_URL` are in your `.env` file (step 3).
   The collector needs all three. `uv run` does not read `.env` by itself, so add
   `--env-file .env` to every collector command. Run them from the repo folder:

   ```bash
   uv run --env-file .env collector backfill --member <member>
   ```

   This pulls the last 90 days of workflow runs, jobs, logs and commit diffs for the repos in
   your list. GitHub keeps only 90 days of runs, so do not wait. GitHub allows 5,000 requests per
   hour per token, so a backfill can take hours. You can stop it with Ctrl+C at any time. Run the
   same command again to resume.

3. Check what has been collected:

   ```bash
   uv run --env-file .env collector status --member <member>
   ```

   It prints counts per repo, days with no runs, the backfill state, the last daily sync and your
   remaining GitHub requests. A day with no runs can just be a quiet day. The rate limit line says
   "unavailable" if `GITHUB_TOKEN` is not set.

4. After the backfill has finished, keep the data up to date with:

   ```bash
   uv run --env-file .env collector daily --member <member>
   ```

   It fetches runs created since the last sync, and looks again at the last 14 days, because
   re-runs of older runs show up later. If you run `daily` before the backfill has finished, it
   tells you to run the backfill first. Running it twice is harmless.

### Run `daily` on a schedule

**Missed days are fine.** Run `daily` at least every week or two. The next run catches up on
everything since the last successful one. If more than 90 days pass, GitHub has already deleted
the older runs, and the collector warns you that data was lost.

**Linux and macOS (cron).** Run `crontab -e` and add this line, with `<repo>` replaced by the full
path of your checkout (escape spaces with a backslash). It runs every day at 03:15:

```
15 3 * * * cd <repo> && uv run --env-file .env collector daily --member test >> data/collector.log 2>&1
```

Use your own member name instead of `test`. Cron has a short PATH, so it may not find `uv`. If the
log says `uv: not found`, put the full path from `which uv` in the line, or add a line
`PATH=/home/<you>/.local/bin:/usr/local/bin:/usr/bin:/bin` at the top of the crontab. The `data`
folder must exist (`mkdir -p data`). Docker must be running at that time.

**Windows (Task Scheduler).** In **Command Prompt (cmd.exe)** (not PowerShell: it handles the quotes differently), run this once, with the path of your
checkout in place of `C:\path\to\Final Research` (keep the `\"` around the path):

```
schtasks /Create /TN "PipelineIQ collector daily" /SC DAILY /ST 03:15 /TR "cmd /c cd /d \"C:\path\to\Final Research\" && uv run --env-file .env collector daily --member test >> data\collector.log 2>&1"
```

Create the `data` folder first (`mkdir data`) and check that `where uv` finds uv in a new Command Prompt. A repo that fails makes the command exit non-zero; the next run simply covers a longer window.

The task runs only while you are logged in, and the computer must be on at 03:15 (Task Scheduler
runs a missed task when you next log in only if you tick that option in the GUI). Check it with
`schtasks /Query /TN "PipelineIQ collector daily"`, run it now with
`schtasks /Run /TN "PipelineIQ collector daily"`, and remove it with
`schtasks /Delete /TN "PipelineIQ collector daily" /F`.

The same thing in the app: open **Task Scheduler**, then **Create Basic Task**. Name it, choose
**Daily** and a time, then **Start a program**. Set **Program/script** to `cmd`, **Arguments** to
`/c uv run --env-file .env collector daily --member test >> data\collector.log 2>&1`, and
**Start in** to your repo folder. Finish, open the task's **Properties**, and on **Settings** tick
**Run task as soon as possible after a scheduled start is missed**.

## 9. Start your expert (available after P2.1)

The template folder, `experts/_template`, is still empty. It arrives in P2.1. Once it exists,
follow the section "Starting your expert" in `docs/experts/README.md`. In short:

1. Copy `experts/_template/` to your expert folder (`experts/dependency`, `experts/build`, `experts/testing` or `experts/infra`; note the test member's folder is `testing`) and rename the package inside it. Set your
   port: dependency 8101, build 8102, test 8103, infra 8104.
2. When your folder has a `pyproject.toml`, remove the folder from the `exclude` list in the root
   `pyproject.toml`. Also add your package folder to the `files` list under `[tool.mypy]`.
3. Write `docs/experts/<member>.md`. It covers labels, features, the model plan and the diagnosis
   details. Use `docs/experts/test.md` as an example.
4. Fill in your repo list in `config/repos/<member>.yaml`, your rows in `config/file_tags.yaml`,
   and your patterns in `config/router_rules.yaml`.
5. Run the collector backfill from step 8.
6. Replace the stub functions one at a time, and run `make check` after each change. Never edit
   `shared/contracts/`. If you need a change there, raise it with the team.

## If something goes wrong

- **`make up` fails with "Cannot connect to the Docker daemon", or it hangs.** Docker Desktop is
  not running. Start it, wait until it says it is running, and try again.
- **`port is already allocated` or `address already in use` on 5432 or 6379.** Another program
  uses that port, often a local Postgres or Redis. Either stop that program, or move the port:
  1. In `.env`, set `POSTGRES_PORT=5433` (or `REDIS_PORT=6380`).
  2. Update the matching URL in your terminal, for example
     `export DATABASE_URL=postgresql+psycopg://pipelineiq:change-me@127.0.0.1:5433/pipelineiq`.
  3. Run `make up` again. If you already started the stack, run `make down` first.
- **Commands feel slow, or a connection waits about 2 seconds on Windows.** Use `127.0.0.1` in
  every URL, not `localhost`. The values in `.env.example` already do this.
- **`uv: command not found` or `make: command not found`.** Close Git Bash and open it again. If
  the command still is not found, uv usually installs to `~/.local/bin` in Git Bash. Check that
  folder exists. For make, check the winget install step ran without error.
- **`DATABASE_URL is not set`.** Check that `.env` exists and has the line. `make` reads it;
  direct `uv`/`alembic` calls need it exported.
- **`password authentication failed`.** `DATABASE_URL` does not match `POSTGRES_PASSWORD`, or you
  changed the password after the first `make up`. Postgres reads the password only when it
  creates its data volume. Before you collect any data, you can start over. Run
  `docker compose down -v`, which deletes the local database, then run `make up` and
  `make migrate` again.
- **Something else fails.** Ask in the team channel. Include the exact command and the last 20
  lines of output. Remove your token and the salt from anything you paste.
