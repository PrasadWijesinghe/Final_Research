# Knowledge base v1 (P4.10)

Plain-language report on the first version of the Test expert's knowledge base: what is in it,
how cases were found and tagged, and a small check of whether retrieval finds useful neighbours.
Numbers come from `experts/testing/reports/kb_v1_metrics.json` (`python -m testing_expert.kb eval`,
400 queries, seed 0) and from the database as built on 2026-10-29.

## 1. What was built

A case is one past test failure that we know how it ended: either a later commit fixed it and the
job then passed (a **genuine** case), or the same commit passed when re-run (a **flaky** case).
Each case keeps the failure's signature, a rule-based root-cause and fix tag set, a short
extractive fix summary, a bounded excerpt of the fix diff, the repo licence and links. They are
stored as `VerifiedCase(origin="mined")` in `testing.kb_cases` and searched with full-text plus
vector search merged by reciprocal rank fusion (RRF). No LLM was used anywhere in this step.

## 2. Cases mined

Source: the rule labels of P4.3 (`fix_commit_touches_file`, `persistent_failure_streak`,
`rerun_pass_same_sha`). 1,233 cases in total, all embedded.

| Kind | Rule | Cases |
|---|---|---|
| genuine | `fix_commit_touches_file` (a commit within 7 days touched the failing test file, then the job passed) | 565 |
| genuine | `stack_file_fix` (a streak-labelled failure; first commit within 7 days touching a stack or test file, then a pass) | 37 |
| flaky | `rerun_pass_same_sha` (same commit passed on another attempt) | 631 |

How many candidates fell away: 1,152 were duplicates (the same failure in several matrix legs of
one commit gives one case), 264 streak failures had no stack-file fix, 123 logs had no parsable
Jest, Mocha or Vitest failure.

| Repo | genuine | flaky |
|---|---|---|
| vitejs/vite | 143 | 311 |
| puppeteer/puppeteer | 120 | 168 |
| vitest-dev/vitest | 105 | 38 |
| jestjs/jest | 66 | 4 |
| redis/node-redis | 45 | 22 |
| Automattic/mongoose | 27 | 36 |
| babel/babel | 25 | 6 |
| sequelize/sequelize | 16 | 8 |
| TanStack/query | 15 | 5 |
| mikro-orm/mikro-orm | 14 | 15 |
| eslint/eslint | 7 | 5 |
| typeorm/typeorm | 7 | 1 |
| typescript-eslint/typescript-eslint | 7 | 0 |
| svelte | 3 | 10 |
| node-postgres | 2 | 1 |
| socket.io | 0 | 1 |

Licences: MIT 945 cases, Apache-2.0 288 cases. Every repo is permissive, so the licence filter
removes nothing today; it is a switch for when a non-permissive repo is added (tested on seeded
data with a GPL case).

Cases span 2026-07-27 to 2026-10-25 (by the time each became known).

## 3. Tags

Four tag families live in one array: `kind:` (genuine or flaky), `fam:` (error family from
`triage/evidence.py`), `rc:` (root-cause category from the flaky-test literature) and `fix:`
(FlakyFix categories that make sense for JavaScript, a few common JavaScript fixes, and the scope
of the change). Rules are regexes in `config/test_kb.yaml`, documented in `kb/tags.py`.

Root-cause tags (`rc:`), cases that carry each:

| tag | genuine | flaky |
|---|---|---|
| unspecified (no rule fired) | 382 | 371 |
| timeout | 95 | 210 |
| resource_leak | 68 | 41 |
| concurrency | 38 | 11 |
| platform | 32 | 12 |
| randomness | 26 | 0 |
| async_wait | 19 | 5 |
| network | 8 | 13 |
| time_date | 7 | 1 |
| test_order_dependency | 7 | 0 |

Fix tags (`fix:`), genuine cases: misc 358, test_only 348, source_change 249,
update_expected_value 113, change_condition 76, dependency_update 48, change_assertion 42,
handle_exception 38, close_resource 26, update_snapshot 17, mock_dependency 14, skip_test 14,
add_wait 13, handle_timeout 12, reset_variable 7, reorder_data 4, dynamic_port 1. Every flaky
case has `fix:rerun`.

**How much to trust them.** These are weak labels from patterns, not human judgement.
- About 62% of genuine and 59% of flaky cases get `rc:unspecified`: the text did not match any
  rule. Honest, but it means most cases carry no root-cause category.
- Reading the diff, only small diffs (80 changed lines or fewer in the failing test and stack
  files) are scanned for fix patterns. A bigger diff is usually a new test, where `await` or
  `.close()` appear by chance. Those cases get only the scope tags.
- A manual look at 10 random cases with fix tags showed a loose fit: the tags notice a visible
  pattern, but the category is often debatable (for example, a `setTimeout` wrapper around
  `res.end()` was tagged `close_resource`, and a Vitest case was tagged `platform` because the
  word "windows" appears in a test name). No second person has labelled them, so there is no
  kappa.
- FlakyFix was built on Java. Only the categories that make sense in JavaScript are mapped.

## 4. Embeddings

Model: `ibm-granite/granite-embedding-small-english-r2`, 384 dimensions, run locally with
sentence-transformers on the Mac's GPU (MPS). It is about 100 MB and is cached by Hugging Face
(set `EMBEDDING_CACHE_DIR` to move the cache). The download worked, so nothing was substituted.

| Step | Time |
|---|---|
| Mining all cases (reading about 2,800 candidate logs, 6 processes) | 53 to 92 s |
| Embedding 1,233 cases, batches of 32 | 57.6 s (about 21 cases/s) |
| Loading the model in a fresh process (weights cached) | about 14 s |
| Embedding one query | about 14 ms |

