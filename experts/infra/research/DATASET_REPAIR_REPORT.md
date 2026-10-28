# Infra dataset and model: investigation and next steps

## What was found

The 3,000 records are collected GitHub Actions workflow runs, not 3,000 separate failures.
There are 2,573 observed successes, 187 labelled infrastructure failures, 65 labelled other
failures, and 175 unresolved failures. The 187 positives cover 105 different incidents. The two
legacy labels marked "Manually verified" as positive do not, by themselves, prove who reviewed
them or whether the review was independent. Most positives were assistant-reviewed and need an
independent check. Sampling was targeted: model output is not a measured rate across all pushes.

Only one positive row was labelled container registry availability and one runner storage
capacity. Many types of infrastructure failure are too rare to learn or evaluate separately.
Further, changes to an image name do not change any current input describing image validity.
Some runtime failures depend on secrets, services or network conditions that are not known from a
commit alone. A pre-run model cannot reliably foresee information it cannot observe.

Two flags (`Is_Pull_Request` and `Is_Scheduled`) were absent in 2,000 records even though the
trigger was recorded. `prepare_features` now derives them from that trigger. The live workflow
extractor now uses the original dataset collector's definitions for its workflow signals and
preserves unknowns for missing snapshots. A remaining mismatch is that live prediction combines
all workflow snapshots into one row, whereas the dataset has a row per workflow run.

## Results from 2026-10-28

The baseline and corrected experiments use the same dataset and no-history model inputs. Each
repository fold selects among four model candidates and chooses its threshold using only other
repositories, with separate chronological fitting, probability calibration and selection sets.
The final temporal test was not included in this repository benchmark. Results use legacy labels.
This temporal test was already inspected during previous research, so it is no longer a pristine
final holdout for the overall project; reserve genuinely new data for the final acceptance test.

| Experiment | Held-out positive rows | Detected | Missed | False alerts |
|---|---:|---:|---:|---:|
| Before event-flag fix | 184 | 1 | 183 | 39 |
| After event-flag fix | 184 | 5 | 179 | 35 |

This is not a reliable result just because the number improved: the model still misses nearly all
labelled failures, the labels need review, and positive incidents are not independent rows. The
corrected v5 model also missed both positives in the temporal test, which had only two positives.
Scores across repository folds come from different calibrated models. Holdout folds exclude the
repository but are not also restricted to earlier timestamps. A separate future-and-new-repository
test with enough reviewed failures remains necessary. The model stays research-only.

The full no-history artifact and metrics are in `artifacts/v5-event-flags-fixed/`. The prior
nested report is in `artifacts/v5-audit/`. Those generated files and the datasets are local and
ignored by Git; back them up if you need to share them. The serving model has not been replaced.

## Evidence collected from the demo

The public run `40689505413` for `PrasadWijesinghe/VERSE-Luxury` failed at **Initialize
containers**, and its public annotations state that Docker pull failed. The run was deliberately
injected and is a challenge example, not natural training data. Run `40689505426` failed at a
deployment-configuration validation step; its annotation contains only a generic exit code, so
the full cause needs separate review. Sanitised evidence and workflow snapshots are stored in
`data/review_candidates/`, marked `human_verified: false` and `use_for_training: false`.
Public check annotations may combine attempts and workflow-at-head is a proxy; verify both.

To collect another public run, from this `research` folder, use its numeric GitHub run ID:

```powershell
.\.venv\Scripts\python -m model.collect_cases --repo OWNER/REPO --run-id 123456 --output-dir data/review_candidates
```

For a deliberately injected case add `--challenge`. The collector never marks a case verified,
does not overwrite an existing file, and does not merge it into model training. Public API access
can be rate limited; for a private repository, an authorised `GITHUB_TOKEN` may be required.
Never put a token in a command, dataset or review file. Check the source and permissions first.

## Reviewing the existing failure labels

1. Open `artifacts/v6-review-queue/label_review_queue.jsonl` in an editor. There is one JSON
   object per line. Work on a copy and retain the workbook and queue checksums.
2. Independently open the run URL. Check which attempt failed, the evidence for the cause,
   whether it really belongs to Infra and which workflow revision actually ran. If evidence is
   missing or ambiguous, leave `human_verified` as `false`.
3. For a supported label, complete `reviewer_id` (use a pseudonym), `reviewed_at_utc` (ISO UTC),
   `evidence_url`, a short `sanitised_evidence`, and `verified_target` (1 for Infra, 0 for a
   verified other failure). For a positive add `verified_subtype`. Set
   `workflow_revision_verified` and `human_verified` to `true` only after those checks.
4. Keep enough reviews across repositories and dates, including negative controls. A signed-off
   field records a claim of human review; the software cannot verify who actually reviewed it or
   that their judgment is correct. Have an independent reviewer audit a sample.
5. After substantial review, use the `--reviews` command in the README to retrain. Unreviewed
   failure labels are excluded, not silently treated as successes. Do not expect training to work
   with just a few reviews: all time partitions and model selection folds need both classes.

New standalone collected cases still need a separately audited importer with pre-run features,
commit diffs, deduplication, provenance, and leakage-safe splits before they can join the workbook.
The human-review queue currently applies only to rows already present in the workbook. Then gather
more examples from multiple independent repositories, include both successful and failed runs,
and evaluate on newer repositories and dates unseen in training. Agree on acceptable recall,
false-alert rate and calibration before adopting any artifact for real predictions.
