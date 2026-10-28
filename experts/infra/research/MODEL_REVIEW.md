# Infrastructure model review

## Update: 2026-10-28

The dashboard uses the packaged **v4-no-history** research model. It is not the v5 model below,
and a low percentage must not be interpreted as proof that a workflow will pass.

The workbook has 187 positive rows but only 105 distinct incidents. Of the 187 rows, 185 have
assistant-reviewed annotations rather than documented independent human verification. There are
175 unresolved failure rows. The largest positive categories are approval gates (113 rows) and
trigger configuration (59 rows), leaving few container, runner, network and deployment examples.
The sampled failure rate is not representative of arbitrary GitHub pushes.

Two event flags were missing in 2,000 rows even though the event was present. These flags are now
derived from the event. Live workflow flag extraction was also aligned more closely with the
historical collector; missing snapshots no longer silently become negative flags. Live commits still
aggregate multiple workflows while training rows describe one workflow run. Image availability is
not an input, and an invalid image name by itself is invisible to the current model.

A new no-history v5 random forest was trained with the corrected event flags. Its final temporal
test missed both of its two positive cases; two positives are far too few to estimate performance.
In a stricter leave-one-repository-out experiment, all model fitting, calibration, candidate
selection and threshold selection excluded the held-out repository. The retrained fold models
detected 5 of 184 positive rows and produced 35 false positives (179 false negatives, 1,895 true
negatives). This is an exploratory benchmark on the legacy labels, not an independently verified
acceptance test. The full v5 artifact is saved for research and was not promoted to the dashboard.

The previous `model.validate` report refits without each repository but uses a candidate and a
threshold selected globally; it is therefore exploratory and is not a fully independent test of
model selection. The new `model.benchmark` command performs nested repository validation.
The evidence, review steps and remaining work are in `DATASET_REPAIR_REPORT.md`.

Reviewed on 2026-10-25. The code pipeline is complete, but the trained model is **research-only**
and should not be connected to the production frontend yet.

## Data used

- Source rows: 3,000
- Unresolved labels excluded: 175
- Incident-crossing rows excluded from the main split: 199
- Model-fit rows: 1,261, including 153 failures
- Probability-calibration rows: 316, including 16 failures
- Validation rows: 537, including 15 failures
- Final temporal test rows: 512, including only 2 failures

The two positive test cases are too few for a stable final estimate. Positive cases are also
concentrated in a small number of repositories.

## Compared experiments

| Profile | Validation PR-AUC | Rolling PR-AUC | Rolling recall | Unseen-repository PR-AUC | Unseen-repository recall |
|---|---:|---:|---:|---:|---:|
| Full history | 0.491 | 0.439 | 0.083 | 0.186 | 0.000 |
| No history | 0.485 | 0.497 | 0.233 | 0.083 | 0.000 |

Recall values use each artifact's validation-selected threshold. The full-history profile ranks
unseen-repository cases better, while the no-history profile behaves better across rolling time
folds. Neither profile raises an alert for any of the 184 failures in leave-one-repository-out
validation at its saved threshold.

The final temporal test also has zero recall for both profiles. Its PR-AUC is approximately 0.006,
but this number is not stable because that split contains only two failures.

## Code review findings addressed

- Added an incident-purged chronological calibration set and sigmoid probability calibration.
- Added an explicit `no_history` feature profile for portability experiments.
- Made evaluation, robust validation, XAI and serving use the feature list stored in the artifact.
- Added a deterministic zip bundle with a SHA-256 manifest.
- Prevented RAG from returning unrelated zero-similarity cases and rejected duplicate case IDs.
- Added one-command orchestration in `build_model.py`.
- Added tests for calibration splitting, feature profiles, bundling and retrieval edge cases.

## Decision

Use `v4-full` only as the current research reference because it has the stronger
unseen-repository ranking. Do not integrate it into the existing Infra Expert application yet.
The next dataset should add independently reviewed failures from more repositories, then repeat
the same temporal test and leave-one-repository-out acceptance checks before integration.
