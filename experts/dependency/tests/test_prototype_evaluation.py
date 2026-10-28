"""Tests for local prototype dataset audits and grouped baseline evaluation."""

import csv
from pathlib import Path

import pytest
from dependency_expert.features.prototype_evaluation import (
    evaluate_dataset,
    read_and_audit_dataset,
)

FIELDS = (
    "run_id",
    "attempt",
    "repository",
    "commit_sha",
    "total_dependency_files_changed",
    "has_package_json_change",
    "has_lockfile_change",
    "is_failure",
)


def _write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as output_file:
        writer = csv.DictWriter(output_file, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def _rows(repository_count: int = 8) -> list[dict[str, str]]:
    return [
        {
            "run_id": f"{repository_index}-{run_index}",
            "attempt": "1",
            "repository": f"owner/repo-{repository_index}",
            "commit_sha": f"sha-{repository_index}-{run_index}",
            "total_dependency_files_changed": str(run_index % 3 + 1),
            "has_package_json_change": str(run_index % 2),
            "has_lockfile_change": str((run_index // 2) % 2),
            "is_failure": str(run_index % 2),
        }
        for repository_index in range(repository_count)
        for run_index in range(12)
    ]


def test_audit_removes_identical_duplicate_run_and_reports_class_balance(tmp_path: Path) -> None:
    rows = _rows()
    rows.append(rows[0].copy())
    csv_path = tmp_path / "features.csv"
    _write_csv(csv_path, rows)

    audited_rows, quality = read_and_audit_dataset(csv_path)

    assert len(audited_rows) == len(rows) - 1
    assert quality["input_rows"] == len(rows)
    assert quality["unique_rows"] == len(rows) - 1
    assert quality["identical_duplicate_rows_removed"] == 1
    assert quality["repository_count"] == 8
    assert quality["label_counts"] == {"success": 48, "workflow_failure": 48}


def test_audit_keeps_distinct_attempts_for_the_same_run(tmp_path: Path) -> None:
    rows = _rows()
    second_attempt = rows[0].copy()
    second_attempt["attempt"] = "2"
    rows.append(second_attempt)
    csv_path = tmp_path / "attempts.csv"
    _write_csv(csv_path, rows)

    audited_rows, quality = read_and_audit_dataset(csv_path)

    assert len(audited_rows) == len(rows)
    assert quality["unique_rows"] == len(rows)
    same_run_attempts = [
        row["attempt"] for row in audited_rows if row["run_id"] == rows[0]["run_id"]
    ]
    assert same_run_attempts == [1, 2]


def test_audit_rejects_missing_columns_and_invalid_values(tmp_path: Path) -> None:
    missing_column = tmp_path / "missing.csv"
    missing_column.write_text("run_id,repository\n1,owner/repo\n", encoding="utf-8")
    with pytest.raises(ValueError, match="missing columns"):
        read_and_audit_dataset(missing_column)

    invalid_label = _rows()
    invalid_label[0]["is_failure"] = "2"
    invalid_csv = tmp_path / "invalid.csv"
    _write_csv(invalid_csv, invalid_label)
    with pytest.raises(ValueError, match="non-binary value in is_failure"):
        read_and_audit_dataset(invalid_csv)

    invalid_attempt = _rows()
    invalid_attempt[0]["attempt"] = "0"
    attempt_csv = tmp_path / "invalid-attempt.csv"
    _write_csv(attempt_csv, invalid_attempt)
    with pytest.raises(ValueError, match="non-positive attempt"):
        read_and_audit_dataset(attempt_csv)


def test_audit_rejects_conflicting_duplicate_runs(tmp_path: Path) -> None:
    rows = _rows()
    conflicting = rows[0].copy()
    conflicting["is_failure"] = str(1 - int(conflicting["is_failure"]))
    rows.append(conflicting)
    csv_path = tmp_path / "conflict.csv"
    _write_csv(csv_path, rows)

    with pytest.raises(ValueError, match="conflicting duplicate"):
        read_and_audit_dataset(csv_path)


def test_evaluation_uses_reproducible_repository_disjoint_split(tmp_path: Path) -> None:
    csv_path = tmp_path / "features.csv"
    _write_csv(csv_path, _rows())
    rows, quality = read_and_audit_dataset(csv_path)

    first = evaluate_dataset(rows, quality, seed=17)
    second = evaluate_dataset(rows, quality, seed=17)

    assert first == second
    split = first["split"]
    assert split["method"] == "GroupShuffleSplit by repository"
    assert len(split["held_out_repositories"]) == 2
    assert split["train_rows"] + split["test_rows"] == len(rows)
    assert set(split["train_label_counts"]) == {"success", "workflow_failure"}
    metrics = first["metrics"]
    assert set(metrics) == {"majority_baseline", "logistic_regression"}
    assert set(metrics["logistic_regression"]) == {
        "accuracy",
        "balanced_accuracy",
        "precision",
        "recall",
        "f1",
        "roc_auc",
    }
    assert "not ground truth" in first["target"]


def test_evaluation_rejects_one_repository_and_invalid_test_size(tmp_path: Path) -> None:
    csv_path = tmp_path / "single-repo.csv"
    _write_csv(
        csv_path,
        [
            {
                **row,
                "repository": "owner/only",
            }
            for row in _rows(1)
        ],
    )
    audited_single_repo_rows, quality = read_and_audit_dataset(csv_path)
    with pytest.raises(ValueError, match="at least two repositories"):
        evaluate_dataset(audited_single_repo_rows, quality)

    two_or_more_repos_csv = tmp_path / "features.csv"
    _write_csv(two_or_more_repos_csv, _rows())
    audited_rows, quality = read_and_audit_dataset(two_or_more_repos_csv)
    with pytest.raises(ValueError, match="test_size must be between 0 and 1"):
        evaluate_dataset(audited_rows, quality, test_size=1.0)
