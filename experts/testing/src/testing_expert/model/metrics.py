"""Metrics, calibration and the P(fail) threshold (test.md section 5)."""

from __future__ import annotations

from typing import Any

import numpy as np
from sklearn.isotonic import IsotonicRegression
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (
    average_precision_score,
    precision_recall_curve,
    precision_recall_fscore_support,
    roc_auc_score,
)

from .data import CLASSES

GENUINE, FLAKY = 1, 2
# DEFAULT: precision target 0.8 for the P(fail) threshold (brief), F0.5 fallback when unreachable.
PRECISION_TARGET = 0.8
MIN_FLAGGED = 20
NAN = float("nan")


def p_fail(proba: np.ndarray) -> np.ndarray:
    """P(fail) = P(genuine) + P(flaky)."""
    return proba[:, GENUINE] + proba[:, FLAKY]


def _safe(fn: Any, y: np.ndarray, s: np.ndarray) -> float:
    if y.min() == y.max():
        return NAN
    return float(fn(y, s))


def compute_metrics(
    y: np.ndarray, proba: np.ndarray, hard: np.ndarray | None = None
) -> dict[str, float]:
    """Per-class P/R/F1 and PR-AUC, any-fail PR-AUC, ROC-AUC, macro F1, false-flaky rate."""
    pred = proba.argmax(axis=1) if hard is None else hard
    prec, rec, f1, _ = precision_recall_fscore_support(
        y, pred, labels=list(range(len(CLASSES))), zero_division=0
    )
    out: dict[str, float] = {"n": float(len(y))}
    roc = []
    for i, name in enumerate(CLASSES):
        yi = (y == i).astype(int)
        out[f"precision_{name}"] = float(prec[i])
        out[f"recall_{name}"] = float(rec[i])
        out[f"f1_{name}"] = float(f1[i])
        out[f"pr_auc_{name}"] = _safe(average_precision_score, yi, proba[:, i])
        out[f"roc_auc_{name}"] = _safe(roc_auc_score, yi, proba[:, i])
        roc.append(out[f"roc_auc_{name}"])
    present = [i for i in range(len(CLASSES)) if (y == i).any()]
    out["macro_f1"] = float(np.mean([f1[i] for i in present])) if present else NAN
    valid_roc = [r for r in roc if not np.isnan(r)]
    out["roc_auc_macro"] = float(np.mean(valid_roc)) if valid_roc else NAN
    yf = (y > 0).astype(int)
    out["pr_auc_any_fail"] = _safe(average_precision_score, yf, p_fail(proba))
    out["roc_auc_any_fail"] = _safe(roc_auc_score, yf, p_fail(proba))
    out["fail_base_rate"] = float(yf.mean()) if len(yf) else NAN
    genuine = y == GENUINE
    out["false_flaky_rate"] = float((pred[genuine] == FLAKY).mean()) if genuine.any() else NAN
    out["flaky_f1"] = out["f1_flaky_fail"]
    return out


def brier(y: np.ndarray, proba: np.ndarray) -> float:
    """Multiclass Brier score: mean over rows of the summed squared error."""
    onehot = np.eye(len(CLASSES))[y]
    return float(((proba - onehot) ** 2).sum(axis=1).mean())


def brier_fail(y: np.ndarray, proba: np.ndarray) -> float:
    """Binary Brier score of P(fail)."""
    return float(((p_fail(proba) - (y > 0)) ** 2).mean())


CALIBRATORS = ("isotonic", "sigmoid", "beta", "isotonic_floor")
# DEFAULT: no calibrated class probability goes below this, so a risk score is never a hard 0.0
# (isotonic regression maps every low raw score to exactly 0 when the fit slice has no failure
# there, which a user reads as "impossible").
MIN_PROB = 1e-4
_EPS = 1e-6


def _logit_features(p: np.ndarray, kind: str) -> np.ndarray:
    """Platt uses logit(p); beta calibration (Kull et al. 2017) uses ln p and -ln(1-p)."""
    p = np.clip(p, _EPS, 1 - _EPS)
    if kind == "sigmoid":
        return np.log(p / (1 - p))[:, None]
    return np.column_stack([np.log(p), -np.log(1 - p)])


def fit_calibrator(y: np.ndarray, proba: np.ndarray, kind: str = "isotonic") -> dict[str, Any]:
    """One calibration map per class (one-vs-rest); a one-outcome class stays raw.

    kind: isotonic (step function), sigmoid (Platt), beta (3-parameter, can bend both tails) or
    isotonic_floor (isotonic, but never below the class's own base rate times FLOOR_SHARE).
    """
    if kind not in CALIBRATORS:
        raise ValueError(f"unknown calibrator {kind!r}")
    models: list[Any] = []
    for i in range(len(CLASSES)):
        yi = (y == i).astype(float)
        if yi.min() == yi.max():
            models.append(None)
        elif kind in ("sigmoid", "beta"):
            # DEFAULT: C=1e4, i.e. almost unregularised, so the map follows the data.
            lr = LogisticRegression(C=1e4, max_iter=1000)
            lr.fit(_logit_features(proba[:, i], kind), yi.astype(int))
            models.append(lr)
        else:
            iso = IsotonicRegression(y_min=0.0, y_max=1.0, out_of_bounds="clip")
            iso.fit(proba[:, i], yi)
            models.append(iso)
    # DEFAULT: the isotonic floor is 20% of the class base rate in the fit slice.
    floors = [FLOOR_SHARE * float((y == i).mean()) for i in range(len(CLASSES))]
    return {"kind": kind, "models": models, "floors": floors, "classes": list(CLASSES)}


