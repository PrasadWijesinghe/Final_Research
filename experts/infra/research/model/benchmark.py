"""Nested repository validation: choose model and threshold without held-out repo labels."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from sklearn.metrics import (
    confusion_matrix,
    f1_score,
    matthews_corrcoef,
    precision_score,
    recall_score,
)

from .calibration import fit_sigmoid_calibrator, split_model_fit_and_calibration
from .dataset import DEFAULT_INPUT, load_prepared_dataset
from .features import FEATURE_PROFILES, TARGET_COLUMN, feature_columns
from .train import candidate_models, choose_threshold, compute_metrics, make_pipeline


def inner_partitions(training: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Keep fitting, probability calibration and model/threshold selection disjoint."""
    earlier, selection, _ = split_model_fit_and_calibration(training)
    fit, calibration, _ = split_model_fit_and_calibration(earlier)
    return fit, calibration, selection


def nested_repository_benchmark(
    input_path: Path,
    output_dir: Path,
    *,
    feature_profile: str = "no_history",
    reviews_path: Path | None = None,
) -> dict[str, Any]:
    """Retrain candidates inside each repository fold; do not use the final temporal test."""
    dataset = load_prepared_dataset(input_path, reviews_path=reviews_path)
    development = dataset.frame.loc[dataset.frame.Split.isin(["train", "validation"])].copy()
    columns = feature_columns(feature_profile)
    fold_reports = []
    predictions = []
    for repository in sorted(development.Repository.unique()):
        print(f"Nested repository fold: {repository}", flush=True)
        evaluation = development.loc[development.Repository.eq(repository)]
        training = development.loc[~development.Repository.eq(repository)]
        # Purge shared incidents even if a future source accidentally reuses IDs across repos.
        training = training.loc[~training.Incident_ID.isin(evaluation.Incident_ID)]
        try:
            fit, calibration, selection = inner_partitions(training)
        except ValueError as exc:
            fold_reports.append({"repository": repository, "skipped": str(exc)})
            continue
        labels = fit[TARGET_COLUMN].to_numpy(dtype=int)
        selection_labels = selection[TARGET_COLUMN].to_numpy(dtype=int)
        candidates: dict[str, Any] = {}
        scores = {}
        thresholds = {}
        for name, classifier in candidate_models(labels).items():
            pipeline = make_pipeline(classifier, columns)
            pipeline.fit(fit.loc[:, columns], labels)
            predictor = fit_sigmoid_calibrator(pipeline, calibration, columns)
            probability = predictor.predict_proba(selection.loc[:, columns])[:, 1]
            threshold = choose_threshold(selection_labels, probability)
            scores[name] = compute_metrics(selection_labels, probability, threshold)
            candidates[name] = predictor
            thresholds[name] = threshold
        winner = max(scores, key=lambda name: scores[name]["pr_auc"])
        probability = candidates[winner].predict_proba(evaluation.loc[:, columns])[:, 1]
        threshold = thresholds[winner]
        metrics = compute_metrics(
            evaluation[TARGET_COLUMN].to_numpy(dtype=int), probability, threshold
        )
        fold_reports.append(
            {
                "repository": repository,
                "selected_model": winner,
                "inner_selection_scores": scores,
                "fit_rows": len(fit),
                "calibration_rows": len(calibration),
                "selection_rows": len(selection),
                "evaluation": metrics,
            }
        )
        rows = evaluation.loc[:, ["Record_ID", "Repository", "Incident_ID", TARGET_COLUMN]].copy()
        rows["probability"] = probability
        rows["threshold"] = threshold
        rows["predicted"] = (probability >= threshold).astype(int)
        rows["selected_model"] = winner
        predictions.append(rows)
    if not predictions:
        raise ValueError("no repository fold had sufficient labelled data")
    combined = pd.concat(predictions, ignore_index=True)
    labels = combined[TARGET_COLUMN].to_numpy(dtype=int)
    predicted = combined.predicted.to_numpy(dtype=int)
    metrics = compute_metrics(labels, combined.probability.to_numpy(dtype=float), 0.5)
    # Classification uses each fold's independently selected threshold, not a global threshold.
    metrics.update(
        {
            "threshold": None,
            "threshold_policy": "inner chronological selection per held-out repository",
            "accuracy": float(np.mean(labels == predicted)),
            "precision": float(precision_score(labels, predicted, zero_division=0)),
            "recall": float(recall_score(labels, predicted, zero_division=0)),
            "f1": float(f1_score(labels, predicted, zero_division=0)),
            "confusion_matrix": confusion_matrix(labels, predicted, labels=[0, 1]).tolist(),
        }
    )
    # MCC at 0.5 would be misleading after replacing the predictions.
    metrics["mcc"] = float(matthews_corrcoef(labels, predicted))
    result = {
        "feature_profile": feature_profile,
        "source_sha256": dataset.source_sha256,
        "reviews_sha256": dataset.reviews_sha256,
        "folds": fold_reports,
        "combined_metrics": metrics,
        "evaluated_rows": len(combined),
        "development_rows": len(development),
        "deployment_ready": False,
        "limitations": [
            (
                "Failure labels require recorded human review; review quality still needs auditing."
                if reviews_path
                else "Legacy failure labels are not independently human-verified ground truth."
            ),
            "Outer repos excluded from fitting, calibration, model and threshold selection.",
            "Inner splits are chronological and incident-purged; outer folds are not future-only.",
            "Final temporal test rows excluded from this benchmark.",
            "Pooled PR-AUC compares scores calibrated separately in different folds.",
            "Does not establish representative failure probabilities for arbitrary pushes.",
        ],
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "nested_repository_report.json").write_text(
        json.dumps(result, indent=2), encoding="utf-8"
    )
    combined.to_csv(output_dir / "nested_repository_predictions.csv", index=False)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--feature-profile", choices=sorted(FEATURE_PROFILES), default="no_history")
    parser.add_argument("--reviews", type=Path, help="Human-verified label review JSONL")
    args = parser.parse_args()
    report = nested_repository_benchmark(
        args.input, args.output_dir, feature_profile=args.feature_profile, reviews_path=args.reviews
    )
    print(json.dumps(report["combined_metrics"], indent=2))


if __name__ == "__main__":
    main()
