"""Test-results extraction (P4.2): job logs on disk to testing.test_ids and test_results.

Run from the repository root (needs the database in DATABASE_URL and the logs in `data/logs/`):

    uv run --env-file .env python -m testing_expert.extract --member test
    make extract-test-results

Full scope (v2): `--all` reads EVERY stored job log that is not yet in `testing.log_parses`
(passing and failing; a log with no test runner is recorded as not-a-test-job and writes no rows).
Writes use COPY into temporary tables plus set-based inserts. `--sample N` reads a random N logs
(a throughput trial; the rows it writes are real and the full run skips them).

Which logs are read in the default (v1) mode: the logs of FAILED job attempts, plus every other
attempt or run of the same job (same repo, same head_sha, same job name) that has a stored log.
A failed job counts as a test job only when the parsers recognise a test runner in its log, and
only then are its siblings read. `--all` replaces that cut.

It is safe to run again: a job that already has a row in `testing.log_parses` is skipped, so an
interrupted run resumes where it stopped, and writing a job replaces that job's earlier rows.
Log text is never stored or logged; only test names, outcomes and counts are.
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import multiprocessing
import os
import sys
import time
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from sqlalchemy import Connection, text

from testing_expert.parsing.make_fixtures import default_data_dir
from testing_expert.parsing.models import ParsedLog
from testing_expert.parsing.parse import parse_log

log = logging.getLogger(__name__)

REASON_SEED = "seed_failed"
REASON_SIBLING = "sibling"
REASON_ALL = "all"
# DEFAULT: v1 cut, the failed job attempts are the seeds; their same-SHA siblings come second.
DEFAULT_BATCH = 200  # DEFAULT: jobs written per transaction; small enough to lose little on a crash
DEFAULT_LOG_EVERY = 1000  # DEFAULT: one progress line per 1000 jobs
# DEFAULT: results per job are capped so one giant passing log cannot flood the table.
MAX_ROWS_PER_JOB = 20000

# DEFAULT: "same job" = same name (matrix values included, e.g. `test (20)`), falling back to the
# YAML job key when the name is null. job_key alone is too coarse: every matrix cell shares it.
_JOB_NAME = "coalesce(j.name, j.job_key)"
_SEEDS_SQL = f"""
select j.job_id, j.run_id, j.attempt, r.full_name, w.head_sha, {_JOB_NAME}, l.storage_path
from raw.jobs j
join raw.logs l on l.job_id = j.job_id
join raw.workflow_runs w on w.run_id = j.run_id and w.attempt = j.attempt
join raw.repos r on r.id = w.repo_id
where r.member = :member and l.status = 'ok' and j.conclusion = 'failure'
  and not exists (select 1 from testing.log_parses p where p.job_id = j.job_id)
order by j.job_id
"""
_SIBLINGS_SQL = f"""
select distinct on (j.job_id)
       j.job_id, j.run_id, j.attempt, r.full_name, w.head_sha, {_JOB_NAME}, l.storage_path
from testing.log_parses s
join raw.repos r on r.full_name = s.repo and r.member = :member
join raw.workflow_runs w on w.repo_id = r.id and w.head_sha = s.head_sha
join raw.jobs j on j.run_id = w.run_id and j.attempt = w.attempt and {_JOB_NAME} = s.job_name
join raw.logs l on l.job_id = j.job_id
where s.reason = '{REASON_SEED}' and s.is_test_job and l.status = 'ok'
  and not exists (select 1 from testing.log_parses p where p.job_id = j.job_id)
order by j.job_id
"""


_ALL_SQL = f"""
select j.job_id, j.run_id, j.attempt, r.full_name, w.head_sha, {_JOB_NAME}, l.storage_path
from raw.jobs j
join raw.logs l on l.job_id = j.job_id
join raw.workflow_runs w on w.run_id = j.run_id and w.attempt = j.attempt
join raw.repos r on r.id = w.repo_id
where r.member = :member and l.status = 'ok'
  and not exists (select 1 from testing.log_parses p where p.job_id = j.job_id)
