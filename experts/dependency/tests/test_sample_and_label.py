"""Offline tests for the Dependency sample log labeler."""

import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from experts.dependency.research.prototype.sample_and_label import (
    classify_failed_run,
    sample_and_label_dataset,
)
from shared.github import GitHubClient
from shared.sanitise import SanitiseResult


def _run(run_id: int, *, attempt: int = 1) -> dict[str, object]:
    return {
        "repository": "owner/repo",
        "run_id": run_id,
        "attempt": attempt,
        "status": "failure",
        "commit_sha": "a" * 40,
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
    client.get_bytes.assert_called_once_with("/repos/owner/repo/actions/jobs/12/logs")
    assert result["label_category"] == "dependency_error_candidate"
    assert result["matched_patterns"] == ["npm_eresolve"]


def test_missing_log_is_undetermined_not_other_failure() -> None:
    client = Mock(spec=GitHubClient)
    client.paginate.return_value = iter([{"id": 10, "run_attempt": 1, "conclusion": "failure"}])
    client.get_bytes.return_value = None

    result = classify_failed_run(client, _run(123))

    assert result["label_category"] == "undetermined"
    assert result["log_fetched"] is False


def test_complete_logs_without_known_pattern_are_not_claimed_as_other_failure() -> None:
    client = Mock(spec=GitHubClient)
    client.paginate.return_value = iter([{"id": 10, "run_attempt": 1, "conclusion": "failure"}])
    client.get_bytes.return_value = b"AssertionError: expected true to be false"

    result = classify_failed_run(client, _run(123))

    assert result["label_category"] == "no_dependency_pattern_match"
    assert result["log_fetched"] is True


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
