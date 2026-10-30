"""Evaluate and export a verified-label XGBoost model using sanitized-log flags only."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import pandas as pd
from dependency_expert.features.log_features import LOG_FEATURE_COLUMNS
from sklearn.dummy import DummyClassifier
from sklearn.metrics import (
    accuracy_score,
    confusion_matrix,
    f1_score,
    precision_score,
    recall_score,
)
from sklearn.model_selection import GroupKFold
from xgboost import XGBClassifier

REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_DATASET = (
    REPOSITORY_ROOT / "data/datasets/dependency/master_training_table_with_log_features.csv"
)
DEFAULT_MODEL = REPOSITORY_ROOT / "models/dependency/xgboost_log_only.joblib"
DEFAULT_REPORT = REPOSITORY_ROOT / "data/datasets/dependency/evaluation_report_log_only.json"
LABELS = {"other_failure": 0, "dependency_failure": 1}
FOLDS = 5
SEED = 42


def _make_xgboost() -> XGBClassifier:
    return XGBClassifier(
        n_estimators=50,
        max_depth=2,
        learning_rate=0.05,
        eval_metric="logloss",
        random_state=SEED,
        n_jobs=1,
    )


def _metric_report(actual: np.ndarray, predicted: np.ndarray) -> dict[str, Any]:
    return {
        "accuracy": float(accuracy_score(actual, predicted)),
        "precision_dependency_failure": float(
            precision_score(actual, predicted, pos_label=1, zero_division=0)
        ),
        "recall_dependency_failure": float(
            recall_score(actual, predicted, pos_label=1, zero_division=0)
        ),
        "f1_dependency_failure": float(f1_score(actual, predicted, pos_label=1, zero_division=0)),
        "macro_f1": float(f1_score(actual, predicted, average="macro", zero_division=0)),
        "confusion_matrix_other_then_dependency": confusion_matrix(
            actual, predicted, labels=[0, 1]
        ).tolist(),
    }


def train_log_only_model(
    dataset_path: Path = DEFAULT_DATASET,
    model_path: Path = DEFAULT_MODEL,
    report_path: Path = DEFAULT_REPORT,
) -> dict[str, Any]:
    """Run repository-held-out CV, fit all complete rows, and export the inference bundle."""
    frame = pd.read_csv(dataset_path)
    required = {
        "repository",
        "run_id",
        "attempt",
        "verified_label",
        "log_evidence_complete",
        *LOG_FEATURE_COLUMNS,
    }
    missing = sorted(required - set(frame.columns))
    if missing:
        raise ValueError(f"dataset is missing required columns: {', '.join(missing)}")

    complete = pd.to_numeric(frame["log_evidence_complete"], errors="raise") == 1
    verified_binary = frame["verified_label"].isin(LABELS)
    records_excluded = int((~(complete & verified_binary)).sum())
    frame = frame.loc[complete & verified_binary].copy()
    if frame.empty:
        raise ValueError("no complete verified binary records are available")
    if frame.duplicated(["repository", "run_id", "attempt"]).any():
        raise ValueError("dataset contains duplicate repository/run_id/attempt keys")

    X = frame.loc[:, LOG_FEATURE_COLUMNS].apply(pd.to_numeric, errors="raise")
    if not np.isfinite(X.to_numpy(dtype=float)).all() or not set(np.unique(X)).issubset({0, 1}):
        raise ValueError("log feature columns must contain only finite boolean/0/1 values")
    y = frame["verified_label"].map(LABELS).to_numpy(dtype=int)
    groups = frame["repository"].astype(str).to_numpy()
    if len(set(groups)) < FOLDS or set(np.unique(y)) != {0, 1}:
        raise ValueError("five-fold GroupKFold requires at least five repositories and both labels")

    splitter = GroupKFold(n_splits=FOLDS)
    predictions = np.full(len(frame), -1, dtype=int)
    baseline_predictions = np.full(len(frame), -1, dtype=int)
    fold_details: list[dict[str, Any]] = []
    for fold, (train_index, test_index) in enumerate(splitter.split(X, y, groups), start=1):
        train_groups = set(groups[train_index])
        test_groups = set(groups[test_index])
        if train_groups & test_groups:
            raise ValueError(f"repository leakage detected in fold {fold}")
        estimator = _make_xgboost()
        estimator.fit(X.iloc[train_index], y[train_index])
        predictions[test_index] = estimator.predict(X.iloc[test_index]).astype(int)

        baseline = DummyClassifier(strategy="most_frequent")
        baseline.fit(X.iloc[train_index], y[train_index])
        baseline_predictions[test_index] = baseline.predict(X.iloc[test_index]).astype(int)
        fold_details.append(
            {
                "fold": fold,
                "train_records": len(train_index),
                "test_records": len(test_index),
                "train_repositories": len(train_groups),
                "held_out_repositories": sorted(test_groups),
            }
        )

    if np.any(predictions < 0) or np.any(baseline_predictions < 0):
        raise RuntimeError("GroupKFold did not generate a prediction for every complete record")

    model_metrics = _metric_report(y, predictions)
    baseline_metrics = _metric_report(y, baseline_predictions)
    final_model = _make_xgboost()
    final_model.fit(X, y)
    model_path.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(
        {
            "model": final_model,
            "feature_columns": list(LOG_FEATURE_COLUMNS),
            "positive_class": "dependency_failure",
            "model_type": "XGBoost",
            "training_records": len(frame),
            "training_repositories": len(set(groups)),
            "evaluation_strategy": "5-fold repository-held-out GroupKFold",
            "seed": SEED,
        },
        model_path,
    )

    report: dict[str, Any] = {
        "dataset": {
            "path": str(dataset_path),
            "records_evaluated": len(frame),
            "records_excluded_for_incomplete_or_unverified_data": records_excluded,
            "repositories": len(set(groups)),
            "label_counts": {
                label: int((frame["verified_label"] == label).sum()) for label in LABELS
            },
            "features": list(LOG_FEATURE_COLUMNS),
            "feature_source": "extract_sanitized_log_features; 14 boolean flags only",
        },
        "evaluation": {
            "status": "Exploratory / preliminary baseline",
            "strategy": "5-fold repository-held-out GroupKFold",
            "seed": SEED,
            "xgboost": model_metrics,
            "majority_baseline": baseline_metrics,
            "folds": fold_details,
        },
        "artifact": str(model_path),
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--model", type=Path, default=DEFAULT_MODEL)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    args = parser.parse_args()
    report = train_log_only_model(args.dataset, args.model, args.report)
    metrics = report["evaluation"]["xgboost"]
    print(
        f"Log-only XGBoost accuracy: {metrics['accuracy']:.4f} "
        f"(N={report['dataset']['records_evaluated']})"
    )
    print(f"Artifact: {args.model}")
    print(f"Report: {args.report}")


if __name__ == "__main__":
    main()
