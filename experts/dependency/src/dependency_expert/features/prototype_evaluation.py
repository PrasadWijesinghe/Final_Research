"""Data audit and repository-held-out evaluation for the Dependency prototype."""

import csv
from collections import Counter
from pathlib import Path
from typing import NotRequired, TypedDict

from sklearn.dummy import DummyClassifier  # type: ignore[import-untyped]
from sklearn.linear_model import LogisticRegression  # type: ignore[import-untyped]
from sklearn.metrics import (  # type: ignore[import-untyped]
    accuracy_score,
    balanced_accuracy_score,
    f1_score,
    precision_score,
    recall_score,
    roc_auc_score,
)
from sklearn.model_selection import GroupShuffleSplit  # type: ignore[import-untyped]
from sklearn.pipeline import make_pipeline  # type: ignore[import-untyped]
from sklearn.preprocessing import StandardScaler  # type: ignore[import-untyped]

REQUIRED_COLUMNS = (
    "run_id",
    "attempt",
    "repository",
    "commit_sha",
    "total_dependency_files_changed",
    "has_package_json_change",
    "has_lockfile_change",
    "is_failure",
)
FEATURE_COLUMNS = (
    "total_dependency_files_changed",
    "has_package_json_change",
    "has_lockfile_change",
)
# DEFAULT: 42 makes the held-out repository selection repeatable.
DEFAULT_SEED = 42
# DEFAULT: reserve one fifth of repositories for a simple exploratory holdout.
DEFAULT_TEST_SIZE = 0.2


class EvaluationRow(TypedDict):
    run_id: str
    attempt: int
    repository: str
    commit_sha: str
    total_dependency_files_changed: int
    has_package_json_change: int
    has_lockfile_change: int
    is_failure: int


class LabelCounts(TypedDict):
    success: int
    workflow_failure: int


class DatasetAudit(TypedDict):
    input_rows: int
    unique_rows: int
    identical_duplicate_rows_removed: int
    repository_count: int
    label_counts: LabelCounts
    workflow_failure_rate: float


class MetricScores(TypedDict):
    accuracy: float
    balanced_accuracy: float
    precision: float
    recall: float
    f1: float
    roc_auc: NotRequired[float]


class EvaluationSplit(TypedDict):
    method: str
    train_rows: int
    test_rows: int
    train_label_counts: LabelCounts
    test_label_counts: LabelCounts
    held_out_repositories: list[str]


class EvaluationReport(TypedDict):
    target: str
    seed: int
    test_size: float
    data_quality: DatasetAudit
    split: EvaluationSplit
    metrics: dict[str, MetricScores]
    interpretation: str


def read_and_audit_dataset(csv_path: Path) -> tuple[list[EvaluationRow], DatasetAudit]:
    """Validate the feature CSV, collapse identical duplicate runs, and summarize its labels."""
    with csv_path.open(encoding="utf-8", newline="") as input_file:
        reader = csv.DictReader(input_file)
        columns = reader.fieldnames
        if columns is None:
            raise ValueError("dataset CSV must include a header")
        missing_columns = sorted(set(REQUIRED_COLUMNS) - set(columns))
        if missing_columns:
            raise ValueError(f"dataset CSV is missing columns: {', '.join(missing_columns)}")

        rows_by_run: dict[tuple[str, str, int], EvaluationRow] = {}
        input_rows = 0
        duplicate_rows = 0
        for line_number, raw in enumerate(reader, start=2):
            input_rows += 1
            row = _parse_row(raw, line_number)
            key = (row["repository"], row["run_id"], row["attempt"])
            previous = rows_by_run.get(key)
            if previous is not None:
                if previous != row:
                    raise ValueError(
                        f"conflicting duplicate repository/run_id/attempt at CSV line {line_number}"
                    )
                duplicate_rows += 1
                continue
            rows_by_run[key] = row

    rows = list(rows_by_run.values())
    if not rows:
        raise ValueError("dataset CSV must contain at least one data row")
    repositories = {row["repository"] for row in rows}
    label_counts = Counter(str(row["is_failure"]) for row in rows)
    data_quality: DatasetAudit = {
        "input_rows": input_rows,
        "unique_rows": len(rows),
        "identical_duplicate_rows_removed": duplicate_rows,
        "repository_count": len(repositories),
        "label_counts": {
            "success": label_counts["0"],
            "workflow_failure": label_counts["1"],
        },
        "workflow_failure_rate": label_counts["1"] / len(rows),
    }
    return rows, data_quality


