# Decisions

Agreed in the planning session on 16 October 2026 unless dated otherwise. Each entry: what
was decided, why, and its status. Add new entries at the bottom; to reverse one, add a new
entry that references it rather than editing history.

## Research direction

**D01 — Test expert reframed around flakiness and per-failure triage.** Pre-run, it predicts genuine vs flaky failure risk per test job, with fixable reasons. Post-failure triage (bug or flaky, for this specific failure) is the core contribution, and the pre-run flaky risk feeds it.
*Why:* a plain "will it fail?" forecast mostly repeats what running the tests tells you, and false alarms erode trust. One test run can't reveal flakiness, but a predictor can; Haben et al. show per-test labels misclassify real bugs.
*Status:* **awaiting supervisor approval.**

**D02 — Unit of prediction.** Test expert: one row per test job per run attempt. The other three experts: one prediction per commit.
*Why:* GitHub reports results per job; re-runs are separate attempts.

**D03 — Every warning comes with a reason and a fix hint, and only above a high-precision threshold.**
*Why:* vague or frequent warnings get ignored.

## Scope

**D04 — Claude Code builds the shared services first, then the Test expert, with stubs for the other three experts.** Each teammate builds their own expert.
*Why:* shared services are urgent (data is being deleted) and everyone needs them; stubs make end-to-end testing possible early.

**D05 — AI tools may be used to build the implementation.** Keep `docs/AI_LOG.md` up to date for the final report's disclosure.

## Data

**D06 — Collection: one 90-day backfill now, then a daily top-up with catch-up.** Datasets are frozen versions built from the raw data.
*Why:* GitHub deletes run history after 90 days (from 1 Oct 2026); continuous collection gives 5–6 months by training time.

**D07 — Each member has their own repo list and builds their own dataset, using their own token.**
*Why:* datasets are category-specific; pooling tokens would breach GitHub's terms.

**D08 — GitHub API only, no local clones.** Diffs from the commit endpoint (one call per commit); file contents from `raw.githubusercontent.com`.
*Why:* no VPS, and some laptops are weak.

**D09 — Collector:** daily, resumes from where it stopped, pauses itself near the rate limit, re-scans the last 14 days for new re-runs, and only covers the member's relevant workflows.

**D10 — Each run attempt is stored as its own row.**
*Why:* attempt 1 failing and attempt 2 passing on the same SHA is the flaky signal.

**D11 — Logs: all failed jobs, plus passing jobs in the member's category workflows.**
*Why:* per-test history (flip rates) needs passing runs too.

**D12 — No VPS. Each member runs the full stack locally in Docker with their own PostgreSQL.** Data is shared with `make export-data` / `make import-data` (pg_dump + logs archive), never by copying volume folders. The archive is also the weekly backup.
*Why:* no hosting budget is being used; volume folders aren't portable across machines.

**D13 — Identities are salted SHA-256 hashes, with one salt shared privately by the team.**
*Why:* the same developer gets the same hash in every dataset; no raw logins are stored.

**D14 — File-tagging patterns as listed in `docs/DATA.md` §5.** Patches over 1 MB are dropped; lockfiles are stored by reference.

**D15 — Shared `FailureLabel` format; each member writes rules for their own category; conflicts go to manual review.**

## Integration

**D16 — Microservices talking only through the contracts; each expert owns its own schema; no expert reads another's.**
*Why:* settles Build's "APIs only" vs Dependency/Infra's "shared database", and keeps each member's work separately testable and markable.

**D17 — Transport:** synchronous HTTP for `/predict`; Redis + RQ queue for diagnosis; experts post results back to the backend.
*Why:* prediction is fast; LLM diagnosis can take minutes.

**D18 — Endpoints as listed in `docs/ARCHITECTURE.md` §4.** `/predict` returns a list of `RiskOutput`s (the Test expert returns one per test job).

**D19 — Router: rules first (step names and log patterns per expert), a trained router later, compared against the rules.** It sends to two experts when their scores are close.
*Why:* works from day one, explainable, and the comparison is a clean result.

