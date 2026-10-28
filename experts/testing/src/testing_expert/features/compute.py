"""One feature row per (run_id, attempt, job_id): the single entry point for training and serving.

`compute_features` (one job, for serving) and `compute_features_batch` (many, for the dataset
builder) run the same pure code in `build_row`; they differ only in how much history they load.
History features use only data completed strictly before the run's `created_at` (history_cutoff).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Sequence
from typing import Any

import psycopg

from . import (
    change_size,
    config_change,
    dependencies,
    history,
    repo,
    run_setup,
    source_test_link,
    test_changes,
)
from ._types import ChangedTestFile, FeatureDict, FileChange, JobTarget
from .history import EMPTY_SERIES, SeriesIndex
from .repo import EMPTY_DURATIONS, EMPTY_TIMELINE, DurationIndex, FrameworkTimeline
from .settings import FeatureSettings, get_settings

logger = logging.getLogger(__name__)

JobKey = tuple[int, int, int]  # (run_id, attempt, job_id)

FEATURE_GROUPS = (
    "change_size",
    "source_test_link",
    "test_config",
    "test_deps",
    "history",
    "history_by_os",
    "test_changes",
    "run_setup",
    "repo",
)


def feature_names(settings: FeatureSettings | None = None) -> tuple[str, ...]:
    """All feature names in output order."""
    s = settings or get_settings()
    return (
        *change_size.FEATURE_NAMES,
        *source_test_link.FEATURE_NAMES,
        *config_change.FEATURE_NAMES,
        *dependencies.FEATURE_NAMES,
        *history.history_feature_names(s),
        *history.history_feature_names(s, history.OS_PREFIX),
        *test_changes.FEATURE_NAMES,
        *run_setup.FEATURE_NAMES,
        *repo.FEATURE_NAMES,
    )


def build_row(
    target: JobTarget,
    files: Sequence[FileChange],
    series: SeriesIndex,
    durations: DurationIndex,
    timeline: FrameworkTimeline,
    settings: FeatureSettings,
    os_series: SeriesIndex = EMPTY_SERIES,
    test_files: Sequence[ChangedTestFile] = (),
    test_change_row: FeatureDict | None = None,
) -> FeatureDict:
    """Pure: combine every group for one job attempt. No I/O.

    `test_change_row` lets a caller reuse the (slow) tree-sitter result for jobs of one commit.
    """
    framework = timeline.at(target.cutoff) or repo.framework_from_name(
        target.series_key, settings.frameworks
    )
    row: FeatureDict = {}
    row.update(change_size.change_size(files, settings))
    row.update(source_test_link.source_test_link(files))
    row.update(config_change.config_features(files))
    row.update(dependencies.dependency_features(files, settings))
    row.update(series.features(target.cutoff, settings))
    row.update(os_series.features(target.cutoff, settings, history.OS_PREFIX))
    row.update(
        test_change_row
        if test_change_row is not None
        else test_changes.what_changed_in_tests(test_files)
    )
    row.update(run_setup.run_setup(target))
    row.update(repo.repo_features(durations, target.cutoff, framework, settings))
    return row


def compute_features_batch(
    conn: psycopg.Connection[Any],
    keys: Sequence[JobKey],
    settings: FeatureSettings | None = None,
) -> dict[JobKey, FeatureDict]:
    """Feature rows for many job attempts with a handful of set-based queries (dataset builder)."""
    from . import loader

    s = settings or get_settings()
    t0 = time.perf_counter()
    targets = loader.load_targets(conn, keys)
    if not targets:
        return {}
    repo_ids = sorted({t.repo_id for t in targets})
    # Loading only rows completed before the latest cutoff is a speed-up, not the leakage guard:
    # each row's own cutoff is applied again in `build_row`.
    before = max(t.cutoff for t in targets)
    files = loader.load_files(conn, {(t.repo_id, t.head_sha) for t in targets})
    only = None
    if len(targets) <= 50:  # serving path: avoid scanning the whole repo history
        only = sorted({(t.repo_id, t.workflow_path, t.series_key) for t in targets})
    series = loader.load_series(conn, repo_ids, before, only)
    os_only = None if only is None else sorted({(r, p, k) for r, p, k in only})
    os_series = loader.load_series_by_os(conn, repo_ids, before, os_only)
    test_files = loader.load_test_files(conn, {(t.repo_id, t.head_sha) for t in targets})
    tw_cache: dict[tuple[int, str], FeatureDict] = {}  # one parse per commit, not per job
    durations = loader.load_durations(conn, repo_ids, before, s)
    timelines = loader.load_frameworks(conn, repo_ids, s)
    out: dict[JobKey, FeatureDict] = {}
    for t in targets:
        sha_key = (t.repo_id, t.head_sha)
        if sha_key not in tw_cache:
            tw_cache[sha_key] = test_changes.what_changed_in_tests(test_files.get(sha_key, []))
        out[(t.run_id, t.attempt, t.job_id)] = build_row(
            t,
            files.get((t.repo_id, t.head_sha), []),
            series.get(loader.series_key_of(t), EMPTY_SERIES),
            durations.get(t.repo_id, EMPTY_DURATIONS),
            timelines.get(t.repo_id, EMPTY_TIMELINE),
            s,
            os_series.get(loader.os_series_key_of(t), EMPTY_SERIES),
            test_change_row=tw_cache[sha_key],
        )
    logger.info(
        "features computed",
        extra={
            "rows": len(out),
            "repos": len(repo_ids),
            "seconds": round(time.perf_counter() - t0, 2),
        },
    )
    return out


def compute_features(
    conn: psycopg.Connection[Any],
    run_id: int,
    attempt: int,
    job_id: int,
    settings: FeatureSettings | None = None,
) -> FeatureDict | None:
    """Feature dict for one job attempt, or None when the job is not in `raw`."""
    return compute_features_batch(conn, [(run_id, attempt, job_id)], settings).get(
        (run_id, attempt, job_id)
    )
