"""Evaluate a saved Infra model on the untouched temporal test partition."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import joblib

from .dataset import DEFAULT_INPUT, load_dataset_for_artifact
from .features import TARGET_COLUMN
from .train import DEFAULT_ARTIFACTS_DIR, compute_metrics


def evaluate(
    input_path: Path, artifact_dir: Path, reviews_path: Path | None = None
) -> dict[str, Any]:
    artifact = joblib.load(artifact_dir / "model.joblib")
    dataset = load_dataset_for_artifact(input_path, artifact, reviews_path)
    test = dataset.split("test")
    predictor = artifact.get("predictor", artifact["pipeline"])
    feature_columns = artifact["feature_columns"]
    probability = predictor.predict_proba(test.loc[:, feature_columns])[:, 1]
    metrics = compute_metrics(
        test[TARGET_COLUMN].to_numpy(dtype=int), probability, float(artifact["threshold"])
    )
    per_repository = []
    for repository, indexes in test.groupby("Repository").groups.items():
        rows = test.loc[indexes]
        per_repository.append(
            {
                "repository": repository,
                "rows": len(rows),
                "positive": int(rows[TARGET_COLUMN].sum()),
                "mean_predicted_probability": float(
                    probability[test.index.get_indexer(indexes)].mean()
                ),
            }
        )
    result = {
        "model_name": artifact["model_name"],
        "feature_profile": artifact.get("feature_profile", "legacy_full"),
        "calibration_method": artifact.get("calibration_method", "none"),
        "split_policy": artifact["split_policy"],
        "metrics": metrics,
        "per_repository": sorted(per_repository, key=lambda item: item["repository"]),
        "interpretation_limit": (
            "Report the positive count with every metric; the current temporal test set is too "
            "small for stable positive-class conclusions."
        ),
    }
    (artifact_dir / "evaluation.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
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
    evaluate(args.input, args.artifacts_dir / args.version, args.reviews)


if __name__ == "__main__":
    main()
