"""Manual-review sample: stratified random rows to CSV, plus a hidden key with the rule labels.

The human sees no rule label (so the check is independent). `human_label` and `ai_prelabel` are
left empty. The rule labels go to a second file, `<name>_key.csv`, which the kappa step reads.
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

from sqlalchemy import Connection, text

# DEFAULT: ~50 per label (test.md section 3); flaky is split by confidence, so 6 strata.
PER_STRATUM = 50
DEFAULT_SEED = "p4.3-v1"  # DEFAULT: fixed seed, same rows every run while labels are unchanged

SAMPLE_COLUMNS = [
    "job_id", "repo", "run_id", "attempt", "head_sha", "branch", "workflow_path", "job_name",
    "run_url", "same_sha_outcomes", "n_failing_tests", "failing_tests", "log_path",
    "human_label", "ai_prelabel", "notes",
]  # fmt: skip
KEY_COLUMNS = ["job_id", "rule_label", "rule_name", "confidence"]

_SQL = """
with ranked as (
  select l.job_id, l.run_id, l.attempt, l.sub_label, l.rule_name, l.confidence, l.payload,
         row_number() over (partition by l.sub_label, l.confidence
                            order by md5(:seed || l.job_id::text)) as rn
  from testing.labels l
  join raw.jobs j on j.job_id = l.job_id
  join raw.workflow_runs w on w.run_id = j.run_id and w.attempt = j.attempt
  join raw.repos r on r.id = w.repo_id and r.member = :member
  where l.labelled_by = 'rule'
)
select k.job_id, k.payload->>'repo', k.run_id, k.attempt, k.payload->>'sha',
       k.payload->>'branch', k.payload->>'workflow_path', k.payload->>'job_name',
       k.payload->>'same_sha_outcomes', k.sub_label, k.rule_name, k.confidence,
       ft.n, ft.names, lg.storage_path
from ranked k
left join lateral (
  select count(*) as n, string_agg(full_name, ' | ' order by full_name) filter (where rn <= 5)
         as names
  from (select full_name, row_number() over (order by full_name) as rn
        from (select distinct full_name from testing.test_results t
              where t.job_id = k.job_id and t.outcome = 'failed') d) x
) ft on true
left join raw.logs lg on lg.job_id = k.job_id
where k.rn <= :n
order by md5(:seed || 'order' || k.job_id::text)
"""


def export_sample(
    conn: Connection,
    member: str,
    out_dir: Path,
    per_stratum: int = PER_STRATUM,
    seed: str = DEFAULT_SEED,
    name: str = "test_labels_sample",
) -> tuple[Path, Path, int]:
    """Write `<name>.csv` and `<name>_key.csv` under out_dir; return both paths, row count."""
    rows = conn.execute(text(_SQL), {"member": member, "seed": seed, "n": per_stratum}).all()
    out_dir.mkdir(parents=True, exist_ok=True)
    sample_path = out_dir / f"{name}.csv"
    key_path = out_dir / f"{name}_key.csv"
    with sample_path.open("w", newline="", encoding="utf-8") as fh:
        wr = csv.writer(fh)
        wr.writerow(SAMPLE_COLUMNS)
        for r in rows:
            repo = r[1]
            url = f"https://github.com/{repo}/actions/runs/{r[2]}/attempts/{r[3]}"
            log_path = f"data/{r[14]}" if r[14] else ""
            wr.writerow([r[0], repo, r[2], r[3], r[4], r[5], r[6], r[7], url, r[8] or "",
                         r[12] or 0, r[13] or "", log_path, "", "", ""])  # fmt: skip
    with key_path.open("w", newline="", encoding="utf-8") as fh:
        wr = csv.writer(fh)
        wr.writerow(KEY_COLUMNS)
        for r in rows:
            wr.writerow([r[0], r[9], r[10], r[11]])
    return sample_path, key_path, len(rows)


def sample_summary(rows: list[dict[str, Any]]) -> dict[str, int]:
    """Count sample rows per label from key rows (helper for reports and tests)."""
    out: dict[str, int] = {}
    for r in rows:
        k = f"{r['rule_label']}/{r['confidence']}"
        out[k] = out.get(k, 0) + 1
    return out
