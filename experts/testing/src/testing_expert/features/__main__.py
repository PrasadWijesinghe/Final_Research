"""Run the batch pipeline on one real repo (`python -m testing_expert.features --repo o/n`)."""

from __future__ import annotations

import argparse
import json
import os
import time

import psycopg

from .compute import compute_features_batch
from .loader import SERIES_KEY_SQL
from .settings import get_settings


def main() -> None:
    """Compute features for every finished test-like job attempt of a repo; print timing."""
    ap = argparse.ArgumentParser(description=main.__doc__)
    ap.add_argument("--repo", required=True, help="full_name, e.g. vitest-dev/vitest")
    ap.add_argument("--limit", type=int, default=0, help="0 = all attempts")
    args = ap.parse_args()
    url = os.environ["DATABASE_URL"].replace("+psycopg", "")
    sql = f"""
        select j.run_id, j.attempt, j.job_id
        from raw.jobs j join raw.workflow_runs w on w.run_id = j.run_id and w.attempt = j.attempt
        join raw.repos r on r.id = w.repo_id
        where r.full_name = %s and j.conclusion in ('success', 'failure')
          and {SERIES_KEY_SQL} ~* %s
        order by w.created_at {"limit " + str(args.limit) if args.limit else ""}
    """
    with psycopg.connect(url) as conn:
        keys = [tuple(r) for r in conn.execute(sql, (args.repo, get_settings().test_job_regex))]
        t0 = time.perf_counter()
        rows = compute_features_batch(conn, keys)  # type: ignore[arg-type]
        secs = time.perf_counter() - t0
    print(f"{len(rows)} rows in {secs:.1f}s ({len(rows) / max(secs, 1e-9):.0f} rows/s)")
    if rows:
        key = list(rows)[len(rows) * 3 // 4]
        print(key)
        print(json.dumps(rows[key], indent=1))


if __name__ == "__main__":
    main()
