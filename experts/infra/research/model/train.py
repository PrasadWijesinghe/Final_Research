"""Train and select binary Infrastructure failure model candidates."""

from __future__ import annotations

import argparse
import json
import platform
import warnings
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import pandas as pd
from sklearn.dummy import DummyClassifier
from sklearn.ensemble import RandomForestClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (
    accuracy_score,
    average_precision_score,
    brier_score_loss,
    confusion_matrix,
    f1_score,
    matthews_corrcoef,
    precision_recall_curve,
    precision_score,
    recall_score,
    roc_auc_score,
)
from sklearn.pipeline import Pipeline

from .calibration import fit_sigmoid_calibrator, split_model_fit_and_calibration
from .dataset import DEFAULT_INPUT, PreparedDataset, load_prepared_dataset
from .features import (
    FEATURE_PROFILES,
    FEATURE_SCHEMA_VERSION,
    TARGET_COLUMN,
    build_preprocessor,
    feature_columns,
)

RESEARCH_DIR = Path(__file__).resolve().parents[1]
DEFAULT_ARTIFACTS_DIR = RESEARCH_DIR / "artifacts"
RANDOM_STATE = 42


def candidate_models(y_train: np.ndarray) -> dict[str, Any]:
    """Return reproducible candidates with imbalance-aware training."""
    try:
        from xgboost import XGBClassifier
    except ImportError as exc:  # pragma: no cover - exercised only in an incomplete environment
        raise RuntimeError("install research/requirements.txt before training") from exc

    positive = int(y_train.sum())
    negative = int(y_train.size - positive)
    if positive == 0:
        raise ValueError("the training split contains no positive examples")
    scale_pos_weight = negative / positive
    return {
        "dummy_prior": DummyClassifier(strategy="prior"),
        "logistic_regression": LogisticRegression(
            class_weight="balanced",
            max_iter=2_000,
            random_state=RANDOM_STATE,
            solver="liblinear",
        ),
        "random_forest": RandomForestClassifier(
            n_estimators=500,
            min_samples_leaf=5,
            class_weight="balanced_subsample",
            n_jobs=-1,
            random_state=RANDOM_STATE,
        ),
        "xgboost": XGBClassifier(
            objective="binary:logistic",
            eval_metric="logloss",
            n_estimators=400,
            max_depth=4,
            learning_rate=0.05,
            min_child_weight=3,
            subsample=0.8,
            colsample_bytree=0.8,
            reg_lambda=1.0,
            scale_pos_weight=scale_pos_weight,
            n_jobs=-1,
            random_state=RANDOM_STATE,
            tree_method="hist",
        ),
    }


def make_pipeline(classifier: Any, columns: tuple[str, ...]) -> Pipeline:
    return Pipeline(
        steps=[
            ("preprocess", build_preprocessor(columns)),
            ("classifier", classifier),
        ]
    )


def choose_threshold(y_true: np.ndarray, probability: np.ndarray) -> float:
    """Choose the validation threshold with maximum positive-class F1."""
    precision, recall, thresholds = precision_recall_curve(y_true, probability)
    if thresholds.size == 0:
        return 0.5
    denominator = precision[:-1] + recall[:-1]
    f1 = np.divide(
        2 * precision[:-1] * recall[:-1],
        denominator,
        out=np.zeros_like(denominator),
        where=denominator > 0,
    )
    return float(thresholds[int(np.argmax(f1))])


def calibration_summary(
    y_true: np.ndarray, probability: np.ndarray, *, bins: int = 10
) -> dict[str, Any]:
    """Summarise probability calibration with fixed-width reliability bins."""
    if bins < 2:
        raise ValueError("bins must be at least 2")
    if y_true.size != probability.size:
        raise ValueError("labels and probabilities must have the same length")
    if y_true.size == 0:
        raise ValueError("calibration requires at least one row")
    if np.any((probability < 0) | (probability > 1)):
        raise ValueError("probabilities must be between 0 and 1")

    bin_index = np.minimum((probability * bins).astype(int), bins - 1)
    reliability = []
    expected_calibration_error = 0.0
    for index in range(bins):
        mask = bin_index == index
        count = int(mask.sum())
        if count == 0:
            continue
        mean_probability = float(probability[mask].mean())
        observed_rate = float(y_true[mask].mean())
        expected_calibration_error += count / y_true.size * abs(mean_probability - observed_rate)
        reliability.append(
            {
                "lower": index / bins,
                "upper": (index + 1) / bins,
                "rows": count,
                "positive": int(y_true[mask].sum()),
                "mean_probability": mean_probability,
                "observed_positive_rate": observed_rate,
            }
        )
    return {
        "expected_calibration_error": float(expected_calibration_error),
        "bins": reliability,
    }


