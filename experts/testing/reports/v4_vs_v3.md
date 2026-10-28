# Test expert: model v4 vs v3

Dataset v3 (unchanged), the same time-ordered splits as v3: 148,425 train rows, 21,186 calibration rows (first 60%, 12,711, fit the calibrators; last 40%, 8,475, is a held-out recent validation part used for thresholds and picks), 42,389 test rows (830 failures: 589 genuine, 241 flaky; 1315 streak rows, 171 of them failing). Configs: **V3** = the shipped v3 (two-stage booster, 3-class beta calibration), **V3C** = the same booster with the new calibration, **V4** = two separate binary models (genuine vs rest, flaky vs rest) with the new calibration.

## Verdict

**V4 is modestly better than v3 and is shipped; the streak defect is only partly fixed.** (1) Calibration: calibrating on the whole pre-test history (out-of-fold scores over the train period plus the calibration slice, with a 30-day recency half-life) instead of the 10% slice cuts ECE on the test slice from 0.0127 to 0.0071 and Brier from 0.01811 to 0.01772 on the same booster (validation: ECE 0.0077 to 0.0037), with no hard zeros. (2) Two separate binary models (V4) against v3 as shipped: any-fail PR-AUC 0.183 to 0.189 (+0.006, interval -0.003 to +0.015: not a proven gain), ROC-AUC 0.791 to 0.807, Brier 0.01819 to 0.01774, ECE 0.0118 to 0.0066; genuine PR-AUC 0.093 to 0.106 (+0.012, interval +0.003 to +0.022, real but small), flaky PR-AUC 0.177 to 0.191 (not significant), flaky ROC-AUC 0.819 to 0.859. Against the same booster with the same new calibration (V3C), V4 ranks clearly better (any-fail +0.021, interval +0.013 to +0.030). Leave-one-repo-out is mixed: flaky PR-AUC 0.080 to 0.171 (large), genuine unchanged, any-fail PR-AUC 0.214 to 0.205 (slightly worse). (3) **Streaks are still under-scored**: test rows after >= 3 failures in a row fail 13% of the time; mean P(fail) went from 0.016 (v3) to 0.033 (v4), and the naive 'same as last outcome' rule (precision 0.56 / recall 0.64) still beats the model there. V4 does rank streak rows better (PR-AUC on them 0.276 to 0.413). Per-class thresholds rest on a few dozen validation failures and are noisy (flaky precision at its threshold fell from 0.80 to 0.34), so read the threshold rows with care. Labels are rule labels, so all of this is agreement with the rules.

## 1. Calibration fix (V3 booster, any-fail)

The raw booster is the same in every row; only the map from raw score to probability changes. 'Pool' = out-of-fold scores over the train period (expanding window, each block scored by a model trained only on earlier blocks) plus the first 60% of the calibration slice, so calibration sees far more history than the 10% slice. Validation = the held-out last 40% of the calibration slice. The test columns are shown but did **not** choose anything.

| calibration | Brier val | ECE val | Brier test | ECE test | streak mean P(fail), test (observed) | threshold (val) | test prec / rec |
|---|---|---|---|---|---|---|---|
| v3 style: calibration slice only (first 60%) | 0.01221 | 0.0077 | 0.01811 | 0.0127 | 0.014 (obs 0.130) | 0.044 | 0.308 / 0.136 |
| recent window: last 15000 pool rows | 0.01220 | 0.0073 | 0.01807 | 0.0123 | 0.015 (obs 0.130) | 0.049 | 0.308 / 0.136 |
| recent window: last 30000 pool rows | 0.01216 | 0.0065 | 0.01796 | 0.0100 | 0.020 (obs 0.130) | 0.056 | 0.308 / 0.136 |
| recent window: last 60000 pool rows | 0.01225 | 0.0044 | 0.01783 | 0.0089 | 0.025 (obs 0.130) | 0.087 | 0.308 / 0.136 |
| whole pool, no weights | 0.01217 | 0.0038 | 0.01764 | 0.0059 | 0.034 (obs 0.130) | 0.112 | 0.308 / 0.136 |
| whole pool, recency half-life 7 days | 0.01219 | 0.0055 | 0.01791 | 0.0098 | 0.021 (obs 0.130) | 0.067 | 0.308 / 0.136 |
| whole pool, recency half-life 14 days | 0.01217 | 0.0045 | 0.01780 | 0.0084 | 0.025 (obs 0.130) | 0.084 | 0.308 / 0.136 |
| whole pool, recency half-life 30 days | 0.01217 | 0.0037 | 0.01772 | 0.0071 | 0.029 (obs 0.130) | 0.097 | 0.308 / 0.136 |
| whole pool, recency half-life 60 days | 0.01217 | 0.0036 | 0.01768 | 0.0065 | 0.031 (obs 0.130) | 0.104 | 0.308 / 0.136 |

