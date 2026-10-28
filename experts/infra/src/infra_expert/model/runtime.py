"""Load the packaged Infra research artifact and make calibrated predictions."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Protocol, cast

import joblib
import numpy as np
import pandas as pd

from infra_expert.features import prepared_features_from_context
from shared.contracts import CommitContext

PACKAGED_MODEL = Path(__file__).with_name("infra-v6-public-dataset.joblib")


class ProbabilityPredictor(Protocol):
    """Small protocol implemented by scikit-learn probability estimators."""

    def predict_proba(self, features: pd.DataFrame) -> np.ndarray: ...


class InfraModel:
    """Validated view over the joblib artifact produced by research/model/train.py."""

    def __init__(self, artifact_path: Path) -> None:
        self.artifact_path = artifact_path
        raw = joblib.load(artifact_path)
        if not isinstance(raw, dict):
            raise ValueError("Infra model artifact must contain a mapping")
        required = {"predictor", "threshold", "feature_columns", "feature_profile"}
        missing = sorted(required - set(raw))
        if missing:
            raise ValueError(f"Infra model artifact is missing: {missing}")
        if raw["feature_profile"] != "no_history":
            raise ValueError("the serving artifact must use the no_history feature profile")
        self.artifact = cast(dict[str, Any], raw)
        self._limit_prediction_threads()

    def _limit_prediction_threads(self) -> None:
        """Avoid expensive per-request worker-pool creation for a single prediction row."""
        predictor = self.artifact["predictor"]
        predictor.n_jobs = 1
        for calibrated in predictor.calibrated_classifiers_:
            frozen = calibrated.estimator
            pipeline = getattr(frozen, "estimator", frozen)
            classifier = pipeline.named_steps.get("classifier")
            if classifier is not None and hasattr(classifier, "n_jobs"):
                classifier.n_jobs = 1

    @property
    def threshold(self) -> float:
        return float(self.artifact["threshold"])

    def predict_probability(self, context: CommitContext) -> float:
        prepared = prepared_features_from_context(context)
        columns = cast(list[str], self.artifact["feature_columns"])
        predictor = cast(ProbabilityPredictor, self.artifact["predictor"])
        probability = float(predictor.predict_proba(prepared.loc[:, columns])[0, 1])
        return min(1.0, max(0.0, probability))


def configured_model_path() -> Path:
    """Resolve the optional override, otherwise use the model packaged with this expert."""
    configured = os.environ.get("INFRA_MODEL_PATH")
    return Path(configured) if configured else PACKAGED_MODEL


@lru_cache(maxsize=1)
def load_model() -> InfraModel:
    """Load and cache one immutable artifact per API process."""
    return InfraModel(configured_model_path())
