# Build & Code Quality expert (Member 2)

Folder `experts/build`, package `build_expert`, schema `build`, port 8102.
Proposal: C2, "Intelligent Code Quality Failure Expert" (V.T. Nawodya, IT23417748).

## 1. What it does

- **Before a run:** for each commit, predict the risk that CI fails because of the build or
  code-quality toolchain: the TypeScript compiler (`tsc`), ESLint, Prettier, or a bundler
  (Vite, webpack, Rollup, esbuild). Explain the risk with SHAP factors that point at what
  changed, for example "strictness settings changed in tsconfig" or "3 export lines removed".
- **After a failure:** read the compiler, linter or bundler log, find the failing tool, the error
  codes and the file locations, and suggest a fix grounded in verified past fixes (RAG).

This document covers the dataset in detail (sections 2 to 5) because that is what exists now.
Model, explanations and diagnosis (sections 6 to 8) are the plan.

## 2. Repos and workflows

### 2.1 Repo list (`config/repos/build.yaml`)

20 enabled repos, 10 reserves and 4 licence-risky repos (research only). They were chosen on
22 Oct 2026 from 80 Node.js candidates with a **static screen**: each candidate's workflow
files, `package.json` scripts, licence file and default-branch history were read through git.

| Rule | Why |
|---|---|
| TypeScript-heavy | The expert targets tsconfig, type errors and TS-aware lint rules |
| A workflow the build rule keeps (2.2), triggered on pull requests | Build and lint failures happen mostly on PRs |
| Permissive licence (MIT, Apache-2.0, BSD) | Cases go into a knowledge base that may be commercial (DATA.md section 8) |
| Active default branch | A rough sign of CI volume |
| A mix of libraries, apps/monorepos and bundler-heavy repos | Each group fails in different ways (types and lint; everything at once; bundling) |

The static screen could not see real CI runs: the session that built this list had no GitHub
API access to other people's repos. **Before the first backfill, run the build screen**
(2.4), which counts real failures and estimates cost, and trim the list. pnpm and nuxt are
already reserves: the Test member's real collection found about 30,000 runs each in 90 days.

### 2.2 Which workflows count (`config/workflow_detection.yaml`, member `build`)

A workflow is kept if any job's `run:` line compiles, type-checks, lints, format-checks or
bundles: package-manager scripts (`npm run build`, `pnpm -r typecheck`, `yarn lint:ci`,
`npm run format:check`), tools called directly (`npx tsc`, `eslint`, `vue-tsc`, `tsup`,
`webpack`, `rollup`, `esbuild`), framework CLIs (`vite build`, `next build`), `prettier --check`,
Biome, and monorepo runners with a build target (`turbo run build`, `nx run-many -t lint`).
Installs, `echo` lines, comments, `docker build` and paths such as `./packages/webpack` do not
count. Most CI workflows that run tests also build first, so they are kept too. Labelling
(section 3) decides which failures are build failures. Tests:
`services/collector/tests/test_workflow_detection_build.py`.

### 2.3 Which logs are fetched (two passes, D38 and D42)

Most jobs pass, and each log takes about 2.3 s to download, so passing-job logs are what makes
collection slow. The collector collects in two passes (D38): `--logs failed` stores everything
except passing-job logs, and `fill-logs` fetches those later, newest first. Build uses
`--logs failed` for both `backfill` and `daily` (D42), and runs `fill-logs` when there is time:

| Command | Fetches | Needed for |
|---|---|---|
| `collector backfill --member build --logs failed` | runs, jobs, steps, commits, diffs, config files, failed-job logs | labels and every feature group |
| `collector daily --member build --logs failed` | the same for new runs, plus a 14-day re-scan | keeping the data growing |
| `collector fill-logs --member build [--limit N]` | passing-job logs, newest first | the "last warnings" history features only |

Failed-job logs are always fetched. A log GitHub keeps failing on (5xx) stays pending and the
run is retried later (D39).

### 2.4 Build screen (`scripts/build_screen.py`)

```bash
uv run --env-file .env python scripts/build_screen.py          # enabled repos
uv run --env-file .env python scripts/build_screen.py --all    # reserves too
```

For each repo it counts 90-day runs and failures in 7-day windows (the hardened counting of the
re-run check), works out from the newest 300 runs which share are build workflows, opens up to 12
failed build-workflow runs to see which step failed, and estimates: build failures in 90 days,
pass 1 hours and failed-log download hours. The suggestion column says `keep` (50+ estimated
build failures and pass 1 under about 5 hours), `too quiet` or `expensive`. About 30 to 45 REST
calls per repo. Report: `data/reports/build_screen-<date>.md`.

## 3. Labels

Every failed job attempt gets a decision. The rules only ever **claim** the `build` category;
a job they do not claim is left to the other members' rules. Claimed jobs become shared
`FailureLabel`s (`category="build"`, `labelled_by="rule"`, `rule_name="build.<rule>.v1"`) in
`platform.failure_labels`, with `is_final=false` (D15: conflicts go to manual review).

