"""Run rolling-temporal and leave-one-repository-out model validation."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import pandas as pd
from sklearn.base import clone
from sklearn.metrics import f1_score, precision_score, recall_score
from sklearn.pipeline import Pipeline

from .calibration import fit_sigmoid_calibrator, split_model_fit_and_calibration
from .dataset import DEFAULT_INPUT, PreparedDataset, load_dataset_for_artifact
from .features import TARGET_COLUMN
from .train import DEFAULT_ARTIFACTS_DIR, compute_metrics

DEFAULT_TEMPORAL_FOLDS = 4
DEFAULT_INITIAL_TRAIN_FRACTION = 0.40


def expanding_temporal_folds(
    frame: pd.DataFrame,
    *,
    folds: int = DEFAULT_TEMPORAL_FOLDS,
    initial_train_fraction: float = DEFAULT_INITIAL_TRAIN_FRACTION,
) -> list[tuple[pd.DataFrame, pd.DataFrame]]:
    """Create expanding time folds while purging incidents shared across a boundary."""
    if folds < 2:
        raise ValueError("folds must be at least 2")
    if not 0 < initial_train_fraction < 1:
        raise ValueError("initial_train_fraction must be between 0 and 1")

    ordered = frame.sort_values("Run_Created_At_UTC").copy()
    unique_times = ordered["Run_Created_At_UTC"].drop_duplicates().sort_values().to_numpy()
    if unique_times.size < folds + 1:
        raise ValueError("not enough unique timestamps for the requested folds")

    boundary_fractions = np.linspace(initial_train_fraction, 1.0, folds + 1)
    boundary_indexes = np.minimum(
        (boundary_fractions * unique_times.size).astype(int), unique_times.size - 1
    )
    boundary_indexes[-1] = unique_times.size

    result = []
    for fold_index in range(folds):
        evaluation_start = unique_times[boundary_indexes[fold_index]]
        if fold_index == folds - 1:
            evaluation = ordered.loc[ordered["Run_Created_At_UTC"].ge(evaluation_start)]
        else:
            evaluation_end = unique_times[boundary_indexes[fold_index + 1]]
            evaluation = ordered.loc[
                ordered["Run_Created_At_UTC"].ge(evaluation_start)
                & ordered["Run_Created_At_UTC"].lt(evaluation_end)
            ]
        training = ordered.loc[ordered["Run_Created_At_UTC"].lt(evaluation_start)]
        overlapping_incidents = set(training["Incident_ID"]) & set(evaluation["Incident_ID"])
        training = training.loc[~training["Incident_ID"].isin(overlapping_incidents)]
        if training.empty or evaluation.empty:
            raise ValueError("a temporal fold is empty")
        result.append((training.copy(), evaluation.copy()))
    return result


def _fresh_pipeline(template: Pipeline, y_train: np.ndarray) -> Pipeline:
    """Clone a selected pipeline and refresh imbalance weighting when supported."""
    pipeline = clone(template)
    parameter_name = "classifier__scale_pos_weight"
    if parameter_name in pipeline.get_params():
        positive = int(y_train.sum())
        if positive == 0:
            raise ValueError("training fold contains no positive examples")
        pipeline.set_params(**{parameter_name: (y_train.size - positive) / positive})
    return pipeline


def _fit_predict(
    template: Pipeline,
    training: pd.DataFrame,
    evaluation: pd.DataFrame,
    feature_columns: tuple[str, ...],
) -> np.ndarray:
    model_fit, calibration, _ = split_model_fit_and_calibration(training)
    y_model_fit = model_fit[TARGET_COLUMN].to_numpy(dtype=int)
    if np.unique(y_model_fit).size != 2:
        raise ValueError("training rows must contain both target classes")
    pipeline = _fresh_pipeline(template, y_model_fit)
    pipeline.fit(model_fit.loc[:, feature_columns], y_model_fit)
    predictor = fit_sigmoid_calibrator(pipeline, calibration, feature_columns)
    return predictor.predict_proba(evaluation.loc[:, feature_columns])[:, 1]


def _prediction_rows(
    rows: pd.DataFrame,
    probability: np.ndarray,
    threshold: float,
    **extra: Any,
) -> pd.DataFrame:
    result = rows.loc[
        :, ["Record_ID", "Run_Key", "Repository", "Run_Created_At_UTC", TARGET_COLUMN]
    ].copy()
    result["Predicted_Probability"] = probability
    result["Predicted_Label"] = (probability >= threshold).astype(int)
    for name, value in extra.items():
        result[name] = value
    return result


def _threshold_sweep(rows: pd.DataFrame, threshold: float) -> list[dict[str, Any]]:
    """Show alert-volume and recall trade-offs without selecting on evaluation labels."""
    y_true = rows[TARGET_COLUMN].to_numpy(dtype=int)
    probability = rows["Predicted_Probability"].to_numpy(dtype=float)
    thresholds = sorted({0.05, 0.10, 0.20, 0.30, 0.50, threshold})
    result = []
    for candidate in thresholds:
        predicted = probability >= candidate
        result.append(
            {
                "threshold": candidate,
                "predicted_positive": int(predicted.sum()),
                "precision": float(precision_score(y_true, predicted, zero_division=0)),
                "recall": float(recall_score(y_true, predicted, zero_division=0)),
                "f1": float(f1_score(y_true, predicted, zero_division=0)),
            }
        )
    return result


def rolling_temporal_validation(
    dataset: PreparedDataset,
    template: Pipeline,
    threshold: float,
    feature_columns: tuple[str, ...],
) -> tuple[dict[str, Any], pd.DataFrame]:
    """Evaluate the selected model on four expanding, future-only folds."""
    development = dataset.frame.loc[dataset.frame["Split"].isin(["train", "validation"])]
    fold_metrics = []
    predictions = []
    for fold_number, (training, evaluation) in enumerate(
        expanding_temporal_folds(development), start=1
    ):
        print(f"rolling temporal fold {fold_number}/{DEFAULT_TEMPORAL_FOLDS}")
        probability = _fit_predict(template, training, evaluation, feature_columns)
        y_evaluation = evaluation[TARGET_COLUMN].to_numpy(dtype=int)
        metrics = compute_metrics(y_evaluation, probability, threshold)
        metrics.update(
            {
                "fold": fold_number,
                "train_rows": len(training),
                "train_positive": int(training[TARGET_COLUMN].sum()),
                "evaluation_start_utc": evaluation["Run_Created_At_UTC"].min().isoformat(),
                "evaluation_end_utc": evaluation["Run_Created_At_UTC"].max().isoformat(),
            }
        )
        fold_metrics.append(metrics)
        predictions.append(
            _prediction_rows(evaluation, probability, threshold, Temporal_Fold=fold_number)
        )

    combined = pd.concat(predictions, ignore_index=True)
    combined_metrics = compute_metrics(
        combined[TARGET_COLUMN].to_numpy(dtype=int),
        combined["Predicted_Probability"].to_numpy(dtype=float),
        threshold,
    )
    return {
        "folds": fold_metrics,
        "combined_metrics": combined_metrics,
        "threshold_sweep": _threshold_sweep(combined, threshold),
    }, combined


def leave_one_repository_out_validation(
    dataset: PreparedDataset,
    template: Pipeline,
    threshold: float,
    feature_columns: tuple[str, ...],
) -> tuple[dict[str, Any], pd.DataFrame]:
    """Exploratory refits; global candidate/threshold selection is not repository-independent."""
    development = dataset.frame.loc[dataset.frame["Split"].isin(["train", "validation"])].copy()
    repositories = sorted(development["Repository"].unique())
    repository_metrics = []
    predictions = []
    for position, repository in enumerate(repositories, start=1):
        print(f"leave-one-repository-out {position}/{len(repositories)}: {repository}")
        evaluation = development.loc[development["Repository"].eq(repository)]
        training = development.loc[~development["Repository"].eq(repository)]
        probability = _fit_predict(template, training, evaluation, feature_columns)
        metrics = compute_metrics(
            evaluation[TARGET_COLUMN].to_numpy(dtype=int), probability, threshold
        )
        metrics["repository"] = repository
        repository_metrics.append(metrics)
        predictions.append(
            _prediction_rows(
                evaluation,
                probability,
                threshold,
                Held_Out_Repository=repository,
            )
        )

    combined = pd.concat(predictions, ignore_index=True)
    combined_metrics = compute_metrics(
        combined[TARGET_COLUMN].to_numpy(dtype=int),
        combined["Predicted_Probability"].to_numpy(dtype=float),
        threshold,
    )
    return {
        "repositories": repository_metrics,
        "combined_metrics": combined_metrics,
        "threshold_sweep": _threshold_sweep(combined, threshold),
    }, combined


def _validation_card_row(name: str, metrics: dict[str, Any]) -> str:
    values = [
        name,
        str(metrics["rows"]),
        str(metrics["positive"]),
        f"{metrics['precision']:.4f}",
        f"{metrics['recall']:.4f}",
        f"{metrics['f1']:.4f}",
        f"{metrics['pr_auc']:.4f}",
    ]
    return "| " + " | ".join(values) + " |"


def _update_model_card(artifact_dir: Path, result: dict[str, Any]) -> None:
    """Add robust-validation evidence to the generated model card."""
    path = artifact_dir / "model_card.md"
    existing = path.read_text(encoding="utf-8")
    existing = existing.partition("\n## Robust validation\n")[0].rstrip()
    temporal = result["rolling_temporal"]["combined_metrics"]
    repository = result["leave_one_repository_out"]["combined_metrics"]
    temporal_row = _validation_card_row("Rolling temporal", temporal)
    repository_row = _validation_card_row("Leave one repository out", repository)
    section = f"""

