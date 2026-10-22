"""Tests for feature conversion used by the experimental Dependency baseline."""

import csv
import json
from pathlib import Path

import pytest
from dependency_expert.features.prototype import dependency_feature_rows, engineer_features


def test_dependency_feature_rows_identifies_manifests_and_lockfiles() -> None:
    rows = dependency_feature_rows(
        [
            {
                "run_id": 5,
                "repository": "owner/repo",
                "commit_sha": "abc",
                "status": "failure",
                "dependency_files_changed": [
                    {"filename": "package.json"},
                    {"filename": "packages/api/pnpm-lock.yaml"},
                ],
            }
        ]
    )

    assert rows == [
        {
            "run_id": "5",
            "repository": "owner/repo",
            "commit_sha": "abc",
            "total_dependency_files_changed": 2,
            "has_package_json_change": 1,
            "has_lockfile_change": 1,
            "is_failure": 1,
        }
    ]


def test_engineer_features_writes_csv_and_creates_parent_directory(tmp_path: Path) -> None:
    input_json = tmp_path / "runs.json"
    output_csv = tmp_path / "output" / "features.csv"
    input_json.write_text(
        json.dumps(
            [
                {
                    "run_id": 1,
                    "status": "success",
                    "dependency_files_changed": [{"filename": "yarn.lock"}],
                }
            ]
        ),
        encoding="utf-8",
    )

    assert engineer_features(input_json, output_csv) == 1
    with output_csv.open(encoding="utf-8", newline="") as output_file:
        rows = list(csv.DictReader(output_file))
    assert rows[0]["has_lockfile_change"] == "1"
    assert rows[0]["is_failure"] == "0"


@pytest.mark.parametrize("records", [None, {"status": "failure"}])
def test_dependency_feature_rows_rejects_non_array_input(records: object) -> None:
    with pytest.raises(ValueError, match="records must be a JSON array"):
        dependency_feature_rows(records)


def test_dependency_feature_rows_rejects_invalid_changed_files() -> None:
    with pytest.raises(ValueError, match="dependency_files_changed must be a list"):
        dependency_feature_rows([{"dependency_files_changed": "package.json"}])
