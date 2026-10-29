"""Time-ordered splits (docs/experts/build.md section 6).

A commit's label is known only once its CI has finished (`completed_at`). With the embargo on, a
train commit must have finished before the first validation commit's cut-off, and a validation
commit before the first test commit's cut-off; otherwise the model would learn from outcomes that
were still in the future at prediction time.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class Split:
    """Row indices of each part, oldest first."""

    train: np.ndarray
    validation: np.ndarray
    test: np.ndarray
    dropped_by_embargo: int

    def window(self, cutoff: np.ndarray, part: str) -> tuple[str, str] | None:
        """First and last cut-off (ISO dates) of a part."""
        idx: np.ndarray = getattr(self, part)
        if len(idx) == 0:
            return None
        times = cutoff[idx]
        return (str(times.min())[:10], str(times.max())[:10])


def _finished_before(completed: np.ndarray, idx: np.ndarray, start: np.datetime64) -> np.ndarray:
    done = completed[idx]
    # NaT (unknown finish) compares False, so such rows are dropped: their label time is unknown.
    return idx[done < start]


def temporal_split(
    cutoff: np.ndarray,
    completed: np.ndarray,
    train_fraction: float,
    validation_fraction: float,
    *,
    embargo: bool = True,
    rows: np.ndarray | None = None,
) -> Split:
    """Oldest `train_fraction` -> train, next `validation_fraction` -> validation, rest -> test.

    `rows` limits the split to those row indices (used inside leave-one-repo-out).
    """
    pool = np.arange(len(cutoff)) if rows is None else np.asarray(rows)
    order = pool[np.argsort(cutoff[pool], kind="stable")]
    n = len(order)
    n_train = int(round(n * train_fraction))
    n_val = int(round(n * validation_fraction))
    train, val, test = order[:n_train], order[n_train : n_train + n_val], order[n_train + n_val :]
    dropped = 0
    if embargo:
        if len(val):
            kept = _finished_before(completed, train, cutoff[val].min())
            dropped += len(train) - len(kept)
            train = kept
        if len(test) and len(val):
            kept = _finished_before(completed, val, cutoff[test].min())
            dropped += len(val) - len(kept)
            val = kept
    return Split(train=train, validation=val, test=test, dropped_by_embargo=dropped)


def train_validation_split(
    cutoff: np.ndarray,
    completed: np.ndarray,
    rows: np.ndarray,
    validation_fraction: float,
    *,
    embargo: bool = True,
) -> Split:
    """Two parts only (no test): the newest `validation_fraction` of `rows` validates."""
    order = rows[np.argsort(cutoff[rows], kind="stable")]
    n_val = int(round(len(order) * validation_fraction))
    train, val = order[: len(order) - n_val], order[len(order) - n_val :]
    dropped = 0
    if embargo and len(val):
        kept = _finished_before(completed, train, cutoff[val].min())
        dropped = len(train) - len(kept)
        train = kept
    return Split(
        train=train, validation=val, test=np.array([], dtype=int), dropped_by_embargo=dropped
    )
