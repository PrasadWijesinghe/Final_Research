"""Out-of-fold pre-run flaky risk for the triage training rows (P4.9).

Triage uses the pre-run model's `flaky_fail` risk as one input. At serving time that risk comes
from the served bundle. For training it must not come from a model that saw the same rows, so it is
cross-fitted: the repos are split into folds, a flaky-vs-rest booster (same data, features and
settings as the served v4 flaky model) is trained on the other folds and scores the held-out repos,
and one beta calibrator maps those raw scores to probabilities.

# DEFAULT: 4 repo folds. Cross-fitting by repo (not by time) keeps the score honest about the row
# itself; it can still use other repos' later rows, a small leak that is recorded in the report.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from testing_expert.model import data as mdata
from testing_expert.model.metrics import apply_binary_calibrator, fit_binary_calibrator
from testing_expert.model.models import SEPARATE_KIND, fit_model

FOLDS = 4
# DEFAULT: dataset v3 is the one the served model was trained on (features.json: dataset_version).
DATASET = "v3"


def oof_flaky_risk(dataset: str = DATASET, folds: int = FOLDS) -> pd.Series:  # type: ignore[type-arg]
    """Calibrated out-of-fold P(flaky fail) per `job_id` for every row of the dataset."""
    df, _ = mdata.load_dataset(dataset)
    cols = mdata.feature_columns(df)
    repos = sorted(df["repo"].unique())
    fold_of = {r: i % folds for i, r in enumerate(repos)}
    fold = df["repo"].map(fold_of).to_numpy()
    raw = np.zeros(len(df))
    for k in range(folds):
        model = fit_model(SEPARATE_KIND, df[fold != k], cols)
        raw[fold == k] = model.predict_proba(df[fold == k])[:, mdata.FLAKY_IDX]
    y = (df["y"].to_numpy() == mdata.FLAKY_IDX).astype(int)
    cal = fit_binary_calibrator(y, raw, "beta")
    return pd.Series(apply_binary_calibrator(cal, raw), index=df["job_id"].to_numpy())
