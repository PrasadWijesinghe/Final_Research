# Test expert: model v5 vs v4

Dataset v4 (same rows and labels as v3, plus the `ht_` group). Same time-ordered splits as v4: 148,425 train, 21,186 calibration, 42,389 test rows (830 failures: 589 genuine, 241 flaky; 1315 streak rows, 171 failing). Configs: **v4** = v4 recipe without `ht_` (model v4's own feature set, so also the ablation "v5 minus ht_"; 68 features), **v5** = same recipe with all `ht_` (83), **v5_job** = only job-level `ht_`, **v5_touched** = only the `ht_touched_*` group.

## Verdict

**Not better. Model v4 stays in service; v5 is not shipped.** Adding the per-test and per-file history group (`ht_`, 15 features) to the v4 recipe gives no reliable gain on the temporal test: any-fail PR-AUC moves from 0.189 to 0.193 (+0.004, interval -0.008 to +0.016, so it could be zero), genuine PR-AUC drops from 0.106 to 0.096 (-0.010, interval just below zero), flaky PR-AUC rises from 0.191 to 0.207 (+0.015, interval barely above zero). Ranking by ROC-AUC improves (0.807 to 0.834), calibration is a little better (Brier 0.01774 to 0.01756, ECE 0.0066 to 0.0063), and the alert threshold now flags more failures (recall 0.060 to 0.084) at a lower precision (0.532 to 0.365). Leave-one-repo-out is a wash: macro mean any-fail PR-AUC 0.205 for v4 and 0.201 for v5, with v5 ahead in 6 of 12 repos. 'Same as last outcome' still beats every model on streak rows. The labels are rule labels (the human check is pending), so all of this measures agreement with the rules.

## What was compared

All configs use the same rows, the same time-ordered splits, and the v4 recipe (two separate monotone LightGBM models, recency-weighted beta calibration, thresholds from the validation part). `v4` is the recipe without `ht_`; this is exactly model v4's feature set, so it is also the ablation 'v5 minus ht_'. Besides the full `v5` there are two sub-ablations: only the job-level `ht_` features (`v5_job`), and only the `ht_touched_*` features about the changed test files (`v5_touched`).

## What the ablations say

The job-level group helps and the touched-file group does not. `v5_job` improves any-fail PR-AUC by +0.018 (interval +0.006 to +0.027) and flaky by +0.022 (interval +0.003 to +0.040); genuine +0.010 is within noise. `v5_touched` alone changes nothing (any-fail -0.006) and hurts genuine (-0.008). Adding the touched-file features on top of the job-level ones removes the gain (v5 any-fail +0.004 against +0.018 for `v5_job`). Why the touched group is weak: on the test slice only 49% of rows change a test file and only 7% of rows have earlier results for those files, Mocha (the biggest framework) has no file names at all, and the Jest repo's own tests fail on purpose, which makes its history look alarming for every commit.

## Why v5_job is not shipped either

`v5_job` was singled out after looking at these test results, among three variants, so its gain is partly the winner's curse; shipping it on that basis would break the rule that the test slice never chooses anything. Its leave-one-repo-out check is only slightly positive (macro 0.206 against 0.205, better in 8 of 12 repos, with big swings both ways: redis/node-redis +0.084, mikro-orm -0.072). The honest next step is to decide between `v5` and `v5_job` on the validation part, or on a later time period once more runs exist, before shipping.

## Where the new history comes from

Dataset v4 has the same 212,000 rows and labels as v3 plus 15 `ht_` columns read from 17.1 million parsed test results (P4.2 full scope). On the test slice, 99.7% of rows have earlier parsed runs of their job within 90 days, so the job-level numbers are almost always available (over all 212,000 rows, 20% have no earlier parsed run of their job at all). In the flaky model the `ht_` features carry 18.5% of the importance (`ht_last_run_failed_tests` 7.2%), and in the genuine model 25.8% (`ht_failed_tests_30d` 9.6%), so the model does use them; they largely overlap what `h_*` and `h_os_*` already say about the job.

## Bundle

The v5 bundle is kept at `models/testing_candidates/v5/`, not in `models/testing/`, because the service serves the newest `v<N>` folder of `models/testing/` after a restart and would otherwise pick v5 up silently. To ship it later, move the folder to `models/testing/v5` and rebuild: `docker compose --profile experts up -d --build testing-api`.

## Numbers

### Any failure

| config | PR-AUC | ROC-AUC | Brier | ECE | threshold | precision / recall | PR-AUC on streak rows |
|---|---|---|---|---|---|---|---|
| v4 | 0.189 | 0.807 | 0.01774 | 0.0066 | 0.169 | 0.532 / 0.060 | 0.413 |
| v5 | 0.193 | 0.834 | 0.01756 | 0.0063 | 0.163 | 0.365 / 0.084 | 0.394 |
| v5_job | 0.206 | 0.825 | 0.01758 | 0.0073 | 0.172 | 0.509 / 0.071 | 0.406 |
| v5_touched | 0.183 | 0.798 | 0.01777 | 0.0065 | 0.151 | 0.425 / 0.078 | 0.350 |

### Genuine failure

| config | PR-AUC | ROC-AUC | Brier | ECE | threshold | precision / recall | PR-AUC on streak rows |
|---|---|---|---|---|---|---|---|
| v4 | 0.106 | 0.783 | 0.01321 | 0.0056 | 0.135 | 0.200 / 0.029 | 0.418 |
| v5 | 0.096 | 0.807 | 0.01313 | 0.0035 | 0.132 | 0.172 / 0.059 | 0.372 |
| v5_job | 0.116 | 0.793 | 0.01308 | 0.0058 | 0.144 | 0.200 / 0.031 | 0.394 |
| v5_touched | 0.098 | 0.775 | 0.01324 | 0.0052 | 0.145 | 0.188 / 0.025 | 0.333 |

### Flaky failure

| config | PR-AUC | ROC-AUC | Brier | ECE | threshold | precision / recall | PR-AUC on streak rows |
|---|---|---|---|---|---|---|---|
| v4 | 0.191 | 0.859 | 0.00512 | 0.0017 | 0.078 | 0.342 / 0.158 | 0.547 |
| v5 | 0.207 | 0.881 | 0.00506 | 0.0020 | 0.067 | 0.476 / 0.162 | 0.619 |
| v5_job | 0.213 | 0.875 | 0.00506 | 0.0019 | 0.066 | 0.311 / 0.174 | 0.456 |
| v5_touched | 0.190 | 0.853 | 0.00514 | 0.0018 | 0.079 | 0.326 / 0.187 | 0.538 |

### Paired bootstrap, PR-AUC difference against v4 (1,000 resamples, 95% interval)

| config, event | difference | interval | failures |
|---|---|---|---|
| v5, any-fail | +0.0038 | -0.0083 to +0.0155 | 830 |
| v5, genuine | -0.0099 | -0.0211 to -0.0000 | 589 |
| v5, flaky | +0.0152 | +0.0006 to +0.0348 | 241 |
| v5_job, any-fail | +0.0175 | +0.0056 to +0.0268 | 830 |
| v5_job, genuine | +0.0102 | -0.0012 to +0.0213 | 589 |
| v5_job, flaky | +0.0220 | +0.0028 to +0.0401 | 241 |
| v5_touched, any-fail | -0.0061 | -0.0135 to +0.0012 | 830 |
| v5_touched, genuine | -0.0075 | -0.0160 to -0.0008 | 589 |
| v5_touched, flaky | -0.0014 | -0.0166 to +0.0136 | 241 |

### Rows with and without readable history

42,283 of 42,389 test rows have at least one earlier parsed run of their job in the last 90 days (830 failures). Any-fail PR-AUC on them: v4 0.189, v5 0.193. On the rest: v4 n/a, v5 n/a.

### Streak rows (3 or more failures in a row just before)

| config | mean P(fail) | observed | PR-AUC | precision at threshold | recall at threshold |
|---|---|---|---|---|---|
| v4 | 0.033 | 0.130 | 0.413 | 0.583 | 0.041 |
| v5 | 0.041 | 0.130 | 0.394 | 0.517 | 0.088 |
| v5_job | 0.042 | 0.130 | 0.406 | 0.545 | 0.070 |
| v5_touched | 0.035 | 0.130 | 0.350 | 0.538 | 0.041 |
| same as last outcome | - | 0.130 | - | 0.564 | 0.643 |

### Leave one repo out (PR-AUC, any-fail / genuine / flaky)

| held-out repo | v4 | v5 | v5_job | any-fail diff v5 | any-fail diff v5_job |
|---|---|---|---|---|---|
| Automattic/mongoose | 0.10 / 0.09 / 0.01 | 0.10 / 0.10 / 0.01 | 0.11 / 0.10 / 0.01 | +0.004 | +0.012 |
| TanStack/query | 0.58 / 0.29 / 0.15 | 0.60 / 0.34 / 0.16 | 0.57 / 0.29 / 0.18 | +0.018 | -0.006 |
| babel/babel | 0.07 / 0.07 / 0.00 | 0.06 / 0.07 / 0.00 | 0.07 / 0.08 / 0.00 | -0.002 | +0.006 |
| eslint/eslint | 0.43 / 0.18 / 0.31 | 0.43 / 0.17 / 0.28 | 0.41 / 0.17 / 0.26 | +0.002 | -0.022 |
| jestjs/jest | 0.03 / 0.03 / 0.00 | 0.03 / 0.03 / 0.00 | 0.03 / 0.03 / 0.00 | -0.000 | +0.002 |
| mikro-orm/mikro-orm | 0.27 / 0.15 / 0.00 | 0.18 / 0.07 / 0.01 | 0.20 / 0.08 / 0.01 | -0.091 | -0.072 |
| puppeteer/puppeteer | 0.29 / 0.21 / 0.09 | 0.24 / 0.21 / 0.10 | 0.25 / 0.20 / 0.12 | -0.048 | -0.043 |
| redis/node-redis | 0.38 / 0.24 / 0.34 | 0.36 / 0.13 / 0.23 | 0.47 / 0.27 / 0.45 | -0.023 | +0.084 |
| sequelize/sequelize | 0.01 / 0.01 / 0.00 | 0.02 / 0.02 / 0.00 | 0.01 / 0.01 / 0.00 | +0.017 | +0.003 |
| typeorm/typeorm | 0.03 / 0.02 / 1.00 | 0.05 / 0.01 / 1.00 | 0.03 / 0.02 / 1.00 | +0.028 | +0.005 |
| vitejs/vite | 0.23 / 0.13 / 0.14 | 0.22 / 0.11 / 0.18 | 0.23 / 0.12 / 0.18 | -0.006 | +0.007 |
| vitest-dev/vitest | 0.06 / 0.06 / 0.00 | 0.12 / 0.11 / 0.01 | 0.10 / 0.10 / 0.00 | +0.055 | +0.033 |

Macro mean any-fail PR-AUC: v4 0.205, v5 0.201, v5_job 0.206; v5 better in 6 of 12 repos, v5_job in 8.

### Importance (share of gain)

- gen model, `ht_` share 25.8%; top: `ht_failed_tests_30d` 9.6%, `h_os_fail_rate_7d` 9.0%, `h_os_consecutive_failures` 4.6%, `cs_lines_added` 4.5%, `h_weighted_flip_rate` 4.3%, `rp_test_jobs_in_window` 3.9%, `rp_avg_test_job_minutes` 3.9%, `ht_parsed_runs_30d` 3.9%.
- flaky model, `ht_` share 18.5%; top: `rs_jobs_in_run` 16.2%, `rp_avg_test_job_minutes` 13.0%, `ht_last_run_failed_tests` 7.2%, `h_os_runs_7d` 5.0%, `rp_test_jobs_in_window` 3.8%, `h_os_runs_30d` 3.5%, `rs_attempt` 2.7%, `h_runs_7d` 2.3%.
