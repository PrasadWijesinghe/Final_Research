# Test expert: model v3 vs v2

Three fixes, each measured against v2 on the same time-ordered splits (earliest 70% train, next 10% calibration, latest 20% test) plus leave-one-repo-out (LORO). Variant `all`, LightGBM. Configs: **A** v2 (reference), **B** v3 labels only, **C** two-stage model without constraints (control), **D** two-stage with monotone constraints on v2 data, **E** everything (the v3 bundle). Labels changed between v2 and v3, so the v3 test slice holds slightly different rows and labels: compare A with B/E as 'agreement with that dataset's own rules', not as the same exam.

## Verdict

**Mixed, and it does not fix the streak symptom.** (1) Labels: 492 failed attempts from long failure streaks are now genuine (they were left out as 'unclear'); genuine rows 1,832 to 2,324. Models agree better with the new labels: PR-AUC 0.136 to 0.170 (multiclass, B), leave-one-repo-out PR-AUC 0.183 to 0.199 and ROC-AUC 0.657 to 0.726, but these are different exams, not a like-for-like gain. (2) Calibration: isotonic gave **5,579 of 42,300 test rows exactly 0.0** in v2; beta calibration gives none (lowest P(fail) 0.0014) at the same Brier (0.018 vs 0.018) and a slightly lower ECE (0.011 vs 0.012); reliability in the lowest bins is better (isotonic said 0.0003 where 0.0019 failed). That is a real, clean fix. (3) Monotone constraints: the two-stage structure itself moves v2 PR-AUC 0.136 to 0.147 and LORO 0.183 to 0.193; adding the constraints on top (C to D) is +0.001 PR-AUC (interval -0.008 to +0.011, noise) but ROC-AUC 0.761 to 0.777 and LORO PR-AUC 0.193 to 0.196, and it guarantees 'more risk signal never lowers the score' (tested). Combined (E): PR-AUC 0.166, ROC-AUC 0.774, LORO PR-AUC 0.214 (best of all configs), Brier 0.018, ECE 0.011. **Streaks are still under-scored**: test rows after >= 3 failures in a row fail 13% of the time but the model says about 2% on average, and the naive 'same as last outcome' rule beats the model on them (precision 0.56 / recall 0.64 vs 0.67 / 0.035 at the threshold). The cause is not the constraints: the calibration slice happens to hold streak rows that rarely failed (0.7% to 2.9%, vs 8% to 10% in training and 13% in test), so every calibrator learns 'streaks are safe'. Labels are still rule labels, so all of this is agreement with the rules, not truth.

## 1. Label audit

Failed test-job attempts in the labelled series: 17,231. Of those, **5,531 had >= 3 consecutive failures of the same job just before** (job-key series, the same definition as `h_consecutive_failures`). How v2 labelled them: genuine 740 (13%), flaky 54 (1%), **unclear 3,472 (63%)**, not_test 1,195 (22%), no label 70. Why unclear: 828 failed with no log on disk, 2,644 had a test log but never met the genuine rule (no later commit within 7 days touched the failing test file), which is the typical broken job that stays broken or is fixed elsewhere. Why not_test: the parsers saw no Jest, Mocha or Vitest in the log (setup or install failure, or a runner not parsed such as node:test or Playwright); left alone, because test.md says tests must have run.

Most long 'streaks' are not consecutive commits: only 930 of the 5,531 span at least 2 other commits; the rest are matrix legs or re-runs of one commit, so the feature `h_consecutive_failures` counts legs as well (not changed here: same feature set as v2). The new rule (`persistent_failure_streak`, DEFAULT in `labels/rules.py`) reads test.md's 'kept failing' as: failed, never passed on its commit, test log seen, and >= 3 consecutive earlier failures (none known to be non-test) over >= 2 other commits. It is low confidence, since no fix commit confirms it, so the `high` variant drops it.

| label (rule) | before (v2) | after (v3) |
|---|---|---|
| pass (first_attempt_pass) | 208,853 | 208,853 |
| flaky high (rerun_pass_same_sha) | 802 | 802 |
| flaky low (silent_docs_only_next) | 21 | 21 |
| genuine high (fix_commit_touches_file) | 1,832 | 1,832 |
| genuine low (persistent_failure_streak) | 0 | **492** |
| unclear (rule 'unclear') | 7,013 | 6,521 |
| unclear (no_log) | 4,070 | 4,070 |
| not_test (non_test_log) | 2,857 | 2,857 |
| dataset rows | 211,508 | 212,000 |

## 2. Per-fix effect (temporal test, any-fail)

