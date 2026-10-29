# Test expert: model v6 vs v4

Dataset v4 (212,000 rows). The same time-ordered splits as v5: 148,425 train, 21,186 calibration, 42,389 test rows (830 failures: 589 genuine, 241 flaky; the calibration slice has 228 failures). **v4** is the model in service. **v6** is the v4 recipe plus (1) the "still failing" rule, (2) a threshold chosen on the calibration slice only, (3) the job-level `ht_` features only if they win without the test slice (here: not included).

## Verdict

**v6 is not shipped; model v4 stays in service.** The ship rule needed v6 to beat v4 on the test slice on any-fail PR-AUC, or on F1 at its own threshold, without worse calibration. v6 does neither. Any-fail PR-AUC goes from 0.189 to 0.170 (-0.019, interval -0.027 to -0.012, a real loss). F1 at the chosen threshold is 0.108 for both, because the threshold picked on the calibration slice (0.1692) is practically the one v4 already uses (0.1691), and no "still failing" rule value reaches it. Calibration does improve a little (ECE 0.0066 to 0.0054, interval of the difference -0.0025 to -0.0005; Brier 0.01774 to 0.01767), but that does not satisfy the rule on its own. The v6 bundle is in `models/testing_candidates/v6/`; nothing was added to `models/testing/`. The job-level `ht_` features were left out because they lose on the calibration slice and on the train-period out-of-fold scores. The labels are rule labels (the human check is pending), so every number is agreement with the rules.

## What was built

1. **The "still failing" rule** (`model/rule.py`). When the job's previous run failed (same runner OS when the job has an earlier run on that OS, else the job's overall history), the final probability is the larger of the model's probability and the rule's probability. The rule's probability is the plain fail rate of earlier rows with the same number of failures in a row, in four buckets (1, 2, 3 to 9, 10 or more), counted on train + calibration rows only. The same code is used for training, the report and serving. In serving it adds a first reason ("This job already failed N times in a row on <OS>; the failure is likely not caused by this push.") and a `still_failing` entry in `sub_risks`; the contract is unchanged.
2. **The threshold** is the maximum-F1 point on the whole calibration slice (21,186 rows, 228 failures). The test slice never chose anything. The other operating points and the whole curve are below.
3. **The job-level `ht_` group** (9 features) was re-checked on the calibration slice and on the out-of-fold scores of the train period, not on the test slice. Rule fixed in advance: include only if any-fail PR-AUC is higher on both.

## Why the rule does not help here

The rule's probabilities are low: a job whose previous run failed fails again only 4.6% (1 in a row), 10.5% (2), 7.6% (3 to 9) and 10.9% (10 or more) of the time in the train + calibration rows, and 11.8% on the test slice. These are below the alert threshold (0.169), so the rule cannot raise an alert by itself at the default threshold, and where it applies it only replaces the model's low scores with one flat number. That flattening is why PR-AUC falls: on the 2,559 test rows where the rule applies, PR-AUC drops from 0.306 to 0.207 because the model's ordering inside that group is lost. Using a lower threshold (the "top 5% of pushes" point, 0.0769) does let the rule fire for the 2 and 10+ buckets.

Why the "same as last outcome" baseline looks so much better (precision 0.56, recall 0.64 on streak rows): that baseline reads the *label* of the previous run (a confirmed test failure, labelled genuine or flaky). That label is only known after the fact (for example, the rule that a fix touched the failing test file within 7 days looks at later commits), and it excludes the raw failures that were build errors, unlabelled or not test failures. The history available at prediction time is only "did the previous run fail", and that is a weak signal: on the streak rows it flags 98.8% of them and is right 13.0% of the time (the baseline row "previous run failed (raw history)" in the tables). So the strong baseline is not reproducible from deployable inputs. Its number is also dominated by one repository (mikro-orm: 55 of its 55 such rows failed in the test slice). The bucket rates also drift a lot between periods (bucket 3 to 9: 7.5% in train, 8.2% in calibration, 19.1% in test; bucket 10+: 13.4%, 0%, 10.9%), so no rate learned before the test period can match it.

## Does v6 win only because of the rule

v6 does not win. The model inside v6 is the v4 model unchanged (same columns, same fits), so everything that moves is the rule. The rule makes calibration a little better, ranking worse on the temporal test, and changes no alert at the default threshold. On leave-one-repo-out (the rule fitted on the other repos) the picture is mixed in the other direction: macro mean any-fail PR-AUC 0.198 for the v4 model alone and 0.208 with the rule, ahead in 7 of 12 repos (mikro-orm +0.061, eslint +0.019, node-redis +0.027; jest -0.008, typeorm -0.009), so the rule has some value across repositories but not across time on this test slice.