**D20 — Time budgets:** 10 seconds total for the pre-push hook (late experts show "not ready"); 2 minutes for the PR note, updated when late results arrive.
*Why:* developers wait on the hook. Build's slow compiler/ESLint features run only for the PR note.

**D21 — Contracts v1.1:** seven messages; `pre_push` event added; `replay` flag on `RoutedFailure`. Changes need all four members' approval.

## Live system

**D22 — GitHub App for live repos** (the demo forks in a team organisation, and private repos in the commercial version). Research repos use the collector with a personal token instead.
*Why:* you can't install apps or webhooks on repos you don't own.

**D23 — Webhooks reach laptops through a smee.io channel** (stable URL, documented by GitHub), with Cloudflare Tunnel as a fallback. This replaces the earlier polling idea.
*Why:* no VPS means no public address.

**D24 — The pre-push hook is a core feature.** It's a small Node.js script that sends the local diff to the backend's `/predict`, prints warnings and asks "Push anyway?". It never blocks on its own. Personal API keys are issued from the dashboard.

**D25 — Observer, not gate.** GitHub always runs the tests; the system never skips, blocks or auto-fixes.
*Why:* predictions are never certain, diagnosis needs the real run's evidence, and flakiness can only be seen by running.

**D26 — Dashboard MVP:** risk dashboard, pipeline history, diagnosis and review (with a decision timer), basic settings. Knowledge base browser and executive summary come later. "Sign in with GitHub", one role.

## AI components

**D27 — LLM: a hosted API (provider to be chosen), behind one LLM gateway that holds the only key.** A mock provider is used until the key is bought, and always in tests. Per-expert spending caps, caching and call logging.

**D28 — Embeddings: `ibm-granite/granite-embedding-small-english-r2` (384-d) for all experts**, with hybrid retrieval (full-text + vector). Compare alternatives (BM25, MiniLM, EmbeddingGemma, a code model) once about 100 verified cases exist, and switch if one clearly wins.
*Why:* small, code-aware, Apache-2.0, runs on a laptop CPU; public benchmarks don't cover CI test logs.

**D29 — Each member trains their own model. MLflow is used only for local experiment tracking; the winning model is exported to `models/<expert>/vN/`.** Training and serving share the same feature code.

**D30 — Replay mode is part of the shared services** and is how datasets are built. It respects `history_cutoff`, stores results separately and never touches GitHub.

## Test expert

**D31 — Labelling rules as in `docs/experts/test.md` §3:** a 7-day window for "a later commit fixed it"; silent-flaky cases labelled flaky with low confidence; ~50 per label double-checked, with kappa reported.

**D32 — Evaluation adds PR-AUC and the "last outcome" and "always re-run" baselines.** The headline experiment is per-failure triage vs per-test labelling.

**D33 — Initial repo list: 25 repos plus reserves in `config/repos/test.yaml`**, pending the re-run check. n8n, Strapi and Immich are excluded from any commercial knowledge base for licence reasons.

## Conventions

**D34 — Tooling and git rules as in `docs/CONVENTIONS.md`:** Python 3.12, uv, ruff, mypy, pytest, pre-commit; Node 22, pnpm, TypeScript strict; protected `main`, PRs, CODEOWNERS.

**D35 — Working name "PipelineIQ"**, taken from the prototype UI. Rename freely.

**D36 — Every component gets a plain-language `README.md` in its folder, written in the same task as the code**, from `docs/templates/COMPONENT_README.md`, and listed in `docs/COMPONENTS.md`.
*Why:* the team must understand and explain every part (viva, teammates picking up shared code) without reading the code.

**D37 — Claude Code works as an orchestrator with project subagents in `.claude/agents/`:** `implementer` and `reviewer` on Sonnet; `scaffolder` and `doc-writer` on Haiku. Each task goes build → review → document, and the main session ticks and commits.
*Why:* keeps cost down, keeps the main session's context small across a long build, and gives every task an independent review.

