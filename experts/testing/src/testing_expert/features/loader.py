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

from ._types import FileChange, JobTarget
from .history import SeriesIndex
from .repo import (
    DurationIndex,
    FrameworkTimeline,
    framework_from_package_json,
)
from .settings import FeatureSettings, find_config_file

logger = logging.getLogger(__name__)

SeriesKey = tuple[int, str]  # (repo_id, workflow_path + "\x1f" + job key)

# DEFAULT: job key, else the job name with its "(matrix)" suffix cut off, so `Tests (node 22)` and
# `Tests (node 24)` share one history. Mirrors the brief's "job_key, fall back to job name".
SERIES_KEY_SQL = "coalesce(j.job_key, regexp_replace(j.name, '\\s*\\(.*$', ''))"

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
               (select count(*) from raw.jobs m where m.run_id = j.run_id and m.attempt = j.attempt)
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
