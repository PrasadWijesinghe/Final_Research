# Verified-case preparation

This workflow keeps candidate extraction separate from human approval. It will never convert the
workbook's agent-reviewed labels directly into verified RAG evidence.

## 1. Export the review queue

From `experts/infra/research`:

```powershell
.\.venv\Scripts\python -m rag.prepare_cases export
```

This reads positive rows from the workbook, groups repeated runs into one incident, applies the
repository's shared secret sanitiser, and writes `data/rag_case_review.jsonl`. It refuses to
overwrite an existing queue unless `--overwrite` is supplied, because that file may contain human
review work.

The current workbook produces 105 incident candidates. It contains no collected verified fixes,
so every candidate starts with `human_verified` set to `false`.

## 2. Review each candidate

JSONL has one JSON object per line. A reviewer should inspect the evidence and complete these
fields only when the cause and successful fix are known:

- `root_cause`
- `remediation`
- `verification_evidence`
- `verified_by` (a team role is sufficient)
- `verified_at` (ISO date, such as `2026-10-25`)
- `human_verified` (`true` only after the review is complete)

`suggested_root_cause` comes from the source annotations. It is a review aid, not verified truth.
Copy or rewrite it into `root_cause` only after checking the evidence. Leave uncertain cases set to
`false`.

## 3. Finalise and build the index

```powershell
.\.venv\Scripts\python -m rag.prepare_cases finalise
```

This command:

1. selects only records where `human_verified` is the JSON boolean `true`;
2. requires evidence, root cause, remediation and verification evidence;
3. sanitises the approved text again;
4. writes `data/verified_cases.jsonl`; and
5. builds `artifacts/rag/case_retriever.joblib`.

It refuses to create either output when there are no approved cases or an approved case is
incomplete. It also refuses to overwrite existing outputs unless `--overwrite` is supplied.

If a fully verified JSONL file already exists, the index can instead be rebuilt directly:

```powershell
.\.venv\Scripts\python -m rag.build_index data/verified_cases.jsonl
```

The direct builder requires every case to contain `verified: true` and `sanitised: true`.
