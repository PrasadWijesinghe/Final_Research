"""Build the triage training table: one row per labelled failure (genuine or flaky) (P4.9).

Run: `python -m testing_expert.triage.dataset --out data/datasets/testing/triage_v1`.

Two tables: `rows.parquet` (the whole log is parsed) and `rows_excerpt.parquet` (only the last
20 000 characters, what the router sends), so the report can show how much the short excerpt costs.

Rows come from `testing.labels` (`genuine` and `flaky`; unclear and not_test are left out). For each
failure the sanitised log is read from disk and parsed, and `gather` reads the database as of
the failed job's `completed_at`.
"""

from __future__ import annotations

import argparse
import gzip
import json
import logging
import os
import sys
import time
from collections.abc import Sequence
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any

import pandas as pd
import psycopg

from testing_expert.parsing import clean_bytes

from . import prerun
from .features import FEATURE_NAMES, LogEvidence, feature_row, read_log
from .gather import EXCERPT_CHARS, FailureRef, gather

log = logging.getLogger(__name__)

_SQL = """
select l.job_id, l.sub_label, l.confidence, l.rule_name, j.completed_at, r.full_name, w.head_sha,
       j.attempt, coalesce(j.name, j.job_key), coalesce(w.workflow_path, ''), lg.storage_path
from testing.labels l
join raw.jobs j on j.job_id = l.job_id
join raw.workflow_runs w on w.run_id = j.run_id and w.attempt = j.attempt
join raw.repos r on r.id = w.repo_id
join raw.logs lg on lg.job_id = j.job_id and lg.status = 'ok'
where l.sub_label in ('genuine', 'flaky') and l.labelled_by = 'rule' and l.is_final
  and j.completed_at is not null
order by j.completed_at, l.job_id"""


def _data_dir() -> Path:
    env = os.environ.get("DATA_DIR")
    return Path(env) if env else Path(__file__).resolve().parents[5] / "data"


def _read(args: tuple[str, str, str]) -> tuple[LogEvidence, LogEvidence] | None:
    """Worker process: read one gz log; parse all of it and, separately, its last 20 000 chars."""
    path, repo, _ = args
    try:
        data = Path(path).read_bytes()
        text = clean_bytes(data).text()
    except (OSError, EOFError, gzip.BadGzipFile):
        return None
    return read_log(text, repo), read_log(text[-EXCERPT_CHARS:], repo)


def build(out_dir: Path, workers: int = 6) -> dict[str, Any]:
    """Build and write rows.parquet and manifest.json; returns the manifest."""
    t0 = time.perf_counter()
    url = os.environ["DATABASE_URL"].replace("+psycopg", "")
    with psycopg.connect(url) as conn:
        with conn.cursor() as cur:
            cur.execute(_SQL)
            rows = cur.fetchall()
    log.info("labelled failures with a log: %d", len(rows))
    data_dir = _data_dir()
    jobs = [(str(data_dir / r[10]), r[5], "") for r in rows]
    with ProcessPoolExecutor(workers) as pool:
        logs = list(pool.map(_read, jobs, chunksize=16))
    log.info("logs parsed in %.0fs", time.perf_counter() - t0)
    risk = prerun.oof_flaky_risk()
    log.info("pre-run risk cross-fitted in %.0fs", time.perf_counter() - t0)

    def one(i: int, part: int) -> dict[str, Any] | None:
        r, both = rows[i], logs[i]
        if both is None:
            return None
        lg = both[part]
        ref = FailureRef(
            repo=r[5], head_sha=r[6], job_id=r[0], run_attempt=r[7], job_name=r[8],
            workflow_path=r[9], cutoff=r[4],
        )  # fmt: skip
        with psycopg.connect(url) as c:
            ev = gather(c, ref, lg, float(risk.get(r[0], float("nan"))))
        return {
            "job_id": r[0],
            "repo": r[5],
            "label": "flaky" if r[1] == "flaky" else "genuine",
            "label_confidence": r[2],
            "rule_name": r[3],
            "failed_at": r[4],
            "is_test_failure": lg.is_test_failure,
            **feature_row(ev),
        }

    out_dir.mkdir(parents=True, exist_ok=True)
    for part, name in ((0, "rows.parquet"), (1, "rows_excerpt.parquet")):
        with ThreadPoolExecutor(8) as ex:
            out = [x for x in ex.map(partial(one, part=part), range(len(rows))) if x is not None]
        df = pd.DataFrame(out)
        df["failed_at"] = pd.to_datetime(df["failed_at"], utc=True)
        df.to_parquet(out_dir / name, index=False)
    manifest = {
        "version": out_dir.name,
        "built_at": datetime.now(UTC).isoformat(),
        "n_rows": len(df),
        "labels": df["label"].value_counts().to_dict(),
        "n_test_failures_parsed": int(df["is_test_failure"].sum()),
        "features": list(FEATURE_NAMES),
        "seconds": round(time.perf_counter() - t0),
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=1))
    return manifest


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point."""
    ap = argparse.ArgumentParser(prog="testing_expert.triage.dataset", description=__doc__)
    ap.add_argument("--out", type=Path, default=Path("data/datasets/testing/triage_v1"))
    ap.add_argument("--workers", type=int, default=6)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    print(json.dumps(build(args.out, args.workers), indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
