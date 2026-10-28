"""Create global and example-level SHAP explanations for a saved Infra model."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import pandas as pd

from .dataset import DEFAULT_INPUT, load_dataset_for_artifact
from .train import DEFAULT_ARTIFACTS_DIR, RANDOM_STATE


def _dense(values: Any) -> np.ndarray:
    return values.toarray() if hasattr(values, "toarray") else np.asarray(values)


def _positive_class_values(values: np.ndarray) -> np.ndarray:
    if values.ndim == 3:
        return values[:, :, -1]
    return values


def explain(
    input_path: Path, artifact_dir: Path, sample_size: int, reviews_path: Path | None = None
) -> None:
    try:
        import shap
    except ImportError as exc:
        raise RuntimeError(
            "SHAP is not installed; run: python -m pip install -r requirements.txt"
        ) from exc

    artifact = joblib.load(artifact_dir / "model.joblib")
    dataset = load_dataset_for_artifact(input_path, artifact, reviews_path)
    train = dataset.split("train")
    test = dataset.split("test")
    feature_columns = artifact["feature_columns"]
    pipeline = artifact["pipeline"]
    preprocessor = pipeline.named_steps["preprocess"]
    classifier = pipeline.named_steps["classifier"]

    background_rows = train.sample(min(100, len(train)), random_state=RANDOM_STATE)
    sample_rows = test.sample(min(sample_size, len(test)), random_state=RANDOM_STATE)
    background = _dense(preprocessor.transform(background_rows.loc[:, feature_columns]))
    samples = _dense(preprocessor.transform(sample_rows.loc[:, feature_columns]))
    feature_names = [str(name) for name in preprocessor.get_feature_names_out()]

    explainer = shap.Explainer(classifier, background, feature_names=feature_names)
    explanation = explainer(samples)
    values = _positive_class_values(np.asarray(explanation.values))
    mean_absolute = np.abs(values).mean(axis=0)
    importance = pd.DataFrame(
        {"feature": feature_names, "mean_abs_shap": mean_absolute}
    ).sort_values("mean_abs_shap", ascending=False)
    importance.to_csv(artifact_dir / "shap_global_importance.csv", index=False)

    examples = []
    for row_number, record_id in enumerate(sample_rows["Record_ID"].tolist()):
        order = np.argsort(np.abs(values[row_number]))[::-1][:5]
        examples.append(
            {
                "record_id": int(record_id),
                "top_factors": [
                    {
                        "feature": feature_names[index],
                        "shap_value": float(values[row_number, index]),
                    }
                    for index in order
                ],
            }
        )
    (artifact_dir / "shap_examples.json").write_text(
        json.dumps(examples, indent=2), encoding="utf-8"
    )
    print(importance.head(20).to_string(index=False))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--artifacts-dir", type=Path, default=DEFAULT_ARTIFACTS_DIR)
    parser.add_argument("--version", default="v1")
    parser.add_argument("--reviews", type=Path)
    parser.add_argument("--sample-size", type=int, default=100)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    explain(args.input, args.artifacts_dir / args.version, args.sample_size, args.reviews)


if __name__ == "__main__":
    main()
