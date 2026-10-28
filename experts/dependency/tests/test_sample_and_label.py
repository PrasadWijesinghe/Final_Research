"""Offline tests for the Dependency sample log labeler."""

import json
import logging
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import Mock

import pytest

from experts.dependency.research.prototype.sample_and_label import (
    MAX_JOB_LOG_BYTES,
    _control_client_config,
    _matched_pattern_ids,
    _select_distinct_failed_runs,
    classify_failed_run,
    sample_and_label_dataset,
    scan_dependency_candidates,
    scan_random_failure_controls,
)
from shared.github import GitHubClient, GitHubError, GitHubNotFoundError
from shared.sanitise import SanitiseResult


def _run(
    run_id: int,
    *,
    attempt: int = 1,
    repository: str = "owner/repo",
    commit_sha: str | None = None,
) -> dict[str, object]:
    return {
        "repository": repository,
        "run_id": run_id,
        "attempt": attempt,
        "status": "failure",
        "commit_sha": commit_sha or f"{run_id:040d}",
    }


def test_classify_failed_run_matches_only_failed_jobs_from_selected_attempt() -> None:
    client = Mock(spec=GitHubClient)
    client.paginate.return_value = iter(
        [
            {"id": 10, "run_attempt": 1, "conclusion": "failure"},
            {"id": 11, "run_attempt": 2, "conclusion": "success"},
            {"id": 12, "run_attempt": 2, "conclusion": "failure"},
        ]
    )
    client.get_bytes.return_value = b"npm ERR! code ERESOLVE"

    result = classify_failed_run(client, _run(123, attempt=2))

    client.paginate.assert_called_once_with(
        "/repos/owner/repo/actions/runs/123/jobs",
        {"filter": "all"},
        items_key="jobs",
    )
    client.get_bytes.assert_called_once_with(
        "/repos/owner/repo/actions/jobs/12/logs",
        max_bytes=MAX_JOB_LOG_BYTES,
    )
    assert result["label_category"] == "dependency_error_candidate"
    assert result["matched_patterns"] == ["npm_eresolve"]


def test_missing_log_is_undetermined_not_other_failure() -> None:
    client = Mock(spec=GitHubClient)
    client.paginate.return_value = iter([{"id": 10, "run_attempt": 1, "conclusion": "failure"}])
    client.get_bytes.return_value = None

    result = classify_failed_run(client, _run(123))

    assert result["label_category"] == "undetermined"
    assert result["log_fetched"] is False


def test_control_requests_use_bounded_timeout_without_retries() -> None:
    config = _control_client_config()

    assert config.timeout == 15.0
    assert config.max_retries == 0


def test_missing_run_jobs_are_undetermined_and_do_not_abort_scan() -> None:
    client = Mock(spec=GitHubClient)

    def missing_jobs() -> Iterator[dict[str, object]]:
        raise GitHubNotFoundError("not found", 404)
        yield {}

    client.paginate.return_value = missing_jobs()

    result = classify_failed_run(client, _run(123))

    assert result["label_category"] == "undetermined"
    assert result["log_status"] == "not_found"
    assert result["log_fetched"] is False
    assert result["jobs"] == []


def test_complete_logs_without_known_pattern_are_not_claimed_as_other_failure() -> None:
    client = Mock(spec=GitHubClient)
    client.paginate.return_value = iter([{"id": 10, "run_attempt": 1, "conclusion": "failure"}])
    client.get_bytes.return_value = b"AssertionError: expected true to be false"

    result = classify_failed_run(client, _run(123))

    assert result["label_category"] == "no_dependency_pattern_match"
    assert result["log_fetched"] is True


def test_oversized_log_is_skipped_before_sanitizing_or_pattern_matching(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "experts.dependency.research.prototype.sample_and_label.MAX_JOB_LOG_BYTES",
        8,
    )
    client = Mock(spec=GitHubClient)
    client.paginate.return_value = iter([{"id": 10, "run_attempt": 1, "conclusion": "failure"}])
    client.get_bytes.return_value = b"npm ERR! code ERESOLVE"
    sanitise_fn = Mock(side_effect=AssertionError("oversized logs must not be sanitised"))

    result = classify_failed_run(client, _run(123), sanitise_fn=sanitise_fn)

    assert result["label_category"] == "undetermined"
    assert result["log_fetched"] is False
    assert result["jobs"] == [
        {
            "job_id": 10,
            "log_status": "too_large",
            "log_size_bytes": len(b"npm ERR! code ERESOLVE"),
            "max_log_size_bytes": 8,
            "matched_patterns": [],
        }
    ]
    sanitise_fn.assert_not_called()


