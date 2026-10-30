"""The "still failing" rule (P4.7 v6): a job whose previous run failed is likely to fail again.

The model under-scores jobs that keep failing, so the final risk is `max(model P, rule P)`.
`rule P` is the plain fail rate of earlier rows whose job had already failed `N` times in a row,
learned in buckets of `N`. The rule is data: `fit_rule` returns a small dict that goes into the
model bundle (`rule.json`), and the training report and the serving code both call
`apply_rule`, so they cannot drift apart.

"Previous run failed" is read from the history features. The same runner OS is preferred
(`h_os_last_failed`, `h_os_consecutive_failures`); with no earlier run on that OS the job-level
numbers are used (`h_last_failed`, `h_consecutive_failures`).
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

# DEFAULT: buckets of "failures in a row just before this run": 1, 2, 3-9, 10 or more. Few
# buckets, so each holds enough rows for a stable rate; the edges follow the streak rule of the
# labels (3+) and a "long broken" tail (10+). Edges are inclusive; None = no upper limit.
BUCKETS: tuple[tuple[int, int | None], ...] = ((1, 1), (2, 2), (3, 9), (10, None))
# DEFAULT: a bucket with fewer than this many training rows is not used (rule P = 0 there).
MIN_BUCKET_ROWS = 50

_OS_COLS = ("h_os_n_prior", "h_os_last_failed", "h_os_consecutive_failures")
_JOB_COLS = ("h_last_failed", "h_consecutive_failures")


def bucket_label(lo: int, hi: int | None) -> str:
    """Name of a bucket: "1", "2", "3-9", "10+"."""
    return f"{lo}+" if hi is None else (str(lo) if lo == hi else f"{lo}-{hi}")


def _col(df: pd.DataFrame, name: str) -> np.ndarray:
    if name not in df.columns:
        return np.zeros(len(df))
    return np.nan_to_num(pd.to_numeric(df[name], errors="coerce").to_numpy(dtype=float))


def previous_failures(df: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
    """(failures in a row before this run, 1 when that count is on the same OS else 0).

    Count 0 means the previous run passed or there is no history. Prefers the same-OS history
    when the job has an earlier run on that OS.
    """
    has_os = _col(df, "h_os_n_prior") > 0
    last = np.where(has_os, _col(df, "h_os_last_failed"), _col(df, "h_last_failed")) > 0
    streak = np.where(
        has_os, _col(df, "h_os_consecutive_failures"), _col(df, "h_consecutive_failures")
    )
    # a failed last run is at least a streak of 1 even if the count column says 0
    count = np.where(last, np.maximum(streak, 1.0), 0.0).astype(int)
    return count, has_os.astype(int)


def bucket_index(count: np.ndarray) -> np.ndarray:
    """Bucket number of each count (0-based, -1 where the rule does not apply)."""
    out = np.full(len(count), -1, dtype=int)
    for i, (lo, hi) in enumerate(BUCKETS):
        out[(count >= lo) & ((count <= hi) if hi is not None else True)] = i
    return out


def fit_rule(df: pd.DataFrame) -> dict[str, Any]:
    """Empirical rates per bucket from labelled rows (column `y`: 0 pass, 1 genuine, 2 flaky)."""
    count, _ = previous_failures(df)
    idx = bucket_index(count)
    y = df["y"].to_numpy(dtype=int)
    buckets = []
    for i, (lo, hi) in enumerate(BUCKETS):
        m = idx == i
        n = int(m.sum())
        use = n >= MIN_BUCKET_ROWS
        buckets.append(
            {
                "label": bucket_label(lo, hi),
                "min": lo,
                "max": hi,
                "n": n,
                "any": float((y[m] > 0).mean()) if use else 0.0,
                "genuine": float((y[m] == 1).mean()) if use else 0.0,
                "flaky": float((y[m] == 2).mean()) if use else 0.0,
            }
        )
    return {"buckets": buckets, "min_bucket_rows": MIN_BUCKET_ROWS}


def apply_rule(rule: dict[str, Any], df: pd.DataFrame) -> dict[str, np.ndarray]:
    """Per row: `active`, `count`, `same_os`, and rule P as `any`, `genuine`, `flaky` (0 if off)."""
    count, same_os = previous_failures(df)
    idx = bucket_index(count)
    out = {
        "active": idx >= 0,
        "count": count,
        "same_os": same_os,
        "any": np.zeros(len(df)),
        "genuine": np.zeros(len(df)),
        "flaky": np.zeros(len(df)),
    }
    for i, b in enumerate(rule["buckets"]):
        m = idx == i
        for k in ("any", "genuine", "flaky"):
            out[k][m] = b[k]
    out["active"] = out["active"] & (out["any"] > 0)
    return out


def combine(model_p: np.ndarray, rule_p: np.ndarray) -> np.ndarray:
    """Final P = max(model P, rule P)."""
    return np.maximum(model_p, rule_p)