**D38 — Collection order: two passes (`backfill --logs failed`, then `fill-logs`) and one parallel lane per repo.** Date: 2026-10-23. The end state of D11 is unchanged (all failed-job logs, plus passing-job logs in relevant workflows); only the order changes. `backfill` still defaults to `--logs all` and `daily` always fetches all logs.
*Why:* downloading logs of passing jobs dominates the backfill (about 2.3 s per log, around 20 jobs per run in some repos), which would take 1 to 2 weeks for one runner, while GitHub deletes a day of history every day. Failed logs and metadata come first, passing logs are filled newest first.

**D39 — A run GitHub keeps failing on (5xx) is skipped and retried later; the repo still finishes.** Date: 2026-10-23. *Why:* in the real backfill the jobs listing of some big runs returned 502 on every retry, so the repo aborted and every re-run hit the same run (puppeteer, mongoose, prettier, babel). The jobs listing now falls back to 30 then 10 per page; if a run still fails with a 5xx it is recorded in `raw.skipped_runs` (migration 0003), warned about, and retried first by later `backfill` and `daily`. Network errors, 401/403 and rate limits still abort, so an outage never marks runs skipped. A log that fails with a 5xx stays pending for `fill-logs`.

**D40 — For the first model, passing-job logs are fetched only in runs that contain a failed job (`fill-logs --passing failed-runs`).** Date: 2026-10-26. The D11 target is unchanged: the rest can be added later by running `fill-logs` without the flag (it fills what is still pending) and then retraining. A passing job counts when any job of the same run, in any attempt, has the conclusion `failure`, `timed_out` or `startup_failure`; so a re-run whose attempt 1 failed and whose attempt 2 passed keeps the passing logs of attempt 2 (the flip evidence). Logs of failed jobs and every other log rule are unchanged, and `--limit` and newest-run-first order work as before. `daily` still fetches all logs, and `collector status` "logs pending" keeps counting the passing logs that were skipped on purpose. The two big repos prettier and vuejs/core are postponed for this first round: the supervisor skips them for now; they stay in `config/repos/test.yaml`.
*Why:* collection time. On the live database (2026-10-25) pass 2 had 210,695 pending logs, of which 90,793 are in runs with a failed job, so the flag cuts the work by 57%. Per-test flip history around failures is kept; base rates from fully green runs are deferred. Observed on 2026-10-25: pass 1 and pass 2 ran side by side (4 + 5 lanes), and at about 18:22 UTC every lane started getting HTTP 403 "API rate limit exceeded" on job-log requests, with `x-ratelimit-limit: 5000`, `x-ratelimit-remaining: 0`, `x-ratelimit-used: 5000` and `x-ratelimit-resource: core`. A direct probe confirmed it. The lanes stalled until the hourly reset, while `collector status` (GitHub's `/rate_limit`) showed a stale, higher remaining count. So job-log downloads are throttled by the same hourly limit as the other REST calls; the earlier note that logs do not count against it (in the handover brief and the collector README) was wrong or incomplete. With 210,695 logs pending, pass 2 cannot finish faster than the hourly quota allows: 210,695 calls at 5,000 per hour is more than 42 hours, before counting pass 1's own calls. Fetching one log archive per run attempt instead of one call per job is to be investigated and would get its own decision; 7,002 run attempts hold all 210,695 pending jobs, and 2,271 hold the 90,793 kept ones.

**D41 — `fill-logs` fetches the logs of a run attempt as one zip instead of one request per job (`--no-archive` keeps one request per job).** Date: 2026-10-26. *Why:* the 5,000 requests per hour quota is the bottleneck, and every job-log download is one request (D40). The 210,695 pending jobs sit in only 7,002 run attempts (about 30 jobs each), so pass 2 drops from about 210,000 requests to about 7,000; with D40's filter, 2,271 attempts hold the 90,793 kept jobs. Measured on 2026-10-25 on a public repo (vitejs/vite, run 40135357833, attempt 1, 10 jobs): `GET /repos/{owner}/{repo}/actions/runs/{run_id}/attempts/{attempt}/logs` answers 302 to blob storage (the token must not be sent there, and the client already drops it, as for job logs) and the body is a zip (120 KB for 10 jobs). In that zip each job had a top-level file `{n}_{job name}.txt` (the numbers looked like the job's position in the run, with gaps; that is a reading of one run, not verified) and a folder `{job name}/system.txt`, which is ignored. GitHub strips characters that are illegal in file names (the `:` of `Build&Test: node-24, ubuntu-latest` is gone in the zip), and a skipped job had no file (seen once, not verified further). One entry, `7_Build&Test node-24, ubuntu-latest.txt`, was byte for byte identical to what `GET /actions/jobs/{job_id}/logs` returns for that job (156,426 bytes both, UTF-8 byte order mark included). A second real run on 2026-10-26 (Automattic/mongoose run 37232717702, two attempts, one job each) gave the same result: the attempt-1 archive entry and the attempt-2 archive entry were each byte for byte identical to the per-job download of that attempt's job, and the two logs differ. So the existing code (clean secrets, gzip, write the file, save the `raw.logs` row with its redaction count) runs unchanged on the entry bytes.
*Matching rule and why it is safe:* a job is taken from the zip only when it is certain. The key of a name is its letters and digits, lower-cased; the zip file name loses its `{n}_` prefix and `.txt` first. A job is matched only if exactly one job of the attempt has that key (counting ALL jobs of the attempt in `raw.jobs`, not only the pending ones) and exactly one top-level file has it. Anything else, such as two jobs differing only in stripped characters, a job without a file, a file with a path separator or an unreadable file, gets its own request with the normal ok, expired or missing handling. A log can therefore never land on the wrong job; the price of doubt is extra requests.
*Outcomes of the archive request:*

