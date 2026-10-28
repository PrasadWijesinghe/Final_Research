"""Evaluate verified dependency labels with repository-held-out cross-validation."""

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from dependency_expert.features.log_features import LOG_FEATURE_COLUMNS
from sklearn.dummy import DummyClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (
    accuracy_score,
    balanced_accuracy_score,
    confusion_matrix,
    f1_score,
    precision_score,
    recall_score,
)
from sklearn.model_selection import GroupKFold
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler
from xgboost import XGBClassifier

REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_CSV = (
    REPOSITORY_ROOT / "data/datasets/dependency/master_training_table_with_log_features.csv"
)
DEFAULT_REPORT = REPOSITORY_ROOT / "data/datasets/dependency/evaluation_report_verified.json"
DEFAULT_MISCLASSIFIED = (
    REPOSITORY_ROOT / "data/datasets/dependency/misclassified_records_best_model.csv"
)
FEATURE_COLUMNS_WITH_DIAGNOSTICS = (
    "total_dependency_files_changed",
    "has_package_json_change",
    "has_lockfile_change",
    *LOG_FEATURE_COLUMNS,
)
CACHE_MISS_DIAGNOSTIC = "has_package_manager_cache_miss"
FEATURE_COLUMNS = tuple(
    column for column in FEATURE_COLUMNS_WITH_DIAGNOSTICS if column != CACHE_MISS_DIAGNOSTIC
)
LABELS = {"dependency_failure": 1, "other_failure": 0}
DEFAULT_FOLDS = 5
DEFAULT_SEED = 42


def _counts(values: pd.Series) -> dict[str, int]:
    """Return class counts with stable readable label names."""
    return {
        "dependency_failure": int((values == 1).sum()),
        "other_failure": int((values == 0).sum()),
    }


def _metrics(actual: np.ndarray, predicted: np.ndarray) -> dict[str, Any]:
    """Calculate metrics with dependency_failure as the positive class."""
    return {
        "accuracy": float(accuracy_score(actual, predicted)),
        "balanced_accuracy": float(balanced_accuracy_score(actual, predicted)),
        "precision_dependency_failure": float(
            precision_score(actual, predicted, pos_label=1, zero_division=0)
        ),
        "recall_dependency_failure": float(
            recall_score(actual, predicted, pos_label=1, zero_division=0)
        ),
        "f1_dependency_failure": float(f1_score(actual, predicted, pos_label=1, zero_division=0)),
        "macro_f1": float(f1_score(actual, predicted, average="macro", zero_division=0)),
        "confusion_matrix_labels_other_then_dependency": confusion_matrix(
            actual, predicted, labels=[0, 1]
        ).tolist(),
    }


def _evaluate_grouped(
    name: str,
    make_estimator: Callable[[], Any],
    features: pd.DataFrame,
    target: np.ndarray,
    repositories: np.ndarray,
    folds: int,
    seed: int,
) -> dict[str, Any]:
    """Collect out-of-fold predictions and fold details without splitting repositories."""
    splitter = GroupKFold(n_splits=folds)
    predictions = np.full(len(target), -1, dtype=int)
    prediction_folds = np.full(len(target), -1, dtype=int)
    fold_reports: list[dict[str, Any]] = []

    for fold_number, (train_idx, test_idx) in enumerate(
        splitter.split(features, target, groups=repositories), start=1
    ):
        train_repositories = set(repositories[train_idx])
        test_repositories = set(repositories[test_idx])
        if train_repositories & test_repositories:
            raise ValueError(f"repository leakage detected in fold {fold_number}")
        if len(np.unique(target[train_idx])) < 2:
            raise ValueError(f"fold {fold_number} training data contains only one class")

        estimator = make_estimator()
        estimator.fit(features.iloc[train_idx], target[train_idx])
        predictions[test_idx] = estimator.predict(features.iloc[test_idx]).astype(int)
        prediction_folds[test_idx] = fold_number
        fold_reports.append(
            {
                "fold": fold_number,
                "train_records": len(train_idx),
                "test_records": len(test_idx),
                "train_repositories": len(train_repositories),
                "held_out_repositories": sorted(test_repositories),
                "train_label_counts": _counts(pd.Series(target[train_idx])),
                "test_label_counts": _counts(pd.Series(target[test_idx])),
            }
        )

    if np.any(predictions < 0):
        raise RuntimeError(f"{name} did not produce an out-of-fold prediction for every row")
    return {
        "metrics": _metrics(target, predictions),
        "folds": fold_reports,
        "predictions": predictions.tolist(),
        "prediction_folds": prediction_folds.tolist(),
    }