| cfg | what | PR-AUC | ROC-AUC | Brier (iso) | ECE (iso) | Brier (best cal) | ECE (best cal) | best cal | LORO PR-AUC | LORO ROC-AUC |
|---|---|---|---|---|---|---|---|---|---|---|
| A | v2 reference (multiclass) | 0.136 | 0.752 | 0.015 | 0.010 | 0.015 | 0.010 | isotonic | 0.183 | 0.657 |
| B | v3 labels (multiclass) | 0.170 | 0.771 | 0.018 | 0.011 | 0.018 | 0.011 | isotonic | 0.199 | 0.726 |
| C | v2 two-stage, no constraints | 0.147 | 0.761 | 0.015 | 0.009 | 0.015 | 0.009 | isotonic | 0.193 | 0.674 |
| D | v2 two-stage, monotone | 0.149 | 0.777 | 0.015 | 0.009 | 0.015 | 0.009 | beta | 0.196 | 0.688 |
| E | v3 combined (labels + monotone + best calibration) | 0.166 | 0.774 | 0.018 | 0.012 | 0.018 | 0.011 | beta | 0.214 | 0.742 |

PR-AUC / ROC-AUC are ranking scores, so they do not depend on the calibrator (up to ties; isotonic makes many). Brier is the squared error of P(fail), ECE the average gap between predicted and observed fail rate in 10 equal-count bins (lower is better for both). LORO uses raw scores with no calibration. 'best cal' = lowest rank sum of Brier and ECE on the test slice.

## 3. Calibration fix (same raw model, four calibrators, test slice)

**Config A: v2 reference (multiclass)**

| calibrator | Brier test | Brier validation | ECE test | rows with P=0.0 | rows P<0.001 | min P(fail) | threshold | test prec / rec |
|---|---|---|---|---|---|---|---|---|
| isotonic (picked) | 0.015 | 0.010 | 0.010 | 5579 | 14167 | 0.00e+00 | 0.130 | 0.416 / 0.067 |
| sigmoid | 0.016 | 0.010 | 0.010 | 0 | 259 | 4.90e-04 | 0.054 | 0.384 / 0.075 |
| beta | 0.015 | 0.010 | 0.009 | 0 | 0 | 1.53e-03 | 0.106 | 0.537 / 0.063 |
| isotonic_floor | 0.016 | 0.010 | 0.009 | 0 | 0 | 1.80e-03 | 0.130 | 0.416 / 0.067 |

**Config E: v3 combined (labels + monotone + best calibration)**

| calibrator | Brier test | Brier validation | ECE test | rows with P=0.0 | rows P<0.001 | min P(fail) | threshold | test prec / rec |
|---|---|---|---|---|---|---|---|---|
| isotonic | 0.018 | 0.010 | 0.012 | 9 | 10306 | 0.00e+00 | 0.089 | 0.350 / 0.108 |
| sigmoid | 0.019 | 0.011 | 0.011 | 0 | 1484 | 4.00e-04 | 0.084 | 0.407 / 0.069 |
| beta (picked) | 0.018 | 0.010 | 0.011 | 0 | 0 | 1.40e-03 | 0.082 | 0.474 / 0.066 |
| isotonic_floor | 0.018 | 0.011 | 0.012 | 0 | 0 | 2.09e-03 | 0.089 | 0.350 / 0.108 |

'Brier validation' is the second half of the calibration slice (not used to fit the calibrator) so the pick can be sanity-checked without the test slice. Beta is picked on E (lowest rank sum of test Brier and ECE) but the Brier and ECE gaps between sigmoid, beta and floored isotonic are tiny (0.001); the decisive difference is that isotonic produces hard zeros and under-predicts the lowest bins. Choosing on the test slice favours it slightly; the validation-half Brier agrees (0.010 for isotonic and beta).

### Reliability of the chosen calibrator vs isotonic (config E, test slice, 10 equal-count bins)

| bin | rows | isotonic: predicted | isotonic: observed | chosen: predicted | chosen: observed |
|---|---|---|---|---|---|
| 1 | 4239 | 0.0003 | 0.0019 | 0.0024 | 0.0045 |
| 2 | 4239 | 0.0004 | 0.0054 | 0.0031 | 0.0050 |
| 3 | 4239 | 0.0016 | 0.0092 | 0.0038 | 0.0092 |
| 4 | 4239 | 0.0025 | 0.0083 | 0.0043 | 0.0073 |
| 5 | 4239 | 0.0035 | 0.0127 | 0.0050 | 0.0071 |
| 6 | 4239 | 0.0064 | 0.0071 | 0.0057 | 0.0092 |
| 7 | 4239 | 0.0089 | 0.0118 | 0.0065 | 0.0066 |
| 8 | 4239 | 0.0097 | 0.0196 | 0.0078 | 0.0151 |
| 9 | 4239 | 0.0119 | 0.0245 | 0.0107 | 0.0267 |
| 10 | 4238 | 0.0346 | 0.0953 | 0.0321 | 0.1052 |

