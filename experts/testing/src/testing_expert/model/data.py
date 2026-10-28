"""Load the v1 dataset, build variants and splits, encode features (P4.6/P4.7 v1)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

CLASSES = ("pass", "genuine_fail", "flaky_fail")
CLASS_INDEX = {c: i for i, c in enumerate(CLASSES)}
FLAKY_IDX = CLASS_INDEX["flaky_fail"]
FEATURE_PREFIXES = ("cs_", "stl_", "tc_", "td_", "h_", "rs_", "rp_", "tw_")
VARIANTS = ("all", "high", "strict")

# DEFAULT: v1 cut, temporal split by created_at: earliest 70% train, next 10% calibration,
# last 20% test (the usual 70/10/20 holdout; cut on timestamp so one run never straddles a cut).
TRAIN_FRAC = 0.70
CAL_FRAC = 0.10
# DEFAULT: a repo is held out in leave-one-repo-out only with >= 20 non-pass rows (brief).
MIN_NON_PASS_PER_REPO = 20


def repo_root() -> Path:
    """Repository root (this file lives in experts/testing/src/testing_expert/model)."""
    return Path(__file__).resolve().parents[5]


def dataset_dir(version: str, root: Path | None = None) -> Path:
    """Folder of one dataset version."""
    return (root or repo_root()) / "data" / "datasets" / "testing" / version


def load_dataset(version: str, root: Path | None = None) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Read rows.parquet + manifest.json, sorted by time, with `y` and the last-outcome lag."""
    d = dataset_dir(version, root)
    manifest: dict[str, Any] = json.loads((d / "manifest.json").read_text())
    df = pd.read_parquet(d / "rows.parquet")
    df["created_at"] = pd.to_datetime(df["created_at"], utc=True)
    df = df.sort_values(["created_at", "run_id", "attempt"], kind="stable").reset_index(drop=True)
    df["y"] = df["label"].map(CLASS_INDEX).astype(int)
    df["_last_y"] = last_outcome(df)
    return df, manifest


def last_outcome(df: pd.DataFrame) -> pd.Series:  # type: ignore[type-arg]
    """Label of the series' previous run (strictly earlier created_at), else `pass`.

    Attempts of one run share created_at and are collapsed first, so a retry never sees the label
    of the attempt it is retrying (that label is only known after the retry).
    """
    last_per_time = df.groupby(["series_key", "created_at"], sort=False)["y"].last().reset_index()
    last_per_time["_prev"] = last_per_time.groupby("series_key", sort=False)["y"].shift(1)
    merged = df[["series_key", "created_at"]].merge(
        last_per_time[["series_key", "created_at", "_prev"]],
        on=["series_key", "created_at"],
        how="left",
    )
    # DEFAULT: first run of a series has no previous outcome, treated as `pass`.
    return merged["_prev"].fillna(0).astype(int).set_axis(df.index)


def feature_columns(df: pd.DataFrame) -> list[str]:
    """Feature columns present in the frame (by prefix), in file order."""
    return [c for c in df.columns if c.startswith(FEATURE_PREFIXES)]


def apply_variant(df: pd.DataFrame, variant: str) -> pd.DataFrame:
    """all = every row; high = no low-confidence labels; strict = genuine only if strict."""
    if variant == "all":
        return df
    if variant == "high":
        return df[df["label_confidence"] != "low"]
    if variant == "strict":
        # DEFAULT: non-strict genuine rows are dropped, not relabelled (doubtful, not `pass`).
        return df[(df["label"] != "genuine_fail") | df["genuine_strict"].astype(bool)]
    raise ValueError(f"unknown variant {variant!r}")


def temporal_split(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Train (earliest), calibration (middle), test (latest), cut on created_at timestamps."""
    t_train = df["created_at"].quantile(TRAIN_FRAC)
    t_cal = df["created_at"].quantile(TRAIN_FRAC + CAL_FRAC)
    train = df[df["created_at"] <= t_train]
    cal = df[(df["created_at"] > t_train) & (df["created_at"] <= t_cal)]
    test = df[df["created_at"] > t_cal]
    return train, cal, test


def eligible_repos(df: pd.DataFrame) -> list[str]:
    """Repos with enough non-pass rows to be held out."""
    counts = df[df["y"] > 0].groupby("repo").size()
    return sorted(counts[counts >= MIN_NON_PASS_PER_REPO].index)


@dataclass
class Encoder:
    """Remembers which columns are numeric or categorical and the training categories."""

    numeric: list[str] = field(default_factory=list)
    categorical: dict[str, list[str]] = field(default_factory=dict)

    @classmethod
    def fit(cls, df: pd.DataFrame, cols: list[str]) -> Encoder:
        """Learn column kinds and categories from the training rows."""
        enc = cls()
        for c in cols:
            s = df[c]
            if pd.api.types.is_bool_dtype(s) or pd.api.types.is_numeric_dtype(s):
                enc.numeric.append(c)
            else:
                enc.categorical[c] = sorted(str(v) for v in s.dropna().unique())
        return enc

    @property
    def columns(self) -> list[str]:
        """Model input columns: numeric first, then categorical."""
        return [*self.numeric, *self.categorical]

    def lgbm_frame(self, df: pd.DataFrame) -> pd.DataFrame:
        """Numeric as float, strings as fixed-category dtype (unseen values become NaN)."""
        out = pd.DataFrame(index=df.index)
        for c in self.numeric:
            out[c] = pd.to_numeric(df[c], errors="coerce").astype("float64")
        for c, cats in self.categorical.items():
            out[c] = pd.Categorical(
                df[c].astype("object").where(df[c].notna(), None), categories=cats
            )
        return out

    def lr_frame(self, df: pd.DataFrame) -> pd.DataFrame:
        """Numeric as float (NaN kept for the imputer), strings as plain text."""
        out = pd.DataFrame(index=df.index)
        for c in self.numeric:
            out[c] = pd.to_numeric(df[c], errors="coerce").astype("float64")
        for c in self.categorical:
            out[c] = df[c].astype("object").where(df[c].notna(), "__missing__").astype(str)
        return out

    def to_json(self) -> dict[str, Any]:
        """Serialisable form for features.json."""
        return {"numeric": self.numeric, "categorical": self.categorical}


def class_weights(y: np.ndarray) -> dict[int, float]:
    """Balanced class weights (n / (k * count)), the 'class weights, no oversampling' rule."""
    counts = np.bincount(y, minlength=len(CLASSES)).astype(float)
    k = int((counts > 0).sum())
    return {i: float(len(y) / (k * c)) for i, c in enumerate(counts) if c > 0}
