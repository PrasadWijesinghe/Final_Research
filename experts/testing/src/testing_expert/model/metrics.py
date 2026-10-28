"""Metrics, calibration and the P(fail) threshold (test.md section 5)."""

from __future__ import annotations

from typing import Any

import numpy as np
from sklearn.isotonic import IsotonicRegression
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


def fit_calibrator(y: np.ndarray, proba: np.ndarray) -> dict[str, Any]:
    """One isotonic regression per class (one-vs-rest); a one-outcome class stays raw."""
    models: list[IsotonicRegression | None] = []
    for i in range(len(CLASSES)):
        yi = (y == i).astype(float)
        if yi.min() == yi.max():
            models.append(None)
            continue
        iso = IsotonicRegression(y_min=0.0, y_max=1.0, out_of_bounds="clip")
        iso.fit(proba[:, i], yi)
        models.append(iso)
    return {"isotonic": models, "classes": list(CLASSES)}


def apply_calibrator(cal: dict[str, Any], proba: np.ndarray) -> np.ndarray:
    """Calibrate each class, then renormalise rows to sum to 1."""
    out = proba.copy()
    for i, iso in enumerate(cal["isotonic"]):
        if iso is not None:
            out[:, i] = iso.predict(proba[:, i])
    total = out.sum(axis=1, keepdims=True)
    # DEFAULT: a row where every isotonic output is 0 falls back to the uncalibrated row.
    bad = total[:, 0] <= 0
    out[bad] = proba[bad]
    total = out.sum(axis=1, keepdims=True)
    return out / total


def pick_threshold(y: np.ndarray, score: np.ndarray) -> dict[str, Any]:
    """P(fail) threshold: max recall with precision >= 0.8, else best F0.5."""
    yf = (y > 0).astype(int)
    if yf.sum() == 0:
        return {"threshold": 0.5, "rule": "no_positives_default", "precision": NAN, "recall": NAN}
    prec, rec, thr = precision_recall_curve(yf, score)
    prec, rec = prec[:-1], rec[:-1]
    ok = prec >= PRECISION_TARGET
    if ok.any():
        idx = int(np.flatnonzero(ok)[np.argmax(rec[ok])])
        rule = "precision>=0.8_max_recall"
    else:
        f05 = 1.25 * prec * rec / np.maximum(0.25 * prec + rec, 1e-12)
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
