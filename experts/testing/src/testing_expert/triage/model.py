"""The small triage classifier, its confidence floors and the verdict rule (P4.9).

A failure gets P(flaky) from a small model (logistic regression or a small LightGBM, picked on
validation), mapped to a probability by a Platt calibrator fitted on out-of-fold scores. Two floors
turn the probability into a verdict:

- `flaky_floor`: P(flaky) at or above it is a confident `flaky` verdict (the only one that may
  suggest a re-run). Chosen on validation so that few real bugs are called flaky.
- `genuine_floor`: P(genuine) = 1 - P(flaky) at or above it is a confident `genuine` verdict.
- anything between is `unclear`, and the confidence reported is that of the leading class.
"""
# ruff: noqa: E501

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import joblib
import lightgbm as lgb
import numpy as np
import pandas as pd
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import StratifiedKFold
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

Kind = Literal["logreg", "lightgbm"]
Verdict = Literal["genuine", "flaky", "unclear"]
KINDS: tuple[Kind, ...] = ("logreg", "lightgbm")
SEED = 42
# DEFAULT: small fixed hyper-parameters, no search (3 147 labelled failures; speed and
# explainability over tuning). Logistic regression: median imputation plus a missing-value
# indicator, standardised, mild L2.
LOGREG_C = 0.3
LGBM_PARAMS: dict[str, Any] = {
    "n_estimators": 150,
    "learning_rate": 0.05,
    "num_leaves": 7,
    "min_child_samples": 20,
    "subsample": 0.8,
    "subsample_freq": 1,
    "colsample_bytree": 0.8,
    "reg_lambda": 5.0,
}
# DEFAULT: Platt scores come from 5-fold out-of-fold predictions of the training rows.
CALIBRATION_FOLDS = 5
# DEFAULT: at most 5% of genuine failures may be called flaky (a re-run advised for a real bug is
# the costly error; 10% would let about 1 in 4 flaky verdicts be wrong, see the report's sweep),
# and genuine verdicts should be right at least 85% of the time. Both floors stay at or above
# 0.5: a verdict must at least be the more probable class.
MAX_FALSE_FLAKY = 0.05
MIN_GENUINE_PRECISION = 0.85
MIN_FLOOR = 0.5


def make_estimator(kind: Kind) -> Any:
    """A fresh, unfitted estimator of the given kind."""
    if kind == "logreg":
        return Pipeline(
            [
                (
                    "imp",
                    SimpleImputer(strategy="median", add_indicator=True, keep_empty_features=True),
                ),
                ("sc", StandardScaler()),
                ("clf", LogisticRegression(C=LOGREG_C, max_iter=2000, random_state=SEED)),
            ]
        )
    return lgb.LGBMClassifier(random_state=SEED, n_jobs=2, verbose=-1, **LGBM_PARAMS)


def _logit(p: np.ndarray) -> np.ndarray:
    p = np.clip(p, 1e-6, 1 - 1e-6)
    return np.asarray(np.log(p / (1 - p))).reshape(-1, 1)


@dataclass
class TriageModel:
    """A fitted classifier + calibrator + floors. `predict_flaky` is the calibrated P(flaky)."""

    kind: Kind
    columns: list[str]
    est: Any
    platt: LogisticRegression | None
    flaky_floor: float = 0.5
    genuine_floor: float = 0.5

    def raw_flaky(self, x: pd.DataFrame) -> np.ndarray:
        """Uncalibrated P(flaky) from the estimator."""
        return np.asarray(self.est.predict_proba(x[self.columns].astype("float64"))[:, 1])

    def predict_flaky(self, x: pd.DataFrame) -> np.ndarray:
        """Calibrated P(flaky) for each row (NaN features are allowed)."""
        raw = self.raw_flaky(x)
        if self.platt is None:
            return raw
        return np.asarray(self.platt.predict_proba(_logit(raw))[:, 1])

    def verdicts(self, p_flaky: np.ndarray) -> list[tuple[Verdict, float]]:
        """(verdict, confidence) per row from the floors."""
        return [decide(float(p), self.flaky_floor, self.genuine_floor) for p in p_flaky]