def compute_metrics(
    y_true: np.ndarray, probability: np.ndarray, threshold: float
) -> dict[str, Any]:
    predicted = probability >= threshold
    unique = np.unique(y_true)
    roc_auc = float(roc_auc_score(y_true, probability)) if unique.size == 2 else None
    average_precision = float(average_precision_score(y_true, probability)) if 1 in unique else None
    matrix = confusion_matrix(y_true, predicted, labels=[0, 1])
    mcc = float(matthews_corrcoef(y_true, predicted)) if unique.size == 2 else 0.0
    return {
        "rows": int(y_true.size),
        "positive": int(y_true.sum()),
        "negative": int(y_true.size - y_true.sum()),
        "threshold": threshold,
        "accuracy": float(accuracy_score(y_true, predicted)),
        "precision": float(precision_score(y_true, predicted, zero_division=0)),
        "recall": float(recall_score(y_true, predicted, zero_division=0)),
        "f1": float(f1_score(y_true, predicted, zero_division=0)),
        "pr_auc": average_precision,
        "roc_auc": roc_auc,
        "mcc": mcc,
        "brier": float(brier_score_loss(y_true, probability)),
        "calibration": calibration_summary(y_true, probability),
        "confusion_matrix": matrix.tolist(),
    }


def _xy(dataset: PreparedDataset, split: str, columns: tuple[str, ...]) -> tuple[Any, np.ndarray]:
    rows = dataset.split(split)
    return rows.loc[:, columns], rows[TARGET_COLUMN].to_numpy(dtype=int)


def _prediction_frame(
    rows: pd.DataFrame, probability: np.ndarray, threshold: float
) -> pd.DataFrame:
    """Build an auditable, row-level prediction export."""
    identity_columns = [
        "Record_ID",
        "Run_Key",
        "Repository",
        "Run_Created_At_UTC",
        TARGET_COLUMN,
    ]
    result = rows.loc[:, identity_columns].copy()
    result["Predicted_Probability"] = probability
    result["Predicted_Label"] = (probability >= threshold).astype(int)
    return result


def _metric_text(value: Any) -> str:
    return "not available" if value is None else f"{float(value):.4f}"


def _model_card_row(name: str, metrics: dict[str, Any]) -> str:
    values = [
        name,
        str(metrics["rows"]),
        str(metrics["positive"]),
        _metric_text(metrics["precision"]),
        _metric_text(metrics["recall"]),
        _metric_text(metrics["f1"]),
        _metric_text(metrics["pr_auc"]),
        _metric_text(metrics["roc_auc"]),
    ]
    return "| " + " | ".join(values) + " |"


def _write_model_card(
    output_dir: Path,
    model_name: str,
    feature_profile: str,
    validation_metrics: dict[str, Any],
    test_metrics: dict[str, Any],
    label_policy: str,
) -> None:
    """Write the minimum evidence and limitations beside each model artifact."""
    validation_row = _model_card_row("Validation", validation_metrics)
    test_row = _model_card_row("Test", test_metrics)
    unstable_note = (
        f"The temporal test split has only {test_metrics['positive']} positive cases; "
        "its metrics are unstable."
    )
    card = f"""# Infrastructure failure model card

## Intended use

Research-only prediction of the binary `Infra_Failure` label from pre-execution workflow fields.
This artifact is not connected to the existing Infrastructure Expert application.

## Selected model

- Candidate: `{model_name}`
- Feature profile: `{feature_profile}`
- Label policy: {label_policy}
- Selection rule: highest validation PR-AUC
- Decision threshold: {validation_metrics["threshold"]:.6f}, selected on validation F1
- Split: purged chronological 60/20/20 by run timestamp and incident
- Calibration: sigmoid calibration on the newest 20% of the training period, with crossing
  incidents purged

## Results

| Split | Rows | Positives | Precision | Recall | F1 | PR-AUC | ROC-AUC |
|---|---:|---:|---:|---:|---:|---:|---:|
{validation_row}
{test_row}

## Limitations

- {unstable_note}
- Positive labels and failure categories are concentrated in a small number of repositories.
- Review evidence and independence must be checked before relying on labels.
- This model predicts risk only; it does not diagnose a root cause or recommend a fix.
- Do not deploy until newer, broader positive cases are collected and independently evaluated.
"""
    (output_dir / "model_card.md").write_text(card, encoding="utf-8")