## Non-streak rows (previous run passed): the model's own value on new failures

39,830 test rows have a previous run that passed or no history; 529 of them fail (1.3%). The rule is off there, so v4 and v6 are identical: PR-AUC 0.133 (10 times the base rate), and at the threshold 0.1692 or 0.1691 precision 0.721 and recall 0.059 with 0.11% of rows alerted. That is the model's value on new failures: the few alerts it makes there are mostly right, but it finds only about 6% of those failures. The model is not useful at its default threshold for a first-time failure either; at a looser threshold the picture is in the curve below.

## Threshold

Default: **the maximum-F1 point on the calibration slice, threshold 0.1692** (calibration precision 0.317, recall 0.175, F1 0.223; on test 0.532 / 0.060, F1 0.108, 0.22% of pushes alerted). It is practically the threshold v4 already has because v4's own validation-based rule ends at about the same score. The two other operating points, also chosen on calibration: **precision >= 0.30 with the most recall** is 0.1672 (almost the same point: test precision 0.535, recall 0.064), and **top 5% of pushes** is 0.0769 (calibration alert rate 5.0%; test precision 0.157, recall 0.306, F1 0.207, 3.8% alerted). On the test slice the top-5% point has about twice the F1 of the default, but it cannot be chosen for that reason because the test slice must not choose; it is a product decision (how many alerts per 100 pushes the team will tolerate). The calibration slice has only 228 failures and a trough of long streaks, so its curve is noisy: the F1 peak rests on about 40 flagged failures.

## Job-level ht_ features: decision and serving latency