def decide(p_flaky: float, flaky_floor: float, genuine_floor: float) -> tuple[Verdict, float]:
    """The verdict rule. Confidence is the probability of the verdict (the leading class when
    unclear). A flaky verdict wins a tie, but both floors above 0.5 cannot both hold."""
    p_gen = 1.0 - p_flaky
    if p_flaky >= flaky_floor:
        return "flaky", p_flaky
    if p_gen >= genuine_floor:
        return "genuine", p_gen
    return "unclear", max(p_flaky, p_gen)


def fit_model(
    kind: Kind, x: pd.DataFrame, y: np.ndarray, columns: list[str], calibrate: bool = True
) -> TriageModel:
    """Fit on rows `x` (flaky = 1). The Platt calibrator uses out-of-fold scores of `x`."""
    xs = x[columns].astype("float64")
    est = make_estimator(kind)
    est.fit(xs, y)
    platt = None
    if calibrate and 0 < int(y.sum()) < len(y):
        oof = np.zeros(len(y))
        n_splits = min(CALIBRATION_FOLDS, int(y.sum()), int(len(y) - y.sum()))
        if n_splits >= 2:
            skf = StratifiedKFold(n_splits, shuffle=True, random_state=SEED)
            for tr, te in skf.split(xs, y):
                m = make_estimator(kind)
                m.fit(xs.iloc[tr], y[tr])
                oof[te] = m.predict_proba(xs.iloc[te])[:, 1]
            platt = LogisticRegression(C=1e4, max_iter=1000).fit(_logit(oof), y)
    return TriageModel(kind, list(columns), est, platt)


# --- bundle ------------------------------------------------------------------------------------


def save_bundle(model: TriageModel, folder: Path, meta: dict[str, Any], card: str) -> None:
    """Write model.joblib, calibrator.joblib, features.json, floors.json and card.md."""
    folder.mkdir(parents=True, exist_ok=True)
    joblib.dump(model.est, folder / "model.joblib")
    joblib.dump(model.platt, folder / "calibrator.joblib")
    (folder / "features.json").write_text(
        json.dumps({"kind": model.kind, "columns": model.columns, **meta}, indent=1)
    )
    (folder / "floors.json").write_text(
        json.dumps(
            {"flaky_floor": model.flaky_floor, "genuine_floor": model.genuine_floor}, indent=1
        )
    )
    (folder / "card.md").write_text(card)


def load_bundle(folder: Path) -> TriageModel:
    """Read a triage bundle written by `save_bundle`; raises on anything unexpected."""
    meta = json.loads((folder / "features.json").read_text())
    floors = json.loads((folder / "floors.json").read_text())
    if meta["kind"] not in KINDS:
        raise ValueError(f"unsupported triage model kind {meta['kind']!r}")
    return TriageModel(
        kind=meta["kind"],
        columns=list(meta["columns"]),
        est=joblib.load(folder / "model.joblib"),
        platt=joblib.load(folder / "calibrator.joblib"),
        flaky_floor=float(floors["flaky_floor"]),
        genuine_floor=float(floors["genuine_floor"]),
    )


# --- floors and metrics --------------------------------------------------------------------------


