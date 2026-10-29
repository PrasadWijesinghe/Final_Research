"""The two baselines from proposal section 3.6 (docs/experts/build.md section 6).

1. Last outcome: warn when the previous finished commit on the same branch (or the default branch
   for a new pull-request branch) had a build failure (`branch_last_build_failed`).
2. Warning rule: warn when the latest earlier run on the branch printed any TypeScript error, or
   at least `t` ESLint warnings (`last_type_errors`, `last_lint_warnings`). `t` is chosen on the
   validation part for the best F1. These counts come from stored logs, so they are missing
   until the passing-job logs are fetched (`collector fill-logs`); the report shows how many
   rows have them.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .data import TrainingData
from .metrics import best_f1_threshold

# DEFAULT: any TypeScript error in the last run counts as above every lint threshold.
TYPE_ERROR_SCORE = 1e9


@dataclass(frozen=True)
class Baseline:
    """A fitted baseline: a score per row and the threshold it flags at."""

    name: str
    description: str
    threshold: float
    coverage: float  # share of rows that have the inputs the baseline needs

    def scores(self, data: TrainingData, rows: np.ndarray) -> np.ndarray:
        """Higher means more likely to fail; NaN means the baseline has nothing to go on."""
        if self.name == "last_outcome":
            return data.column("branch_last_build_failed")[rows]
        return warning_scores(data, rows)

    def predict(self, data: TrainingData, rows: np.ndarray) -> np.ndarray:
        """Flag rows whose score reaches the threshold (missing scores are never flagged)."""
        s = self.scores(data, rows)
        return np.nan_to_num(s, nan=-np.inf) >= self.threshold


def warning_scores(data: TrainingData, rows: np.ndarray) -> np.ndarray:
    """Lint warning count, or a very high score when the last run had TypeScript errors."""
    lint = data.column("last_lint_warnings")[rows]
    types = data.column("last_type_errors")[rows]
    score = lint.copy()
    score[np.nan_to_num(types, nan=0.0) >= 1] = TYPE_ERROR_SCORE
    return score


def fit_baselines(data: TrainingData, target: str, validation: np.ndarray) -> list[Baseline]:
    """Fit both baselines' thresholds on the validation rows."""
    y_val = data.labels[target][validation]
    last = data.column("branch_last_build_failed")
    warn = warning_scores(data, np.arange(len(data)))
    t_warn, _ = best_f1_threshold(y_val, warn[validation])
    return [
        Baseline(
            name="last_outcome",
            description="same as the last finished build on this branch",
            threshold=0.5,
            coverage=round(float(np.mean(~np.isnan(last))), 4) if len(data) else 0.0,
        ),
        Baseline(
            name="warning_rule",
            description="last run had a TypeScript error or many ESLint warnings",
            threshold=t_warn,
            coverage=round(float(np.mean(~np.isnan(warn))), 4) if len(data) else 0.0,
        ),
    ]