def evaluate_verified_dataset(
    csv_path: Path,
    folds: int = DEFAULT_FOLDS,
    seed: int = DEFAULT_SEED,
) -> dict[str, Any]:
    """Evaluate simple classifiers on the clean, feature-joined manual-label table."""
    frame = pd.read_csv(csv_path)
    required = {
        "repository",
        "run_id",
        "attempt",
        "verified_label",
        "log_evidence_complete",
        *FEATURE_COLUMNS,
    }
    missing = sorted(required - set(frame.columns))
    if missing:
        raise ValueError(f"master CSV is missing required columns: {', '.join(missing)}")
    if frame.empty:
        raise ValueError("master CSV must contain at least one record")
    incomplete_mask = pd.to_numeric(frame["log_evidence_complete"], errors="raise") != 1
    incomplete_records = int(incomplete_mask.sum())
    master_label_counts = {label: int((frame["verified_label"] == label).sum()) for label in LABELS}
    frame = frame.loc[~incomplete_mask].copy()
    if frame.empty:
        raise ValueError("no records have complete failed-job log evidence")
    if frame[["repository", "run_id", "attempt"]].isna().any().any():
        raise ValueError("master CSV contains a missing composite-key field")
    if frame.duplicated(["repository", "run_id", "attempt"]).any():
        raise ValueError("master CSV contains duplicate repository/run_id/attempt keys")

    observed_labels = set(frame["verified_label"].dropna().astype(str))
    if observed_labels != set(LABELS):
        raise ValueError("master CSV must contain only dependency_failure and other_failure labels")
    all_features = frame.loc[:, FEATURE_COLUMNS_WITH_DIAGNOSTICS].apply(
        pd.to_numeric, errors="raise"
    )
    features = all_features.loc[:, FEATURE_COLUMNS]
    if not np.isfinite(features.to_numpy(dtype=float)).all():
        raise ValueError("feature columns must contain finite numeric values")
    if (features["total_dependency_files_changed"] < 0).any():
        raise ValueError("total_dependency_files_changed cannot be negative")
    for column in ("has_package_json_change", "has_lockfile_change"):
        if not set(features[column].unique()).issubset({0, 1}):
            raise ValueError(f"{column} must contain only 0 or 1")

    target = frame["verified_label"].map(LABELS).to_numpy(dtype=int)
    repositories = frame["repository"].astype(str).to_numpy()
    repository_count = len(set(repositories))
    if repository_count < folds:
        raise ValueError(f"at least {folds} repositories are required for {folds}-fold GroupKFold")
    if set(np.unique(target)) != {0, 1}:
        raise ValueError("master CSV must contain both verified labels")

    majority_label = Counter(target.tolist()).most_common(1)[0][0]
    majority_name = "dependency_failure" if majority_label == 1 else "other_failure"
    models = {
        "logistic_regression": lambda: make_pipeline(
            StandardScaler(), LogisticRegression(max_iter=1000, random_state=seed)
        ),
        "xgboost": lambda: XGBClassifier(
            n_estimators=50,
            max_depth=2,
            learning_rate=0.05,
            eval_metric="logloss",
            random_state=seed,
            n_jobs=1,
        ),
    }
    model_results = {
        name: _evaluate_grouped(name, factory, features, target, repositories, folds, seed)
        for name, factory in models.items()
    }
    model_results_with_cache_miss = {
        name: _evaluate_grouped(
            f"{name}_with_cache_miss",
            factory,
            all_features,
            target,
            repositories,
            folds,
            seed,
        )
        for name, factory in models.items()
    }
    model_results_without_cache_miss = {
        name: _evaluate_grouped(
            f"{name}_without_cache_miss",
            factory,
            features,
            target,
            repositories,
            folds,
            seed,
        )
        for name, factory in models.items()
    }
    dummy_result = _evaluate_grouped(
        "majority_dummy",
        lambda: DummyClassifier(strategy="most_frequent"),
        features,
        target,
        repositories,
        folds,
        seed,
    )

    best_model_name = max(
        model_results,
        key=lambda name: (
            model_results[name]["metrics"]["accuracy"],
            model_results[name]["metrics"]["macro_f1"],
        ),
    )
    best_result = model_results[best_model_name]
    label_names = {value: name for name, value in LABELS.items()}
    misclassified_records: list[dict[str, Any]] = []
    for index, (actual, predicted) in enumerate(
        zip(target, best_result["predictions"], strict=True)
    ):
        if actual == predicted:
            continue
        source_row = frame.iloc[index]
        misclassified_records.append(
            {
                "repository": str(source_row["repository"]),
                "run_id": str(source_row["run_id"]),
                "attempt": int(source_row["attempt"]),
                "fold": best_result["prediction_folds"][index],
                "verified_label": label_names[int(actual)],
                "predicted_label": label_names[int(predicted)],
                **{column: int(source_row[column]) for column in FEATURE_COLUMNS},
            }
        )

    return {
        "dataset": {
            "path": str(csv_path),
            "master_records": len(frame) + incomplete_records,
            "records_evaluated": len(frame),
            "records_excluded_for_incomplete_logs": incomplete_records,
            "repositories": repository_count,
            "label_counts": {
                "dependency_failure": int((target == 1).sum()),
                "other_failure": int((target == 0).sum()),
            },
            "master_label_counts_before_log_availability_filter": master_label_counts,
            "majority_class": majority_name,
            "majority_class_share": float(max(np.mean(target == 0), np.mean(target == 1))),
            "features": list(FEATURE_COLUMNS),
            "diagnostic_only_columns": [CACHE_MISS_DIAGNOSTIC],
            "excluded_columns": [
                "repository",
                "run_id",
                "attempt",
                "commit_sha",
                "is_failure",
                "verified_reason",
                "source_file",
                "log_scan_status",
                "failed_jobs_expected",
                "failed_jobs_fetched",
                "failed_jobs_unavailable",
                "transport_retry_count",
                "transport_failure_count",
                "redaction_count",
                "request_statuses",
                CACHE_MISS_DIAGNOSTIC,
            ],
            "raw_logs_used": False,
            "verified_reason_used": False,
        },
        "evaluation": {
            "status": "Exploratory / preliminary baseline",
            "strategy": f"{folds}-fold GroupKFold grouped by repository",
            "seed": seed,
            "positive_class": "dependency_failure",
            "folds": dummy_result["folds"],
            "pooled_out_of_fold_metrics": {
                "majority_dummy": dummy_result["metrics"],
                **{name: result["metrics"] for name, result in model_results.items()},
            },
            "limitations": [
                (
                    "The manually labeled dataset is small; metrics are exploratory, and the "
                    "complete-log subset may be smaller still."
                ),
                (
                    "Log-derived features are keyword indicators from sanitized logs, not a full "
                    "representation of failure evidence."
                ),
                (
                    "Out-of-fold results do not establish production performance or causal "
                    "attribution."
                ),
            ],
        },
        "feature_ablation": {
            "purpose": "Check whether has_package_manager_cache_miss adds useful held-out signal.",
            "fold_strategy": f"{folds}-fold GroupKFold grouped by repository",
            "models": {
                name: {
                    "with_cache_miss_feature": model_results_with_cache_miss[name]["metrics"],
                    "without_cache_miss_feature": model_results_without_cache_miss[name]["metrics"],
                }
                for name in models
            },
        },
        "misclassification_review": {
            "best_model_by_accuracy_then_macro_f1": best_model_name,
            "selection_metric": "pooled out-of-fold accuracy, then macro F1 as tie-breaker",
            "misclassified_count": len(misclassified_records),
            "records": misclassified_records,
            "verified_reason_and_raw_logs_used_as_features": False,
        },
    }


