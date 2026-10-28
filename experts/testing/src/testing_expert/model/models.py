"""Logistic regression and LightGBM, both with class weights (P4.7 v1)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import joblib
import lightgbm as lgb
import numpy as np
import pandas as pd
from sklearn.compose import ColumnTransformer
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler

from .data import CLASSES, Encoder, class_weights

MODELS = ("logreg", "lightgbm")
SEED = 42
# DEFAULT: fixed small hyper-parameters, no grid search (v1 is speed over tuning).
LOGREG_PARAMS: dict[str, Any] = {"C": 0.5, "max_iter": 500}
LGBM_PARAMS: dict[str, Any] = {
    "n_estimators": 200,
    "learning_rate": 0.05,
    "num_leaves": 31,
    "min_child_samples": 30,
    "subsample": 0.8,
    "subsample_freq": 1,
    "colsample_bytree": 0.8,
    "reg_lambda": 1.0,
}


@dataclass
class FittedModel:
    """A trained model plus the encoder that turns raw rows into its input."""

    kind: str
    encoder: Encoder
    est: Any

    def predict_proba(self, df: pd.DataFrame) -> np.ndarray:
        """Class probabilities in CLASSES order, shape (n, 3)."""
        if self.kind == "logreg":
            raw = self.est.predict_proba(self.encoder.lr_frame(df))
            seen = self.est.named_steps["clf"].classes_
        else:
            raw = self.est.predict_proba(self.encoder.lgbm_frame(df))
            seen = self.est.classes_
        out = np.zeros((len(df), len(CLASSES)))
        out[:, np.asarray(seen, dtype=int)] = raw
        return out

    def save(self, folder: Path) -> str:
        """Write model.txt (LightGBM) or model.joblib (logreg); return the file name."""
        folder.mkdir(parents=True, exist_ok=True)
        if self.kind == "lightgbm":
            self.est.booster_.save_model(str(folder / "model.txt"))
            return "model.txt"
        joblib.dump(self.est, folder / "model.joblib")
        return "model.joblib"


def fit_model(kind: str, train: pd.DataFrame, cols: list[str]) -> FittedModel:
    """Fit one model on the training rows (column `y` is the class index)."""
    enc = Encoder.fit(train, cols)
    y = train["y"].to_numpy(dtype=int)
    weights = class_weights(y)
    if kind == "lightgbm":
        est: Any = lgb.LGBMClassifier(
            objective="multiclass",
            class_weight=weights,
            random_state=SEED,
            n_jobs=4,
            verbose=-1,
            **LGBM_PARAMS,
        )
        est.fit(enc.lgbm_frame(train), y)
    elif kind == "logreg":
        transformers: list[tuple[str, Any, list[str]]] = []
        if enc.numeric:
            transformers.append(
                (
                    "num",
                    Pipeline([("imp", SimpleImputer(strategy="median")), ("sc", StandardScaler())]),
                    enc.numeric,
                )
            )
        if enc.categorical:
            transformers.append(
                ("cat", OneHotEncoder(handle_unknown="ignore"), list(enc.categorical))
            )
        est = Pipeline(
            [
                ("prep", ColumnTransformer(transformers)),
                (
                    "clf",
                    LogisticRegression(class_weight=weights, random_state=SEED, **LOGREG_PARAMS),
                ),
            ]
        )
        est.fit(enc.lr_frame(train), y)
    else:
        raise ValueError(f"unknown model {kind!r}")
    return FittedModel(kind, enc, est)


def write_features_json(folder: Path, model: FittedModel, extra: dict[str, Any]) -> None:
    """features.json: column order, kinds and categories the serving code must reproduce."""
    body = {"classes": list(CLASSES), "kind": model.kind, **model.encoder.to_json(), **extra}
    (folder / "features.json").write_text(json.dumps(body, indent=2))