FLOOR_SHARE = 0.2


def apply_calibrator(cal: dict[str, Any], proba: np.ndarray) -> np.ndarray:
    """Calibrate each class, then renormalise rows to sum to 1.

    Reads old bundles too (v1 / v2 stored {"isotonic": [...]} with no "kind").
    """
    kind = cal.get("kind", "isotonic")
    models = cal["models"] if "models" in cal else cal["isotonic"]
    out = proba.copy()
    for i, m in enumerate(models):
        if m is None:
            continue
        if kind in ("sigmoid", "beta"):
            out[:, i] = m.predict_proba(_logit_features(proba[:, i], kind))[:, 1]
        else:
            out[:, i] = m.predict(proba[:, i])
            if kind == "isotonic_floor":
                out[:, i] = np.maximum(out[:, i], cal["floors"][i])
    if kind != "isotonic":  # new calibrators never emit a hard zero; v1/v2 bundles keep old maths
        out[:, 1:] = np.maximum(out[:, 1:], MIN_PROB)
    total = out.sum(axis=1, keepdims=True)
    # DEFAULT: a row where every isotonic output is 0 falls back to the uncalibrated row.
    bad = total[:, 0] <= 0
    out[bad] = proba[bad]
    total = out.sum(axis=1, keepdims=True)
    return out / total


def ece(y: np.ndarray, score: np.ndarray, bins: int = 10) -> float:
    """Expected calibration error of P(fail): equal-count bins, weighted |mean score - rate|."""
    yf = (y > 0).astype(float)
    order = np.argsort(score, kind="stable")
    total = 0.0
    for chunk in np.array_split(order, bins):
        if len(chunk):
            total += len(chunk) * abs(float(score[chunk].mean()) - float(yf[chunk].mean()))
    return total / len(y)


def reliability(y: np.ndarray, score: np.ndarray, bins: int = 10) -> list[dict[str, float]]:
    """Per equal-count bin: n, mean predicted P(fail), observed fail rate (rare-event friendly)."""
    yf = (y > 0).astype(float)
    order = np.argsort(score, kind="stable")
    return [
        {"n": float(len(c)), "pred": float(score[c].mean()), "obs": float(yf[c].mean())}
        for c in np.array_split(order, bins)
        if len(c)
    ]


def pick_threshold(y: np.ndarray, score: np.ndarray) -> dict[str, Any]:
    """P(fail) threshold: max recall with precision >= 0.8, else best F0.5."""
    yf = (y > 0).astype(int)
    if yf.sum() == 0:
        return {"threshold": 0.5, "rule": "no_positives_default", "precision": NAN, "recall": NAN}
    prec, rec, thr = precision_recall_curve(yf, score)
    prec, rec = prec[:-1], rec[:-1]
    # DEFAULT: a threshold must flag >= 20 validation rows, else precision 0.8 on a handful of
    # top-scored rows is a fluke (this gave a useless threshold of 1.0 before).
    flagged = len(score) - np.searchsorted(np.sort(score), thr, side="left")
    support = flagged >= MIN_FLAGGED
    if not support.any():
        support = np.ones_like(support)
    ok = (prec >= PRECISION_TARGET) & support
    if ok.any():
        idx = int(np.flatnonzero(ok)[np.argmax(rec[ok])])
        rule = "precision>=0.8_max_recall"
    else:
        f05 = 1.25 * prec * rec / np.maximum(0.25 * prec + rec, 1e-12)
        f05 = np.where(support, f05, -1.0)
        idx = int(np.argmax(f05))
        rule = "best_f0.5 (precision 0.8 unreachable)"
    return {
        "threshold": float(thr[idx]),
        "rule": rule,
        "precision": float(prec[idx]),
        "recall": float(rec[idx]),
    }


def threshold_metrics(y: np.ndarray, score: np.ndarray, threshold: float) -> dict[str, float]:
    """Precision and recall of 'any fail' at a fixed threshold."""
    yf = y > 0
    pred = score >= threshold
    tp = float((pred & yf).sum())
    return {
        "precision": tp / pred.sum() if pred.sum() else NAN,
        "recall": tp / yf.sum() if yf.sum() else NAN,
        "flagged_share": float(pred.mean()) if len(pred) else NAN,
    }