def choose_floors(
    y: np.ndarray,
    p: np.ndarray,
    max_false_flaky: float = MAX_FALSE_FLAKY,
    min_genuine_precision: float = MIN_GENUINE_PRECISION,
) -> tuple[float, float]:
    """Pick (flaky_floor, genuine_floor) on validation scores `p` (calibrated P(flaky), y=1 flaky).

    - flaky_floor: the lowest floor (at least 0.5) whose false-flaky rate (genuine failures called flaky, as a
      share of all genuine failures) is at most `max_false_flaky`. Lowest = most flaky verdicts
      that still respect the limit.
    - genuine_floor: the lowest floor, at least 0.5, (most genuine verdicts) whose genuine verdicts are right at
      least `min_genuine_precision` of the time among rows not already called flaky. When no
      floor reaches that, genuine verdicts are switched off (floor above 1).
    """
    n_gen = int((y == 0).sum())
    flaky_floor = 1.01  # no flaky verdict possible
    for t in sorted(set(np.round(p, 4))):
        if t < MIN_FLOOR:
            continue
        ffr = float(((p >= t) & (y == 0)).sum()) / max(n_gen, 1)
        if ffr <= max_false_flaky:
            flaky_floor = float(t)
            break
    not_flaky = p < flaky_floor
    genuine_floor = 1.01
    for g in sorted(set(np.round(1 - p[not_flaky], 4))):
        if g < MIN_FLOOR:
            continue
        called = not_flaky & ((1 - p) >= g)
        if called.sum() >= 20 and float((y[called] == 0).mean()) >= min_genuine_precision:
            genuine_floor = float(g)
            break
    return flaky_floor, genuine_floor


def evaluate(
    y: np.ndarray, p: np.ndarray, flaky_floor: float, genuine_floor: float
) -> dict[str, float]:
    """Triage metrics for scores `p` (y: 1 flaky, 0 genuine) and the two floors."""
    verdicts = np.array([decide(float(x), flaky_floor, genuine_floor)[0] for x in p])
    return verdict_metrics(y, verdicts, p)


def verdict_metrics(
    y: np.ndarray, verdicts: np.ndarray, scores: np.ndarray | None = None
) -> dict[str, float]:
    """Metrics of verdicts (`genuine`, `flaky`, `unclear`) against labels (y: 1 flaky, 0 genuine).

    - coverage: share of failures with a verdict other than `unclear`.
    - accuracy_decided, balanced_accuracy, macro_f1: over decided rows only (unclear is not a guess).
    - accuracy_all: unclear counts as wrong.
    - flaky precision/recall/F1: recall over ALL flaky failures (an unclear one is a miss).
    - false_flaky_rate: genuine failures called flaky / all genuine failures (the costly error).
    - pr_auc_flaky, roc_auc: threshold-free, from `scores` when given.
    """
    from sklearn.metrics import (
        average_precision_score,
        balanced_accuracy_score,
        f1_score,
        roc_auc_score,
    )

    nan = float("nan")
    decided = verdicts != "unclear"
    pred = (verdicts == "flaky").astype(int)
    n_flaky, n_gen = int((y == 1).sum()), int((y == 0).sum())
    tp = int(((pred == 1) & (y == 1)).sum())
    fp = int(((pred == 1) & (y == 0)).sum())
    prec = tp / (tp + fp) if tp + fp else nan
    rec = tp / n_flaky if n_flaky else nan
    f1 = 2 * prec * rec / (prec + rec) if tp else 0.0
    yd, pr = y[decided], pred[decided]
    both = 0 < n_flaky < len(y)
    return {
        "n": float(len(y)),
        "n_flaky": float(n_flaky),
        "coverage": float(decided.mean()) if len(y) else nan,
        "accuracy_decided": float((yd == pr).mean()) if decided.any() else nan,
        "accuracy_all": float(((pred == y) & decided).mean()) if len(y) else nan,
        "balanced_accuracy": float(balanced_accuracy_score(yd, pr))
        if decided.any() and len(set(yd)) > 1
        else nan,
        "macro_f1": float(f1_score(yd, pr, average="macro")) if decided.any() else nan,
        "flaky_precision": prec,
        "flaky_recall": rec,
        "flaky_f1": f1,
        "false_flaky_rate": fp / n_gen if n_gen else nan,
        "n_called_flaky": float(pred.sum()),
        "pr_auc_flaky": float(average_precision_score(y, scores))
        if scores is not None and both
        else nan,
        "roc_auc": float(roc_auc_score(y, scores)) if scores is not None and both else nan,
    }