The embedded text is the failure part (family, test names, normalised error and top project
frames) plus the root cause, fix summary and tags, cut at 1,500 characters. The embedding step
commits after every batch and only embeds rows without a vector, so a stopped run resumes. A
second `kb build` re-mined everything, kept all 1,233 embeddings and embedded 0 rows.

## 5. Retrieval check

Question: for a genuine case, if we ask the knowledge base with only the failure text (as a live
failure would), as of the time that failure happened, does the top 5 hold something relevant?
The case itself, anything from the same run or commit, and anything not yet known at that time
are excluded by the same filters the live path uses. 400 genuine cases were sampled with a fixed
seed (those with at least 5 eligible cases).

Relevance is judged four ways because no single one is right. `hit@5` is the share of queries
where at least one of the top 5 is relevant; `p@5` is the average share of the top 5 that are
relevant. The random row is what picking 5 eligible cases blindly would give. Queries where no
eligible case could be relevant are skipped, so `n` differs by column.

| hit@5 / p@5 | same repo (n=391) | same error family (n=394) | same failing test (n=152) | same fix tag* (n=141) |
|---|---|---|---|---|
| full text only | 0.71 / 0.57 | 0.94 / 0.78 | 0.95 / 0.71 | 0.43 / 0.17 |
| vector only | 0.82 / 0.69 | 0.92 / 0.80 | 0.96 / 0.73 | 0.29 / 0.10 |
| hybrid (RRF) | 0.82 / 0.66 | 0.96 / 0.84 | 0.93 / 0.73 | 0.40 / 0.13 |
| random | 0.49 / 0.17 | 0.63 / 0.23 | 0.14 / 0.03 | 0.32 / 0.08 |

*A fix tag that says something: not `misc`, `test_only`, `source_change` or `dependency_update`.

What this says, carefully:
- **Same failing test is found very well** (0.93 to 0.96 against 0.14 for random), by all three
  methods. This is the easy part: the query contains the test name, and a test that failed
  before tends to fail again. It does not prove that the past fix helps.
- **Same error family** is also high, but the query text states its family, so this is close to
  by construction.
- **Same repo**: vector and hybrid beat full text (0.82 against 0.71), so the embedding picks up
  repo style (names, paths).
- **Same fix category is the weak spot.** Full text reaches 0.43 against 0.32 for random. Vector
  only (0.29) is below random, and hybrid (0.40) is in between. The failure text alone barely
  predicts the kind of fix. This is the case that matters for suggesting a fix, so the real
  usefulness of the retrieved fixes is not shown by this check; it needs a human look (P5).
- **Hybrid is not a clear winner.** It is best on error family and mid-table elsewhere. With
  about 150 to 400 queries the sampling error is a few points, so differences under about 5
  points should not be read as real.
- Many near-duplicates: top results often come from the same fix commit or the same test failing
  in several matrix legs (see the example). There is no diversity step.

Limits of the check: only genuine cases are queries; the pool is 1,233 cases from 16 repos and 3
months; one sample, one seed; the case text contains the fix and the tags while the query does
not, which helps text overlap with cases from the same fix.

## 6. Example

Query from a real failing job (redis/node-redis, an assertion failure in
"multi-db topologies > sentinel members > auto-fallback returns to a recovered sentinel
deployment") sent through `diagnose()` with the real model and database. Triage said `genuine`
(confidence 0.99). Retrieved, with similarity (fused score divided by its best possible value):

| # | sim | case (all genuine, redis/node-redis) | fix commit |
|---|---|---|---|
| 1 | 1.00 | multi-db topologies > cluster members > f... (job `tests (20, 8.4.0, 8.4)`) | 49bd923 |
| 2 | 0.98 | same test, job `tests (20, 8.2, 8.2)` | 49bd923 |
| 3 | 0.97 | same test, job `tests (20, 8.8.0, 8.8)` | 49bd923 |
| 4 | 0.95 | same test, job `tests (20, 8.4.0, 8.4)` | 49bd923 |
| 5 | 0.94 | same test, job `tests (20, 8.2, 8.2)` | 49bd923 |

Relevant, and also a good picture of the duplicate problem: five cases, one fix.

## 7. Contract gaps (proposals only; contracts were not touched)

1. `VerifiedCase.origin` is `live` or `mined`. Flaky cases (fix = a re-run) have no honest value;
   they are marked with the tag `kind:flaky` and `fix_commit_sha` is the commit that was re-run.
   Proposal: a `case_kind` field (`fix` or `rerun`) or an extra `origin` value.
2. `VerifiedCase` has no place for links (failed run, job, fix commit), the failure time, a diff
   excerpt or the log signature's source. They sit in extra columns of `testing.kb_cases`.
   Proposal: optional `source_urls`, `failed_at` and `fix_excerpt`.
3. `RetrievedCase.similarity` is a 0 to 1 float, but RRF scores are rank-based. We send the
   fused score divided by its maximum. Proposal: document that it is a rank score, or add an
   optional `score_kind`.
4. `EvidenceLine.source` has no `history` value (already noted for P4.9).

## 8. Limits

- Weak labels all the way: the case set comes from rule labels, the "fix" is the first commit
  touching the failing test file (it may be unrelated), and tags are regexes. Nothing was checked
  by a human.
- The 3-month window and 16 repos mean a new repo gets neighbours from other repos only.
- Cases that disappear from the labels are not deleted from the table by `kb build` (no prune).
- The Docker worker image has no torch, so inside Docker retrieval is full-text only
  (hybrid needs the model; see the README).
- The first diagnosis in a process loads the model (about 14 s with the weights cached).
- `kb eval` needs the database and the real model.
