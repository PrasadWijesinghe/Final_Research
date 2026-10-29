"""Gradient-boosted trees (LightGBM) for "will this commit's build fail?" (section 6).

1. Train on the train part with class weights from the observed imbalance.
2. Stop adding trees when PR-AUC on the validation part stops improving.
3. Calibrate the scores on the validation part (isotonic regression), so 0.7 means about 70%.
4. Choose two thresholds on the validation part: the best-F1 one (for comparing with the
   baselines, H1) and the high-precision operating one (D03).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import lightgbm as lgb
import numpy as np
from sklearn.isotonic import IsotonicRegression

from build_expert.features import CATEGORICAL_FEATURES

from .data import TrainingData
from .metrics import best_f1_threshold, precision_threshold
from .settings import TrainSettings

logger = logging.getLogger(__name__)

BASE_PARAMS: dict[str, Any] = {
    "objective": "binary",
    "metric": "average_precision",
    "verbosity": -1,
}


class TooFewPositives(ValueError):
    """The train or validation part has too few build failures to fit or tune a model."""


@dataclass
class FittedModel:
    """A trained booster with its calibration and thresholds."""

    target: str
    booster: lgb.Booster
    feature_names: tuple[str, ...]
    calibrator: IsotonicRegression
    threshold_f1: float  # on calibrated probabilities
    threshold_operating: float  # D03: high precision, on calibrated probabilities
    operating_reached_target: bool
    best_iteration: int
    scale_pos_weight: float
    train_rows: int
    validation_rows: int
    rounds_from: str = "validation"  # "cv" (folds inside train) or "validation" (early stopping)

    def raw_scores(self, x: np.ndarray) -> np.ndarray:
        """Uncalibrated scores (good for ranking: PR-AUC, ROC-AUC)."""
        return np.asarray(self.booster.predict(x, num_iteration=self.best_iteration))

    def probabilities(self, x: np.ndarray) -> np.ndarray:
        """Calibrated probability of a build failure."""
        return np.asarray(self.calibrator.predict(self.raw_scores(x)))

    def contributions(self, x: np.ndarray) -> np.ndarray:
        """SHAP values per feature (log-odds units); the last column is the base value."""
        return np.asarray(
            self.booster.predict(x, num_iteration=self.best_iteration, pred_contrib=True)
        )


def _params(settings: TrainSettings, scale_pos_weight: float) -> dict[str, Any]:
    params = {**BASE_PARAMS, **settings.lightgbm}
    params["scale_pos_weight"] = scale_pos_weight
    return params


def cv_folds(
    data: TrainingData, target: str, train: np.ndarray, settings: TrainSettings
) -> list[tuple[np.ndarray, np.ndarray]]:
    """Expanding-window folds inside the train part, as positions into `train`.

    Fold i fits on the oldest i/(k+1) of the rows and validates on the next 1/(k+1), with the
    same embargo as the main split. Folds with fewer than `min_positives` failures are left out.
    """
    k = settings.cv_folds
    if k < 2 or len(train) < 4 * (k + 1):
        return []
    order = np.argsort(data.cutoff[train], kind="stable")
    step = len(order) // (k + 1)
    y = data.labels[target][train]
    cut, done = data.cutoff[train], data.completed[train]
    folds: list[tuple[np.ndarray, np.ndarray]] = []
    for i in range(1, k + 1):
        fit_pos = order[: i * step]
        val_pos = order[i * step : (i + 1) * step] if i < k else order[i * step :]
        if settings.embargo:
            fit_pos = fit_pos[done[fit_pos] < cut[val_pos].min()]
        if (
            y[fit_pos].sum() >= settings.min_positives
            and y[val_pos].sum() >= settings.min_positives
        ):
            folds.append((np.sort(fit_pos), np.sort(val_pos)))
    return folds


def _cv_rounds(
    params: dict[str, Any],
    dataset: lgb.Dataset,
    folds: list[tuple[np.ndarray, np.ndarray]],
    settings: TrainSettings,
) -> int:
    """The tree count with the best mean PR-AUC over the folds (early stopping on the mean)."""
    result = lgb.cv(
        params,
        dataset,
        num_boost_round=settings.num_boost_round,
        folds=folds,
        stratified=False,
        shuffle=False,
        callbacks=[lgb.early_stopping(settings.early_stopping_rounds, verbose=False)],
    )
    key = next(k for k in result if k.endswith("average_precision-mean"))
    history = result[key]
    # LightGBM cuts the history at the best round.
    return max(1, len(history)) if isinstance(history, list) else settings.num_boost_round


def fit_model(
    data: TrainingData,
    target: str,
    train: np.ndarray,
    validation: np.ndarray,
    settings: TrainSettings,
    *,
    features: tuple[str, ...] | None = None,
    seed: int | None = None,
) -> FittedModel:
    """Fit, choose the tree count, calibrate and pick thresholds. `features` limits the columns
    (ablation); `seed` overrides the settings' seed."""
    names = features or data.feature_names
    cols = data.feature_index(names)
    y = data.labels[target]
    y_tr, y_val = y[train], y[validation]
    for part, labels in (("train", y_tr), ("validation", y_val)):
        if labels.sum() < settings.min_positives:
            raise TooFewPositives(
                f"{target}: only {int(labels.sum())} build failures in the {part} part "
                f"(need {settings.min_positives})"
            )
        if labels.sum() == len(labels):
            raise TooFewPositives(f"{target}: every {part} row is a failure")

    # DEFAULT: class weight = negatives / positives in the train part (section 6: "chosen from
    # the observed distribution"); calibration afterwards undoes the shift in probabilities.
    scale_pos_weight = float((len(y_tr) - y_tr.sum()) / y_tr.sum())
    categorical = [n for n in names if n in CATEGORICAL_FEATURES]
    x_tr = data.x[np.ix_(train, cols)]
    x_val = data.x[np.ix_(validation, cols)]
    params = _params(settings, scale_pos_weight)
    params["seed"] = settings.seeds[0] if seed is None else seed

    def dataset() -> lgb.Dataset:
        return lgb.Dataset(
            x_tr, label=y_tr, feature_name=list(names), categorical_feature=categorical or "auto"
        )

    folds = cv_folds(data, target, train, settings)
    if len(folds) >= 2:
        best = _cv_rounds(params, dataset(), folds, settings)
        booster = lgb.train(params, dataset(), num_boost_round=best)
        rounds_from = "cv"
    else:
        # Too few rows or failures for folds: stop on the validation part instead.
        dtrain = dataset()
        dval = lgb.Dataset(x_val, label=y_val, reference=dtrain)
        booster = lgb.train(
            params,
            dtrain,
            num_boost_round=settings.num_boost_round,
            valid_sets=[dval],
            valid_names=["validation"],
            callbacks=[
                lgb.early_stopping(settings.early_stopping_rounds, verbose=False),
                lgb.log_evaluation(0),
            ],
        )
        best = int(booster.best_iteration or booster.current_iteration())
        rounds_from = "validation"
    raw_val = np.asarray(booster.predict(x_val, num_iteration=best))
    calibrator = IsotonicRegression(out_of_bounds="clip", y_min=0.0, y_max=1.0)
    calibrator.fit(raw_val, y_val)
    p_val = np.asarray(calibrator.predict(raw_val))
    t_f1, _ = best_f1_threshold(y_val, p_val)
    # DEFAULT: the operating threshold must catch at least `min_positives` validation failures.
    t_op, reached = precision_threshold(
        y_val, p_val, settings.target_precision, min_hits=settings.min_positives
    )
    logger.info(
        "model fitted",
        extra={"target": target, "trees": best, "features": len(names), "train": len(train)},
    )
    return FittedModel(
        target=target,
        booster=booster,
        feature_names=tuple(names),
        calibrator=calibrator,
        threshold_f1=t_f1,
        threshold_operating=t_op,
        operating_reached_target=reached,
        best_iteration=best,
        scale_pos_weight=round(scale_pos_weight, 4),
        train_rows=len(train),
        validation_rows=len(validation),
        rounds_from=rounds_from,
    )


def matrix(data: TrainingData, rows: np.ndarray, model: FittedModel) -> np.ndarray:
    """The feature matrix of `rows` in the model's column order."""
    return data.x[np.ix_(rows, data.feature_index(model.feature_names))]
