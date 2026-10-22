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
