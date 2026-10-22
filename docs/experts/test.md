# Test & Application Behaviour expert (Member 3)

Folder `experts/testing`, package `testing_expert`, schema `testing`, port 8103.

## 1. What it does

- **Before a run:** for each test job, predict the risk that it fails, split into a
  **genuine** failure (a real bug) and a **flaky** failure, and explain the risk with
  concrete, fixable reasons (for example "promise not awaited at line 48").
- **After a failure (the core contribution):** decide for **this specific failure**
  whether it's a real bug or flakiness, using the evidence of this run. A test's past
  reputation alone is never enough (Haben et al.). Then suggest a fix grounded in
  verified past cases.
- The pre-run flaky risk is **an input to triage**, which links the two halves.

## 2. Pre-run output (`RiskOutput`, job-scoped)

One per test job in `CommitContext.workflow_jobs`:

- `probability` = P(the job fails) = P(genuine) + P(flaky)
- `sub_risks` = `{"genuine_fail": …, "flaky_fail": …}`
- `top_factors` = up to five SHAP factors, phrased as **risk factors, never causes**
- `threshold` chosen on validation data for **high precision**: warn rarely and be right

## 3. Labels

One label per test job attempt.

| Label | Rule | Confidence |
|---|---|---|
| `pass` | Passed on its first attempt | high |
| `flaky` | Failed, then passed on a re-run of the **same SHA** (later attempt, or another run on that SHA) | high |
| `flaky` (silent) | Failed, no re-run, and the next commit on the branch touched only unrelated files (e.g. docs) and passed | **low** |
| `genuine` | Failed, kept failing on that SHA, and a later commit **within 7 days** that changed the failing test's file or a file in its stack trace made it pass | high |
| `unclear` | Anything else. Excluded from training; sent to manual review | — |
| not test | Runner lost, job cancelled or timed out before tests ran, or the router puts it elsewhere | — |

Labels are written as `FailureLabel(category="test", sub_label=…, rule_name=…, confidence=…)`.
Train with and without low-confidence labels and report both.

**Manual check:** a random sample of about 50 per label, labelled independently by a second
person (supervisor or teammate). Report Cohen's kappa.

## 4. Features (pre-run)

All computed only from data before `history_cutoff`, in `testing_expert/features/`.

| Group | Examples |
|---|---|
| Change size | files changed; lines added and removed; source / test / config split |
| What changed in tests (tree-sitter on changed test files) | new async tests; unawaited promises; `setTimeout` and real timers; `Date.now()` / `new Date()`; `Math.random()`; unmocked network calls (`fetch`, `http`, `axios`); changed timeouts; `.only` / `.skip` added; shared mutable state at file level; missing cleanup in `after*` hooks |
| Source–test link | source changed without test changes; number of test files importing changed modules |
| Test config | test config changed; timeout, workers, test environment changed |
| Test dependencies | test framework, mocking or assertion library version changed; lockfile changed |
| History | job fail rate over 7, 30 and 90 days; weighted flip rate (Gruber et al.); consecutive failures; recent re-runs; flakiness of the touched tests |
| Run setup | runner OS; size of the Node version matrix; PR or push; default branch or not |
| Repo | suite size; average test-job duration; framework |
| Optional | a vector of the changed test code (ablation experiment only) |

**History granularity:** Mocha's default reporter lists every test, but Jest and Vitest by
default list only files unless run verbosely. Compute per-test history where the logs
allow it, and fall back to **per-file** history otherwise. Record which one was used.

## 5. Model

- Three classes: `pass`, `genuine_fail`, `flaky_fail`.
- Candidates: logistic regression, random forest, XGBoost, LightGBM. Class weights for imbalance (no naive oversampling).
- Validation: **temporal split** (train on earlier, test on later) and **leave-one-repo-out**.
- Calibration: isotonic or Platt on a held-out slice; report the Brier score.
- Metrics: precision, recall, F1, **PR-AUC** (main), ROC-AUC, flaky-class F1, macro F1, false-flaky rate.
- **Baselines:** majority class; "same as last outcome"; the re-run heuristic; a whole-pipeline (build-level) predictor; a test-level flakiness model (history + churn, Gruber-style).
- Tracking in MLflow (local). Export the winner to `models/testing/vN/`: `model.txt`, `calibrator.joblib`, `features.json`, `threshold.json`, `card.md`.

