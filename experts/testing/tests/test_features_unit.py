"""Pure feature groups and the history index: no database."""
# ruff: noqa: E501

from __future__ import annotations

import pytest
from testing_expert.features import (
    build_row,
    change_size,
    config_change,
    dependencies,
    describe,
    feature_names,
    history,
    load_descriptions,
    repo,
    run_setup,
    source_test_link,
)
from testing_expert.features._types import FileChange, JobTarget
from testing_expert.features.settings import get_settings

S = get_settings()
DAY = 86_400.0
NOW = 1_800_000_000.0


def fc(path: str, add: int, dele: int, *tags: str, patch: str | None = None) -> FileChange:
    return FileChange(path, add, dele, frozenset(tags), patch)


FILES = [
    fc("src/a.ts", 10, 2, "source"),
    fc("src/a.test.ts", 5, 0, "test"),
    fc("jest.config.js", 1, 1, "test_config"),
    fc("package.json", 2, 2, "package_manifest"),
    fc("README.md", 3, 0, "docs"),
]


def target(**kw: object) -> JobTarget:
    base: dict[str, object] = dict(
        run_id=1, attempt=1, job_id=1, repo_id=1, cutoff=NOW, head_sha="x", event="pull_request",
        branch="feat", default_branch="main", runner_os="Linux", workflow_path="ci.yml",
        series_key="test", matrix_size=3, jobs_in_run=5,
    )  # fmt: skip
    base.update(kw)
    return JobTarget(**base)  # type: ignore[arg-type]


def test_change_size_split() -> None:
    f = change_size.change_size(FILES, S)
    assert f["cs_files_changed"] == 5
    assert (f["cs_lines_added"], f["cs_lines_deleted"]) == (21, 5)
    assert (f["cs_source_files"], f["cs_test_files"], f["cs_config_files"]) == (1, 1, 2)
    assert f["cs_config_lines"] == 6 and f["cs_source_lines"] == 12


def test_change_size_empty() -> None:
    assert set(change_size.change_size([], S).values()) == {0}


def test_source_test_link() -> None:
    assert source_test_link.source_test_link(FILES[:1])["stl_source_without_tests"] == 1
    both = source_test_link.source_test_link(FILES)
    assert both["stl_source_without_tests"] == 0 and both["stl_test_to_source_ratio"] == 1.0
    only_tests = source_test_link.source_test_link(FILES[1:2])
    assert (
        only_tests["stl_only_tests_changed"] == 1 and only_tests["stl_test_to_source_ratio"] is None
    )


def test_config_features() -> None:
    assert config_change.config_features(FILES)["tc_test_config_changed"] == 1
    assert config_change.config_features(FILES[:2])["tc_test_config_files"] == 0


PKG_PATCH = """@@ -10,7 +10,8 @@
   "devDependencies": {
-    "jest": "^29.6.0",
+    "jest": "^29.7.0",
-    "left-pad": "1.0.0",
+    "left-pad": "1.1.0",
+    "sinon": "^17.0.0",
     "eslint": "9.0.0"
"""


def test_dependency_version_bump_detected() -> None:
    assert dependencies.test_lib_changes(PKG_PATCH, S) == (1, 1)  # jest bumped, sinon added
    f = dependencies.dependency_features(
        [
            fc("package.json", 3, 2, "package_manifest", patch=PKG_PATCH),
            fc("yarn.lock", 9, 9, "lockfile"),
        ],
        S,
    )
    assert f == {
        "td_manifest_changed": 1,
        "td_lockfile_changed": 1,
        "td_test_lib_bumped": 1,
        "td_test_lib_added_or_removed": 1,
    }


def test_dependency_no_patch_is_safe() -> None:
    f = dependencies.dependency_features([fc("package.json", 1, 1, "package_manifest")], S)
    assert f["td_manifest_changed"] == 1 and f["td_test_lib_bumped"] == 0


def test_run_setup() -> None:
    f = run_setup.run_setup(target())
    assert f["rs_is_pull_request"] == 1 and f["rs_on_default_branch"] == 0
    assert run_setup.run_setup(target(event="push", branch="main"))["rs_on_default_branch"] == 1
    assert run_setup.run_setup(target(branch=None))["rs_on_default_branch"] is None


def series(*events: tuple[float, int, int]) -> history.SeriesIndex:
    return history.SeriesIndex((ts, fail, rerun, i) for i, (ts, fail, rerun) in enumerate(events))