Streak rows in validation: 396 with only 3 failures, so validation cannot say anything about streaks. Pre-test rolling-origin check (calibrate on everything earlier, score the next block, 5 blocks over the pool plus validation):

| calibration | Brier | mean ECE | streak rows: mean P(fail) | streak rows: observed |
|---|---|---|---|---|
| plain, no weights | 0.00887 | 0.0093 | 0.040 | 0.046 |
| plain, half-life 30d | 0.00884 | 0.0085 | 0.037 | 0.046 |
| plain, half-life 60d | 0.00885 | 0.0089 | 0.039 | 0.046 |
| streak-aware, no weights | 0.00890 | 0.0086 | 0.072 | 0.046 |
| streak-aware, half-life 30d | 0.00888 | 0.0080 | 0.067 | 0.046 |

**Choice: whole pool with a 30-day recency half-life.** Why. The 10% calibration slice is an unlucky stretch for streaks: the streak-row failure rate swings from 2% to 15% between periods of the train data (8.9% on average in train, 1.4% in the calibration slice, 13% in test), and the booster itself already separates streak rows only about 3x from other rows in raw score where the observed ratio is 8x, so no map of the raw score alone can recover it. Calibrating on more history fixes the overall calibration (ECE and Brier improve on both validation and test) and lifts the streak mean P(fail) from 0.014 to 0.029 on test, but it cannot reach 0.13. Half-life 30 days ties with the unweighted pool and the 60-day variant on validation (Brier 0.01217, ECE 0.0036 to 0.0038) and has the best Brier and a better mean ECE in the pre-test rolling-origin check, so it is the pick; the differences between the whole-pool variants are tiny. Recent windows alone are worse than the whole pool. The test slice was not used to choose, but notice that it agrees. I also tried a streak-aware map (streak flag as an extra input): on the test slice it lifts streak recall (0.13 to 0.39) and Brier (0.0174) but the pre-test rolling-origin check says it over-predicts streak rows (0.07 vs 0.046 observed) and does not improve Brier or ECE, so with no pre-test evidence for it, it is not shipped. Note the streak rate in the validation part is a trough (3 failures in 396 rows), which is why validation cannot judge streaks.

### Does the booster have the base signal?

`h_consecutive_failures` and `h_last_failed` are both in the feature set and both carry a +1 monotone constraint (share of total gain in the fail booster: 0.22% and 0.02%; the strongest history feature, `h_weighted_flip_rate`, has 2.18%). Adding explicit streak flags (`>= 3`, `>= 10`) as extra booster inputs changed test PR-AUC from 0.168 to 0.167 and the streak mean from 0.19 to 0.19 in a raw-score check, so the base signal is already in the model; the issue is how little gain the trees give it (it is dominated by flip rate, runner and timing features) plus the period drift above, not a missing feature. Nothing was added.

## 2. V4 against V3, any-fail (temporal test)

| config | PR-AUC | ROC-AUC | Brier | ECE | threshold | prec / rec @ thr |
|---|---|---|---|---|---|---|
| V3 shipped | 0.183 | 0.791 | 0.01819 | 0.0118 | 0.092 | 0.588 / 0.060 |
| V3C (new calibration) | 0.168 | 0.788 | 0.01772 | 0.0071 | 0.097 | 0.308 / 0.136 |
| V4 with v3-style calibration | 0.181 | 0.805 | 0.01841 | 0.0131 | 0.086 | 0.570 / 0.064 |
| V4 | 0.189 | 0.807 | 0.01774 | 0.0066 | 0.169 | 0.532 / 0.060 |

