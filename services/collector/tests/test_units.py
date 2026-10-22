"""Pure-logic tests for the collector: file tags, job keys, windows, log policy, gzip."""

from __future__ import annotations

import gzip
from datetime import UTC, datetime, timedelta

import pytest
from collector.backfill import runner_os, workflow_path_of
from collector.file_tags import glob_to_regex, load_file_tagger
from collector.job_keys import map_job_key
from collector.policy import date_windows, gzip_bytes, should_fetch_log

# ---- file tags ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("pattern", "path", "expected"),
    [
        ("**/*.test.*", "a.test.js", True),
        ("**/*.test.*", "src/deep/a.test.ts", True),
        ("**/*.test.*", "src/a.js", False),
        ("test/**", "test/a/b.js", True),
        ("test/**", "src/test/a.js", False),
        ("vercel.json", "vercel.json", True),
        ("vercel.json", "app/vercel.json", False),
        (".github/workflows/*.yml", ".github/workflows/ci.yml", True),
        (".github/workflows/*.yml", ".github/workflows/sub/ci.yml", False),
        ("**/.mocharc*", ".mocharc.yml", True),
        ("**/Dockerfile*", "svc/Dockerfile.dev", True),
    ],
)
def test_glob_to_regex(pattern: str, path: str, expected: bool) -> None:
    assert bool(glob_to_regex(pattern).fullmatch(path)) is expected


@pytest.mark.parametrize(
    ("path", "tags"),
    [
        ("package-lock.json", {"lockfile"}),
        ("web/pnpm-lock.yaml", {"lockfile"}),
        ("package.json", {"package_manifest"}),
        ("src/a.js", {"source"}),
        ("src/a.test.js", {"test"}),
        ("test/helpers/db.js", {"test"}),
        ("jest.config.js", {"test_config", "source"}),
        ("vite.config.ts", {"bundler_config", "source"}),
        (".github/workflows/test.yml", {"workflow"}),
        ("Dockerfile", {"dockerfile"}),
        ("README.md", {"docs"}),
        ("assets/logo.png", {"other"}),
    ],
)
def test_tags_for(path: str, tags: set[str]) -> None:
    assert load_file_tagger().tags_for(path) == tags


def test_snapshot_and_lockfile_decisions() -> None:
    tagger = load_file_tagger()
    assert tagger.max_patch_bytes == 1_048_576
    assert tagger.is_lockfile("yarn.lock") and not tagger.is_lockfile("package.json")
    for path in ("package.json", "yarn.lock", ".github/workflows/ci.yml", "tsconfig.json"):
        assert tagger.wants_snapshot(path), path
    for path in ("src/a.js", "test/a.js", "README.md", "logo.png"):
        assert not tagger.wants_snapshot(path), path


# ---- job key mapping ------------------------------------------------------------------------

WORKFLOW = """
name: CI
jobs:
  test:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        node: [18, 20]
  lint:
    name: Lint sources
    runs-on: ubuntu-latest
  build:
    name: Build on ${{ matrix.os }}
    strategy:
      matrix:
        os: [ubuntu-latest, windows-latest]
  call:
    uses: ./.github/workflows/reusable.yml
"""


@pytest.mark.parametrize(
    ("job_name", "key"),
    [
        ("test", "test"),
        ("test (18)", "test"),
        ("test (ubuntu-latest, 20)", "test"),
        ("Lint sources", "lint"),
        ("lint", "lint"),
        ("Build on windows-latest", "build"),
        ("call / inner job", "call"),
        ("deploy", None),
        ("tes", None),
        ("Lint sources extra", None),
    ],
)
def test_map_job_key(job_name: str, key: str | None) -> None:
    assert map_job_key(job_name, WORKFLOW) == key


def test_map_job_key_is_null_when_unsure() -> None:
    ambiguous = "jobs:\n  a:\n    name: ${{ matrix.x }}\n  b:\n    name: ${{ matrix.y }}\n"
    assert map_job_key("anything", ambiguous) is None
    assert map_job_key("test", None) is None
    assert map_job_key("test", "not: [valid") is None
    assert map_job_key("test", "- just a list") is None


# ---- windows --------------------------------------------------------------------------------

NOW = datetime(2026, 10, 19, 12, 0, 0, tzinfo=UTC)


def test_date_windows_cover_the_range_without_gaps_or_overlap() -> None:
    windows = date_windows(NOW, 90, 7)
    assert windows[0][1] == NOW  # newest first, ends now
    assert windows[-1][0] == datetime(2026, 7, 21, tzinfo=UTC)  # midnight, 90 days back
    ordered = list(reversed(windows))
    for (_, end), (start, _) in zip(ordered, ordered[1:], strict=False):
        assert start - end == timedelta(seconds=1)
    assert all(start <= end for start, end in windows)
    assert len(windows) == 13


def test_date_windows_edge_cases() -> None:
    assert date_windows(NOW, 1, 7) == [(datetime(2026, 10, 18, tzinfo=UTC), NOW)]
    midnight = datetime(2026, 10, 19, tzinfo=UTC)
    assert date_windows(midnight, 1, 1)[0] == (midnight, midnight)
    with pytest.raises(ValueError, match="at least 1"):
        date_windows(NOW, 0, 7)


def test_date_windows_are_deterministic() -> None:
    assert date_windows(NOW, 90, 7) == date_windows(NOW + timedelta(microseconds=5), 90, 7)


# ---- log policy and gzip --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "conclusion", "relevant", "fetch"),
    [
        ("completed", "failure", True, True),
        ("completed", "failure", False, True),  # every failed job
        ("completed", "timed_out", False, True),
        ("completed", "success", True, True),  # passing job in a relevant workflow
        ("completed", "success", False, False),
        ("completed", "cancelled", True, False),
        ("completed", "skipped", True, False),
        ("in_progress", None, True, False),
        ("queued", None, True, False),
    ],
)
def test_log_policy(status: str, conclusion: str | None, relevant: bool, fetch: bool) -> None:
    assert should_fetch_log(status, conclusion, relevant) is fetch


def test_gzip_is_deterministic_and_round_trips() -> None:
    data = b"line one\nline two\n" * 1000
    first, second = gzip_bytes(data), gzip_bytes(data)
    assert first == second
    assert gzip.decompress(first) == data
    assert first[4:8] == b"\x00\x00\x00\x00"  # mtime field is zero
    assert b"line one" not in first[:10]  # no file name in the header


# ---- small helpers --------------------------------------------------------------------------


def test_workflow_path_of_strips_ref_suffix() -> None:
    assert workflow_path_of({"path": ".github/workflows/ci.yml@refs/pull/7/merge"}) == (
        ".github/workflows/ci.yml"
    )
    assert workflow_path_of({"path": ".github/workflows/ci.yml"}) == ".github/workflows/ci.yml"
    assert workflow_path_of({}) == ""


def test_runner_os_from_labels() -> None:
    assert runner_os(["ubuntu-22.04"]) == "Linux"
    assert runner_os(["self-hosted", "windows-2022"]) == "Windows"
    assert runner_os(["macos-14"]) == "macOS"
    assert runner_os(["custom"]) is None
    assert runner_os([]) is None
