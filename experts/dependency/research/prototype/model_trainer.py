"""Training entry point for the non-production Dependency baseline."""

import argparse
import pickle
from pathlib import Path

import pandas as pd
import shap
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import accuracy_score
from xgboost import XGBClassifier

REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_CSV = REPOSITORY_ROOT / "data/datasets/dependency/prototype/dependency_features.csv"
DEFAULT_MODEL = REPOSITORY_ROOT / "models/dependency/prototype/xgboost_model.pkl"
FEATURE_COLUMNS = [
    "total_dependency_files_changed",
    "has_package_json_change",
    "has_lockfile_change",
]


def train_and_evaluate_models(csv_file: Path, model_file: Path) -> None:
    """Train and report exploratory baselines; this is not a certified evaluation."""
    frame = pd.read_csv(csv_file)
    if len(frame) < 10:
        raise ValueError("at least 10 records are required to train the prototype")
    if not set(frame["is_failure"].dropna().unique()).issubset({0, 1}):
        raise ValueError("is_failure must contain only binary labels")

    features = frame[FEATURE_COLUMNS]
    target = frame["is_failure"]
    split = int(len(frame) * 0.8)
    if split < 1 or split >= len(frame):
        raise ValueError("dataset cannot be split into train and validation sets")
    x_train, x_test = features.iloc[:split], features.iloc[split:]
    y_train, y_test = target.iloc[:split], target.iloc[split:]
    if y_train.nunique() < 2:
        raise ValueError("training subset must contain both success and failure records")

    baseline = LogisticRegression()
    baseline.fit(x_train, y_train)
    print(f"Logistic regression accuracy: {accuracy_score(y_test, baseline.predict(x_test)):.2f}")

    model = XGBClassifier(eval_metric="logloss")
    model.fit(x_train, y_train)
    predictions = model.predict(x_test)
    print(f"XGBoost accuracy: {accuracy_score(y_test, predictions):.2f}")

    model_file.parent.mkdir(parents=True, exist_ok=True)
    with model_file.open("wb") as output_file:
        pickle.dump(model, output_file)

    explainer = shap.Explainer(model, x_train)
    explanations = explainer(x_test)
    print(f"SHAP values generated for {len(explanations)} validation records")


def main() -> None:
    """Parse paths and run the experimental training pipeline."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV)
    parser.add_argument("--model", type=Path, default=DEFAULT_MODEL)
    args = parser.parse_args()
    train_and_evaluate_models(args.csv, args.model)


if __name__ == "__main__":
    main()