V4 'either class crosses its own threshold' alert rule: precision 0.393, recall 0.077, flags 0.38% of test rows (the combined-score threshold rule above flags 0.22%).

## 3. Per class (temporal test)

| class | config | PR-AUC | ROC-AUC | Brier | ECE | threshold (val) | prec / rec @ thr |
|---|---|---|---|---|---|---|---|
| genuine fail | V3 | 0.093 | 0.773 | 0.01360 | 0.0083 | 0.046 | 0.199 / 0.083 |
| genuine fail | V3C | 0.092 | 0.759 | 0.01346 | 0.0078 | 0.017 | 0.113 / 0.431 |
| genuine fail | V4 | 0.106 | 0.783 | 0.01321 | 0.0056 | 0.135 | 0.200 / 0.029 |
| flaky fail | V3 | 0.177 | 0.819 | 0.00516 | 0.0036 | 0.049 | 0.795 / 0.129 |
| flaky fail | V3C | 0.183 | 0.796 | 0.00501 | 0.0021 | 0.125 | 0.733 / 0.137 |
| flaky fail | V4 | 0.191 | 0.859 | 0.00512 | 0.0017 | 0.078 | 0.342 / 0.158 |

V3 and V3C have no per-class thresholds of their own; to compare like with like, each class score of those configs gets the same threshold rule on the same validation part. V3C splits the new any-fail probability with the booster's raw genuine share.

## 4. Failure streaks (test rows whose job failed >= 3 runs in a row just before)

| config | mean P(fail) on streak rows | PR-AUC on streak rows | model prec / rec @ thr | same-as-last-outcome prec / rec |
|---|---|---|---|---|
| V3 | 0.0164 (observed 0.1300) | 0.276 | 0.667 / 0.035 | 0.564 / 0.643 |
| V3C | 0.0291 (observed 0.1300) | 0.262 | 0.333 / 0.135 | 0.564 / 0.643 |
| V4 | 0.0334 (observed 0.1300) | 0.413 | 0.583 / 0.041 | 0.564 / 0.643 |

| failures in a row before | rows | observed fail rate | V3 mean P | V3C mean P | V4 mean P | share whose last run failed |
|---|---|---|---|---|---|---|
| 0-0 | 39729 | 0.014 | 0.0071 | 0.0124 | 0.0140 | 0.018 |
| 1-2 | 1345 | 0.069 | 0.0190 | 0.0375 | 0.0376 | 0.156 |
| 3-9 | 516 | 0.138 | 0.0205 | 0.0395 | 0.0388 | 0.153 |
| 10+ | 799 | 0.125 | 0.0137 | 0.0224 | 0.0299 | 0.145 |

## 5. Reliability of any-fail P (test, 10 equal-count bins)

| bin | rows | V3 pred | V3 obs | V3C pred | V3C obs | V4 pred | V4 obs |
|---|---|---|---|---|---|---|---|
| 1 | 4239 | 0.0026 | 0.0045 | 0.0036 | 0.0014 | 0.0035 | 0.0033 |
| 2 | 4239 | 0.0034 | 0.0045 | 0.0047 | 0.0031 | 0.0049 | 0.0017 |
| 3 | 4239 | 0.0039 | 0.0101 | 0.0055 | 0.0085 | 0.0059 | 0.0087 |
| 4 | 4239 | 0.0044 | 0.0068 | 0.0064 | 0.0118 | 0.0070 | 0.0057 |
| 5 | 4239 | 0.0050 | 0.0075 | 0.0074 | 0.0123 | 0.0084 | 0.0111 |
| 6 | 4239 | 0.0056 | 0.0099 | 0.0087 | 0.0101 | 0.0101 | 0.0097 |
| 7 | 4239 | 0.0063 | 0.0085 | 0.0105 | 0.0083 | 0.0125 | 0.0125 |
| 8 | 4239 | 0.0074 | 0.0120 | 0.0132 | 0.0132 | 0.0158 | 0.0097 |
| 9 | 4239 | 0.0098 | 0.0281 | 0.0187 | 0.0318 | 0.0240 | 0.0234 |
| 10 | 4238 | 0.0290 | 0.1038 | 0.0584 | 0.0953 | 0.0614 | 0.1102 |

