# Handover: moving PipelineIQ work to the Mac (2026-10-25)

Read this first, then CLAUDE.md, `.claude/briefs/PHASE_LEAD.md` and `COMMON.md`. The user is
**Member 3 (Test expert)**. Data collection runs on the Mac from now on: the Windows laptop's
connection (about 90 KB/s) was too slow.

## How we work
- The main session orchestrates and delegates to the project agents (`.claude/agents/`):
  implementer and reviewer (Sonnet), scaffolder and doc-writer (Haiku). Big batches go to a
  phase lead that spawns them. One commit per task, reviewed first.
- Commit messages: Conventional Commits plus the task ID. **No `Co-Authored-By`, no session
  links** (CLAUDE.md).
- Never edit `shared/contracts/`. Never print or paste `.env`. No real network in tests.
- Ask before anything outward-facing or destructive. The user follows from a phone sometimes,
  so keep status replies short.
- Keep Docker light: Postgres and Redis only (`make up`). Experts and e2e are behind compose
  profiles.

## Repo state
- `main` (df4f17d): Phase 0, Phase 1 (P1.1–P1.7), Phase 2 (P2.1–P2.9), plus the GitHub
  empty-results fix. CI is green.
- `test/collection-setup` (c039412): **the branch to run collection from.** It contains:
  - node-redis listed explicitly in `config/repos/test.yaml` (it runs its tests through a local
    composite action, so auto-detection misses it);
  - D38: two-pass collection (`backfill --logs failed`, then `collector fill-logs`);
  - D39: runs GitHub keeps returning 5xx for are skipped into `raw.skipped_runs` and retried
    later (migration 0003), with smaller job pages and a 5-in-a-row breaker.
- PRs still to open (the user opens them in the browser):
  1. `shared/collector-skip-failing-runs` → main. It includes the two-pass commit.
  2. Then `test/collection-setup` → main. Only the config commit is left by then.
- Git on the Mac: this repo uses the project's GitHub account through a local `git config`.
  Pushing works.

## Collection state (exported 2026-10-25 from Windows)
- Archive: `data/exports/test-2026-10-25.tar.gz` (1.23 GB, 4.38 M rows, 28,884 files), copied
  by USB along with `data/supervise_mac.sh`. Import steps: `data/exports/MOVE_TO_MAC.md`
  (`make setup`, `make up`, `make migrate`, `make import-data FILE=...`).
- `collector status --member test` after import should show about:

  | Item | Count |
  |---|---|
  | Runs | 20,275 |
  | Attempts | 21,587 |
  | Jobs | 350,848 |
  | Logs ok | 26,746 |
  | Expired logs | 5,523 |
  | Passing logs pending | 273,173 |
  | Skipped runs | 0 |
- Pass 1 is done for: socket.io, winston, mikro-orm, typeorm, node-redis, knex, eslint, svelte,
  vite, sequelize, vitest, typescript-eslint.
- Pass 1 is unfinished for:
  - puppeteer, mongoose, prettier and babel: they hit 502s before D39;
  - node-postgres, jest, TanStack/query and vue core: vue core hasn't started.
- Start with `nohup bash data/supervise_mac.sh >/dev/null 2>&1 &`. It runs pass 1 in 4 lanes,
  pass 2 in 8 lanes (one big repo each), then one `daily`. It's resumable, so re-run it after any
  restart. Logs: `data/supervise.log`, `data/p1_laneN.log`, `data/p2_laneN.log`.
- **Only the Mac collects now.** Never run the collector on Windows as well, or the two
  databases diverge.

## Gotchas learned on real data
- **Stale `shared` install:** scripts use an installed copy of `shared`, not the source. After
  pulling changes under `shared/`, run
  `uv sync --all-packages --reinstall-package pipelineiq-shared --reinstall-package pipelineiq-collector`.
  Tests aren't affected, which is why this hides.
- **GitHub run counts:** GitHub silently returns 0 runs for some filtered run queries, and
  `total_count` caps at 2,500. That's fixed in the client, but don't trust a single filtered
  count.
- **Logs expire sooner than runs:** older runs often have expired logs (vitest, sequelize,
  typescript-eslint).
- **winston:** every run in 90 days is `action_required` (fork PRs awaiting approval), so it has
  no usable data.
- **Log downloads don't count against the 5,000/h REST limit**, but pass 1 (jobs, commits,
  attempts) does. So pass 2 scales with lanes and bandwidth, pass 1 with the rate limit.
- **Collection costs:** about 1.6 API calls per run in pass 1, and about 16–20 jobs per run in
  matrix repos (mikro-orm, sequelize).
- **On Windows only:** use `127.0.0.1`, not `localhost`. Not relevant on the Mac.

## Decisions waiting for the user
1. The busy five (pnpm, webpack, Ghost, bullmq, nuxt: 28k–32k+ runs each): add them after the
   20, or swap them for reserves (express, ioredis, jsdom, ws, axios, docusaurus)?
2. Replace winston with a reserve (ioredis or jsdom, both Mocha)?
3. Drop or postpone prettier and vue core (stable control repos, slow in pass 1)? The user was
   told this was recommended; no answer yet.
4. If pass 2 is still too slow on Starlink: option B, passing logs only near failures. That
   changes D11 and needs a DECISIONS entry.
5. Schedule `collector daily` on the Mac (launchd or cron) once the supervisor finishes.
6. From Phase 2, for the team:
   - where replay lives: the docs say `services/features`, the code is in `services/api`;
   - storing unmatched failures needs a contract change;
   - auth on the API endpoints before the dashboard.

## Housekeeping to do
- Add a `docs/AI_LOG.md` entry covering 2026-10-22 to 25: the empty-results fix, D38, D39, the
  breaker and the move to the Mac.
- `docs/COMPONENTS.md` Collector row: mention two passes, fill-logs and skipped runs; drop
  "integration tests not yet run".
- Delete the remote test branch if it's still there: `git push origin --delete test/push-check`.

## Next work, after the data is flowing
P1.8 data-quality report → Phase 4 (P4.1 log parsers with real sanitised fixtures from the
collected logs, P4.2 test results, P4.3 labelling, …). Phase 3 (user-facing) is deliberately
later. The user agreed Phase 4 comes first, but D04 suggested otherwise, so a DECISIONS entry is
still owed.
