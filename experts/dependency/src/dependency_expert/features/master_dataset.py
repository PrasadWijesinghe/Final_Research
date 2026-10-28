"""Audit reviewed Dependency labels and join them to prototype features."""

from __future__ import annotations

import csv
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[5]
DEFAULT_DATASET_DIR = REPOSITORY_ROOT / "data/datasets/dependency"
DEFAULT_FEATURE_CSV = REPOSITORY_ROOT / "data/datasets/dependency/prototype/dependency_features.csv"
AUDIT_FIELDS = (
    "repository",
    "run_id",
    "attempt",
    "verified_label",
    "verified_reason",
    "source_file",
    "feature_matched",
)
TRAINING_LABELS = {"dependency_failure", "other_failure"}
EXCLUDED_LABELS = {"uncertain", "mixed_failure_causes"}
FEATURE_COLUMNS = (
    "commit_sha",
    "total_dependency_files_changed",
    "has_package_json_change",
    "has_lockfile_change",
    "is_failure",
)
TRAINING_FIELDS = (
    "repository",
    "run_id",
    "attempt",
    "verified_label",
    "verified_reason",
    "source_file",
    *FEATURE_COLUMNS,
)

type RunKey = tuple[str, str, int]
type LabelRecord = dict[str, str]


def _label_files(dataset_dir: Path) -> list[Path]:
    """Return local labeled review, candidate batch, and random control JSON files."""
    files = set(dataset_dir.glob("labeled_*.json"))
    files.update(dataset_dir.glob("*candidate*.json"))
    files.update(dataset_dir.glob("random_control*.json"))
    return sorted(files)


def _read_final_labels(dataset_dir: Path) -> list[LabelRecord]:
    """Read final run-level labels and retain each record's source file."""
    records: list[LabelRecord] = []
    for source_path in _label_files(dataset_dir):
        with source_path.open(encoding="utf-8") as input_file:
            data: Any = json.load(input_file)
        if isinstance(data, dict):
            data = data.get("candidates", data.get("controls"))
        if not isinstance(data, list) or any(not isinstance(row, dict) for row in data):
            raise ValueError(f"{source_path} must contain an array of labeled run objects")

        for index, row in enumerate(data):
            label = row.get("verified_label")
            if label is None or row.get("is_final") is not True:
                continue
            repository = row.get("repository")
            run_id = row.get("run_id")
            if not isinstance(repository, str) or not repository or run_id is None:
                raise ValueError(f"{source_path} record {index} lacks repository or run_id")
            try:
                attempt = int(row.get("attempt", 1))
            except (TypeError, ValueError) as error:
                raise ValueError(f"{source_path} record {index} has an invalid attempt") from error
            if attempt < 1:
                raise ValueError(f"{source_path} record {index} has a non-positive attempt")
            if not isinstance(label, str):
                raise ValueError(f"{source_path} record {index} has a non-text verified_label")
            reason = row.get("verified_reason")
            records.append(
                {
                    "repository": repository,
                    "run_id": str(run_id),
                    "attempt": str(attempt),
                    "verified_label": label,
                    "verified_reason": reason if isinstance(reason, str) else "",
                    "source_file": source_path.name,
                    "feature_matched": "false",
                }
            )
    return records


def _read_features(feature_csv: Path) -> dict[RunKey, dict[str, str]]:
    """Read feature rows indexed by repository, run ID, and attempt."""
    features_by_key: dict[RunKey, dict[str, str]] = {}
    with feature_csv.open(encoding="utf-8", newline="") as input_file:
        reader = csv.DictReader(input_file)
        if reader.fieldnames is None:
            raise ValueError("feature CSV must include a header")
        missing = {"repository", "run_id", "attempt", *FEATURE_COLUMNS} - set(reader.fieldnames)
        if missing:
            raise ValueError("feature CSV is missing columns: " + ", ".join(sorted(missing)))
        for line_number, row in enumerate(reader, start=2):
            repository = (row.get("repository") or "").strip()
            run_id = (row.get("run_id") or "").strip()
            try:
                attempt = int(row.get("attempt") or "")
            except ValueError as error:
                raise ValueError(
                    f"feature CSV line {line_number} has an invalid attempt"
                ) from error
            if not repository or not run_id or attempt < 1:
                raise ValueError(f"feature CSV line {line_number} has an invalid composite key")
            key = (repository, run_id, attempt)
            feature_row = {column: (row.get(column) or "").strip() for column in FEATURE_COLUMNS}
            previous = features_by_key.get(key)
            if previous is not None and previous != feature_row:
                raise ValueError(
                    "conflicting duplicate feature key "
                    f"{repository}/{run_id} attempt {attempt} at CSV line {line_number}"
                )
            features_by_key[key] = feature_row
    return features_by_key