## 6. Explanations

SHAP TreeExplainer on the tree model. Each feature has a plain-language template in
`features/descriptions.yaml`, e.g. `unawaited_promises_in_changed_tests: "{value} promise(s) in changed tests are not awaited"`.
Point `path` and `line` at the exact spot when the feature knows it.

## 7. Triage (post-failure)

Input: the `RoutedFailure`, its log, the commit context, and the pre-run `RiskOutput`.

| Evidence | How |
|---|---|
| **Stack trace vs diff overlap** (key signal) | Parse stack frames, map paths to repo-relative files, and check whether any frame falls inside, or within ±5 lines of, a changed hunk. Report file-level and line-level overlap |
| Error type | Timeout, connection refused or reset, assertion mismatch, snapshot mismatch, unhandled rejection, out of memory |
| Test history | Flip rate and past outcomes of the failing tests |
| Pre-run risk | `flaky_fail` from this commit's prediction |
| Same-SHA evidence | Has this test passed on this SHA elsewhere (another job in the matrix, or another run)? |

A classifier trained on labelled failures gives the `verdict` (`genuine`, `flaky`, `unclear`)
with `verdict_confidence`. Below a confidence floor the verdict is `unclear`.
`suggest_rerun=true` only for a confident flaky verdict.

## 8. Log parsing

Parsers for Jest, Mocha and Vitest in `testing_expert/parsing/`:

- Strip GitHub's timestamp prefixes, `##[group]` markers and ANSI colour codes first.
- Extract failing test full names, error messages, assertion diffs and stack frames.
- Extract per-test outcomes and durations from passing logs where the reporter prints them.
- Normalise for retrieval: replace timestamps, durations, ports, memory addresses, temp paths and UUIDs with placeholders.
- Build fixtures from real sanitised logs, at least 10 per framework.

## 9. Knowledge base (RAG)

- **Mined cases:** a labelled failure followed, within 7 days, by a fixing commit that touched the failing test or a stack file and then passed. Store as `VerifiedCase(origin="mined")` with the log signature, root cause, a fix summary drawn from the diff and commit message, and the repo licence.
- **Tags:** root-cause categories from the flaky-test literature (async wait, concurrency, timeout, test-order dependency, network, time/date, randomness, resource leak, platform), plus FlakyFix fix categories where they apply.
- **Live cases:** added only after a developer confirms the cause and a re-run passes.
- **Retrieval:** hybrid (PostgreSQL full-text search + pgvector, `granite-embedding-small-english-r2`), merged by reciprocal rank fusion; return the top 5.

## 10. Diagnosis

The worker sends the LLM gateway: the cleaned failing-test excerpt, the overlap result,
the verdict evidence and the top retrieved cases. It asks for a fixed JSON schema. The
reply becomes a `Diagnosis` with `BehaviourDetails`; the remediation may cite only
retrieved case IDs. If the log shows the failure isn't a test failure, return
`status="misrouted"`. The prompt lives in `rag/prompts/diagnose.md`; its git hash is the
`prompt_version`.

## 11. Evaluation

1. **Headline:** per-failure triage vs per-test labelling on the same failures. Show how many real bugs the per-test approach would wrongly call flaky.
2. Pre-run model against all baselines, on temporal and leave-one-repo-out splits.
3. Feature ablation by group, including the optional code vector.
4. Retrieval comparison: BM25, MiniLM, granite, EmbeddingGemma and a code model, measured by precision@5, where "relevant" means the same failing test or the same fix category.
5. Diagnosis quality: agreement with confirmed root causes on a sample.
6. Time-to-diagnosis with developers, measured by the review page timer. **Needs ethics clearance first.**

## 12. Own tables (`testing` schema)

| Table | Holds |
|---|---|
| `test_results` | `job_id`, `attempt`, `test_id`, `file`, `full_name`, `outcome`, `duration_ms`, `granularity` (`test` or `file`) |
| `test_ids` | stable `test_id` = hash(repo, file, full name) |
| `labels` | this expert's label rows (mirrored into `platform.failure_labels`) |
| `prerun_features` | job attempt, `features` jsonb, `dataset_version` |
| `triage_features` | failure, `features` jsonb, `verdict`, `confidence` |
| `kb_cases` | the `VerifiedCase` payload, `embedding vector(384)`, `search tsvector`, `tags` |