def test_sample_is_seeded_and_output_does_not_contain_logs_or_secrets(tmp_path: Path) -> None:
    dataset_path = tmp_path / "runs.json"
    output_path = tmp_path / "labels.json"
    dataset_path.write_text(
        json.dumps([_run(run_id) for run_id in range(1, 8)]),
        encoding="utf-8",
    )
    client = Mock(spec=GitHubClient)
    client.paginate.return_value = iter([{"id": 10, "run_attempt": 1, "conclusion": "failure"}])
    secret_log = b"npm ERR! code ERESOLVE private-marker-should-not-persist"
    client.get_bytes.return_value = secret_log
    client.paginate.side_effect = lambda *args, **kwargs: iter(
        [{"id": 10, "run_attempt": 1, "conclusion": "failure"}]
    )

    calls: list[str] = []

    def sanitise_test_log(text: str) -> SanitiseResult:
        calls.append(text)
        return SanitiseResult(
            text=text.replace("private-marker-should-not-persist", "[REDACTED]"),
            counts={"test_marker": 1},
        )

    first = sample_and_label_dataset(
        client,
        dataset_path,
        output_path,
        sample_size=3,
        seed=42,
        sanitise_fn=sanitise_test_log,
    )
    saved = output_path.read_text(encoding="utf-8")
    second = sample_and_label_dataset(
        client,
        dataset_path,
        tmp_path / "second.json",
        sample_size=3,
        seed=42,
        sanitise_fn=sanitise_test_log,
    )

    assert [row["run_id"] for row in first] == [row["run_id"] for row in second]
    assert len(calls) == 6
    assert "npm ERR! code ERESOLVE" not in saved
    assert "private-marker-should-not-persist" not in saved
    assert '"redaction_count": 1' in saved
    assert "npm_eresolve" in saved


def test_nonpositive_sample_size_is_rejected(tmp_path: Path) -> None:
    client = Mock(spec=GitHubClient)
    dataset_path = tmp_path / "runs.json"
    dataset_path.write_text("[]", encoding="utf-8")

    with pytest.raises(ValueError, match="sample_size must be positive"):
        sample_and_label_dataset(client, dataset_path, tmp_path / "labels.json", sample_size=0)


def test_candidate_selection_is_seeded_bounded_and_uses_distinct_commits() -> None:
    failed_runs = [
        _run(1, repository="a/repo", commit_sha="a"),
        _run(2, repository="a/repo", commit_sha="a"),
        _run(3, repository="a/repo", commit_sha="b"),
        _run(4, repository="b/repo", commit_sha="c"),
        _run(5, repository="b/repo", commit_sha="d"),
        _run(6, repository="c/repo", commit_sha="e"),
    ]

    first = _select_distinct_failed_runs(failed_runs, scan_limit=4, seed=17)
    second = _select_distinct_failed_runs(failed_runs, scan_limit=4, seed=17)

    first_ids = [row["run_id"] for row in first]
    assert first_ids == [row["run_id"] for row in second]
    assert len(first) == 4
    assert len({(row["repository"], row["commit_sha"]) for row in first}) == 4
    repository_counts = {
        repository: sum(row["repository"] == repository for row in first)
        for repository in ("a/repo", "b/repo", "c/repo")
    }
    assert max(repository_counts.values()) - min(repository_counts.values()) <= 1