def test_history_windows_and_rates() -> None:
    idx = series(
        (NOW - 100 * DAY, 1, 0),  # outside 90d
        (NOW - 40 * DAY, 0, 0),
        (NOW - 20 * DAY, 1, 1),
        (NOW - 3 * DAY, 1, 0),
        (NOW - 1 * DAY, 0, 0),
    )
    f = idx.features(NOW, S)
    assert (f["h_runs_7d"], f["h_fail_rate_7d"]) == (2, 0.5)
    assert (f["h_runs_30d"], f["h_fail_rate_30d"]) == (3, 2 / 3)
    assert f["h_runs_90d"] == 4 and f["h_n_prior"] == 5
    assert f["h_fail_rate_all"] == 0.6
    assert f["h_consecutive_failures"] == 0 and f["h_last_failed"] == 0
    assert f["h_reruns_30d"] == 1
    assert f["h_days_since_last_failure"] == pytest.approx(3.0)


def test_history_consecutive_and_flip() -> None:
    steady = series(*[(NOW - (10 - i) * DAY, 1, 0) for i in range(5)])
    f = steady.features(NOW, S)
    assert f["h_consecutive_failures"] == 5 and f["h_weighted_flip_rate"] == 0.0
    alternating = series(*[(NOW - (10 - i) * DAY, i % 2, 0) for i in range(6)])
    assert alternating.features(NOW, S)["h_weighted_flip_rate"] == 1.0
    # a recent flip weighs more than an old one
    old = series(
        (NOW - 5 * DAY, 1, 0),
        (NOW - 4 * DAY, 0, 0),
        *[(NOW - i * DAY / 10, 0, 0) for i in range(1, 6)],
    )
    new = series(
        *[(NOW - (10 - i) * DAY, 0, 0) for i in range(5)],
        (NOW - DAY / 2, 1, 0),
        (NOW - DAY / 4, 0, 0),
    )
    new_rate = new.features(NOW, S)["h_weighted_flip_rate"]
    old_rate = old.features(NOW, S)["h_weighted_flip_rate"]
    assert isinstance(new_rate, float) and isinstance(old_rate, float)
    assert new_rate > old_rate


def test_history_empty() -> None:
    f = history.EMPTY_SERIES.features(NOW, S)
    assert f["h_n_prior"] == 0 and f["h_fail_rate_7d"] is None and f["h_weighted_flip_rate"] is None
    assert f["h_consecutive_failures"] == 0 and f["h_last_failed"] is None


def test_history_cutoff_is_strict() -> None:
    idx = series((NOW - DAY, 1, 0), (NOW, 1, 0), (NOW + DAY, 1, 0))
    assert idx.features(NOW, S)["h_n_prior"] == 1  # the event exactly at the cutoff is excluded


def test_history_ignores_later_events() -> None:
    """Leakage guard (pure): adding events at or after the cutoff changes nothing."""
    base = [(NOW - i * DAY, i % 3 == 0, i % 4 == 0) for i in range(1, 60)]
    later = [(NOW + i * 3600.0, 1, 1) for i in range(0, 30)]
    a = series(*[(t, int(f), int(r)) for t, f, r in base]).features(NOW, S)
    b = series(*[(t, int(f), int(r)) for t, f, r in base + later]).features(NOW, S)
    assert a == b


def test_repo_durations_and_framework() -> None:
    d = repo.DurationIndex(
        [(NOW - DAY, 600.0), (NOW - 2 * DAY, 1200.0), (NOW - 200 * DAY, 9999.0), (NOW, 5.0)]
    )
    assert d.average_minutes(NOW, 90) == (15.0, 2)
    assert repo.DurationIndex(()).average_minutes(NOW, 90) == (None, 0)
    pkg = '{"devDependencies": {"jest": "1", "mocha": "2"}}'
    assert repo.framework_from_package_json(pkg, S.frameworks) == "jest"
    assert repo.framework_from_package_json("not json", S.frameworks) is None
    assert repo.framework_from_name("unit-ava", S.frameworks) == "ava"
    assert repo.framework_from_name("java", S.frameworks) is None
    tl = repo.FrameworkTimeline(
        [(NOW - 10 * DAY, "mocha"), (NOW + DAY, "jest"), (NOW - 5 * DAY, None)]
    )
    assert (
        tl.at(NOW) == "mocha"
    )  # newest known snapshot without a framework is skipped, later ignored


def test_build_row_has_every_named_feature() -> None:
    row = build_row(
        target(), FILES, series((NOW - DAY, 1, 0)), repo.EMPTY_DURATIONS, repo.EMPTY_TIMELINE, S
    )
    assert tuple(row) == feature_names(S)


def test_every_feature_has_a_description() -> None:
    assert set(feature_names(S)) <= set(load_descriptions())
    assert set(load_descriptions()) <= set(feature_names(S))


def test_describe_fills_templates() -> None:
    assert (
        describe("h_fail_rate_7d", 0.25) == "This job failed in 25% of its runs in the last 7 days"
    )
    assert describe("h_fail_rate_7d", None) == "This job has no runs in the last 7 days"
    assert describe("nope", 3) == "nope = 3"