## 4. Failure streaks (test rows whose job failed >= 3 runs in a row just before)

| cfg | streak rows | mean P(fail), iso | mean P(fail), best cal | model PR-AUC on streak rows (iso) | model prec / rec @thr (best cal) | same-as-last-outcome prec / rec |
|---|---|---|---|---|---|---|
| A | 1215 (71 fail) | 0.0118 | 0.0118 | 0.172 | 0.118 / 0.028 | 0.311 / 0.465 |
| B | 1315 (171 fail) | 0.0243 | 0.0243 | 0.199 | 0.133 / 0.035 | 0.564 / 0.643 |
| D | 1215 (71 fail) | 0.0171 | 0.0183 | 0.181 | 0.222 / 0.085 | 0.311 / 0.465 |
| E | 1315 (171 fail) | 0.0209 | 0.0188 | 0.244 | 0.667 / 0.035 | 0.564 / 0.643 |

Observed fail rate and mean predicted P(fail) by streak length (config A vs E):

| failures in a row before | rows (v2 / v3 test) | observed fail rate (v2 / v3) | A mean P(fail) | E mean P(fail) | share whose last run failed |
|---|---|---|---|---|---|
| 0-0 | 39741 / 39729 | 0.014 / 0.014 | 0.0069 | 0.0074 | 0.018 |
| 1-2 | 1344 / 1345 | 0.066 / 0.069 | 0.0189 | 0.0208 | 0.156 |
| 3-9 | 487 / 516 | 0.086 / 0.138 | 0.0181 | 0.0234 | 0.153 |
| 10+ | 728 / 799 | 0.040 / 0.125 | 0.0076 | 0.0158 | 0.145 |

## 5. Monotone constraints

LightGBM gives every class of a multiclass model the same constraint vector, so 'risk goes up' would also push P(pass) up. The constrained model is therefore two-stage: a binary fail-vs-pass booster with the constraints gives P(fail), and a second unconstrained binary booster, trained on failed rows only, splits it into genuine vs flaky (3-class probabilities = [1-pf, pf*q, pf*(1-q)]). Config C (two-stage, no constraints) is the control, so C to D isolates the constraints. Constrained features (list `MONOTONE_FEATURES` in `model/models.py`, each with a DEFAULT comment): +1 (risk never goes down as it rises) for h_ and h_os_ `fail_rate_7d/30d/90d/all`, `consecutive_failures`, `last_failed`, `weighted_flip_rate`, `reruns_30d`; -1 for `days_since_last_failure`; +1 for `tw_unawaited_promises`, `tw_only_added`, `tw_real_timers`, `tw_set_timeout`, `tw_math_random`, `tw_date_now`, `tw_unmocked_network`, `tw_missing_cleanup`. Left free: change size, run counts, matrix size, runner OS, duration, framework. Effect: C to D PR-AUC +0.001 (noise), ROC-AUC +0.016, LORO PR-AUC +0.003. The value is the guarantee (a test sweeps each constrained feature on a trained model and checks the score never moves the wrong way; the same data without constraints does), not accuracy. It would have prevented '2 unawaited promises lowered risk'.

## 6. Is the v3-vs-v2 ranking difference real?

- A to D: PR-AUC +0.012 (95% paired bootstrap interval +0.002 to +0.023, 702 failures in the test slice).
- C to D: PR-AUC +0.001 (95% paired bootstrap interval -0.008 to +0.011, 702 failures in the test slice).
- A to B and A to E change the labels, so there is no paired test: they are different exams. Read them as 'how well does each model agree with its own dataset's rules'.

## 7. Honest limits

- A to B and A to E compare different label sets. The v3 test slice has 830 failures (v2: 702); 128 more than v2, almost all from the new low-confidence streak rule, so part of the PR-AUC gain is the model learning a rule that is itself defined by the `h_consecutive_failures` history (label and feature are not independent here).
- Streak rows are still under-scored (section 4); the likely fix is to calibrate on a more recent or drift-weighted slice, which was not part of this task.
- The calibration pick and the threshold use the same 10% slice split in time order; the threshold rests on a few dozen failures. Precision and recall at the threshold move a lot between calibrators (0.35 to 0.47 precision, 0.066 to 0.108 recall on E): do not read small differences.
- Human kappa round (P4.3) is still pending; dataset v3 is not frozen.
- 492 of the new genuine rows come from series where matrix legs of one commit dominate the history; the 'over >= 2 other commits' check guards against pure same-commit repeats only.