**Decision: excluded.** Any-fail PR-AUC with the 9 job-level features against without them: calibration slice 0.154 against 0.163 (-0.009, interval -0.021 to +0.003), out-of-fold train blocks 0.147 against 0.152 (-0.005, better in 3 of 6 blocks). Both are negative, so by the rule fixed beforehand they do not go in. (The earlier v5 report found a gain for the same set on the test slice; on slices that did not choose it, it is not there. That is the winner's curse the v5 report warned about.)

**Latency.** Measured on this machine with the real database, one test job (`build_features`, all history loaders, median of 3):

| repository / job | before: full `ht_` loader (what serving did for every bundle, even v4) | after: v4-style bundle, `ht_` loading skipped | after: job-level fast loader (only if a bundle used job-level `ht_`) |
|---|---|---|---|
| vitejs/vite, `test` | 4.1 s | 0.12 s | 1.8 s |
| redis/node-redis, `tests` | 4.2 s | 0.06 s | 1.4 s |
| vitest-dev/vitest, `test` | 0.34 s | 0.19 s | 0.8 s |
| eslint/eslint, `test_on_node` | 0.16 s | 0.09 s | 1.4 s |

The budget for a pre-push prediction is `PREDICT_BUDGET_PRE_PUSH_MS` = 10,000 ms. Two changes: (1) serving now skips the `ht_` query when the bundle has no `ht_` column (v4 and v6 have none), which removes up to 4 s from every request; (2) `loader.load_job_test_history` computes only the job-level numbers with three small queries (run list, failed rows of the last 90 days plus the last two runs, newest pass of each failing test) instead of streaming every stored row. It returned identical numbers to the full loader in 24 of 24 sampled series and cutoffs (12 big series, two cutoffs each) and in a database test. So the group would fit the budget (worst case measured 1.8 s, one outlier of 6 s while a training job used the CPUs), but it is excluded for accuracy, not for time.

## Numbers

### Any failure (test slice)

| pipeline | PR-AUC | ROC-AUC | Brier | ECE | threshold | precision | recall | F1 | alert rate |
|---|---|---|---|---|---|---|---|---|---|
| v4 as served (threshold from validation, no rule) | 0.189 | 0.807 | 0.01774 | 0.0066 | 0.1691 | 0.532 | 0.060 | 0.108 | 0.0022 |
| v4 model, threshold = max F1 on calibration | 0.189 | 0.807 | 0.01774 | 0.0066 | 0.1692 | 0.532 | 0.060 | 0.108 | 0.0022 |
| v4 model + rule, threshold = max F1 on calibration | 0.170 | 0.807 | 0.01767 | 0.0054 | 0.1692 | 0.532 | 0.060 | 0.108 | 0.0022 |
| v6 = model + rule, threshold = max F1 on calibration | 0.170 | 0.807 | 0.01767 | 0.0054 | 0.1692 | 0.532 | 0.060 | 0.108 | 0.0022 |
| rule alone (no model) | 0.058 | 0.655 | 0.01884 | 0.0150 | 0.0462 | 0.118 | 0.363 | 0.178 | 0.0604 |

### Genuine failure (test slice, probability scores)

| pipeline | PR-AUC | ROC-AUC | Brier | ECE |
|---|---|---|---|---|
| v4 as served (threshold from validation, no rule) | 0.106 | 0.783 | 0.01321 | 0.0056 |
| v4 model, threshold = max F1 on calibration | 0.106 | 0.783 | 0.01321 | 0.0056 |
| v4 model + rule, threshold = max F1 on calibration | 0.086 | 0.784 | 0.01313 | 0.0049 |
| v6 = model + rule, threshold = max F1 on calibration | 0.086 | 0.784 | 0.01313 | 0.0049 |

### Flaky failure (test slice, probability scores)

| pipeline | PR-AUC | ROC-AUC | Brier | ECE |
|---|---|---|---|---|
| v4 as served (threshold from validation, no rule) | 0.191 | 0.859 | 0.00512 | 0.0017 |
| v4 model, threshold = max F1 on calibration | 0.191 | 0.859 | 0.00512 | 0.0017 |
| v4 model + rule, threshold = max F1 on calibration | 0.191 | 0.857 | 0.00512 | 0.0016 |
| v6 = model + rule, threshold = max F1 on calibration | 0.191 | 0.857 | 0.00512 | 0.0016 |

### By kind of row (test slice; precision / recall / alert rate at each pipeline's own threshold)

**all rows**: 42,389 rows, 830 failures (2.0%).

| pipeline | PR-AUC | precision / recall | F1 | alert rate |
|---|---|---|---|---|
| v4 as served (threshold from validation, no rule) | 0.189 | 0.532 / 0.060 | 0.108 | 0.0022 |
| v4 model, threshold = max F1 on calibration | 0.189 | 0.532 / 0.060 | 0.108 | 0.0022 |
| v4 model + rule, threshold = max F1 on calibration | 0.170 | 0.532 / 0.060 | 0.108 | 0.0022 |
| v6 = model + rule, threshold = max F1 on calibration | 0.170 | 0.532 / 0.060 | 0.108 | 0.0022 |
| rule alone (no model) | 0.058 | 0.118 / 0.363 | 0.178 | 0.0604 |
| baseline: same as last labelled outcome | - | 0.211 / 0.282 | 0.241 | 0.0262 |
| baseline: previous run failed (raw history) | - | 0.118 / 0.363 | 0.178 | 0.0604 |

**streak rows (3+ failures in a row)**: 1,315 rows, 171 failures (13.0%).

| pipeline | PR-AUC | precision / recall | F1 | alert rate |
|---|---|---|---|---|
| v4 as served (threshold from validation, no rule) | 0.413 | 0.583 / 0.041 | 0.077 | 0.0091 |
| v4 model, threshold = max F1 on calibration | 0.413 | 0.583 / 0.041 | 0.077 | 0.0091 |
| v4 model + rule, threshold = max F1 on calibration | 0.193 | 0.583 / 0.041 | 0.077 | 0.0091 |
| v6 = model + rule, threshold = max F1 on calibration | 0.193 | 0.583 / 0.041 | 0.077 | 0.0091 |
| rule alone (no model) | 0.119 | 0.130 / 0.988 | 0.230 | 0.9878 |
| baseline: same as last labelled outcome | - | 0.564 / 0.643 | 0.601 | 0.1483 |
| baseline: previous run failed (raw history) | - | 0.130 / 0.988 | 0.230 | 0.9878 |

**previous run failed (rule applies)**: 2,559 rows, 301 failures (11.8%).

| pipeline | PR-AUC | precision / recall | F1 | alert rate |
|---|---|---|---|---|
| v4 as served (threshold from validation, no rule) | 0.306 | 0.373 / 0.063 | 0.108 | 0.0199 |
| v4 model, threshold = max F1 on calibration | 0.306 | 0.373 / 0.063 | 0.108 | 0.0199 |
| v4 model + rule, threshold = max F1 on calibration | 0.207 | 0.373 / 0.063 | 0.108 | 0.0199 |
| v6 = model + rule, threshold = max F1 on calibration | 0.207 | 0.373 / 0.063 | 0.108 | 0.0199 |
| rule alone (no model) | 0.125 | 0.118 / 1.000 | 0.210 | 1.0000 |
| baseline: same as last labelled outcome | - | 0.422 / 0.558 | 0.481 | 0.1555 |
| baseline: previous run failed (raw history) | - | 0.118 / 1.000 | 0.210 | 1.0000 |

**non-streak rows (previous run passed)**: 39,830 rows, 529 failures (1.3%).

| pipeline | PR-AUC | precision / recall | F1 | alert rate |
|---|---|---|---|---|
| v4 as served (threshold from validation, no rule) | 0.133 | 0.721 / 0.059 | 0.108 | 0.0011 |
| v4 model, threshold = max F1 on calibration | 0.133 | 0.721 / 0.059 | 0.108 | 0.0011 |
| v4 model + rule, threshold = max F1 on calibration | 0.133 | 0.721 / 0.059 | 0.108 | 0.0011 |
| v6 = model + rule, threshold = max F1 on calibration | 0.133 | 0.721 / 0.059 | 0.108 | 0.0011 |
| rule alone (no model) | 0.013 | n/a / 0.000 | 0.000 | 0.0000 |
| baseline: same as last labelled outcome | - | 0.093 / 0.125 | 0.106 | 0.0179 |
| baseline: previous run failed (raw history) | - | n/a / 0.000 | 0.000 | 0.0000 |

### Precision / recall curve of v6 (final score), threshold by threshold

Calibration columns are what the threshold choice saw; test columns are for reading only.

| threshold | cal precision | cal recall | cal F1 | cal alert rate | test precision | test recall | test F1 | test alert rate |
|---|---|---|---|---|---|---|---|---|
| 0.0300 | 0.046 | 0.614 | 0.085 | 0.1441 | 0.089 | 0.637 | 0.156 | 0.1403 |
| 0.0462 | 0.060 | 0.500 | 0.108 | 0.0890 | 0.129 | 0.517 | 0.207 | 0.0784 |
| 0.0500 | 0.065 | 0.491 | 0.115 | 0.0809 | 0.135 | 0.495 | 0.212 | 0.0717 |
| 0.0700 | 0.073 | 0.417 | 0.124 | 0.0616 | 0.153 | 0.408 | 0.223 | 0.0521 |
| 0.0758 | 0.074 | 0.346 | 0.122 | 0.0502 | 0.159 | 0.313 | 0.211 | 0.0386 |
| 0.0900 | 0.079 | 0.320 | 0.127 | 0.0435 | 0.156 | 0.266 | 0.197 | 0.0335 |
| 0.1053 | 0.081 | 0.281 | 0.125 | 0.0375 | 0.156 | 0.235 | 0.188 | 0.0294 |
| 0.1093 | 0.163 | 0.241 | 0.194 | 0.0160 | 0.330 | 0.112 | 0.167 | 0.0067 |
| 0.1100 | 0.163 | 0.241 | 0.195 | 0.0159 | 0.335 | 0.111 | 0.167 | 0.0065 |
| 0.1300 | 0.200 | 0.202 | 0.201 | 0.0109 | 0.384 | 0.086 | 0.140 | 0.0044 |
| 0.1500 | 0.241 | 0.197 | 0.217 | 0.0088 | 0.484 | 0.072 | 0.126 | 0.0029 |
| 0.1700 | 0.320 | 0.171 | 0.223 | 0.0058 | 0.538 | 0.060 | 0.108 | 0.0022 |
| 0.2000 | 0.369 | 0.136 | 0.199 | 0.0040 | 0.780 | 0.055 | 0.103 | 0.0014 |
| 0.2500 | 0.442 | 0.101 | 0.164 | 0.0025 | 0.909 | 0.048 | 0.092 | 0.0010 |
| 0.3000 | 0.684 | 0.057 | 0.105 | 0.0009 | 1.000 | 0.048 | 0.092 | 0.0009 |
| 0.4000 | 1.000 | 0.048 | 0.092 | 0.0005 | 1.000 | 0.035 | 0.068 | 0.0007 |
| 0.5000 | 1.000 | 0.048 | 0.092 | 0.0005 | 1.000 | 0.025 | 0.049 | 0.0005 |

### Operating points of v6 (all chosen on the calibration slice)

| operating point | threshold | calibration precision / recall | cal alert rate | test precision / recall | test F1 | test alert rate |
|---|---|---|---|---|---|---|
| max F1 (the default) | 0.1692 | 0.317 / 0.175 | 0.0059 | 0.532 / 0.060 | 0.108 | 0.0022 |
| precision >= 0.30, most recall | 0.1672 | 0.301 / 0.175 | 0.0063 | 0.535 / 0.064 | 0.114 | 0.0023 |
| top 5% of pushes | 0.0769 | 0.075 / 0.346 | 0.0500 | 0.157 / 0.306 | 0.207 | 0.0382 |

### The rule (learned on train + calibration only)

| failures in a row | train+cal rows | rule P(any) | rule P(genuine) | rule P(flaky) | train rows / rate | cal rows / rate | test rows / rate (not used) |
|---|---|---|---|---|---|---|---|
| 1 | 5,782 | 0.046 | 0.036 | 0.010 | 5,309 / 0.045 | 473 / 0.061 | 1,098 / 0.080 |
| 2 | 1,339 | 0.105 | 0.090 | 0.015 | 1,214 / 0.097 | 125 / 0.184 | 222 / 0.162 |
| 3-9 | 2,982 | 0.076 | 0.069 | 0.007 | 2,725 / 0.075 | 257 / 0.082 | 507 / 0.191 |
| 10+ | 1,940 | 0.109 | 0.098 | 0.011 | 1,577 / 0.134 | 363 / 0.000 | 732 / 0.109 |

### Job-level ht_ decision (no test slice)

| where | v4 columns | v4 + job-level ht_ | difference | note |
|---|---|---|---|---|
| calibration slice, any-fail PR-AUC | 0.163 | 0.154 | -0.0091 | 95% interval -0.0214 to +0.0027, 228 failures |
| out-of-fold train blocks, pooled PR-AUC | 0.152 | 0.147 | -0.0049 | 111,319 rows, 1410 failures; ht_ better in 3 of 6 blocks |
| calibration slice, Brier | 0.00984 | 0.00995 | +0.00011 |  |
| calibration slice, ECE | 0.0083 | 0.0089 | +0.0006 |  |

Per OOF block (any-fail PR-AUC, v4 / with ht_): 0.167 / 0.177 (275 failures); 0.127 / 0.105 (322 failures); 0.155 / 0.117 (287 failures); 0.301 / 0.322 (127 failures); 0.517 / 0.336 (169 failures); 0.073 / 0.085 (230 failures).

### Paired bootstrap, v6 minus v4 as served (test slice, 1,000 resamples, 95% interval)

| measure | difference | interval |
|---|---|---|
| any-fail PR-AUC | -0.0191 | -0.0271 to -0.0119 |
| genuine PR-AUC | -0.0201 | -0.0299 to -0.0119 |
| flaky PR-AUC | -0.0004 | -0.0013 to +0.0005 |
| any-fail ECE (lower is better) | -0.0012 | -0.0025 to -0.0005 |
| any-fail Brier (lower is better) | -0.0001 | -0.0001 to -0.0000 |
| F1 at each one's own threshold | +0.0000 | +0.0000 to +0.0000 |
| any-fail PR-AUC on non-streak rows only | +0.0000 | +0.0000 to +0.0000 |
| any-fail PR-AUC, v6 minus v4 model alone (what the rule adds) | -0.0191 | -0.0271 to -0.0119 |

### Leave one repo out (any-fail PR-AUC on the held-out repo)

| held-out repo | rows / fails | v4 model | v6 model | v4 + rule | v6 + rule | v6 + rule minus v4 |
|---|---|---|---|---|---|---|
| Automattic/mongoose | 16,870 / 487 | 0.098 | 0.098 | 0.097 | 0.097 | -0.001 |
| TanStack/query | 1,503 / 29 | 0.576 | 0.576 | 0.576 | 0.576 | -0.000 |
| babel/babel | 6,012 / 94 | 0.067 | 0.067 | 0.077 | 0.077 | +0.011 |
| eslint/eslint | 4,037 / 47 | 0.455 | 0.455 | 0.474 | 0.474 | +0.019 |
| jestjs/jest | 49,418 / 302 | 0.029 | 0.029 | 0.021 | 0.021 | -0.008 |
| mikro-orm/mikro-orm | 16,796 / 118 | 0.187 | 0.187 | 0.248 | 0.248 | +0.061 |
| puppeteer/puppeteer | 27,190 / 550 | 0.272 | 0.272 | 0.286 | 0.286 | +0.014 |
| redis/node-redis | 2,278 / 106 | 0.362 | 0.362 | 0.390 | 0.390 | +0.027 |
| sequelize/sequelize | 36,788 / 68 | 0.005 | 0.005 | 0.003 | 0.003 | -0.002 |
| typeorm/typeorm | 5,976 / 55 | 0.023 | 0.023 | 0.013 | 0.013 | -0.009 |
| vitejs/vite | 9,848 / 982 | 0.244 | 0.244 | 0.246 | 0.246 | +0.001 |
| vitest-dev/vitest | 28,149 / 266 | 0.059 | 0.059 | 0.060 | 0.060 | +0.000 |

Macro mean: v4 model 0.198, v6 model 0.198, v4 + rule 0.208, v6 + rule 0.208; v6 + rule ahead of v4 in 7 of 12 repos.