def main() -> None:
    """Write a reproducible repository-held-out verified-label evaluation report."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--misclassified-output", type=Path, default=DEFAULT_MISCLASSIFIED)
    parser.add_argument("--folds", type=int, default=DEFAULT_FOLDS)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    args = parser.parse_args()
    if args.folds < 2:
        parser.error("--folds must be at least 2")

    report = evaluate_verified_dataset(args.csv, args.folds, args.seed)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    with args.report.open("w", encoding="utf-8") as output_file:
        json.dump(report, output_file, indent=2)
        output_file.write("\n")
    review_records = report["misclassification_review"]["records"]
    review_fields = [
        "repository",
        "run_id",
        "attempt",
        "fold",
        "verified_label",
        "predicted_label",
        *FEATURE_COLUMNS,
    ]
    args.misclassified_output.parent.mkdir(parents=True, exist_ok=True)
    with args.misclassified_output.open("w", encoding="utf-8", newline="") as output_file:
        writer = csv.DictWriter(output_file, fieldnames=review_fields)
        writer.writeheader()
        writer.writerows(review_records)
    print(f"Wrote verified-label evaluation report to {args.report}")
    print(
        "Best model: "
        f"{report['misclassification_review']['best_model_by_accuracy_then_macro_f1']}; "
        f"misclassified: {len(review_records)}"
    )
    print(f"Wrote misclassification review to {args.misclassified_output}")
    print(json.dumps(report["evaluation"]["pooled_out_of_fold_metrics"], indent=2))


if __name__ == "__main__":
    main()