def train(
    dataset: PreparedDataset,
    output_dir: Path,
    *,
    feature_profile: str = "full",
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    columns = feature_columns(feature_profile)
    training_rows = dataset.split("train")
    model_fit_rows, calibration_rows, calibration_crossing_rows = split_model_fit_and_calibration(
        training_rows
    )
    x_model_fit = model_fit_rows.loc[:, columns]
    y_model_fit = model_fit_rows[TARGET_COLUMN].to_numpy(dtype=int)
    x_validation, y_validation = _xy(dataset, "validation", columns)
    x_test, y_test = _xy(dataset, "test", columns)
    if int(y_test.sum()) < 20:
        warnings.warn(
            "The temporal test split has fewer than 20 positives; metrics are provisional.",
            stacklevel=2,
        )

    leaderboard: dict[str, Any] = {}
    fitted: dict[str, Pipeline] = {}
    calibrated: dict[str, Any] = {}
    thresholds: dict[str, float] = {}
    for name, classifier in candidate_models(y_model_fit).items():
        pipeline = make_pipeline(classifier, columns)
        pipeline.fit(x_model_fit, y_model_fit)
        predictor = fit_sigmoid_calibrator(pipeline, calibration_rows, columns)
        validation_probability = predictor.predict_proba(x_validation)[:, 1]
        threshold = choose_threshold(y_validation, validation_probability)
        leaderboard[name] = compute_metrics(y_validation, validation_probability, threshold)
        fitted[name] = pipeline
        calibrated[name] = predictor
        thresholds[name] = threshold

    def score(item: tuple[str, Any]) -> float:
        value = item[1]["pr_auc"]
        return float(value) if value is not None else float("-inf")

    winner_name = max(leaderboard.items(), key=score)[0]
    winner = fitted[winner_name]
    predictor = calibrated[winner_name]
    threshold = thresholds[winner_name]
    validation_probability = predictor.predict_proba(x_validation)[:, 1]
    test_probability = predictor.predict_proba(x_test)[:, 1]
    test_metrics = compute_metrics(y_test, test_probability, threshold)

    artifact = {
        "pipeline": winner,
        "predictor": predictor,
        "model_name": winner_name,
        "threshold": threshold,
        "feature_profile": feature_profile,
        "feature_columns": list(columns),
        "calibration_method": "sigmoid",
        "feature_schema_version": FEATURE_SCHEMA_VERSION,
        "deployment_ready": False,
        "source_sha256": dataset.source_sha256,
        "label_policy": (
            "human-reviewed failures plus observed successes"
            if dataset.reviews_sha256
            else "legacy research labels; independent human review incomplete"
        ),
        "reviews_sha256": dataset.reviews_sha256,
        "split_policy": "purged chronological 60/20/20 by Run_Created_At_UTC and Incident_ID",
    }
    joblib.dump(artifact, output_dir / "model.joblib")
    (output_dir / "leaderboard.json").write_text(
        json.dumps(leaderboard, indent=2), encoding="utf-8"
    )
    validation_rows = dataset.split("validation")
    test_rows = dataset.split("test")
    _prediction_frame(validation_rows, validation_probability, threshold).to_csv(
        output_dir / "validation_predictions.csv", index=False
    )
    _prediction_frame(test_rows, test_probability, threshold).to_csv(
        output_dir / "test_predictions.csv", index=False
    )
    selected_classifier = winner.named_steps["classifier"]
    metadata = {
        "created_at_utc": datetime.now(UTC).isoformat(),
        "python_version": platform.python_version(),
        "package_versions": {
            package: version(package)
            for package in ["joblib", "numpy", "pandas", "scikit-learn", "xgboost"]
        },
        "random_state": RANDOM_STATE,
        "selected_model": winner_name,
        "selected_model_parameters": selected_classifier.get_params(),
        "decision_threshold": threshold,
        "feature_profile": feature_profile,
        "feature_columns": list(columns),
        "calibration": {
            "method": "sigmoid",
            "rows": len(calibration_rows),
            "positive": int(calibration_rows[TARGET_COLUMN].sum()),
            "cross_boundary_rows_excluded": calibration_crossing_rows,
        },
        "split_counts": {
            split: {
                "rows": int(labels.size),
                "positive": int(labels.sum()),
                "negative": int(labels.size - labels.sum()),
            }
            for split, labels in [
                ("model_fit", y_model_fit),
                (
                    "calibration",
                    calibration_rows[TARGET_COLUMN].to_numpy(dtype=int),
                ),
                ("validation", y_validation),
                ("test", y_test),
            ]
        },
    }
    metadata["feature_schema_version"] = artifact["feature_schema_version"]
    metadata["label_policy"] = artifact["label_policy"]
    metadata["label_status_counts"] = dataset.frame.Label_Status.value_counts().to_dict()
    metadata["deployment_ready"] = False
    metadata["source_sha256"] = dataset.source_sha256
    metadata["reviews_sha256"] = dataset.reviews_sha256
    (output_dir / "training_metadata.json").write_text(
        json.dumps(metadata, indent=2, default=str), encoding="utf-8"
    )
    result = {
        "selected_model": winner_name,
        "feature_profile": feature_profile,
        "validation_metrics": leaderboard[winner_name],
        "test_metrics": test_metrics,
        "warning": (
            "Temporal test metrics are unstable because the test split contains "
            f"only {int(y_test.sum())} positive cases."
        ),
    }
    (output_dir / "metrics.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    _write_model_card(
        output_dir,
        winner_name,
        feature_profile,
        leaderboard[winner_name],
        test_metrics,
        artifact["label_policy"],
    )
    print(json.dumps(result, indent=2))
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--artifacts-dir", type=Path, default=DEFAULT_ARTIFACTS_DIR)
    parser.add_argument("--version", default="v1")
    parser.add_argument("--reviews", type=Path, help="Human-verified label review JSONL")
    parser.add_argument(
        "--feature-profile",
        choices=sorted(FEATURE_PROFILES),
        default="full",
        help="full uses history signals; no_history measures portability without them",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    dataset = load_prepared_dataset(args.input, reviews_path=args.reviews)
    train(dataset, args.artifacts_dir / args.version, feature_profile=args.feature_profile)


if __name__ == "__main__":
    main()
