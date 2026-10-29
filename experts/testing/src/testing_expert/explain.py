"""Explanations for the pre-push model (P4.8): LightGBM contributions turned into `RiskFactor`s.

How a score becomes reasons, in four steps:

1. LightGBM's `pred_contrib` gives, for one row, how much each feature pushed the model's score up
   or down (in log-odds; this is the exact TreeSHAP value for tree models).
2. The v4 bundle has two boosters: one for a genuine failure, one for a flaky failure. The served
   risk is `1 - (1 - pg)(1 - pf)`. Each booster's contributions are weighted by that booster's
   share of the combined risk, `pg / (pg + pf)` and `pf / (pg + pf)`, and added up. A feature that
   matters only to the flaky booster therefore counts more when the flaky risk dominates.
   Older bundles (one booster, or two-stage) use their fail-score contributions directly.
3. Features are ranked by the absolute combined contribution and the top N are kept. Direction is
   the sign: a positive score `raises` the risk, a negative one `lowers` it.
4. Each factor gets plain wording from `features/descriptions.yaml` (`template`, or `missing`
   when the value is not known), a path and line when the feature points at a spot in the diff,
   and is phrased as a risk factor, never as a cause.

`tw_*` factors (what the commit changed in test code) point at the first added line that matches.
"""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from typing import Any

import numpy as np
import pandas as pd

from shared.contracts.risk_output import RiskFactor

from .features import describe
from .features._types import ChangedTestFile, FeatureDict
from .model.data import CLASSES, Encoder
from .model.models import SeparateBooster

# DEFAULT: show at most the 5 factors the contract allows.
MAX_FACTORS = 5
# DEFAULT: a contribution smaller than this is noise and never shown.
MIN_CONTRIB = 1e-6
MAX_REASON_CHARS = 200  # RiskFactor.reason limit in the contract
UNKNOWN = "unknown"

# Where a tw_ feature points: the first added line that matches (path:line evidence).
_TW_LINE = {
    "tw_set_timeout": re.compile(r"\bset(Timeout|Interval)\s*\("),
    "tw_math_random": re.compile(r"\bMath\.random\s*\("),
    "tw_date_now": re.compile(r"\bDate\.now\s*\(|\bnew Date\s*\("),
    "tw_unmocked_network": re.compile(r"\bfetch\s*\(|\baxios\b|\bgot\s*\(|https?\.(get|request)\("),
    "tw_only_added": re.compile(r"\.only\s*\(|\bf(it|describe)\s*\("),
    "tw_skip_added": re.compile(r"\.skip\s*\(|\bx(it|describe|test)\s*\("),
    "tw_real_timers": re.compile(r"useRealTimers"),
}
_HUNK_START = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)")


def evidence_for(
    feature: str, test_files: Sequence[ChangedTestFile]
) -> tuple[str | None, int | None]:
    """File and line of the first added line behind a tw_ feature (path only when no line)."""
    pattern = _TW_LINE.get(feature)
    for f in test_files:
        if f.patch is None or pattern is None:
            continue
        line_no = 0
        for raw in f.patch.split("\n"):
            m = _HUNK_START.match(raw)
            if m:
                line_no = int(m.group(1)) - 1
            elif raw.startswith("+"):
                line_no += 1
                if pattern.search(raw):
                    return f.path, line_no
            elif raw.startswith(" "):
                line_no += 1
    if feature.startswith("tw_") and test_files:
        return test_files[0].path, None
    return None, None


def is_missing(value: object) -> bool:
    """True for None and NaN (a feature the pipeline could not compute)."""
    return value is None or (isinstance(value, float) and math.isnan(value))


def clean_value(value: object) -> str | float | int | bool:
    """A feature value as the contract allows it: numpy scalars unwrapped, unknown as text."""
    if is_missing(value):
        return UNKNOWN
    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, bool | int | float | str):
        return value
    return str(value)