def test_candidate_scan_writes_only_sanitised_pattern_matches(tmp_path: Path) -> None:
    dataset_path = tmp_path / "runs.json"
    output_path = tmp_path / "candidate_batch.json"
    dataset_path.write_text(
        json.dumps(
            [
                _run(1, repository="a/repo", commit_sha="a"),
                _run(2, repository="b/repo", commit_sha="b"),
                _run(3, repository="c/repo", commit_sha="c"),
            ]
        ),
        encoding="utf-8",
    )
    client = Mock(spec=GitHubClient)

    def paginate_side_effect(
        path: str,
        *_: object,
        **__: object,
    ) -> Iterator[dict[str, object]]:
        run_id = int(path.split("/")[-2])
        return iter([{"id": run_id + 100, "run_attempt": 1, "conclusion": "failure"}])

    client.paginate.side_effect = paginate_side_effect
    client.get_bytes.side_effect = lambda path, **_kwargs: {
        "/repos/a/repo/actions/jobs/101/logs": (b"npm ERR! code ERESOLVE secret-log-marker"),
        "/repos/b/repo/actions/jobs/102/logs": b"npm ERR! missing script: test",
        "/repos/c/repo/actions/jobs/103/logs": None,
    }[path]

    def sanitise_test_log(text: str) -> SanitiseResult:
        return SanitiseResult(
            text=text.replace("secret-log-marker", "[REDACTED]"),
            counts={"test_secret": 1} if "secret-log-marker" in text else {},
        )

    candidates = scan_dependency_candidates(
        client,
        dataset_path,
        output_path,
        scan_limit=3,
        seed=42,
        sanitise_fn=sanitise_test_log,
    )
    saved_text = output_path.read_text(encoding="utf-8")
    saved = json.loads(saved_text)

    assert saved["scanned_runs"] == 3
    assert saved["scanned_commits"] == [
        {"repository": row["repository"], "commit_sha": row["commit_sha"]}
        for row in _select_distinct_failed_runs(
            [
                _run(1, repository="a/repo", commit_sha="a"),
                _run(2, repository="b/repo", commit_sha="b"),
                _run(3, repository="c/repo", commit_sha="c"),
            ],
            scan_limit=3,
            seed=42,
        )
    ]
    assert saved["candidate_count"] == 1
    assert len(candidates) == 1
    assert candidates[0]["label_category"] == "dependency_error_candidate"
    assert candidates[0]["verified_label"] is None
    assert candidates[0]["is_final"] is False
    assert candidates[0]["matched_patterns"] == ["npm_eresolve"]
    assert candidates[0]["jobs"][0]["verified_label"] is None
    assert candidates[0]["jobs"][0]["is_final"] is False
    assert "npm ERR! missing script" not in saved_text
    assert "secret-log-marker" not in saved_text
    assert "ERESOLVE" not in saved_text


def test_candidate_scan_excludes_previously_reviewed_commits(tmp_path: Path) -> None:
    dataset_path = tmp_path / "runs.json"
    dataset_path.write_text(
        json.dumps(
            [
                _run(1, repository="owner/repo", commit_sha="reviewed"),
                _run(2, repository="owner/repo", commit_sha="new"),
            ]
        ),
        encoding="utf-8",
    )
    (tmp_path / "labeled_sample_reviewed.json").write_text(
        json.dumps(
            [
                {
                    "repository": "owner/repo",
                    "commit_sha": "reviewed",
                    "verified_label": "other_failure",
                    "is_final": True,
                }
            ]
        ),
        encoding="utf-8",
    )
    client = Mock(spec=GitHubClient)
    client.paginate.return_value = iter([{"id": 102, "run_attempt": 1, "conclusion": "failure"}])
    client.get_bytes.return_value = b"ERR_PNPM_NO_MATCHING_VERSION"

    candidates = scan_dependency_candidates(
        client,
        dataset_path,
        tmp_path / "candidate_batch.json",
        scan_limit=2,
        seed=42,
    )
    saved = json.loads((tmp_path / "candidate_batch.json").read_text(encoding="utf-8"))

    assert saved["excluded_reviewed_commits"] == 1
    assert saved["scanned_runs"] == 1
    assert [candidate["run_id"] for candidate in candidates] == [2]
    client.paginate.assert_called_once_with(
        "/repos/owner/repo/actions/runs/2/jobs",
        {"filter": "all"},
        items_key="jobs",
    )


