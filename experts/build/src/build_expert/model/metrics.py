"""Classification metrics used in the report (proposal Table 3)."""

from __future__ import annotations

from typing import Any

import numpy as np
from sklearn.metrics import (
    average_precision_score,
    brier_score_loss,
    precision_recall_curve,
    roc_auc_score,
)


def _safe(value: float) -> float | None:
    return None if np.isnan(value) else round(float(value), 4)


def binary_metrics(
    y: np.ndarray,
    predicted: np.ndarray,
    scores: np.ndarray | None = None,
    probabilities: np.ndarray | None = None,
) -> dict[str, Any]:
    """Counts, precision, recall and F1 of `predicted`; PR-AUC and ROC-AUC from `scores`
    (any ranking); Brier score from calibrated `probabilities`."""
    y = np.asarray(y, dtype=int)
    p = np.asarray(predicted, dtype=bool)
    tp = int(np.sum(p & (y == 1)))
    fp = int(np.sum(p & (y == 0)))
    fn = int(np.sum(~p & (y == 1)))
    tn = int(np.sum(~p & (y == 0)))
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    out: dict[str, Any] = {
        "n": int(len(y)),
        "positives": int(y.sum()),
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "tn": tn,
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
        "flagged_share": round(float(p.mean()), 4) if len(p) else 0.0,
        "pr_auc": None,
        "roc_auc": None,
        "brier": None,
    }
    both_classes = 0 < out["positives"] < out["n"]
    if scores is not None and both_classes:
        s = np.nan_to_num(np.asarray(scores, dtype=float), nan=-1.0)
        out["pr_auc"] = _safe(average_precision_score(y, s))
        out["roc_auc"] = _safe(roc_auc_score(y, s))
    if probabilities is not None and len(y):
        out["brier"] = _safe(brier_score_loss(y, np.asarray(probabilities, dtype=float)))
    return out


def _curve(y: np.ndarray, scores: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Precision and recall when flagging score >= t, for every distinct t (ascending)."""
    y = np.asarray(y, dtype=int)
    s = np.nan_to_num(np.asarray(scores, dtype=float), nan=-np.inf)
    finite = np.isfinite(s)
    if not y[finite].any() or y[finite].all():
        return np.array([]), np.array([]), np.array([])
    # Rows without a score are never flagged, but still count as missed failures.
    precision, recall, thresholds = precision_recall_curve(y[finite], s[finite])
    recall = recall[:-1] * y[finite].sum() / y.sum()
    return precision[:-1], recall, thresholds


def best_f1_threshold(y: np.ndarray, scores: np.ndarray) -> tuple[float, float]:
    """The score threshold (flag when score >= t) with the highest F1, and that F1."""
    precision, recall, thresholds = _curve(y, scores)
    if not len(thresholds):
        return float("inf"), 0.0
    with np.errstate(divide="ignore", invalid="ignore"):
        f1 = np.where(precision + recall > 0, 2 * precision * recall / (precision + recall), 0.0)
    i = int(np.argmax(f1))
    return float(thresholds[i]), round(float(f1[i]), 4)


def precision_threshold(
    y: np.ndarray, scores: np.ndarray, target_precision: float, min_hits: int = 1
) -> tuple[float, bool]:
    """The lowest threshold whose precision reaches the target (so recall is as high as possible)
    while still catching at least `min_hits` failures (a precision measured on one or two hits
    is luck, not a property of the model).

    Returns (threshold, reached). When no threshold reaches it, falls back to the best-F1 one.
    """
    precision, recall, thresholds = _curve(y, scores)
    hits = np.rint(recall * np.asarray(y, dtype=int).sum())
    ok = np.flatnonzero((precision >= target_precision - 1e-12) & (hits >= max(1, min_hits)))
    if len(ok):
        return float(thresholds[ok[0]]), True
    return best_f1_threshold(y, scores)[0], False