First the log is cleaned and split into steps (`parsing/clean.py`): timestamps and colour codes
are removed and each `##[group]Run <command>` starts a step. The failed step is the one GitHub
marked with `Process completed with exit code N`. Parsers (`parsing/tools.py`) then look for:

| Tool | Recognised by | Sub-label |
|---|---|---|
| tsc (also vue-tsc, Next.js type errors) | `file(line,col): error TS2322: ...`, the `--pretty` form, `error TS5023` config errors | `tsc` |
| ESLint | stylish output (`12:5  error  message  rule-id`), the `✖ N problems` summary, `--max-warnings` exceeded, ESLint crashes | `eslint` |
| Prettier | `Checking formatting...`, `[warn] file`, `Code style issues found` | `format` |
| Bundlers | Vite/Rollup `error during build` and `Could not resolve`, webpack `ERROR in` and `Module not found`, esbuild `✘ [ERROR]`, Next.js `Failed to compile` | `bundler` |

Signs of other causes are noted as hints: dependency (`ERESOLVE`, `ERR_PNPM_*`), resource (heap
out of memory, no disk space), runner lost, network errors, and test-runner summaries.

| Rule (first match wins) | When | Confidence |
|---|---|---|
| `not_build_setup_step` | The failed step is checkout, setup-node, cache or an install | not claimed |
| `not_build_resource` / `_dependency` / `_runner` | No tool error, and the step shows that cause | not claimed |
| `not_build_test_failure` | No tool error, and a test runner reported failures | not claimed |
| `build_step_tool_error` | A tool error in a build, lint, format or type-check step | **high** |
| `tool_error_other_step` | A tool error in a step with another name (for example `./ci.sh`) | medium |
| `build_step_no_signature` | A build-type step failed but no known tool output was found | medium, sub-label `build_other` |
| `tool_error_in_test_step` | A tool error inside a step that also reports test failures (for example ts-jest) | low |
| `tool_error_with_other_cause` | A tool error next to an out-of-memory or dependency error | low |
| `..._whole_log` | The log has no exit-code marker (cut short), so the whole log was read | one level lower |
| `build_step_name_only` | No log (expired or never fetched); only the step name says build, lint or type-check | low |

If the same job **passed on a re-run of the same SHA**, the confidence drops one level and a note
is added. Builds are mostly deterministic, but pull-request runs build a merge commit, so a
re-run can see a different base branch.

**Manual check (to do):** like the Test expert, take a random sample of about 50 jobs per
sub-label and per "not claimed" reason, label them independently, and report Cohen's kappa.

## 4. Features (pre-run)

All features are computed in `build_expert/features/`, the same code for training and serving.
Descriptions for SHAP reasons are in `features/descriptions.yaml` (a test checks every feature
has one). Values are stored as numbers; missing history is empty (`null`), never zero.

| Group | Examples | From |
|---|---|---|
| A. Source change (37) | files and lines changed; TS/JS/test/declaration files; deleted or renamed source files; imports and exports added or removed; interface/type/enum changes; new `any`, `@ts-ignore`, `eslint-disable`, `console` calls and `!` assertions; docs-only and tests-only commits | `CommitContext.changed_files` patches |
| B. Build-tool config (26) | tsconfig strictness, target/module, paths and include lines changed; ESLint severities changed and rules set to error; ESLint plugins/extends; bundler options; package.json build scripts, build-tool dependency versions, TypeScript version, `@types`, module fields, engines; lockfile, workflow, Node version lines and `.nvmrc` changed | Patches of config files |
| C. CI environment (16) | Node versions (min, max, count), Windows/macOS runners, package manager, monorepo runner, whether CI type-checks, lints, format-checks, bundles; pull request, default branch, dependency-bot branch | `CommitContext.workflow_jobs`, branch |
| D. History (20) | build-failure rate of the repo over 7/30/90 days; tsc, lint and bundler failure rates; last build on this branch failed, failure streak, hours since; author's commits and failure rate; failure rate in the same packages; ESLint warnings and type errors printed by the last run on the branch, and the warning trend | Earlier commits' outcomes |

**No look-ahead:** an earlier commit's outcome counts only from the moment its last first-attempt
run finished (`completed_at`), so a commit whose run starts while another is still running does
not see that one's result. A test checks this case.

**Change from the proposal (static signals).** Proposal section 3.3 runs the linter and the
compiler locally on each commit. The team uses no repo clones (D08), so that is not possible
for history. Instead, `last_lint_warnings` and `last_type_errors` read the counts printed by the
most recent earlier run on the same branch (D41). They come from failed-job logs, plus passing
logs once `fill-logs` has fetched them; when no log is there the value is empty.

