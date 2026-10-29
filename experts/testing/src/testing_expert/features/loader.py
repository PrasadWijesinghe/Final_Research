"""Reads the `raw` tables for the feature pipeline. Read-only, set-based SQL, no writes.

Every history query carries `completed_at < to_timestamp(max cutoff)`, so a row finished at or after
a cutoff is never even loaded for that batch; the pure code then applies each row's own cutoff.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Iterable, Sequence
from functools import lru_cache
from typing import Any

import psycopg

from shared.file_tags import FileTagger

from ._types import ChangedTestFile, FileChange, JobTarget
from .history import SeriesIndex
from .history_tests import ResultRow, TestHistory
from .repo import (
    DurationIndex,
    FrameworkTimeline,
    framework_from_package_json,
)
from .settings import FeatureSettings, find_config_file
from .test_changes import MAX_PATCH_CHARS, is_code_path

logger = logging.getLogger(__name__)

SeriesKey = tuple[int, str]  # (repo_id, workflow_path + "\x1f" + job key)
OsSeriesKey = tuple[int, str, str]  # the same plus the normalised runner OS

# DEFAULT: job key, else the job name with its "(matrix)" suffix cut off, so `Tests (node 22)` and
# `Tests (node 24)` share one history. Mirrors the brief's "job_key, fall back to job name".
SERIES_KEY_SQL = "coalesce(j.job_key, regexp_replace(j.name, '\\s*\\(.*$', ''))"

# DEFAULT: runner OS = runner_os, else the runs-on labels, else the job name ("x on windows")
# or a resolved matrix name); anything else (e.g. `${{ matrix.os }}` left unresolved) = 'unknown'.
# Raw `jobs.runner_os` is Linux / Windows / macOS or null; labels like ubuntu-latest, macos-15.
_OS_BLOB = "(coalesce(j.runner_os, '') || ' ' || array_to_string(j.labels, ' '))"
OS_SQL = f"""(case
    when {_OS_BLOB} ~* 'windows' then 'windows'
    when {_OS_BLOB} ~* 'mac|darwin' then 'macos'
    when {_OS_BLOB} ~* 'ubuntu|linux' then 'linux'
    when j.name !~ '[$][{{][{{]' and j.name ~* 'windows' then 'windows'
    when j.name !~ '[$][{{][{{]' and j.name ~* 'mac(os)?([^a-z]|$)' then 'macos'
    when j.name !~ '[$][{{][{{]' and j.name ~* 'ubuntu|linux' then 'linux'
    else 'unknown' end)"""

_JOIN = "raw.jobs j join raw.workflow_runs w on w.run_id = j.run_id and w.attempt = j.attempt"
_FILE_WITH_PATCH = r"(^|/)(package\.json|vite\.config\.[^/]*)$"
_SHA_CHUNK = 5_000


@lru_cache(maxsize=1)
def _tagger() -> FileTagger:
    # DEFAULT: config/file_tags.yaml found above this file (installed `shared` has no config/).
    return FileTagger.load(find_config_file().with_name("file_tags.yaml"))


@lru_cache(maxsize=500_000)
def _tags(path: str, content: str | None) -> frozenset[str]:
    return _tagger().tags_for(path, content)


def load_targets(
    conn: psycopg.Connection[Any], keys: Sequence[tuple[int, int, int]]
) -> list[JobTarget]:
    """Look up run and job facts for (run_id, attempt, job_id) keys; unknown keys are skipped."""
    if not keys:
        return []
    sql = f"""
        select j.run_id, j.attempt, j.job_id, w.repo_id, extract(epoch from w.created_at),
               w.head_sha, w.event, w.head_branch, r.default_branch, j.runner_os, w.workflow_path,
               {SERIES_KEY_SQL},
               (select count(*) from raw.jobs m where m.run_id = j.run_id and m.attempt = j.attempt
                  and coalesce(m.job_key, regexp_replace(m.name, '\\s*\\(.*$', ''))
                      = {SERIES_KEY_SQL}),
               (select count(*) from raw.jobs m
                 where m.run_id = j.run_id and m.attempt = j.attempt),
               {OS_SQL}
        from {_JOIN} join raw.repos r on r.id = w.repo_id
        join unnest(%s::bigint[], %s::int[], %s::bigint[]) as k(run_id, attempt, job_id)
          on k.run_id = j.run_id and k.attempt = j.attempt and k.job_id = j.job_id
    """
    runs, attempts, jobs = ([k[i] for k in keys] for i in range(3))
    with conn.cursor() as cur:
        cur.execute(sql, (runs, attempts, jobs))
        return [
            JobTarget(
                run_id=r[0],
                attempt=r[1],
                job_id=r[2],
                repo_id=r[3],
                cutoff=float(r[4]),
                head_sha=r[5],
                event=r[6],
                branch=r[7],
                default_branch=r[8],
                runner_os=r[9],
                workflow_path=r[10],
                series_key=r[11],
                matrix_size=int(r[12]),
                jobs_in_run=int(r[13]),
                os_name=r[14],
            )
            for r in cur.fetchall()
        ]


def load_files(
    conn: psycopg.Connection[Any], shas: Iterable[tuple[int, str]]
) -> dict[tuple[int, str], list[FileChange]]:
    """Tagged changed files per (repo_id, sha); patches only for package.json and vite config."""
    wanted = sorted(set(shas))
    out: dict[tuple[int, str], list[FileChange]] = {k: [] for k in wanted}
    sql = f"""
        select cf.repo_id, cf.sha, cf.path, cf.additions, cf.deletions,
               case when cf.path ~ '{_FILE_WITH_PATCH}' then cf.patch end
        from raw.commit_files cf
        join unnest(%s::bigint[], %s::text[]) as k(repo_id, sha)
          on k.repo_id = cf.repo_id and k.sha = cf.sha
    """
    for i in range(0, len(wanted), _SHA_CHUNK):
        chunk = wanted[i : i + _SHA_CHUNK]
        with conn.cursor() as cur:
            cur.execute(sql, ([c[0] for c in chunk], [c[1] for c in chunk]))
            for repo_id, sha, path, add, dele, patch in cur.fetchall():
                is_vite = "vite.config" in path
                tags = _tags(path, patch if is_vite else None)
                out[(repo_id, sha)].append(
                    FileChange(
                        path=path,
                        additions=add or 0,
                        deletions=dele or 0,
                        tags=tags,
                        patch=patch if "package_manifest" in tags else None,
                    )
                )
    return out


def load_series(
    conn: psycopg.Connection[Any],
    repo_ids: Sequence[int],
    before: float,
    only: Sequence[tuple[int, str, str]] | None = None,
) -> dict[SeriesKey, SeriesIndex]:
    """Finished job attempts per series, completed before `before` (epoch).

    `only` narrows to (repo_id, workflow_path, series_key) triples (used for single-job serving).
    """
    sql = f"""
        select w.repo_id, w.workflow_path || E'\\x1f' || {SERIES_KEY_SQL} as skey,
               extract(epoch from j.completed_at), (j.conclusion = 'failure')::int,
               (j.attempt > 1)::int, j.job_id
        from {_JOIN}
        where w.repo_id = any(%s) and j.completed_at is not null
          and j.conclusion in ('success', 'failure') and j.completed_at < to_timestamp(%s)
    """
    params: list[Any] = [list(repo_ids), before]
    if only is not None:
        sql += (
            " and (w.repo_id::text || E'\\x1f' || w.workflow_path || E'\\x1f' || " + SERIES_KEY_SQL
        )
        sql += ") = any(%s)"
        params.append([f"{r}\x1f{p}\x1f{k}" for r, p, k in only])
    grouped: dict[SeriesKey, list[tuple[float, int, int, int]]] = defaultdict(list)
    with conn.cursor() as cur:
        cur.execute(sql, params)
        for repo_id, skey, ts, failed, rerun, job_id in cur.fetchall():
            grouped[(repo_id, skey)].append((float(ts), failed, rerun, job_id))
    return {k: SeriesIndex(v) for k, v in grouped.items()}


def load_series_by_os(
    conn: psycopg.Connection[Any],
    repo_ids: Sequence[int],
    before: float,
    only: Sequence[tuple[int, str, str]] | None = None,
) -> dict[OsSeriesKey, SeriesIndex]:
    """Like `load_series`, but one series per (job key, runner OS) for the `h_os_*` features."""
    sql = f"""
        select w.repo_id, w.workflow_path || E'\\x1f' || {SERIES_KEY_SQL} as skey, {OS_SQL},
               extract(epoch from j.completed_at), (j.conclusion = 'failure')::int,
               (j.attempt > 1)::int, j.job_id
        from {_JOIN}
        where w.repo_id = any(%s) and j.completed_at is not null
          and j.conclusion in ('success', 'failure') and j.completed_at < to_timestamp(%s)
    """
    params: list[Any] = [list(repo_ids), before]
    if only is not None:
        sql += (
            " and (w.repo_id::text || E'\\x1f' || w.workflow_path || E'\\x1f' || " + SERIES_KEY_SQL
        )
        sql += ") = any(%s)"
        params.append([f"{r}\x1f{p}\x1f{k}" for r, p, k in only])
    grouped: dict[OsSeriesKey, list[tuple[float, int, int, int]]] = defaultdict(list)
    with conn.cursor() as cur:
        cur.execute(sql, params)
        for repo_id, skey, os_name, ts, failed, rerun, job_id in cur.fetchall():
            grouped[(repo_id, skey, os_name)].append((float(ts), failed, rerun, job_id))
    return {k: SeriesIndex(v) for k, v in grouped.items()}


def load_test_history(
    conn: psycopg.Connection[Any],
    repo_ids: Sequence[int],
    before: float,
    only: Sequence[tuple[int, str, str]] | None = None,
) -> dict[SeriesKey, TestHistory]:
    """Parsed test results per series, of attempts completed before `before` (epoch).

    Reads `testing.log_parses` / `testing.test_results` (P4.2). Attempts whose log was parsed but
    kept no rows still count as parsed runs. Streamed with a server-side cursor.
    """
    sql = f"""
        select w.repo_id, w.workflow_path || E'\\x1f' || {SERIES_KEY_SQL} as skey, j.job_id,
               extract(epoch from j.completed_at), t.test_id, t.file, t.outcome, t.granularity,
               (p.granularity = 'test')::int
        from testing.log_parses p
        join raw.jobs j on j.job_id = p.job_id
        join raw.workflow_runs w on w.run_id = j.run_id and w.attempt = j.attempt
        left join testing.test_results t on t.job_id = p.job_id
        where p.is_test_job and w.repo_id = any(%s) and j.completed_at is not null
          and j.conclusion in ('success', 'failure') and j.completed_at < to_timestamp(%s)
    """
    params: list[Any] = [list(repo_ids), before]
    if only is not None:
        sql += (
            " and (w.repo_id::text || E'\\x1f' || w.workflow_path || E'\\x1f' || "
            + SERIES_KEY_SQL
            + ") = any(%s)"
        )
        params.append([f"{r}\x1f{p}\x1f{k}" for r, p, k in only])
    rows: dict[SeriesKey, list[ResultRow]] = defaultdict(list)
    empty: dict[SeriesKey, list[tuple[float, int, int]]] = defaultdict(list)
    with conn.cursor(name="ht_load") as cur:
        cur.itersize = 50_000
        cur.execute(sql, params)
        for repo_id, skey, job_id, ts, tid, file, outcome, gran, lvl in cur:
            key = (repo_id, skey)
            if tid is None:
                empty[key].append((float(ts), job_id, lvl))
                continue
            rows[key].append(
                ResultRow(job_id, float(ts), tid, file, outcome == "failed", gran, lvl)
            )
    return {k: TestHistory(rows.get(k, ()), empty.get(k, ())) for k in {*rows, *empty}}


def load_job_test_history(
    conn: psycopg.Connection[Any],
    only: Sequence[tuple[int, str, str]],
    before: float,
) -> dict[SeriesKey, TestHistory]:
    """Job-level `ht_*` history of a few series, fast (serving); touched-file numbers are not built.

    Gives the same job-level numbers as `load_test_history` (`ht_test_level`, `ht_parsed_runs_*`,
    `ht_last_run_failed_tests`, `ht_persistent_failing_tests`, `ht_failed_tests_*`,
    `ht_flaky_tests_*`) but reads far less. The full loader streams every stored result row of the
    series (hundreds of thousands for a big matrix job). Those numbers only need: (1) the list of
    parsed runs (cheap), (2) the failed rows of runs in the last 90 days and of the last two runs,
    (3) for each test that failed in the window, its newest passing event (to tell "flaky" from
    "always failing"). So it runs three small queries. The per-file numbers (`ht_touched_*`)
    need every row, so a bundle that uses them must call `load_test_history`.

    # DEFAULT: window = 90 days, the longest `ht_` window (`history_tests.WINDOWS_DAYS`).
    """
    if not only:
        return {}
    window = 90 * 86_400.0
    repos = [o[0] for o in only]
    paths = [o[1] for o in only]
    keys = [o[2] for o in only]
    runs_sql = f"""
        select w.repo_id, w.workflow_path || E'\\x1f' || {SERIES_KEY_SQL} as skey, j.job_id,
               extract(epoch from j.completed_at), (p.granularity = 'test')::int
        from unnest(%s::bigint[], %s::text[], %s::text[]) as k(repo_id, wp, key)
        join raw.workflow_runs w on w.repo_id = k.repo_id and w.workflow_path = k.wp
        join raw.jobs j on j.run_id = w.run_id and j.attempt = w.attempt
        join testing.log_parses p on p.job_id = j.job_id
        where p.is_test_job and j.completed_at is not null
          and j.conclusion in ('success', 'failure') and j.completed_at < to_timestamp(%s)
          and {SERIES_KEY_SQL} = k.key
    """
    runs: dict[SeriesKey, list[tuple[float, int, int]]] = defaultdict(list)
    job_series: dict[int, SeriesKey] = {}
    with conn.cursor() as cur:
        cur.execute(runs_sql, [repos, paths, keys, before])
        for repo_id, skey, job_id, ts, lvl in cur.fetchall():
            runs[(repo_id, skey)].append((float(ts), job_id, lvl))
            job_series[job_id] = (repo_id, skey)
    needed: set[int] = set()
    in_window: set[int] = set()
    for rs in runs.values():
        rs.sort(key=lambda r: (r[0], r[1]))
        needed.update(r[1] for r in rs[-2:])
        for ts, job_id, _lvl in rs:
            if ts >= before - window:
                needed.add(job_id)
                in_window.add(job_id)
    ts_of = {r[1]: r[0] for rs in runs.values() for r in rs}
    lvl_of = {r[1]: r[2] for rs in runs.values() for r in rs}
    rows: dict[SeriesKey, list[ResultRow]] = defaultdict(list)
    failing: set[str] = set()
    if needed:
        with conn.cursor() as cur:
            cur.execute(
                "select job_id, test_id, file, granularity from testing.test_results "
                "where job_id = any(%s) and outcome = 'failed'",
                [sorted(needed)],
            )
            for job_id, tid, file, gran in cur.fetchall():
                rows[job_series[job_id]].append(
                    ResultRow(job_id, ts_of[job_id], tid, file, True, gran, lvl_of[job_id])
                )
                if job_id in in_window:
                    failing.add(tid)
    if failing and in_window:
        with conn.cursor() as cur:
            cur.execute(
                "select distinct on (r.test_id) r.test_id, r.job_id from testing.test_results r "
                "join raw.jobs j on j.job_id = r.job_id "
                "where r.test_id = any(%s) and r.job_id = any(%s) and r.outcome <> 'failed' "
                "order by r.test_id, j.completed_at desc",
                [sorted(failing), sorted(in_window)],
            )
            for tid, job_id in cur.fetchall():
                rows[job_series[job_id]].append(
                    ResultRow(job_id, ts_of[job_id], tid, None, False, "test", lvl_of[job_id])
                )
    return {k: TestHistory(rows.get(k, ()), rs) for k, rs in runs.items()}


def os_series_key_of(t: JobTarget) -> OsSeriesKey:
    """The dict key under which `load_series_by_os` stores this target's series."""
    return (t.repo_id, f"{t.workflow_path}\x1f{t.series_key}", t.os_name)


