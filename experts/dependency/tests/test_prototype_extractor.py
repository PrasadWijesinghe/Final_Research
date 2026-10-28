"""Offline tests for prototype repository configuration and dataset extraction."""

import csv
import json
from pathlib import Path
from unittest.mock import Mock

import pytest
from dependency_expert.prototype_dataset import (
    collect_dataset,
    load_repositories,
    load_repository_group,
    validate_repository_names,
)

from shared.github import GitHubClient


def test_load_repositories_uses_enabled_dependency_entries(tmp_path: Path) -> None:
    config_path = tmp_path / "dependency.yaml"
    config_path.write_text(
        "member: dependency\nrepos:\n"
        "  - full_name: owner/one\n    licence: MIT\n"
        "  - full_name: owner/two\n    licence: MIT\n    enabled: false\n",
        encoding="utf-8",
    )

    assert load_repositories(config_path) == ["owner/one"]


def test_load_repository_group_loads_named_group(tmp_path: Path) -> None:
    groups_path = tmp_path / "groups.yaml"
    groups_path.write_text(
        "groups:\n  backend_frameworks:\n    - expressjs/express\n    - nestjs/nest\n",
        encoding="utf-8",
    )

    assert load_repository_group(groups_path, "backend_frameworks") == [
        "expressjs/express",
        "nestjs/nest",
    ]


def test_validate_repository_names_deduplicates_and_rejects_invalid_names() -> None:
    assert validate_repository_names(["owner/repo", "owner/repo"], "--repo") == ["owner/repo"]

    with pytest.raises(ValueError, match="invalid owner/repo"):
        validate_repository_names(["not a repo"], "--repo")


def test_collect_dataset_appends_dependency_run_without_patch_content(tmp_path: Path) -> None:
    client = Mock(spec=GitHubClient)
    client.iter_workflow_runs.return_value = iter(
        [
            {
                "id": 42,
                "run_attempt": 2,
                "head_sha": "a" * 40,
                "conclusion": "failure",
                "created_at": "2026-10-01T00:00:00Z",
            }
        ]
    )
    client.get_json.return_value = {
        "files": [
            {
                "filename": "package.json",
                "status": "modified",
                "patch": "must not be retained",
            }
        ]
    }
    output_path = tmp_path / "nested" / "runs.json"

    assert collect_dataset(client, ["owner/repo"], output_path) == 1
    records = json.loads(output_path.read_text(encoding="utf-8"))
    assert records[0]["attempt"] == 2
    assert records[0]["dependency_files_changed"] == [
        {"filename": "package.json", "status": "modified"}
    ]
    assert "patch" not in records[0]["dependency_files_changed"][0]
    feature_path = output_path.parent / "dependency_features.csv"
    assert [item["run_id"] for item in json.loads(output_path.read_text("utf-8"))] == [42]
    with feature_path.open(encoding="utf-8", newline="") as feature_file:
        assert [row["run_id"] for row in csv.DictReader(feature_file)] == ["42"]


def test_collect_dataset_skips_existing_attempt_and_filters_dataset(tmp_path: Path) -> None:
    client = Mock(spec=GitHubClient)
    client.iter_workflow_runs.return_value = iter(
        [{"id": 42, "run_attempt": 2, "head_sha": "a" * 40, "conclusion": "failure"}]
    )
    output_path = tmp_path / "runs.json"
    original = [
        {
            "repository": "owner/repo",
            "run_id": 42,
            "attempt": 2,
            "status": "failure",
            "commit_sha": "a" * 40,
            "dependency_files_changed": [],
        }
    ]
    output_path.write_text(json.dumps(original), encoding="utf-8")

    assert collect_dataset(client, ["owner/repo"], output_path) == 0
    assert json.loads(output_path.read_text(encoding="utf-8")) == []
    client.get_json.assert_not_called()


def test_collect_dataset_refreshes_filtered_outputs_without_new_runs(tmp_path: Path) -> None:
    client = Mock(spec=GitHubClient)
    client.iter_workflow_runs.return_value = iter([])
    output_path = tmp_path / "runs.json"
    original = [
        {
            "repository": "owner/repo",
            "run_id": 1,
            "attempt": 1,
            "status": "success",
            "commit_sha": "a" * 40,
            "dependency_files_changed": [{"filename": "package.json"}],
        },
        {
            "repository": "owner/repo",
            "run_id": 2,
            "attempt": 1,
            "status": "failure",
            "commit_sha": "b" * 40,
            "dependency_files_changed": [],
        },
    ]
    output_path.write_text(json.dumps(original), encoding="utf-8")

    assert collect_dataset(client, ["owner/repo"], output_path) == 0
    filtered = json.loads(output_path.read_text(encoding="utf-8"))
    assert [item["run_id"] for item in filtered] == [1]
    with (tmp_path / "dependency_features.csv").open(encoding="utf-8", newline="") as feature_file:
        assert [row["run_id"] for row in csv.DictReader(feature_file)] == ["1"]


def test_collect_dataset_does_not_store_failure_without_dependency_change(
    tmp_path: Path,
) -> None:
    client = Mock(spec=GitHubClient)
    client.iter_workflow_runs.return_value = iter(
        [
            {
                "id": 99,
                "run_attempt": 1,
                "head_sha": "c" * 40,
                "conclusion": "failure",
            }
        ]
    )
    client.get_json.return_value = {"files": [{"filename": "src/index.ts"}]}
    output_path = tmp_path / "runs.json"

    assert collect_dataset(client, ["owner/repo"], output_path) == 0
    assert json.loads(output_path.read_text(encoding="utf-8")) == []
