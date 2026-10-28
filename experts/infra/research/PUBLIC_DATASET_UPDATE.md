# Public GitHub Actions dataset update

Collected on 2026-10-28. The new workbook keeps the original 3,000 records and adds 158
public runs from eight additional repositories. The new batch has 73 observed successes
and 85 failures awaiting independent review. These are workflow runs, not 158 verified
infrastructure failures.

The new repositories are microsoft/playwright, storybookjs/storybook, remix-run/react-router,
vitejs/vite, supabase/supabase, calcom/cal.com, payloadcms/payload and
open-telemetry/opentelemetry-js. The first seven were sampled for both successful and failed
runs. Runs with container, image, deployment or release names were prioritised where available,
but a workflow name is not evidence of a failure cause. The OpenTelemetry run was selected
separately because a public issue explicitly links its failed job to runner disk exhaustion:
https://github.com/open-telemetry/community/issues/3461.

The source run metadata was checked, but the cause still needs independent human review of
the issue and, if available, the original logs and actual execution revision. Some job logs
required GitHub authentication when collected. Many other public annotations contain only a
generic exit code or a runner migration notice. None of the 85 new failure targets was filled
automatically. This keeps them out of model training until reviewed; it also means the batch
does not yet supply more verified positive training examples.

The new runs are deliberately selected by outcome and workflow name, not sampled randomly.
Their success/failure ratios cannot estimate a normal repository's failure probability. New
history fields remain unknown because failure-enriched history would be misleading. Workflow
snapshots use the run head SHA and may differ from the actual executed workflow, particularly
for pull requests. Commit patches describe the head commit, not necessarily every commit in a
push. Review these limits before accepting a case. Existing assistant-labelled failures also
still need independent review.

The workbook is at `outputs/public-repos-20261028/infra_github_actions_v6.xlsx`. The original
workbook and the dashboard's trained model have not been replaced. The dataset and generated
outputs stay local and are ignored by Git; back them up separately when sharing.

## Review and train later

From the `experts/infra/research` folder, build a review queue for this exact workbook:

```powershell
.\.venv\Scripts\python -m model.audit --input outputs/public-repos-20261028/infra_github_actions_v6.xlsx --output-dir artifacts/public-repos-review-20261028
```

The queue is `artifacts/public-repos-review-20261028/label_review_queue.jsonl`. See
`DATASET_REPAIR_REPORT.md` for how an independent person can review each source and record
a decision. Use the queue made for this workbook: the older v6 review queue has the checksum
of the old workbook and cannot be reused unchanged.

Once sufficient cases across repositories and dates have been reviewed, train and test using
the workbook and its review file together:

```powershell
.\.venv\Scripts\python build_model.py --input outputs/public-repos-20261028/infra_github_actions_v6.xlsx --version public-reviewed-v1 --feature-profile no_history --reviews artifacts/public-repos-review-20261028/label_review_queue.jsonl --skip-explain
```

The command requires enough examples from both classes in all splits and does not promote the
result to the dashboard. Independently evaluate on newer, previously unseen repositories before
using any resulting risk percentage. Human review is a recorded process; a checked box alone
cannot prove the quality or independence of the judgment.