def load_test_files(
    conn: psycopg.Connection[Any], shas: Iterable[tuple[int, str]]
) -> dict[tuple[int, str], list[ChangedTestFile]]:
    """Changed JS/TS test files per (repo_id, sha) with their patch (None when GitHub sent none)."""
    wanted = sorted(set(shas))
    out: dict[tuple[int, str], list[ChangedTestFile]] = {k: [] for k in wanted}
    sql = """
        select cf.repo_id, cf.sha, cf.path, case when length(cf.patch) <= %s then cf.patch end
        from raw.commit_files cf
        join unnest(%s::bigint[], %s::text[]) as k(repo_id, sha)
          on k.repo_id = cf.repo_id and k.sha = cf.sha
        where cf.path ~* '\\.[cm]?[jt]sx?$' and coalesce(cf.status, '') <> 'removed'
          and not cf.patch_truncated
    """
    for i in range(0, len(wanted), _SHA_CHUNK):
        chunk = wanted[i : i + _SHA_CHUNK]
        with conn.cursor() as cur:
            cur.execute(sql, (MAX_PATCH_CHARS, [c[0] for c in chunk], [c[1] for c in chunk]))
            for repo_id, sha, path, patch in cur.fetchall():
                if is_code_path(path) and "test" in _tags(path, None):
                    out[(repo_id, sha)].append(ChangedTestFile(path, patch))
    return out


