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


# DEFAULT: monotone constraints (P4.7 v3). +1 = the predicted fail risk may only go up (or stay)
# when the feature goes up, -1 = only down. Only features whose direction is defensible from how
# tests break are listed; size, count-of-runs and categorical features are left free. The same
# rules apply to the `h_os_*` twin of every `h_*` history feature.
MONOTONE_HISTORY: dict[str, int] = {
    "fail_rate_7d": 1,  # a job that failed more of its recent runs fails more next time
    "fail_rate_30d": 1,
    "fail_rate_90d": 1,
    "fail_rate_all": 1,
    "consecutive_failures": 1,  # a longer failure streak never makes the next failure less likely
    "last_failed": 1,  # last run failed -> at least as risky as last run passed
    "weighted_flip_rate": 1,  # more recent pass/fail flipping = flakier job (Gruber et al.)
    "reruns_30d": 1,  # more recent re-runs = more recent flakiness
    "days_since_last_failure": -1,  # the longer since a failure, the lower the risk
}
# Test-change features count constructs that are known to cause flaky or failing tests, so more of
# them never lowers the risk. tw_only_added: a committed `.only` hides other tests and breaks CI
# policy in many repos.
MONOTONE_FEATURES: dict[str, int] = {
    **{f"h_{k}": v for k, v in MONOTONE_HISTORY.items()},
    **{f"h_os_{k}": v for k, v in MONOTONE_HISTORY.items()},
    "tw_unawaited_promises": 1,
    "tw_only_added": 1,
    "tw_real_timers": 1,
    "tw_set_timeout": 1,
    "tw_math_random": 1,
    "tw_date_now": 1,
    "tw_unmocked_network": 1,
    "tw_missing_cleanup": 1,
}
# DEFAULT: LightGBM gives every class of a multiclass model the SAME constraint vector, so "risk
# up" would also push P(pass) up. Instead the constrained model is two-stage: a binary
# fail-vs-pass booster (monotone) gives P(fail); a second, unconstrained binary booster trained on
# the failed rows only splits it into genuine vs flaky.
# 3-class probabilities = [1-pf, pf*q, pf*(1-q)].
TWO_STAGE_KINDS = ("lightgbm_2s", "lightgbm_mono")


def monotone_vector(columns: list[str], constraints: dict[str, int] | None = None) -> list[int]:
    """Constraint per model column (0 = free), in the booster's column order."""
    table = MONOTONE_FEATURES if constraints is None else constraints
    return [table.get(c, 0) for c in columns]


class TwoStageBooster:
    """Duck-types lgb.Booster.predict for serving: 3-class probabilities and fail contributions."""

    def __init__(self, fail: lgb.Booster, split: lgb.Booster) -> None:
        """`fail` is binary fail-vs-pass, `split` is binary genuine-vs-flaky."""
        self.fail = fail
        self.split = split

    def predict(self, frame: pd.DataFrame, pred_contrib: bool = False) -> np.ndarray:
        """(n, 3) class probabilities, or with pred_contrib (n, 3 * (n_features + 1))."""
        if pred_contrib:
            # all fail-score contributions go to the genuine slot, so serving's
            # `contrib[1] + contrib[2]` is the fail log-odds contribution
            c = np.asarray(self.fail.predict(frame, pred_contrib=True))
            z = np.zeros_like(c)
            return np.hstack([z, c, z])
        pf = np.asarray(self.fail.predict(frame))
        q = np.asarray(self.split.predict(frame))
        return np.column_stack([1 - pf, pf * q, pf * (1 - q)])

    def save(self, folder: Path) -> None:
        """model.txt = fail booster, split_model.txt = genuine-vs-flaky booster."""
        self.fail.save_model(str(folder / "model.txt"))
        self.split.save_model(str(folder / "split_model.txt"))

    @classmethod
    def load(cls, folder: Path) -> TwoStageBooster:
        """Read the two files written by `save`."""
        return cls(
            lgb.Booster(model_file=str(folder / "model.txt")),
            lgb.Booster(model_file=str(folder / "split_model.txt")),
        )


@dataclass
class FittedModel:
    """A trained model plus the encoder that turns raw rows into its input."""

    kind: str
    encoder: Encoder
    est: Any

    def predict_proba(self, df: pd.DataFrame) -> np.ndarray:
        """Class probabilities in CLASSES order, shape (n, 3)."""
        if self.kind in TWO_STAGE_KINDS:
            return np.asarray(self.est.predict(self.encoder.lgbm_frame(df)))
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
        if self.kind in TWO_STAGE_KINDS:
            self.est.save(folder)
            return "model.txt"
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
    elif kind in TWO_STAGE_KINDS:
        est = _fit_two_stage(enc, train, y, constrained=kind == "lightgbm_mono")
    elif kind == "logreg":
        transformers: list[tuple[str, Any, list[str]]] = []
        if enc.numeric:
            transformers.append(
                (
                    "num",
                    Pipeline(
                        [
                            # DEFAULT: missing values (e.g. tw_* unknown, None) get the median
                            # plus a missing-indicator column, so "unknown" is not read as a value.
                            # LightGBM instead handles NaN natively (own branch per split).
                            ("imp", SimpleImputer(strategy="median", add_indicator=True)),
                            ("sc", StandardScaler()),
                        ]
                    ),
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


def _fit_two_stage(
    enc: Encoder, train: pd.DataFrame, y: np.ndarray, constrained: bool
) -> TwoStageBooster:
    frame = enc.lgbm_frame(train)
    fail = (y > 0).astype(int)
    params = {**LGBM_PARAMS, "random_state": SEED, "n_jobs": 4, "verbose": -1}
    mono = monotone_vector(enc.columns) if constrained else None
    # DEFAULT: "intermediate" is LightGBM's tighter-than-basic constraint method (less loss of fit).
    stage1 = lgb.LGBMClassifier(
        objective="binary",
        class_weight={0: len(y) / (2 * (fail == 0).sum()), 1: len(y) / (2 * (fail == 1).sum())},
        monotone_constraints=mono,
        monotone_constraints_method="intermediate" if constrained else "basic",
        **params,
    )
    stage1.fit(frame, fail)
    failed = y > 0
    yg = (y[failed] == 1).astype(int)
    n = len(yg)
    stage2 = lgb.LGBMClassifier(
        objective="binary",
        class_weight={0: n / (2 * (yg == 0).sum()), 1: n / (2 * (yg == 1).sum())},
        **params,
    )
    stage2.fit(frame[failed], yg)
    return TwoStageBooster(stage1.booster_, stage2.booster_)


def write_features_json(folder: Path, model: FittedModel, extra: dict[str, Any]) -> None:
    """features.json: column order, kinds and categories the serving code must reproduce."""
    body = {"classes": list(CLASSES), "kind": model.kind, **model.encoder.to_json(), **extra}
    (folder / "features.json").write_text(json.dumps(body, indent=2))
