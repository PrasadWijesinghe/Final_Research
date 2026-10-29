# Test expert: triage v1 (genuine, flaky or unclear)

When a test job has failed, triage says whether the failure looks like a **real bug in the change** (`genuine`), **flakiness** (`flaky`), or that the evidence does not decide (`unclear`). It suggests a re-run only for a confident `flaky` verdict. The code is in `experts/testing/src/testing_expert/triage/`, the bundle in `models/testing/triage/v1/` (git-ignored like the other bundles), the raw numbers in `triage_v1.json` next to this file.

## Verdict

**Triage works well on later failures of the same repos and only partly on repos it has never seen.** On the last 30% of failures in time (897 failures, 275 flaky) it gives a verdict for 83% of them, is right 91% of the time when it gives one, and calls a real bug "flaky" for 4.0% of the genuine failures. Dropped on repos it was not trained on (leave-one-repo-out), it still *ranks* failures well (ROC-AUC 0.88 per repo on average) but the confidence floors do not carry over: it stays careful (0.6% false-flaky) but finds only 9% of the flaky failures there. Two honest warnings come with this:

1. The labels are **rule labels** (human kappa round still pending). The numbers measure agreement with the rules, and the rules themselves look at same-commit re-runs, which is also what the strongest triage evidence looks at (see the ablation). Treat the same-commit result as partly circular.
2. The pre-run risk and the failing tests' own history add **nothing** here. Without the history group the model is slightly better.

## How it decides

Evidence (each item is plain code, each becomes a sentence shown to the developer, `triage/evidence.py`):

| Evidence | What is measured |
|---|---|
| Stack versus diff | the failing test's file, or a file its stack trace passes through, was changed by the commit; a frame within 5 lines of a changed hunk |
| Error type | regex families on the failure messages and the last 4,000 characters of the log: assertion, snapshot, code error, compile, missing module (lean genuine); timeout, connection reset or refused, port in use, network, out of memory, file system, killed (lean flaky); unhandled rejection |
| The failing tests' own history | share never seen before or never failed before, failure rate, flip rate, number of earlier commits on which the test both passed and failed |
| Same-commit evidence available at failure time | on this commit, jobs that had finished by the time this job failed: the same job passed or failed, other matrix legs, unrelated jobs, the attempt number |
| Pre-run risk | the pre-push model's `flaky_fail` for this commit |

Classifier: a small LightGBM (7 leaves, 150 trees, fixed settings) on 45 features, calibrated with a Platt map fitted on out-of-fold scores. Logistic regression was the other candidate. **LightGBM was chosen on the development set** (out-of-fold PR-AUC 0.713 against 0.694); the test set played no part in the choice.

Verdict: `flaky` when P(flaky) is at least **0.917**, `genuine` when P(genuine) is at least **0.502**, otherwise `unclear`. Confidence is the probability of the verdict. `suggest_rerun` is true only for `flaky`.

## Data and protocol

- 3,147 labelled failures (2,324 genuine, 823 flaky; `unclear` and `not_test` excluded). **2,990** are used: the other 157 are logs where no Jest, Mocha or Vitest failure could be parsed, and those are not triaged (the diagnosis stays the stub).
- **Time split** by the failed job's finish time: development = first 70% (2,093 failures, 27 Jul to 1 Oct 2026), test = last 30% (897 failures, 1 to 25 Oct). Inside development, forward-chaining validation (5 time blocks, each scored by a model trained on the earlier ones) picks the model and the floors. The test set was scored once.
- **Leave-one-repo-out** on the 9 repos with at least 30 failures and 5 of each class.
- **No future data.** Every feature uses only rows finished at or before the failed job's `completed_at` (history: strictly before). `tests/test_triage_leakage.py` adds a re-run that passes, later sibling jobs, later test results and mixed outcomes on later commits and checks that no feature moves, and a control shows those same rows do change the features once the cutoff moves later.
- **Pre-run risk** for training is cross-fitted by repo (4 folds, a booster like the served flaky one) so no row is scored by a model that trained on it.

## Results on the time-based test set (897 failures: 622 genuine, 275 flaky)