def series_key_of(t: JobTarget) -> SeriesKey:
    """The dict key under which `load_series` stores this target's series."""
    return (t.repo_id, f"{t.workflow_path}\x1f{t.series_key}")


def load_durations(
    conn: psycopg.Connection[Any], repo_ids: Sequence[int], before: float, settings: FeatureSettings
) -> dict[int, DurationIndex]:
    """Durations of finished test-like jobs per repo, completed before `before` (epoch)."""
    sql = f"""
        select w.repo_id, extract(epoch from j.completed_at),
               extract(epoch from j.completed_at - j.started_at)
        from {_JOIN}
        where w.repo_id = any(%s) and j.completed_at is not null and j.started_at is not null
          and j.conclusion in ('success', 'failure') and j.completed_at < to_timestamp(%s)
          and {SERIES_KEY_SQL} ~* %s
          and j.completed_at - j.started_at > interval '0'
          and j.completed_at - j.started_at <= make_interval(mins => %s)
    """
    grouped: dict[int, list[tuple[float, float]]] = defaultdict(list)
    with conn.cursor() as cur:
        cur.execute(
            sql, (list(repo_ids), before, settings.test_job_regex, settings.max_job_minutes)
        )
        for repo_id, ts, secs in cur.fetchall():
            grouped[repo_id].append((float(ts), float(secs)))
    return {k: DurationIndex(v) for k, v in grouped.items()}


def load_frameworks(
    conn: psycopg.Connection[Any], repo_ids: Sequence[int], settings: FeatureSettings
) -> dict[int, FrameworkTimeline]:
    """Framework per repo from package.json snapshots, dated by the first run on that commit."""
    sql = """
        select s.repo_id, extract(epoch from min(w.created_at)), s.content
        from raw.file_snapshots s
        join raw.workflow_runs w on w.repo_id = s.repo_id and w.head_sha = s.sha
        where s.repo_id = any(%s) and s.path = 'package.json' and s.content is not null
        group by s.repo_id, s.sha, s.content
    """
    grouped: dict[int, list[tuple[float, str | None]]] = defaultdict(list)
    with conn.cursor() as cur:
        cur.execute(sql, (list(repo_ids),))
        for repo_id, ts, content in cur.fetchall():
            grouped[repo_id].append(
                (float(ts), framework_from_package_json(content, settings.frameworks))
            )
    return {k: FrameworkTimeline(v) for k, v in grouped.items()}