def _for_template(value: str | float | int | bool) -> object:
    """Whole floats become ints, so a 0/1 flag reads `1`, not `1.0`."""
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def reason_text(feature: str, value: object) -> str:
    """Plain-language reason for one feature value, never longer than the contract allows."""
    cleaned = clean_value(value)
    text = describe(feature, None if cleaned == UNKNOWN else _for_template(cleaned))
    if len(text) <= MAX_REASON_CHARS:
        return text
    return text[: MAX_REASON_CHARS - 3].rstrip() + "..."


def combine_contributions(
    contrib: np.ndarray,
    n_features: int,
    separate: bool,
    pg: float | None = None,
    pf: float | None = None,
) -> np.ndarray:
    """One score per feature from a flat `pred_contrib` row (3 class blocks of n + 1 values).

    `separate` (v4): block 1 is the genuine booster, block 2 the flaky booster; each is weighted by
    its share of the combined risk (`pg`, `pf` are the calibrated probabilities). Equal shares
    when both are 0. Other bundles: the fail-score contribution is blocks 1 + 2 (the booster
    puts all of it in one of them).
    """
    blocks = np.asarray(contrib).reshape(len(CLASSES), n_features + 1)[:, :n_features]
    if not separate:
        return np.asarray(blocks[1] + blocks[2])
    total = (pg or 0.0) + (pf or 0.0)
    w_g = 0.5 if total <= 0 else (pg or 0.0) / total
    return np.asarray(w_g * blocks[1] + (1.0 - w_g) * blocks[2])


def make_factor(
    feature: str,
    value: object,
    score: float,
    test_files: Sequence[ChangedTestFile] = (),
) -> RiskFactor:
    """Build one `RiskFactor` from a feature, its value and its combined contribution."""
    known = not is_missing(value)
    path, line = (
        evidence_for(feature, test_files) if known and feature.startswith("tw_") else (None, None)
    )
    return RiskFactor(
        feature=feature,
        value=clean_value(value),
        shap_value=float(score),
        direction="raises" if score >= 0 else "lowers",
        reason=reason_text(feature, value),
        path=path,
        line=line,
    )


def top_factors(
    booster: Any,
    encoder: Encoder,
    frame: pd.DataFrame,
    i: int,
    row: FeatureDict,
    test_files: Sequence[ChangedTestFile] = (),
    limit: int = MAX_FACTORS,
    pg: float | None = None,
    pf: float | None = None,
) -> list[RiskFactor]:
    """The `limit` strongest reasons (raising or lowering the risk) for row `i` of `frame`."""
    if limit <= 0:
        return []
    contrib = np.asarray(booster.predict(frame.iloc[[i]], pred_contrib=True))[0]
    scores = combine_contributions(
        contrib,
        len(encoder.columns),
        separate=isinstance(booster, SeparateBooster),
        pg=pg,
        pf=pf,
    )
    out: list[RiskFactor] = []
    for idx in np.argsort(-np.abs(scores)):
        score = float(scores[idx])
        if abs(score) < MIN_CONTRIB:
            break
        name = encoder.columns[idx]
        out.append(make_factor(name, row.get(name), score, test_files))
        if len(out) == limit:
            break
    return out


_OS_LABEL = {"linux": "Linux", "windows": "Windows", "macos": "macOS"}


def still_failing_reason(count: int, os_name: str | None) -> str:
    """The plain-language reason of the "still failing" rule (at most 200 characters)."""
    times = "1 time" if count == 1 else f"{count} times"
    where = f" on {_OS_LABEL[os_name]}" if os_name in _OS_LABEL else ""
    return (
        f"This job already failed {times} in a row{where}; "
        "the failure is likely not caused by this push."
    )


def still_failing_factor(count: int, risk: float, os_name: str | None) -> RiskFactor:
    """The factor of the v6 "still failing" rule: the job's previous runs already failed."""
    return RiskFactor(
        feature="still_failing_streak",
        value=count,
        shap_value=risk,
        direction="raises",
        reason=still_failing_reason(count, os_name),
    )


def model_score_factor(prob: float) -> RiskFactor:
    """Fallback reason for an alert with no feature above the noise floor."""
    return RiskFactor(
        feature="model_score",
        value=round(prob, 4),
        shap_value=prob,
        direction="raises",
        reason="The overall model score is above the alert threshold",
    )
