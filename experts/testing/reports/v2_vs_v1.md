# Test expert: dataset v2 vs v1

Same 211,508 labelled attempts and the same time-ordered splits in both. `v1` has 41 features; `v2` adds the tree-sitter test-change group `tw_*` (14) and per-runner-OS history `h_os_*` (13) for 68. Trained with the same procedure as P4.7 v1 (class weights, isotonic calibration, threshold from the calibration slice, leave-one-repo-out). LR numbers are re-run with the current code (it now adds missing-indicators), so they can differ slightly from the original v1 report.

## Verdict

LightGBM any-fail PR-AUC on the temporal test moves 0.127 to 0.140. No clear gain: the temporal difference is inside the bootstrap noise. Leave-one-repo-out LightGBM PR-AUC moves 0.188 to 0.183 (worse). See the ablation table for which group carries the change. Labels are still rule labels (human kappa round pending), so these numbers measure agreement with the rules, not truth.

## Temporal split (test = latest 20%)

| variant | model | v1 PR-AUC | v1 ROC-AUC | v1 Brier P(fail) | v1 prec / rec @thr | v2 PR-AUC | v2 ROC-AUC | v2 Brier P(fail) | v2 prec / rec @thr |
|---|---|---|---|---|---|---|---|---|---|
| all | logreg | 0.105 | 0.755 | 0.017 | 0.374 / 0.061 | 0.113 | 0.780 | 0.018 | 0.146 / 0.073 |
| all | lightgbm | 0.127 | 0.740 | 0.015 | 0.519 / 0.058 | 0.140 | 0.753 | 0.015 | 0.416 / 0.067 |
| high | logreg | 0.103 | 0.758 | 0.016 | 0.371 / 0.062 | 0.115 | 0.780 | 0.017 | 0.203 / 0.065 |
| high | lightgbm | 0.143 | 0.753 | 0.015 | 0.935 / 0.062 | 0.143 | 0.761 | 0.015 | 0.353 / 0.070 |
| strict | logreg | 0.118 | 0.833 | 0.009 | 0.132 / 0.139 | 0.124 | 0.893 | 0.010 | 0.099 / 0.295 |
| strict | lightgbm | 0.178 | 0.820 | 0.008 | 0.144 / 0.231 | 0.156 | 0.736 | 0.008 | 0.677 / 0.123 |

## Leave-one-repo-out (macro mean over repos)

| variant | model | v1 PR-AUC | v1 ROC-AUC | v2 PR-AUC | v2 ROC-AUC |
|---|---|---|---|---|---|
| all | logreg | 0.169 | 0.727 | 0.161 | 0.702 |
| all | lightgbm | 0.188 | 0.654 | 0.183 | 0.657 |
| high | logreg | 0.168 | 0.727 | 0.160 | 0.705 |
| high | lightgbm | 0.181 | 0.663 | 0.179 | 0.679 |
| strict | logreg | 0.222 | 0.679 | 0.223 | 0.669 |
| strict | lightgbm | 0.214 | 0.615 | 0.215 | 0.606 |

PR-AUC and ROC-AUC are for 'any fail' (P(genuine)+P(flaky)). Brier is for calibrated P(fail), lower is better. Precision / recall are 'any fail' on the test slice at the threshold each model picked on its own calibration slice (precision 0.8 is usually unreachable, so the rule falls back to best F0.5). Leave-one-repo-out uses raw (uncalibrated) scores, so it has no Brier or threshold. Precision and recall at the threshold are noisy: only about 0.3% of test rows (roughly 110) are flagged, and the threshold is chosen on a few dozen failures, so do not read small differences there.

## Ablation (variant all, temporal test, LightGBM and LR)

| config | model | features | temporal PR-AUC | temporal ROC-AUC | Brier P(fail) | LORO PR-AUC |
|---|---|---|---|---|---|---|
| v1 (reference) | logreg | 41 | 0.105 | 0.755 | 0.017 | 0.169 |
| v1 (reference) | lightgbm | 41 | 0.127 | 0.740 | 0.015 | 0.188 |
| v2 (all features) | logreg | 68 | 0.113 | 0.780 | 0.018 | 0.161 |
| v2 (all features) | lightgbm | 68 | 0.140 | 0.753 | 0.015 | 0.183 |
| v2 minus tw_ | logreg | 54 | 0.135 | 0.778 | 0.017 | 0.172 |
| v2 minus tw_ | lightgbm | 54 | 0.148 | 0.761 | 0.015 | 0.180 |
| v2 minus h_os_ | logreg | 55 | 0.096 | 0.754 | 0.017 | 0.171 |
| v2 minus h_os_ | lightgbm | 55 | 0.148 | 0.754 | 0.015 | 0.177 |

## Is the temporal difference real? (LightGBM, variant all)

LightGBM any-fail PR-AUC v2 minus v1 on the test slice: +0.002 (95% paired bootstrap interval -0.007 to +0.011, 702 failures in the test slice). The interval includes 0, so this is not distinguishable from noise.

## Top LightGBM features, v2 (gain, variant all)

| feature | gain | share |
|---|---|---|
| rp_avg_test_job_minutes | 275,471 | 10.2% |
| h_os_runs_7d | 163,072 | 6.0% |
| rp_test_jobs_in_window | 151,159 | 5.6% |
| rs_jobs_in_run | 146,225 | 5.4% |
| cs_lines_added | 125,986 | 4.6% |
| h_os_reruns_30d | 106,149 | 3.9% |
| h_fail_rate_30d | 100,951 | 3.7% |
| h_os_runs_30d | 82,248 | 3.0% |
| h_os_fail_rate_7d | 79,071 | 2.9% |
| h_days_since_last_failure | 78,592 | 2.9% |
| h_os_weighted_flip_rate | 68,689 | 2.5% |
| h_runs_7d | 68,064 | 2.5% |
| h_os_days_since_last_failure | 66,576 | 2.5% |
| cs_test_lines | 63,775 | 2.4% |
| h_reruns_30d | 63,136 | 2.3% |

## Missing values (`tw_*`)

`tw_*` is None when the test change is unknown (no parsable diff), and 0 when the commit changes no test. LightGBM reads None as native missing (each split learns which side missing goes to). Logistic regression fills missing values with the training median and adds a missing-indicator column (`# DEFAULT:` in `models.py`), so unknown is not mistaken for a real value. Only about 0.1% of rows are unknown, so this choice barely matters here.