order by {{order}}
"""


@dataclass(frozen=True)
class JobRef:
    """One job attempt to read: where it came from and where its log is."""

    job_id: int
    run_id: int
    attempt: int
    repo: str
    head_sha: str
    job_name: str | None
    storage_path: str
    reason: str


@dataclass(frozen=True)
class Row:
    """One `testing.test_results` row (plus the `test_ids` fields)."""

    test_id: str
    file: str | None
    full_name: str
    outcome: str
    duration_ms: float | None
    granularity: str


@dataclass(frozen=True)
class Extracted:
    """What reading one log produced."""

    ref: JobRef
    framework: str
    granularity: str
    parsed_ok: bool
    is_test_job: bool
    confidence: float
    error: str | None
    rows: tuple[Row, ...]


def make_test_id(repo: str, file: str | None, full_name: str) -> str:
    """Stable id: sha1 of repo, file and full name (full name is empty for a file-level row)."""
    key = "\x00".join((repo, file or "", full_name))
    return hashlib.sha1(key.encode("utf-8"), usedforsecurity=False).hexdigest()  # noqa: S324


_RANK = {"passed": 0, "skipped": 1, "todo": 1, "failed": 2}


def build_rows(repo: str, parsed: ParsedLog) -> tuple[Row, ...]:
    """Turn a parsed log into deduplicated rows: reporter results plus every failed test or file."""
    rows: dict[str, Row] = {}

    def add(file: str | None, full_name: str, outcome: str, dur: float | None, gran: str) -> None:
        if gran == "file":
            file, full_name = file or full_name, ""
            if not file:
                return
        elif not full_name:
            return
        tid = make_test_id(repo, file, full_name)
        old = rows.get(tid)
        if old is None or _RANK[outcome] > _RANK[old.outcome]:
            rows[tid] = Row(tid, file, full_name, outcome, dur, gran)

    for r in parsed.results:
        gran = "file" if parsed.granularity == "file" else "test"
        add(r.file, r.full_name, r.outcome, r.duration_ms, gran)
    for f in parsed.failures:
        if f.kind == "unhandled":
            continue  # an error outside any test: not a test outcome
        add(f.file, f.full_name, "failed", None, "file" if f.kind == "suite" else "test")
    return tuple(list(rows.values())[:MAX_ROWS_PER_JOB])


def _failure_reason(parsed: ParsedLog) -> str | None:
    """Say why a parse is not ok, as a short fixed category (never log text)."""
    if parsed.parsed_ok:
        return None
    text_ = " ".join(parsed.warnings)
    if "parsing config" in text_:
        return "config"
    if "parser failed" in text_ or "could not be parsed" in text_:
        return "parser_exception"
    if "reports failures but no failing test" in text_:
        return "summary_without_failures"
    return "other"


# DEFAULT: a failed job is a "test job" when the parsers recognise a test runner in its log (a
# framework other than "other") or find any test result or failure. The workflow `relevant` flag is
# too coarse (46 of 272 workflows) and step names vary; the log itself is the best evidence.
def is_test_job(parsed: ParsedLog) -> bool:
    """True when the log shows a Jest, Mocha or Vitest run."""
    return parsed.framework != "other" or bool(parsed.results) or bool(parsed.failures)


def extract_one(args: tuple[JobRef, str]) -> Extracted:
    """Read and parse one job log. Never raises; runs in a worker process."""
    ref, data_dir = args
    path = Path(data_dir) / ref.storage_path

    def fail(error: str) -> Extracted:
        return Extracted(ref, "other", "none", False, False, 0.0, error, ())

    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return fail("missing_file")
    except OSError:
        return fail("unreadable")
    parsed = parse_log(data)
    test_job = is_test_job(parsed)
    rows = build_rows(ref.repo, parsed) if test_job else ()
    return Extracted(
        ref,
        parsed.framework,
        parsed.granularity,
        parsed.parsed_ok,
        test_job,
        parsed.confidence,
        _failure_reason(parsed),
        rows,
    )


_RESULT_COLS = (
    "job_id", "test_id", "attempt", "file", "full_name", "outcome", "duration_ms", "granularity"
)  # fmt: skip
_ID_COLS = ("test_id", "repo", "file", "full_name", "granularity")


def _copy_rows(
    conn: Connection, test_ids: dict[str, dict[str, Any]], results: list[dict[str, Any]]
) -> None:
    """Bulk-load ids and results: COPY into temp tables, then set-based inserts (idempotent)."""
    raw = conn.connection.cursor()
    raw.execute(
        "create temp table _t_ids (test_id text, repo text, file text, full_name text, "
        "granularity text) on commit drop"
    )
    raw.execute(
        "create temp table _t_res (job_id bigint, test_id text, attempt int, file text, "
        "full_name text, outcome text, duration_ms double precision, granularity text) "
        "on commit drop"
    )
    with raw.copy(f"copy _t_ids ({', '.join(_ID_COLS)}) from stdin") as cp:
        for t in test_ids.values():
            cp.write_row([t[c] for c in _ID_COLS])
    with raw.copy(f"copy _t_res ({', '.join(_RESULT_COLS)}) from stdin") as cp:
        for r in results:
            cp.write_row([r[c] for c in _RESULT_COLS])
    raw.execute(
        "insert into testing.test_ids (test_id, repo, file, full_name, granularity) "
        "select test_id, repo, file, full_name, granularity from _t_ids "
        "on conflict (test_id) do nothing"
    )
    raw.execute(
        f"insert into testing.test_results ({', '.join(_RESULT_COLS)}) "
        f"select {', '.join(_RESULT_COLS)} from _t_res on conflict (job_id, test_id) do update set "
        "outcome = excluded.outcome, duration_ms = excluded.duration_ms, "
        "granularity = excluded.granularity, attempt = excluded.attempt"
    )
    raw.close()


def prune_passing(e: Extracted, keep_ids: frozenset[str] | None) -> Extracted:
    """Drop test-level rows that carry no history signal from a full-scope (v2) read.

    # DEFAULT: storing every passing test of every passing log would be ~130M rows (Mocha lists
    # 1,000+ tests per run). A passing or skipped test-level row is kept only when that test has
    # failed somewhere in the data (`keep_ids`); a test that never failed has a fail rate of 0
    # whether or not its passes are stored. File-level rows and all failed rows are always kept.
    """
    if keep_ids is None or e.ref.reason != REASON_ALL:
        return e
    rows = tuple(
        r
        for r in e.rows
        if r.granularity == "file" or r.outcome == "failed" or r.test_id in keep_ids
    )
    return replace(e, rows=rows)


def write_batch(conn: Connection, batch: Sequence[Extracted]) -> None:
    """Replace the rows of each job in `batch` in one transaction (idempotent)."""
    ids = [e.ref.job_id for e in batch]
    conn.execute(text("delete from testing.test_results where job_id = any(:ids)"), {"ids": ids})
    test_ids: dict[str, dict[str, Any]] = {}
    results: list[dict[str, Any]] = []
    for e in batch:
        for r in e.rows:
            test_ids[r.test_id] = {
                "test_id": r.test_id,
                "repo": e.ref.repo,
                "file": r.file or "",
                "full_name": r.full_name,
                "granularity": r.granularity,
            }
            results.append(
                {
                    "job_id": e.ref.job_id,
                    "test_id": r.test_id,
                    "attempt": e.ref.attempt,
                    "file": r.file,
                    "full_name": r.full_name,
                    "outcome": r.outcome,
                    "duration_ms": r.duration_ms,
                    "granularity": r.granularity,
                }
            )
    if test_ids:
        _copy_rows(conn, test_ids, results)
    conn.execute(
        text(
            "insert into testing.log_parses (job_id, run_id, attempt, repo, head_sha, job_name, "
            "reason, framework, granularity, parsed_ok, is_test_job, confidence, n_rows, error) "
            "values (:job_id, :run_id, :attempt, :repo, :head_sha, :job_name, :reason, :framework, "
            ":granularity, :parsed_ok, :is_test_job, :confidence, :n_rows, :error) "
            "on conflict (job_id) do update set framework = excluded.framework, "
            "granularity = excluded.granularity, parsed_ok = excluded.parsed_ok, "
            "is_test_job = excluded.is_test_job, confidence = excluded.confidence, "
            "n_rows = excluded.n_rows, error = excluded.error, parsed_at = now()"
        ),
        [
            {
                "job_id": e.ref.job_id,
                "run_id": e.ref.run_id,
                "attempt": e.ref.attempt,
                "repo": e.ref.repo,
                "head_sha": e.ref.head_sha,
                "job_name": e.ref.job_name,
                "reason": e.ref.reason,
                "framework": e.framework,
                "granularity": e.granularity,
                "parsed_ok": e.parsed_ok,
                "is_test_job": e.is_test_job,
                "confidence": e.confidence,
                "n_rows": len(e.rows),
                "error": e.error,
            }
            for e in batch
        ],
    )


def select_jobs(
    conn: Connection, member: str, reason: str, sample: int | None = None
) -> list[JobRef]:
    """Jobs still to read: failed attempts (seeds), their same-SHA siblings, or all (v2).

    `sample` (reason "all" only) picks a random subset instead of the whole backlog.
    """
    if reason == REASON_ALL:
        order = "random()" if sample else "j.job_id"
        sql = _ALL_SQL.format(order=order) + (f" limit {int(sample)}" if sample else "")
    else:
        sql = _SEEDS_SQL if reason == REASON_SEED else _SIBLINGS_SQL
    rows = conn.execute(text(sql), {"member": member}).all()
    return [JobRef(int(r[0]), int(r[1]), int(r[2]), r[3], r[4], r[5], r[6], reason) for r in rows]


def _chunks(items: Sequence[Extracted] | Iterable[Extracted], n: int) -> Iterator[list[Extracted]]:
    chunk: list[Extracted] = []
    for item in items:
        chunk.append(item)
        if len(chunk) >= n:
            yield chunk
            chunk = []
    if chunk:
        yield chunk


def run_stage(
    engine: Any,
    refs: list[JobRef],
    data_dir: Path,
    workers: int,
    batch_size: int = DEFAULT_BATCH,
    log_every: int = DEFAULT_LOG_EVERY,
    keep_ids: frozenset[str] | None = None,
) -> int:
    """Parse `refs` (in parallel when workers > 1) and write them. Returns jobs written."""
    if not refs:
        return 0
    work = [(r, str(data_dir)) for r in refs]
    started = time.monotonic()
    done = 0
    pool = multiprocessing.get_context("spawn").Pool(workers) if workers > 1 else None
    try:
        results: Iterable[Extracted] = (
            pool.imap_unordered(extract_one, work, chunksize=8) if pool else map(extract_one, work)
        )
        for batch in _chunks(results, batch_size):
            with engine.begin() as conn:
                write_batch(conn, [prune_passing(e, keep_ids) for e in batch])
            before = done
            done += len(batch)
            if done // log_every != before // log_every or done == len(refs):
                log.info(
                    "extract progress",
                    extra={
                        "done": done,
                        "total": len(refs),
                        "seconds": round(time.monotonic() - started),
                    },
                )
    finally:
        if pool:
            pool.close()
            pool.join()
    return done


def report(engine: Any, member: str) -> dict[str, Any]:
    """Counts for the run: jobs, parse failures by reason, rows per granularity and framework."""
    with engine.connect() as conn:

        def pairs(sql: str) -> dict[str, int]:
            return {str(k): int(v) for k, v in conn.execute(text(sql), {"m": member}).all()}

        scope = (
            "from testing.log_parses p join raw.repos r on r.full_name = p.repo where r.member = :m"
        )
        return {
            "jobs_by_reason": pairs(f"select p.reason, count(*) {scope} group by 1"),
            "logs_parsed": pairs(f"select p.error is null, count(*) {scope} group by 1"),
            "test_jobs": pairs(f"select p.is_test_job, count(*) {scope} group by 1"),
            "parse_failures_by_reason": pairs(
                f"select p.error, count(*) {scope} and p.error is not null group by 1"
            ),
            "jobs_by_framework": pairs(
                f"select p.framework, count(*) {scope} and p.is_test_job group by 1"
            ),
            "rows_by_granularity": pairs(
                "select t.granularity, count(*) from testing.test_results t "
                "join testing.log_parses p on p.job_id = t.job_id "
                "join raw.repos r on r.full_name = p.repo where r.member = :m group by 1"
            ),
            "rows_by_framework": pairs(
                "select p.framework, count(*) from testing.test_results t "
                "join testing.log_parses p on p.job_id = t.job_id "
                "join raw.repos r on r.full_name = p.repo where r.member = :m group by 1"
            ),
            "distinct_test_ids": pairs(
                "select 'n', count(*) from testing.test_ids i where exists "
                "(select 1 from raw.repos r where r.full_name = i.repo and r.member = :m)"
            ),
        }


def failed_test_ids(engine: Any) -> frozenset[str]:
    """Every test id that has a failed row in the table (the keep-set for `prune_passing`)."""
    with engine.connect() as conn:
        rows = conn.execute(
            text("select distinct test_id from testing.test_results where outcome = 'failed'")
        ).all()
    return frozenset(str(r[0]) for r in rows)


def main(argv: Sequence[str] | None = None) -> int:
    """Command line entry point."""
    parser = argparse.ArgumentParser(prog="testing_expert.extract", description=__doc__)
    parser.add_argument("--member", required=True, help="collector member, e.g. test")
    parser.add_argument(
        "--workers", type=int, default=max(1, (os.cpu_count() or 2) - 1), help="parser processes"
    )
    parser.add_argument("--data-dir", type=Path, default=None, help="folder holding logs/")
    parser.add_argument("--all", action="store_true", help="read every unparsed log (v2 scope)")
    parser.add_argument("--sample", type=int, default=None, help="with --all: random N logs only")
    parser.add_argument("--limit", type=int, default=None, help="read at most N seed jobs (trial)")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

    from shared.db.session import get_engine

    engine = get_engine()
    data_dir = args.data_dir or default_data_dir()
    if args.all:
        with engine.connect() as conn:
            todo = select_jobs(conn, args.member, REASON_ALL, args.sample)
        print(f"logs to read: {len(todo)}", flush=True)
        t0 = time.monotonic()
        run_stage(engine, todo, data_dir, args.workers, keep_ids=failed_test_ids(engine))
        secs = time.monotonic() - t0
        print(f"read {len(todo)} logs in {secs:.0f}s ({len(todo) / max(secs, 1e-9):.1f}/s)")
        for key, value in report(engine, args.member).items():
            print(f"{key}: {value}")
        return 0
    with engine.connect() as conn:
        seeds = select_jobs(conn, args.member, REASON_SEED)
    if args.limit:
        seeds = seeds[: args.limit]
    print(f"seed jobs (failed attempts) to read: {len(seeds)}", flush=True)
    run_stage(engine, seeds, data_dir, args.workers)
    with engine.connect() as conn:
        siblings = select_jobs(conn, args.member, REASON_SIBLING)
    print(f"sibling jobs (same job, same head_sha) to read: {len(siblings)}", flush=True)
    run_stage(engine, siblings, data_dir, args.workers)
    for key, value in report(engine, args.member).items():
        print(f"{key}: {value}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