def test_candidate_scan_excludes_prior_batch_history_and_reviewed_candidates(
    tmp_path: Path,
) -> None:
    dataset_path = tmp_path / "runs.json"
    dataset_path.write_text(
        json.dumps(
            [
                _run(1, repository="owner/repo", commit_sha="scanned-no-match"),
                _run(2, repository="owner/repo", commit_sha="reviewed-candidate"),
                _run(3, repository="owner/repo", commit_sha="new"),
            ]
        ),
        encoding="utf-8",
    )
    (tmp_path / "labeled_candidate_batch_1.json").write_text(
        json.dumps(
            {
                "scanned_commits": [{"repository": "owner/repo", "commit_sha": "scanned-no-match"}],
                "candidates": [
                    {
                        "repository": "owner/repo",
                        "commit_sha": "reviewed-candidate",
                        "verified_label": "dependency_failure",
                        "is_final": True,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    client = Mock(spec=GitHubClient)
    client.paginate.return_value = iter([{"id": 103, "run_attempt": 1, "conclusion": "failure"}])
    client.get_bytes.return_value = b"ERR_PNPM_NO_MATCHING_VERSION"

    candidates = scan_dependency_candidates(
        client,
        dataset_path,
        tmp_path / "candidates_batch_2.json",
        scan_limit=3,
        seed=42,
    )
    saved = json.loads((tmp_path / "candidates_batch_2.json").read_text(encoding="utf-8"))

    assert saved["excluded_prior_scanned_commits"] == 1
    assert saved["excluded_reviewed_candidate_commits"] == 1
    assert saved["scanned_commits"] == [{"repository": "owner/repo", "commit_sha": "new"}]
    assert [candidate["run_id"] for candidate in candidates] == [3]
    client.paginate.assert_called_once_with(
        "/repos/owner/repo/actions/runs/3/jobs",
        {"filter": "all"},
        items_key="jobs",
    )


def test_candidate_scan_does_not_exclude_its_current_output_history(tmp_path: Path) -> None:
    dataset_path = tmp_path / "runs.json"
    output_path = tmp_path / "candidates_batch_2.json"
    dataset_path.write_text(
        json.dumps([_run(1, repository="owner/repo", commit_sha="same")]),
        encoding="utf-8",
    )
    output_path.write_text(
        json.dumps(
            {
                "scanned_commits": [{"repository": "owner/repo", "commit_sha": "same"}],
                "candidates": [],
            }
        ),
        encoding="utf-8",
    )
    client = Mock(spec=GitHubClient)
    client.paginate.return_value = iter([{"id": 101, "run_attempt": 1, "conclusion": "failure"}])
    client.get_bytes.return_value = b"ERR_PNPM_NO_MATCHING_VERSION"

    candidates = scan_dependency_candidates(
        client,
        dataset_path,
        output_path,
        scan_limit=1,
        seed=42,
    )
    saved = json.loads(output_path.read_text(encoding="utf-8"))

    assert [candidate["run_id"] for candidate in candidates] == [1]
    assert saved["scanned_commits"] == [{"repository": "owner/repo", "commit_sha": "same"}]


@pytest.mark.parametrize(
    ("log_text", "expected_pattern"),
    [
        ("npm error code ERESOLVE", "npm_eresolve"),
        ("npm ERR! 404 Not Found", "npm_e404"),
        ("MODULE_NOT_FOUND", "module_not_found"),
        ("Could not resolve dependency tree", "dependency_resolution"),
        ("YN0035: Package not found", "yarn_package_missing"),
        ("ERR_PNPM_NO_MATCHING_VERSION", "pnpm_error"),
        ("ETARGET", "version_not_found"),
        ("ERR_SOCKET_TIMEOUT", "registry_timeout"),
    ],
)
def test_explicit_package_manager_error_patterns(
    log_text: str,
    expected_pattern: str,
) -> None:
    assert expected_pattern in _matched_pattern_ids(log_text)


@pytest.mark.parametrize(
    ("scan_limit", "message"),
    [(0, "scan_limit must be positive"), (101, "scan_limit must not exceed 100")],
)
def test_candidate_scan_limit_is_rejected(
    tmp_path: Path,
    scan_limit: int,
    message: str,
) -> None:
    dataset_path = tmp_path / "runs.json"
    dataset_path.write_text("[]", encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        scan_dependency_candidates(
            Mock(spec=GitHubClient),
            dataset_path,
            tmp_path / "candidate_batch.json",
            scan_limit=scan_limit,
        )


def test_control_scan_saves_only_complete_zero_match_logs_without_log_text(
    tmp_path: Path,
) -> None:
    dataset_path = tmp_path / "runs.json"
    output_path = tmp_path / "random_control_batch_50.json"
    dataset_path.write_text(
        json.dumps([_run(run_id) for run_id in range(1, 5)]),
        encoding="utf-8",
    )
    client = Mock(spec=GitHubClient)
    client.paginate.side_effect = lambda path, *_args, **_kwargs: iter(
        [
            {
                "id": int(path.split("/")[-2]) + 100,
                "run_attempt": 1,
                "conclusion": "failure",
            }
        ]
    )
    client.get_bytes.side_effect = lambda path, **_kwargs: {
        "/repos/owner/repo/actions/jobs/101/logs": b"AssertionError private-log-marker",
        "/repos/owner/repo/actions/jobs/102/logs": b"npm ERR! code ERESOLVE",
        "/repos/owner/repo/actions/jobs/103/logs": None,
        "/repos/owner/repo/actions/jobs/104/logs": b"Vitest assertion failed",
    }[path]

    controls = scan_random_failure_controls(
        client,
        dataset_path,
        output_path,
        sample_size=50,
        scan_limit=4,
        seed=42,
    )
    saved_text = output_path.read_text(encoding="utf-8")
    saved = json.loads(saved_text)

    assert saved["scanned_runs"] == 4
    assert saved["scanned_commits"] == [
        {"repository": row["repository"], "commit_sha": row["commit_sha"]}
        for row in _select_distinct_failed_runs(
            [_run(run_id) for run_id in range(1, 5)],
            scan_limit=4,
            seed=42,
        )
    ]
    assert saved["control_count"] == len(controls) == 2
    assert {row["run_id"] for row in controls} == {1, 4}
    assert all(row["label_category"] == "random_control_candidate" for row in controls)
    assert all(row["verified_label"] is None and not row["is_final"] for row in controls)
    assert all(row["jobs"][0]["job_url"].startswith("https://github.com/") for row in controls)
    assert "private-log-marker" not in saved_text
    assert "AssertionError" not in saved_text
    assert "Vitest assertion failed" not in saved_text
    assert "ERESOLVE" not in saved_text


def test_control_scan_skips_github_errors_and_logs_safe_run_progress(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    dataset_path = tmp_path / "runs.json"
    output_path = tmp_path / "random_control_batch.json"
    rows = [_run(run_id) for run_id in range(1, 4)]
    dataset_path.write_text(json.dumps(rows), encoding="utf-8")
    selected = _select_distinct_failed_runs(rows, scan_limit=3, seed=42)
    failed_run_id = int(selected[0]["run_id"])
    client = Mock(spec=GitHubClient)

    def paginate(path: str, *_args: object, **_kwargs: object) -> Iterator[dict[str, object]]:
        run_id = int(path.split("/")[-2])
        if run_id == failed_run_id:
            raise GitHubError("transport error: ConnectError")
        return iter([{"id": run_id + 100, "run_attempt": 1, "conclusion": "failure"}])

    client.paginate.side_effect = paginate
    client.get_bytes.return_value = b"AssertionError"

    with caplog.at_level(logging.INFO):
        controls = scan_random_failure_controls(
            client,
            dataset_path,
            output_path,
            sample_size=5,
            scan_limit=3,
            seed=42,
        )

    saved = json.loads(output_path.read_text(encoding="utf-8"))
    assert saved["scan_status"] == "complete_with_errors"
    assert saved["scanned_runs"] == 3
    assert saved["control_count"] == len(controls) == 2
    assert saved["scan_errors"] == [
        {
            "repository": "owner/repo",
            "run_id": failed_run_id,
            "attempt": 1,
            "error_type": "GitHubError",
            "status_code": None,
        }
    ]
    progress = [
        record
        for record in caplog.records
        if record.getMessage().startswith("Processing control run")
    ]
    assert len(progress) == 3
    assert all(hasattr(record, "repo") and hasattr(record, "run_id") for record in progress)
    assert not any("ConnectError" in record.getMessage() for record in progress)


def test_control_scan_persists_partial_output_when_interrupted(tmp_path: Path) -> None:
    dataset_path = tmp_path / "runs.json"
    output_path = tmp_path / "random_control_batch.json"
    dataset_path.write_text(json.dumps([_run(1)]), encoding="utf-8")
    client = Mock(spec=GitHubClient)
    client.paginate.side_effect = KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        scan_random_failure_controls(
            client,
            dataset_path,
            output_path,
            sample_size=1,
            scan_limit=1,
        )

    saved = json.loads(output_path.read_text(encoding="utf-8"))
    assert saved["scan_status"] == "interrupted"
    assert saved["scanned_runs"] == 1
    assert saved["scanned_commits"] == [
        {"repository": "owner/repo", "commit_sha": _run(1)["commit_sha"]}
    ]
    assert saved["control_count"] == 0


def test_control_scan_persists_partial_output_when_unexpected_error_is_raised(
    tmp_path: Path,
) -> None:
    dataset_path = tmp_path / "runs.json"
    output_path = tmp_path / "random_control_batch.json"
    dataset_path.write_text(json.dumps([_run(1)]), encoding="utf-8")
    client = Mock(spec=GitHubClient)
    client.paginate.side_effect = RuntimeError("unexpected client failure")

    with pytest.raises(RuntimeError, match="unexpected client failure"):
        scan_random_failure_controls(
            client,
            dataset_path,
            output_path,
            sample_size=1,
            scan_limit=1,
        )

    saved = json.loads(output_path.read_text(encoding="utf-8"))
    assert saved["scan_status"] == "incomplete"
    assert saved["scanned_runs"] == 1
    assert saved["control_count"] == 0


def test_control_scan_excludes_candidate_history_control_history_and_reviewed_commits(
    tmp_path: Path,
) -> None:
    dataset_path = tmp_path / "runs.json"
    dataset_path.write_text(
        json.dumps(
            [
                _run(1, commit_sha="candidate-scanned"),
                _run(2, commit_sha="control-scanned"),
                _run(3, commit_sha="reviewed"),
                _run(4, commit_sha="new"),
            ]
        ),
        encoding="utf-8",
    )
    (tmp_path / "candidates_batch_1.json").write_text(
        json.dumps(
            {
                "scanned_commits": [
                    {"repository": "owner/repo", "commit_sha": "candidate-scanned"}
                ],
                "candidates": [],
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "random_control_batch_1.json").write_text(
        json.dumps(
            {
                "scanned_commits": [{"repository": "owner/repo", "commit_sha": "control-scanned"}],
                "controls": [],
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "labeled_sample_reviewed.json").write_text(
        json.dumps(
            [
                {
                    "repository": "owner/repo",
                    "commit_sha": "reviewed",
                    "verified_label": "other_failure",
                    "is_final": True,
                }
            ]
        ),
        encoding="utf-8",
    )
    client = Mock(spec=GitHubClient)
    client.paginate.return_value = iter([{"id": 104, "run_attempt": 1, "conclusion": "failure"}])
    client.get_bytes.return_value = b"AssertionError"

    controls = scan_random_failure_controls(
        client,
        dataset_path,
        tmp_path / "random_control_batch_2.json",
        sample_size=1,
        scan_limit=1,
        seed=42,
    )
    saved = json.loads((tmp_path / "random_control_batch_2.json").read_text(encoding="utf-8"))

    assert [row["run_id"] for row in controls] == [4]
    assert saved["scanned_commits"] == [{"repository": "owner/repo", "commit_sha": "new"}]
    client.paginate.assert_called_once_with(
        "/repos/owner/repo/actions/runs/4/jobs",
        {"filter": "all"},
        items_key="jobs",
    )


@pytest.mark.parametrize(
    ("sample_size", "scan_limit", "message"),
    [
        (0, 1, "sample_size must be positive"),
        (1, 0, "scan_limit must be between 1 and 100"),
        (1, 101, "scan_limit must be between 1 and 100"),
    ],
)
def test_control_scan_rejects_invalid_limits(
    tmp_path: Path,
    sample_size: int,
    scan_limit: int,
    message: str,
) -> None:
    dataset_path = tmp_path / "runs.json"
    dataset_path.write_text("[]", encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        scan_random_failure_controls(
            Mock(spec=GitHubClient),
            dataset_path,
            tmp_path / "random_control_batch.json",
            sample_size=sample_size,
            scan_limit=scan_limit,
        )