"Decided" means a verdict other than `unclear`. Recall for flaky counts an `unclear` flaky failure as missed. False-flaky rate = genuine failures called flaky / all genuine failures.

| | Coverage | Accuracy (decided) | Balanced acc. | Macro F1 | Flaky precision | Flaky recall | Flaky F1 | False-flaky rate | PR-AUC flaky |
|---|---|---|---|---|---|---|---|---|---|
| **Triage, LightGBM** (floors 0.917 / 0.502) | 0.83 | 0.909 | 0.868 | 0.879 | 0.860 | 0.556 | 0.675 | **0.040** | 0.854 |
| Triage, logistic regression | 0.89 | 0.841 | 0.734 | 0.765 | 0.879 | 0.396 | 0.546 | 0.024 | 0.789 |
| Always "genuine" | 1.00 | 0.693 | 0.500 | 0.409 | none | 0.000 | 0.000 | 0.000 | none |
| Per-test reputation (flaky if the test ever flipped on a commit) | 1.00 | 0.700 | 0.520 | 0.460 | 0.625 | 0.055 | 0.100 | 0.014 | none |
| Rules only (hand-written, same evidence, no learning) | 0.41 | 0.888 | 0.858 | 0.873 | 0.925 | 0.360 | 0.518 | 0.013 | none |

Reading it:

- Always-genuine is right 69% of the time and never finds a flaky failure. Triage keeps the false-flaky rate at 4% and finds 56% of the flaky ones.
- The per-test reputation approach (the "per-test labelling" the plan wants triage compared with) calls only 5.5% of the flaky failures flaky; this is the "history alone is not enough" result of Haben et al.: a flaky test's past does not say this failure is flaky.
- The hand-written rules are very precise when they fire (0.925) but fire on only 41% of failures. Triage covers twice as much and finds more flaky failures (0.56 against 0.36), at a higher false-flaky rate (4.0% against 1.3%).
- The false-flaky rate on the test set (4.0%) is a little below the 5% target used to choose the floor on development data (5.0% out-of-fold), so the floor held up.
- Accuracy on all 897 failures with "unclear counts as wrong" is 0.756, which is higher than always-genuine (0.693).

### Confidence floor and coverage

The floor is chosen on the development out-of-fold scores: the **lowest flaky floor whose false-flaky rate is at most 5%** (a re-run advice for a real bug is the costly error), and the lowest genuine floor, at least 0.5, whose genuine verdicts are right at least 85% of the time. The 5% target is a default (`# DEFAULT:` in `triage/model.py`); the table shows what other targets would give on the same trained model:

| Target false-flaky | Flaky floor | Coverage | Accuracy (decided) | Flaky precision | Flaky recall | False-flaky (test) |
|---|---|---|---|---|---|---|
| 2% | 0.976 | 0.73 | 0.931 | 0.976 | 0.302 | 0.003 |
| **5% (chosen)** | **0.917** | **0.83** | **0.909** | **0.860** | **0.556** | **0.040** |
| 10% | 0.813 | 0.91 | 0.882 | 0.785 | 0.705 | 0.085 |
| 15% | 0.712 | 0.95 | 0.868 | 0.754 | 0.771 | 0.111 |
| 20% | 0.569 | 0.98 | 0.852 | 0.720 | 0.815 | 0.140 |

At 10%, about 1 in 5 "flaky" verdicts would be a real bug, so 5% is the default. The genuine floor sits at its minimum, 0.50, in all rows (the lowest floor allowed, a genuine verdict only needs to be the more probable class), so almost all `unclear` verdicts come from failures the model thinks are probably flaky but not confidently.

## Ablation (time-based test set; floors re-chosen on development data each time)

| Evidence group | Without it: PR-AUC | Flaky recall | False-flaky | Alone: PR-AUC |
|---|---|---|---|---|
| All five groups | 0.854 | 0.556 | 0.040 | n/a |
| Same-commit evidence | **0.569** | 0.171 | 0.084 | 0.797 |
| Error type | 0.811 | 0.462 | 0.037 | 0.553 |
| Stack versus diff | 0.850 | 0.498 | 0.031 | 0.414 |
| Pre-run risk | 0.851 | 0.549 | 0.040 | 0.468 |
| Test history | **0.885** | 0.596 | 0.031 | 0.318 |