**Change from the proposal (AST).** The proposal derives change indicators from the syntax tree
of the diff. A diff only shows the changed hunks, so the A-group markers are line-level patterns
on added and removed lines. A tree-sitter version can be compared later (feature ablation).

## 5. Dataset

### 5.1 Unit and cut-off

**One row per commit** (D02): all first-attempt runs of build-relevant workflows on that head
SHA. The cut-off (`history_cutoff`) is the earliest of those runs' `created_at`. Commits are
skipped when a run is still in progress, when every run was cancelled, or when replay cannot
rebuild the commit (missing commit or files); the manifest counts each reason.

### 5.2 Build it

```bash
uv run --env-file .env build-dataset --version v1                       # everything stored
uv run --env-file .env build-dataset --version v2 --from 2026-08-01 --to 2026-11-01
uv run --env-file .env build-dataset --version v3 --repo vitejs/vite --no-write-labels
```

It reads Postgres (`DATABASE_URL`) and the stored logs (`DATA_DIR`), never GitHub. With
`--from`, the 90 days before the window are still loaded so history features are complete.
Datasets are frozen: an existing version folder is never overwritten.

### 5.3 Files (`data/datasets/build/<version>/`)

| File | What |
|---|---|
| `rows.parquet` | One row per commit: ids (repo, SHA, branch, event, PR, author hash, cut-off, run ids, licence), labels, and the 99 features |
| `fix_candidates.parquet` | Mined fix cases for the knowledge base: a build failure followed on the same branch within 7 days by a fully green commit, with the error codes and locations, the fix commit's message and changed files, and which error files the fix touched |
| `manifest.json` | Build time, git commit, window, cut-off range, rows per repo, label counts, job labels by rule, skipped commits by reason, rule and parser versions, the feature list by group, and the SHA-256 of each file |

### 5.4 Label columns

| Column | Meaning |
|---|---|
| `y_build` | 1 if any first-attempt job was claimed as a build failure (any confidence). **Main target** |
| `y_build_confident` | Same, high or medium confidence only. Train with both and report both |
| `y_tsc`, `y_lint`, `y_bundler`, `y_build_other` | Sub-targets for `sub_risks` (`y_lint` covers ESLint and Prettier) |
| `y_other_failure` | The commit failed, but not for a build reason: these are the "failures of other categories" negatives from the proposal |
| `label_confidence`, `label_rules`, `top_error_codes` | Best confidence, rules that fired, most common error codes |
| `label_status` | `missing_log` when a failed job could not be checked because its log is gone |

## 6. Model (plan)

- Target `y_build`, sub-risks from `y_tsc`, `y_lint`, `y_bundler`.
- Candidates (proposal 3.4): gradient-boosted trees (LightGBM or XGBoost) on groups A to D, and a
  variant that adds a code embedding of the changed source and config text.
- Validation: temporal split on `history_cutoff`, plus leave-one-repo-out. Tune on validation
  only. Class weights for imbalance, chosen from the observed distribution.
- Calibration (isotonic or Platt) and an operating threshold chosen on validation data for high
  precision (D03).
- Baselines (proposal 3.6): "same as the last build of this workflow" and a rule-based threshold
  on `last_lint_warnings` / `last_type_errors`.
- Export to `models/build/vN/` like the Test expert.

## 7. Explanations (plan)

SHAP TreeExplainer on the tree model. Each top factor becomes a `RiskFactor` with the template
from `features/descriptions.yaml`, phrased as a risk factor, never as a cause.

## 8. Diagnosis (plan)

The worker receives a `RoutedFailure`, runs the same log parsers, and fills `BuildDetails`
(`tool`, error `locations`). Retrieval runs over the `fix_candidates` (after review) and later
over live verified cases. The LLM may cite only retrieved case ids. Router patterns for Build in
`config/router_rules.yaml` were checked against the real tool output in the fixtures.

## 9. Hypotheses and how the dataset serves them

| Hypothesis (proposal 3.9) | Needs from the dataset |
|---|---|
| H1: higher F1 than both baselines | `y_build`, history and last-warning features for the baselines |
| H2: positive lead time | `history_cutoff` vs `completed_at` (the warning comes before the run ends) |
| H3: config features help | Group B can be dropped as an ablation |
| H4: RAG makes diagnoses better | `fix_candidates.parquet` as the starting knowledge base |

## 10. Own tables

None yet. Labels are in `platform.failure_labels`; everything else is in the dataset files.
A `build.job_signals` table (parsed log results per job) would speed up rebuilds and serving and
is the next step when the dataset gets large.

## 11. Open points

| # | Point |
|---|---|
| B-O1 | Run the build screen with a real token and trim `config/repos/build.yaml` |
| B-O2 | GHALogs (proposal 3.2) is not used: the team design collects its own data. Decide with the supervisor whether to add it as extra history |
| B-O3 | Manual label check and kappa (section 3) |
| B-O4 | Replace the tool-real but CI-framed log fixtures with sanitised logs from the first backfill |