## 6. Leave-one-repo-out (12 repos, raw scores, no calibration, macro mean)

| config | any-fail PR-AUC | any-fail ROC-AUC | genuine PR-AUC | flaky PR-AUC |
|---|---|---|---|---|
| V3 | 0.214 | 0.742 | 0.126 | 0.080 |
| V4 | 0.205 | 0.730 | 0.122 | 0.171 |

## 7. Is the ranking difference real? (paired bootstrap, 95%)

- V3 to V4, any-fail PR-AUC: +0.006 (interval -0.003 to +0.015, 830 failures).
- V3 to V4, genuine PR-AUC: +0.012 (interval +0.003 to +0.022, 589 failures).
- V3 to V4, flaky PR-AUC: +0.015 (interval -0.009 to +0.038, 241 failures).
- V3C to V4, any-fail PR-AUC: +0.021 (interval +0.013 to +0.030, 830 failures).
- V3C to V4, genuine PR-AUC: +0.013 (interval +0.005 to +0.023, 589 failures).
- V3C to V4, flaky PR-AUC: +0.008 (interval -0.012 to +0.029, 241 failures).

## 8. What each V4 model leans on (share of total gain, top 6)

- genuine-vs-rest: `rp_avg_test_job_minutes` 11.5%, `h_os_fail_rate_7d` 11.2%, `h_os_consecutive_failures` 7.2%, `h_reruns_30d` 6.4%, `rp_test_jobs_in_window` 6.2%, `cs_lines_added` 4.7%
- flaky-vs-rest: `rp_avg_test_job_minutes` 18.1%, `rs_jobs_in_run` 16.6%, `h_os_runs_7d` 7.2%, `rp_test_jobs_in_window` 4.6%, `h_os_runs_30d` 3.4%, `h_runs_7d` 3.3%

## 9. Constraints (DEFAULTs in `model/models.py`)

Genuine model: +1 on failure rate (7d/30d/90d/all), consecutive failures, last-failed (job and runner-OS history), -1 on days since last failure, +1 on every risky test-change construct including `tw_only_added`. Flip rate and re-runs are left free there because they describe flaky, not genuine, failures. Flaky model: +1 on flip rate, re-runs, failure rates, last-failed and OS-history twins, -1 on days since last failure, +1 on the timing/randomness/network/cleanup constructs; consecutive failures and `tw_only_added` are left free (a stuck job is genuine, not flaky). Both use LightGBM's 'intermediate' constraint method and balanced class weights per binary model. A test sweeps each model on its own signal and checks the score never moves the wrong way.

## 10. Honest limits

- The 'either class crosses its own threshold' alert rule (precision 0.393, recall 0.077) cannot be used as the alert as such: the RiskOutput contract says `alert == (probability >= threshold)`. V4 therefore alerts on the combined P(fail) = 1 - (1-pg)(1-pf) against one threshold picked on validation with the existing rule; the per-class thresholds are stored in `threshold.json` and `sub_risks` carries both class probabilities. The independence assumption in the combination is a DEFAULT; genuine and flaky are mutually exclusive labels, so it slightly over-counts.
- OOF scores come from models trained on earlier blocks only (a quarter to three quarters of the train period), so they are a little weaker than the final model's scores; the calibration is built on slightly under-confident inputs. Accepted for a calibration set about eight times larger.
- V3 shipped differs from the v3 report's number (PR-AUC 0.183 here vs 0.166 there) because the calibration slice is now cut 60/40 instead of 50/50 and v3's 3-class renormalising calibrator changes the any-fail ranking a little (V3C, a monotone map of the raw score, gives 0.168). The V3-to-V4 PR-AUC gain of +0.006 is inside the noise.
- 830 test failures (589 genuine, 241 flaky) and 109 validation failures: per-class thresholds and precision/recall at threshold move a lot between nearby choices.
- The validation part is the most recent slice of the calibration period, the test slice is later still. Drift in streak behaviour between periods is the dominant error source for streak rows and no calibration choice made before the test period can fix it; a periodic recalibration (retrain + recalibrate on the latest data) is the practical answer.
- Human kappa round (P4.3) is still pending; labels are rule labels.