- **Same-commit evidence carries the verdict**: whether the same job or its matrix legs passed before this one failed. This is the signal the labels themselves are built from (a pass on the same commit means flaky), so some of its strength is agreement with the rules and would be lower against human labels.
- Error type helps next (timeouts and file system errors point to flaky; snapshot mismatches to genuine).
- Stack versus diff overlap, the signal the plan called key, helps only a little (0.850 without, 0.854 with). It is strong when it fires (a commit that changes the failing test file is almost always genuine) but the commit rarely touches the failing code: in this data the stack passes through changed code for only about 5% of failures.
- Test history and the pre-run risk add nothing (history slightly hurts). The history of tests that fail is dominated by long failing streaks, which look the same for flaky and genuine failures.
- Training on high-confidence labels only (2,582 failures) gives a better test result (PR-AUC 0.898, flaky recall 0.588, false-flaky 2.7%), as expected when the doubtful labels are dropped; the shipped model uses all labels.

## Leave-one-repo-out

| | Coverage | Balanced acc. | Macro F1 | Flaky F1 | False-flaky | PR-AUC | ROC-AUC |
|---|---|---|---|---|---|---|---|
| Pooled over 9 held-out repos | 0.87 | 0.562 | 0.561 | 0.166 | 0.006 | 0.602 | 0.810 |
| Mean of the 9 repos | 0.87 | 0.714 | 0.711 | 0.293 | 0.006 | 0.749 | 0.882 |

Per repo, ROC-AUC is between 0.73 (mongoose) and 0.99 (jest, sequelize), so the model orders failures well even for a repo it has never seen. But the floors (chosen on mixed-repo data) are too strict for repos whose flaky share is high or whose scores sit higher or lower: vite (374 flaky of 955) and puppeteer (204 of 550) get very few flaky verdicts (flaky F1 0.04 and 0.13) while keeping the false-flaky rate near zero. In practice: for a **new repo**, treat the verdict as a ranking and re-check the floor on that repo's own first failures before trusting `suggest_rerun`.

## What the short excerpt costs

The router sends only the last 20,000 characters of the log. The classifier is trained on features read from the whole log. Re-reading the whole test set from the 20,000-character tail only: a Jest, Mocha or Vitest failure is still found for 87% of the failures (for the rest the worker would answer with the stub; here they are scored with mostly missing features), and the overall result stays close (PR-AUC 0.867, false-flaky 2.6%, flaky recall 0.51, coverage 0.82). The worker reads the full log when `DATA_DIR` is mounted and the excerpt otherwise; the Docker worker has no log folder yet, so it uses the excerpt.

## Limits

- Rule labels only; kappa pending. Genuine labels mostly mean "a later commit changed the failing test file" or a long failure streak, flaky labels mean a same-commit re-run passed. Failures nobody re-ran are labelled genuine or `unclear` by rule, so the data over-represents failures that someone re-ran.
- Many rows come from a few repos and from the same commit (matrix legs of one broken commit fail together), so the 2,990 failures are fewer independent cases than they look.
- The error-family regexes were written once from known error names, not tuned on this data.
- The stack-versus-diff check needs the commit's files in `raw.commit_files` and patches for the line-level check; a live commit not yet collected gives "unknown", which the model treats as missing.
- Same-commit evidence and test history come from the database. With no database the worker triages from the log alone and the model sees those features as missing (not tested for accuracy in this report).
- No LLM text yet: the root cause and remediation text of the diagnosis are still the stub text (P4.11 adds the narrative).

## Defaults (all marked `# DEFAULT:` in the code)

5% false-flaky target, 85% genuine precision, both floors at least 0.5, 70/30 time split, 5 forward blocks, LightGBM 7 leaves and 150 trees, Platt calibration from 5-fold out-of-fold scores, 20 failing tests looked at per failure, ±5 lines for the line overlap, last 4,000 characters searched for crash messages, 4 repo folds for the pre-run risk (dataset v3), frame path matching by `/`-boundary suffix.
