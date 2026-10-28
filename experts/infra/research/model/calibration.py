"""Chronological probability calibration for Infrastructure model candidates."""

from __future__ import annotations

from typing import Any

import pandas as pd
from sklearn.calibration import CalibratedClassifierCV
from sklearn.frozen import FrozenEstimator

from .features import TARGET_COLUMN

DEFAULT_CALIBRATION_FRACTION = 0.20


def split_model_fit_and_calibration(
    rows: pd.DataFrame,
    *,
    calibration_fraction: float = DEFAULT_CALIBRATION_FRACTION,
) -> tuple[pd.DataFrame, pd.DataFrame, int]:
    """Make an earlier model-fit set and later, incident-purged calibration set."""
    if not 0 < calibration_fraction < 1:
        raise ValueError("calibration_fraction must be between 0 and 1")
    if rows.empty:
        raise ValueError("cannot calibrate an empty training set")
    required = {"Incident_ID", "Run_Created_At_UTC", TARGET_COLUMN}
    missing = sorted(required - set(rows.columns))
    if missing:
        raise ValueError(f"calibration rows are missing required columns: {missing}")

    ordered = rows.sort_values("Run_Created_At_UTC").copy()
    cutoff = ordered["Run_Created_At_UTC"].quantile(1 - calibration_fraction)
    model_fit = ordered.loc[ordered["Run_Created_At_UTC"].le(cutoff)]
    calibration = ordered.loc[ordered["Run_Created_At_UTC"].gt(cutoff)]

    crossing = set(model_fit["Incident_ID"]) & set(calibration["Incident_ID"])
    crossing_rows = int(ordered["Incident_ID"].isin(crossing).sum())
    model_fit = model_fit.loc[~model_fit["Incident_ID"].isin(crossing)].copy()
    calibration = calibration.loc[~calibration["Incident_ID"].isin(crossing)].copy()

    for name, split in (("model-fit", model_fit), ("calibration", calibration)):
        if split.empty:
            raise ValueError(f"the {name} split is empty")
        if split[TARGET_COLUMN].nunique() != 2:
            raise ValueError(f"the {name} split must contain both target classes")
    return model_fit, calibration, crossing_rows


def fit_sigmoid_calibrator(
    fitted_estimator: Any,
    calibration_rows: pd.DataFrame,
    feature_columns: tuple[str, ...],
) -> CalibratedClassifierCV:
    """Fit Platt/sigmoid calibration without refitting the supplied estimator."""
    labels = calibration_rows[TARGET_COLUMN].to_numpy(dtype=int)
    calibrator = CalibratedClassifierCV(
        FrozenEstimator(fitted_estimator),
        method="sigmoid",
    )
    calibrator.fit(calibration_rows.loc[:, feature_columns], labels)
    return calibrator