| Answer | What happens |
|---|---|
| 200, a zip | matched jobs are stored from the zip, every other pending job of the attempt uses its own request |
| 410 | not believed at once: the attempt's first pending job gets one normal request. If that is a 410 too (stored as `expired`), the attempt's logs expired and the other pending jobs are marked `expired` with no more requests; with `--limit` only up to the remaining budget are marked (the confirming job counts), the rest stay pending for the next run. If it is not a 410, that job is handled as usual and the rest use their own requests. Cost: one extra request per expired attempt |
| 404, a body over 150 MiB, anything that cannot be opened as a zip (bad zip, names that are not valid UTF-8, an unsupported version), a file that cannot be read (bad CRC, encrypted, bzip2 or lzma errors) | the attempt's jobs, or just that job, use their own requests; the bytes are untrusted, so any error opening or reading them falls back per job; the size is checked while streaming, so a huge archive is never held in memory |
| 5xx or a network error | the attempt's jobs use their own requests (a 5xx on a job's own request keeps the D39 rule: left pending; a network error on it still stops the repo); the failure counts toward a guard: after 5 archives in a row failed, the repo stops with "GitHub looks unhealthy" so retries do not burn the quota, and the count carries over to the next repo of the same command (it fails fast there until one archive works); a re-run resumes, and `--no-archive` is the escape if the same attempts keep failing |
| 401, 403 or 429 | the repo stops (a dead token, a refusal, or a rate limit that never cleared); a re-run resumes |
| any other 4xx | the attempt's jobs use their own requests, which record what they get |