def _parse_row(raw: dict[str, str | None], line_number: int) -> EvaluationRow:
    """Parse one feature row and reject missing or out-of-range values."""
    identifiers = ("run_id", "repository", "commit_sha")
    for name in identifiers:
        if not (raw.get(name) or "").strip():
            raise ValueError(f"CSV line {line_number} has an empty {name}")

    parsed_identifiers = {name: (raw[name] or "").strip() for name in identifiers}
    try:
        attempt = int((raw.get("attempt") or "").strip())
    except ValueError as error:
        raise ValueError(f"CSV line {line_number} has an invalid integer in attempt") from error
    if attempt < 1:
        raise ValueError(f"CSV line {line_number} has a non-positive attempt")
    parsed_features: dict[str, int] = {}
    for name in FEATURE_COLUMNS + ("is_failure",):
        value = (raw.get(name) or "").strip()
        try:
            parsed_features[name] = int(value)
        except ValueError as error:
            raise ValueError(f"CSV line {line_number} has an invalid integer in {name}") from error

    if parsed_features["total_dependency_files_changed"] < 0:
        raise ValueError(f"CSV line {line_number} has a negative dependency file count")
    for name in ("has_package_json_change", "has_lockfile_change", "is_failure"):
        if parsed_features[name] not in (0, 1):
            raise ValueError(f"CSV line {line_number} has a non-binary value in {name}")

    return EvaluationRow(
        run_id=parsed_identifiers["run_id"],
        attempt=attempt,
        repository=parsed_identifiers["repository"],
        commit_sha=parsed_identifiers["commit_sha"],
        total_dependency_files_changed=parsed_features["total_dependency_files_changed"],
        has_package_json_change=parsed_features["has_package_json_change"],
        has_lockfile_change=parsed_features["has_lockfile_change"],
        is_failure=parsed_features["is_failure"],
    )


def evaluate_dataset(
    rows: list[EvaluationRow],
    data_quality: DatasetAudit,
    seed: int = DEFAULT_SEED,
    test_size: float = DEFAULT_TEST_SIZE,
) -> EvaluationReport:
    """Compare a majority baseline and logistic regression on held-out repositories."""
    if not 0 < test_size < 1:
        raise ValueError("test_size must be between 0 and 1")
    if len({row["repository"] for row in rows}) < 2:
        raise ValueError("at least two repositories are required for a grouped split")
    if len({row["is_failure"] for row in rows}) < 2:
        raise ValueError("dataset must contain both success and workflow-failure labels")

    features = [
        [
            row["total_dependency_files_changed"],
            row["has_package_json_change"],
            row["has_lockfile_change"],
        ]
        for row in rows
    ]
    labels = [row["is_failure"] for row in rows]
    groups = [row["repository"] for row in rows]
    splitter = GroupShuffleSplit(n_splits=1, test_size=test_size, random_state=seed)
    train_indices, test_indices = next(splitter.split(features, labels, groups))
    train_labels = [labels[index] for index in train_indices]
    test_labels = [labels[index] for index in test_indices]
    if len(set(train_labels)) < 2 or len(set(test_labels)) < 2:
        raise ValueError("repository-held-out split must contain both labels in train and test")

    train_features = [features[index] for index in train_indices]
    test_features = [features[index] for index in test_indices]
    held_out_repositories = sorted({groups[index] for index in test_indices})
    majority = DummyClassifier(strategy="most_frequent")
    majority.fit(train_features, train_labels)
    logistic = make_pipeline(
        StandardScaler(),
        # DEFAULT: allow extra iterations for stable convergence on this small baseline.
        LogisticRegression(max_iter=1000, random_state=seed),
    )
    logistic.fit(train_features, train_labels)

    return {
        "target": (
            "is_failure (workflow outcome only; not ground truth that a dependency change "
            "caused the failure)"
        ),
        "seed": seed,
        "test_size": test_size,
        "data_quality": data_quality,
        "split": {
            "method": "GroupShuffleSplit by repository",
            "train_rows": len(train_indices),
            "test_rows": len(test_indices),
            "train_label_counts": _label_counts(train_labels),
            "test_label_counts": _label_counts(test_labels),
            "held_out_repositories": held_out_repositories,
        },
        "metrics": {
            "majority_baseline": _metrics(
                [int(value) for value in majority.predict(test_features)],
                test_labels,
            ),
            "logistic_regression": _metrics(
                [int(value) for value in logistic.predict(test_features)],
                test_labels,
                [float(value) for value in logistic.predict_proba(test_features)[:, 1]],
            ),
        },
        "interpretation": (
            "Exploratory workflow-failure prediction for dependency-change runs; "
            "not a production or dependency-causality result."
        ),
    }


def _label_counts(labels: list[int]) -> LabelCounts:
    counts = Counter(labels)
    return {"success": counts[0], "workflow_failure": counts[1]}


def _metrics(
    predictions: list[int],
    actual: list[int],
    probabilities: list[float] | None = None,
) -> MetricScores:
    result: MetricScores = {
        "accuracy": float(accuracy_score(actual, predictions)),
        "balanced_accuracy": float(balanced_accuracy_score(actual, predictions)),
        "precision": float(precision_score(actual, predictions, zero_division=0)),
        "recall": float(recall_score(actual, predictions, zero_division=0)),
        "f1": float(f1_score(actual, predictions, zero_division=0)),
    }
    if probabilities is not None:
        result["roc_auc"] = float(roc_auc_score(actual, probabilities))
    return result