def _write_csv(path: Path, fieldnames: tuple[str, ...], rows: list[dict[str, str]]) -> None:
    """Write rows to a CSV file with a stable header."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as output_file:
        writer = csv.DictWriter(output_file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def audit_and_prepare_dataset(dataset_dir: Path, feature_csv: Path) -> dict[str, Any]:
    """Write source audit, conflict report, and clean feature-joined training table."""
    source_records = _read_final_labels(dataset_dir)
    if not source_records:
        raise ValueError(f"no final run-level labels found under {dataset_dir}")
    features = _read_features(feature_csv)

    grouped: dict[RunKey, list[LabelRecord]] = defaultdict(list)
    audit_records: list[LabelRecord] = []
    for record in source_records:
        key = (record["repository"], record["run_id"], int(record["attempt"]))
        feature_row = features.get(key)
        if feature_row is not None:
            record["feature_matched"] = "true"
        audit_records.append(record)
        grouped[key].append(record)

    conflicts: list[dict[str, str]] = []
    training_rows: list[dict[str, str]] = []
    duplicate_rows_removed = 0
    feature_unmatched_keys = 0
    conflict_keys: set[RunKey] = set()
    for key, group in sorted(grouped.items()):
        unique_labels = sorted({record["verified_label"] for record in group})
        duplicate_rows_removed += len(group) - 1
        if len(unique_labels) != 1:
            conflict_keys.add(key)
            conflicts.extend(
                record | {"conflicting_labels": json.dumps(unique_labels)} for record in group
            )
            continue

        label = unique_labels[0]
        if label in EXCLUDED_LABELS or label not in TRAINING_LABELS:
            continue
        feature_row = features.get(key)
        if feature_row is None:
            feature_unmatched_keys += 1
            continue
        canonical = group[0]
        training_rows.append(
            {
                **{field: canonical[field] for field in AUDIT_FIELDS[:-1]},
                **feature_row,
            }
        )

    _write_csv(dataset_dir / "audit_full_records.csv", AUDIT_FIELDS, audit_records)
    _write_csv(
        dataset_dir / "audit_label_conflicts.csv",
        (*AUDIT_FIELDS, "conflicting_labels"),
        conflicts,
    )
    _write_csv(dataset_dir / "master_training_table.csv", TRAINING_FIELDS, training_rows)

    counts = {
        label: sum(row["verified_label"] == label for row in training_rows)
        for label in sorted(TRAINING_LABELS)
    }
    report = {
        "source_records": len(source_records),
        "unique_composite_keys": len(grouped),
        "duplicate_rows_removed": duplicate_rows_removed,
        "conflicting_composite_keys": len(conflict_keys),
        "feature_unmatched_training_keys": feature_unmatched_keys,
        "training_rows": len(training_rows),
        "training_label_counts": counts,
        "audit_output": str(dataset_dir / "audit_full_records.csv"),
        "conflict_output": str(dataset_dir / "audit_label_conflicts.csv"),
        "training_output": str(dataset_dir / "master_training_table.csv"),
    }
    print("=== Dependency label audit ===")
    print(f"Final labeled source records: {report['source_records']}")
    print(f"Unique composite keys: {report['unique_composite_keys']}")
    print(f"Duplicate source rows removed from training: {report['duplicate_rows_removed']}")
    print(f"Composite keys with conflicting labels: {report['conflicting_composite_keys']}")
    print(
        "Binary-labeled keys without matching features: "
        f"{report['feature_unmatched_training_keys']}"
    )
    print(f"Training rows: {report['training_rows']}")
    print(f"Training label counts: {counts}")
    print(f"Full audit: {report['audit_output']}")
    print(f"Conflicts: {report['conflict_output']}")
    print(f"Training table: {report['training_output']}")
    return report