*Rest of the design:* archive mode is the default; `--no-archive` makes one request per job exactly as before. An archive is used only for an attempt with at least 2 pending jobs (one pending job costs one request either way, with no matching risk). Jobs are still grouped from the same newest-first list, so the order and D40's `--passing` filter work as before. Each log is still saved in its own transaction, so Ctrl+C is safe and a re-run sees only what is missing. `--limit N` counts logs stored and never overshoots: an archive stores at most what is left under the limit, newest first, and with fewer than 2 left the single request is used. `backfill` and `daily` are unchanged and keep fetching one log per job. Because pass 2 is now affordable, D40's `--passing failed-runs` becomes an option, not a requirement: passing logs of fully green runs can be collected too if the time is there. On a single log a 401 now also stops the repo (it used to be stored as `missing`, which with a dead token would mark every log missing for good); a plain 403 on a single log is still stored as `missing`. The client also reads the rate limit from the last answer in a redirect chain that carries the headers (the final answer first), because the storage answer after a log redirect (presumably) has none.
*Not verified on real data:* two real runs were checked (vitejs/vite run 40135357833, attempt 1: the `:` stripping and one byte-identical entry; Automattic/mongoose run 37232717702: both attempts, one job each, byte-identical). Still not verified: re-run attempts with several jobs, expired archives (a 410 versus a 404), non-ASCII names and reusable-workflow names; that `{n}` is the job's position and that skipped jobs have no file. Every other stripped character in the key rule is an assumption; the key drops all non-alphanumerics so it should not matter, but it was not observed. Zip name encoding is unchecked: Python decodes names as cp437 unless the zip sets the UTF-8 flag, so non-ASCII job names may come out as mojibake and then simply fall back to their own request, which costs requests. That a 410 on the archive means the same as a per-job 410 (`expired` is terminal) is a design assumption, which is why one job is checked before the rest are marked; if GitHub answers 404 for an expired archive the code falls back per job, which is correct but saves nothing. That the storage answer after a log redirect carries (presumably) no quota headers while the API's 302 does is also not measured live. The "about 7,000 requests" figure assumes every job matches; each fallback adds requests.

**D42 — Log parser scope and shape.** Date: 2026-10-27. The log parsers (P4.1, `experts/testing/src/testing_expert/parsing/`) work like this:
- **Scope:** they cover Jest, Mocha and Vitest only. `node:test`, TAP and Playwright are detected and labelled `other`; the job's conclusion gives the label later.
- **`other` is common:** in a 2,000-log survey, 47% of failed logs have no test output at all (build, install and lint failures). That is expected.
- **Granularity:** per-test results appear only where the reporter prints them. The dot and min reporters print failures only (granularity `none`). Vitest's default reporter gives granularity `file`.
- **Stack paths** are relative to the directory the tests ran from. typescript-eslint runs from a package folder, so its paths are not repo-root paths. A path that starts with `../` or is an http(s) location becomes `unknown`, so P4.3 must match on the suffix.
- **Safety limits:** parsers never raise. Lines are cut at 2 KB for matching (the cleaner keeps 32 KB per line). A log yields at most 1000 failures.
- **Fixtures:** 50 real sanitised logs (jest 13, mocha 16, vitest 15, other 6), about 2.2 MB. Emails are replaced by `user@example.invalid`.
- **Survey result** on 2,000 failed logs (seed 42): 0 exceptions. Of the 882 logs whose summary reports failures, 100% yield at least one failing name, and 97.7% agree exactly with their own summary.
- **Known misses:** nested or interleaved runs in babel and vitest-dev; multi-line generated test names in typescript-eslint.

*Why:* a parser for each extra runner now would be built on too little data. Trusting the repo's configured framework was rejected because it was wrong for node-postgres.

**D43 — Phase 4 before Phase 3.** Date: 2026-10-27. The user agreed (HANDOVER, 2026-10-25) to build the Test expert (Phase 4) before the user-facing Phase 3, so a working model exists first. D04 already puts the shared services first and then the Test expert, with stubs for the other three experts, and it does not mention Phase 3 (the user-facing work). The HANDOVER brief read it as a conflict, so this entry records the order explicitly: Phase 4 next, Phase 3 later.
*Why:* the project needs a trained model soon. More repos will be added later, and the model will be retrained when they are.

**D44 — Model v6 (the "still failing" rule) is not served; v4 stays.** Date: 2026-10-29. v6 = v4 plus a rule that floors the probability of a job whose previous run failed at the empirical fail rate of such rows (buckets 1, 2, 3-9, 10+; train + calibration only), a threshold chosen as max F1 on the calibration slice, and the job-level `ht_` features only if they won without the test slice (they did not). On the same test slice v6 is worse on any-fail PR-AUC (0.189 to 0.170, interval -0.027 to -0.012), the same on F1 at its threshold (0.108), slightly better calibrated (ECE 0.0066 to 0.0054), so the ship rule (better PR-AUC or better F1, calibration not worse) was not met. The bundle is kept in `models/testing_candidates/v6/`. Also decided: serving loads the per-test history only when the bundle has an `ht_` column, and uses a fast job-level loader when it only needs the job-level numbers (same numbers as the full loader).

