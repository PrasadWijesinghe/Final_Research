"""Baselines for P4.6 v1: majority, same-as-last-outcome, re-run heuristic."""

from __future__ import annotations

import numpy as np
import pandas as pd

from .data import CLASSES, FLAKY_IDX

BASELINES = ("majority", "last_outcome", "rerun_heuristic")
# DEFAULT: flaky is predicted when >= 20% of the series' runs in the last 30 days were re-runs.
RERUN_RATE_THRESHOLD = 0.2


def baseline_predict(
    name: str, train: pd.DataFrame, df: pd.DataFrame
) -> tuple[np.ndarray, np.ndarray]:
    """Return (probabilities, hard predictions) of a baseline for the rows of `df`."""
    n = len(df)
    k = len(CLASSES)
    if name == "majority":
        prior = np.bincount(train["y"], minlength=k) / max(len(train), 1)
        proba = np.tile(prior, (n, 1))
        return proba, np.full(n, int(prior.argmax()))
    if name == "last_outcome":
        # Previous run outcome of the series (time order, data.last_outcome: no leakage).
        last = df["_last_y"].to_numpy(dtype=int)
        proba = np.eye(k)[last]
        return proba, last
    if name == "rerun_heuristic":
        runs = pd.to_numeric(df.get("h_runs_30d", 0), errors="coerce").fillna(0).to_numpy(float)
        reruns = pd.to_numeric(df.get("h_reruns_30d", 0), errors="coerce").fillna(0).to_numpy(float)
        rate = np.divide(reruns, runs, out=np.zeros(n), where=runs > 0).clip(0.0, 1.0)
        proba = np.zeros((n, k))
        proba[:, 0] = 1 - rate
        proba[:, FLAKY_IDX] = rate
        hard = np.where(rate >= RERUN_RATE_THRESHOLD, FLAKY_IDX, 0)
        return proba, hard
    raise ValueError(f"unknown baseline {name!r}")