## Robust validation

All values below use the saved threshold of {result["fixed_threshold"]:.6f}.
Candidate and threshold selection previously used overlapping development data, so these
are exploratory refit metrics. Use `model.benchmark` for nested repository validation.

| Check | Rows | Positives | Precision | Recall | F1 | PR-AUC |
|---|---:|---:|---:|---:|---:|---:|
{temporal_row}
{repository_row}

The large drop under leave-one-repository-out validation shows that the current model does not
generalise reliably to repositories it has not seen. Keep this artifact research-only.
"""
    path.write_text(existing + section, encoding="utf-8")


def validate(
    input_path: Path, artifact_dir: Path, reviews_path: Path | None = None
) -> dict[str, Any]:
    """Run robust validation and write its report beside the model artifact."""
    artifact = joblib.load(artifact_dir / "model.joblib")
    dataset = load_dataset_for_artifact(input_path, artifact, reviews_path)
    pipeline = artifact["pipeline"]
    threshold = float(artifact["threshold"])
    feature_columns = tuple(artifact["feature_columns"])
    temporal, temporal_predictions = rolling_temporal_validation(
        dataset, pipeline, threshold, feature_columns
    )
    repository, repository_predictions = leave_one_repository_out_validation(
        dataset, pipeline, threshold, feature_columns
    )
    result = {
        "model_name": artifact["model_name"],
        "feature_profile": artifact.get("feature_profile", "legacy_full"),
        "calibration_method": artifact.get("calibration_method", "none"),
        "fixed_threshold": threshold,
        "rolling_temporal": temporal,
        "leave_one_repository_out": repository,
        "interpretation": [
            "All classification metrics use the saved validation-selected threshold.",
            "Rolling folds train only on earlier rows and purge incidents crossing a boundary.",
            (
                "Repository folds exclude the repo from fitting/calibration only; global "
                "model and threshold selection can overlap evaluation labels. Use model.benchmark."
            ),
            (
                "Repository slices with one target class have no ROC-AUC; PR-AUC is "
                "absent when there are no positives."
            ),
        ],
    }
    (artifact_dir / "validation_report.json").write_text(
        json.dumps(result, indent=2), encoding="utf-8"
    )
    temporal_predictions.to_csv(artifact_dir / "rolling_temporal_predictions.csv", index=False)
    repository_predictions.to_csv(artifact_dir / "leave_one_repo_out_predictions.csv", index=False)
    _update_model_card(artifact_dir, result)
    print(json.dumps(result, indent=2))
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--artifacts-dir", type=Path, default=DEFAULT_ARTIFACTS_DIR)
    parser.add_argument("--version", default="v1")
    parser.add_argument("--reviews", type=Path)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    validate(args.input, args.artifacts_dir / args.version, args.reviews)


if __name__ == "__main__":
    main()