**D45 — Triage is its own classifier with two floors, trained on the labelled failures (P4.9).** Date: 2026-10-29. A failed test job gets a verdict (genuine, flaky, unclear) from a small LightGBM on 45 evidence features (stack vs diff, error type, the failing tests' history, same-commit jobs finished by the failure time, pre-run flaky risk), calibrated; the flaky floor is the lowest one whose false-flaky rate is at most 5% on forward-chained validation, the genuine floor is at least 0.5; `suggest_rerun` only for a flaky verdict. Reason: a re-run advice for a real bug is the costly error. Triage lives in `models/testing/triage/v<N>`, outside the pre-push `models/testing/v<N>` search. The weak spots (same-commit evidence is partly circular with the rule labels, unseen repos need their own floor) are in `experts/testing/reports/triage_v1.md`.
*Why:* a raw "previous run failed" flag is right only about 12% of the time, so a rule built from it is too weak to alert at the model's threshold and only flattens the model's ordering. The strong "same as last outcome" baseline reads the previous run's label, which is only known after later events, so it cannot be reproduced at prediction time. Using the test slice to pick the "top 5% of pushes" threshold (twice the F1) was rejected: the test slice must not choose. That operating point stays a product decision.

## Open items (don't block the build)

| # | Item | Owner |
|---|---|---|
| O1 | Supervisor approval of D01 | Member 3 |
| O2 | Ethics clearance for the developer study (time-to-diagnosis, think-aloud sessions) | Team |
| O3 | Teammates confirm their contract sections, write their expert docs, fill their repo lists, tagging rows and router patterns | Members 1, 2, 4 |
| O4 | Run the re-run check and finalise the repo list | Member 3 |
| O5 | Choose and buy the LLM provider key | Team |
| O6 | Update the proposal text: no VPS and budget, 30 s → 10 s, AI-use disclosure, F1 target mismatch, Haben et al. figures, 9- vs 12-month timeline, scrambled sentences in 1.1.2 | Member 3 |
| O7 | Tune defaults once data exists: thresholds, router margin, triage confidence floor | Each owner |

**D46 — Test knowledge base v1: rule-mined cases, our own retrieval SQL, flaky cases marked by tag.** Date: 2026-10-30. Cases are mined from the P4.3 rule labels (genuine with a fix commit or a stack-file fix, and flaky re-runs), tagged by regexes in `config/test_kb.yaml`, embedded with granite and stored in `testing.kb_cases`. Retrieval is full text plus vector merged by RRF with SQL filters for time (a case counts only once its fix or re-run passed), self-retrieval and licence, so it lives in `testing_expert/kb/retrieve.py` instead of the shared `Collection` (which has no filters). The contract has no flaky `origin`, so flaky cases carry `kind:flaky` until the contract is revisited. Reason: a working, honest prototype before the LLM step; first retrieval numbers (`reports/kb_v1.md`) show that same-test and same-family neighbours are found well but fix categories are not predicted from the failure text.

**D47 — The test diagnosis narrative: gateway first, template always behind it, mock text never shown (P4.11).** Date: 2026-10-30. The verdict, confidence, evidence and `BehaviourDetails` come from triage and are never changed by the LLM. The root cause and remediation come from the LLM gateway (JSON schema reply, 20 s timeout, no retry) when a real model answers; otherwise a deterministic template writes them from triage and the best past case. The mock provider returns placeholders ("mock"), so its text is discarded: the template is used and `llm_model` reads "mock-1 (placeholder answers, text from template)". Remediation cites only retrieved case ids (others are dropped and noted). A log with no Jest, Mocha or Vitest failure that matches dependency, build or infra patterns is returned as `status="misrouted"` without a model call. `prompt_version` is the git blob hash of the prompt file, computed in Python so it also works in Docker. The diagnosis note is an evidence line because the contract has no notes field. Reason: a demo that reads sensibly today with the mock, and nothing to change in the expert when the real provider arrives (O5).
