"""Tests for consolidating and auditing manually reviewed Dependency labels."""

import csv
import json
from pathlib import Path

import pytest
from dependency_expert.features.master_dataset import audit_and_prepare_dataset

FEATURE_FIELDS = (
    "run_id",
    "attempt",
    "repository",
    "commit_sha",
    "total_dependency_files_changed",
    "has_package_json_change",
    "has_lockfile_change",
    "is_failure",
)


def _label(
    run_id: int,
    label: str,
    *,
    attempt: int = 1,
    reason: str = "review reason",
    final: bool = True,
) -> dict[str, object]:
    return {
        "repository": "owner/repo",
        "run_id": run_id,
        "attempt": attempt,
        "verified_label": label,
        "verified_reason": reason,
        "labelled_by": "manual",
        "is_final": final,
    }


def _write_features(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as output_file:
        writer = csv.DictWriter(output_file, fieldnames=FEATURE_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def test_audit_preserves_sources_detects_conflicts_and_builds_training_table(
    tmp_path: Path,
) -> None:
    dataset_dir = tmp_path / "data"
    dataset_dir.mkdir()
    first_labels = [
        _label(1, "dependency_failure"),
        _label(2, "uncertain"),
        _label(3, "other_failure", attempt=2),
        _label(4, "dependency_failure", final=False),
    ]
    second_batch = {
        "candidates": [
            _label(1, "dependency_failure"),
            _label(1, "other_failure", reason="conflicting review"),
            _label(3, "other_failure", attempt=2, reason="same-label duplicate"),
        ]
    }
    (dataset_dir / "labeled_sample_1.json").write_text(json.dumps(first_labels), encoding="utf-8")
    (dataset_dir / "candidates_batch_2.json").write_text(json.dumps(second_batch), encoding="utf-8")
    features_path = tmp_path / "features.csv"
    _write_features(
        features_path,
        [
            {
                "run_id": "1",
                "attempt": "1",
                "repository": "owner/repo",
                "commit_sha": "sha-1",
                "total_dependency_files_changed": "1",
                "has_package_json_change": "1",
                "has_lockfile_change": "0",
                "is_failure": "1",
            },
            {
                "run_id": "2",
                "attempt": "1",
                "repository": "owner/repo",
                "commit_sha": "sha-2",
                "total_dependency_files_changed": "1",
                "has_package_json_change": "0",
                "has_lockfile_change": "1",
                "is_failure": "1",
            },
            {
                "run_id": "3",
                "attempt": "1",
                "repository": "owner/repo",
                "commit_sha": "sha-3-old",
                "total_dependency_files_changed": "1",
                "has_package_json_change": "1",
                "has_lockfile_change": "0",
                "is_failure": "1",
            },
            {
                "run_id": "3",
                "attempt": "2",
                "repository": "owner/repo",
                "commit_sha": "sha-3-new",
                "total_dependency_files_changed": "2",
                "has_package_json_change": "1",
                "has_lockfile_change": "1",
                "is_failure": "1",
            },
        ],
    )

    report = audit_and_prepare_dataset(dataset_dir, features_path)

    assert report["source_records"] == 6
    assert report["unique_composite_keys"] == 3
    assert report["duplicate_rows_removed"] == 3
    assert report["conflicting_composite_keys"] == 1
    assert report["feature_unmatched_training_keys"] == 0
    assert report["training_label_counts"] == {
        "dependency_failure": 0,
        "other_failure": 1,
    }
    with (dataset_dir / "audit_full_records.csv").open(encoding="utf-8", newline="") as input_file:
        audit_rows = list(csv.DictReader(input_file))
    assert len(audit_rows) == 6
    assert sum(row["feature_matched"] == "true" for row in audit_rows) == 6

    with (dataset_dir / "audit_label_conflicts.csv").open(
        encoding="utf-8", newline=""
    ) as input_file:
        conflict_rows = list(csv.DictReader(input_file))
    assert len(conflict_rows) == 3
    assert {row["conflicting_labels"] for row in conflict_rows} == {
        '["dependency_failure", "other_failure"]'
    }

    with (dataset_dir / "master_training_table.csv").open(
        encoding="utf-8", newline=""
    ) as input_file:
        training_rows = list(csv.DictReader(input_file))
    assert len(training_rows) == 1
    assert training_rows[0]["run_id"] == "3"
    assert training_rows[0]["attempt"] == "2"
    assert training_rows[0]["commit_sha"] == "sha-3-new"


def test_audit_requires_attempt_in_feature_csv(tmp_path: Path) -> None:
    dataset_dir = tmp_path / "data"
    dataset_dir.mkdir()
    (dataset_dir / "labeled_sample.json").write_text(
        json.dumps([_label(1, "dependency_failure")]), encoding="utf-8"
    )
    features_path = tmp_path / "features.csv"
    features_path.write_text(
        "repository,run_id,commit_sha,total_dependency_files_changed,"
        "has_package_json_change,has_lockfile_change,is_failure\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="missing columns: attempt"):
        audit_and_prepare_dataset(dataset_dir, features_path)


def test_audit_uses_attempt_one_for_legacy_label_records(tmp_path: Path) -> None:
    dataset_dir = tmp_path / "data"
    dataset_dir.mkdir()
    row = _label(5, "dependency_failure")
    del row["attempt"]
    (dataset_dir / "labeled_sample.json").write_text(json.dumps([row]), encoding="utf-8")
    features_path = tmp_path / "features.csv"
    _write_features(
        features_path,
        [
            {
                "run_id": "5",
                "attempt": "1",
                "repository": "owner/repo",
                "commit_sha": "sha-5",
                "total_dependency_files_changed": "1",
                "has_package_json_change": "1",
                "has_lockfile_change": "0",
                "is_failure": "1",
            }
        ],
    )

    report = audit_and_prepare_dataset(dataset_dir, features_path)

    assert report["training_rows"] == 1
    with (dataset_dir / "master_training_table.csv").open(
        encoding="utf-8", newline=""
    ) as input_file:
        assert next(csv.DictReader(input_file))["attempt"] == "1"


def test_audit_includes_final_random_control_labels_and_ignores_pending_controls(
    tmp_path: Path,
) -> None:
    dataset_dir = tmp_path / "data"
    dataset_dir.mkdir()
    final_control = _label(20, "other_failure", attempt=2, reason="Firewall blocked the request.")
    pending_control = _label(21, "other_failure", final=False)
    (dataset_dir / "random_control_batch_50.json").write_text(
        json.dumps({"controls": [final_control, pending_control]}),
        encoding="utf-8",
    )
    features_path = tmp_path / "features.csv"
    _write_features(
        features_path,
        [
            {
                "run_id": "20",
                "attempt": "2",
                "repository": "owner/repo",
                "commit_sha": "sha-20",
                "total_dependency_files_changed": "1",
                "has_package_json_change": "1",
                "has_lockfile_change": "0",
                "is_failure": "1",
            },
            {
                "run_id": "21",
                "attempt": "1",
                "repository": "owner/repo",
                "commit_sha": "sha-21",
                "total_dependency_files_changed": "1",
                "has_package_json_change": "0",
                "has_lockfile_change": "1",
                "is_failure": "1",
            },
        ],
    )

    report = audit_and_prepare_dataset(dataset_dir, features_path)

    assert report["source_records"] == 1
    assert report["unique_composite_keys"] == 1
    assert report["training_label_counts"] == {
        "dependency_failure": 0,
        "other_failure": 1,
    }
    with (dataset_dir / "audit_full_records.csv").open(encoding="utf-8", newline="") as input_file:
        audit_rows = list(csv.DictReader(input_file))
    assert audit_rows[0]["source_file"] == "random_control_batch_50.json"
    assert audit_rows[0]["attempt"] == "2"
    with (dataset_dir / "master_training_table.csv").open(
        encoding="utf-8", newline=""
    ) as input_file:
        training_rows = list(csv.DictReader(input_file))
    assert len(training_rows) == 1
    assert training_rows[0]["run_id"] == "20"
