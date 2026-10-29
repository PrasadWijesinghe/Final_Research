"""Which features drive the model's warnings (SHAP values from LightGBM's own TreeSHAP).

Section 7: each top factor is later phrased with its template from `features/descriptions.yaml`,
as a risk factor, never as a cause. Here they are summed over the test part for the report.
"""

from __future__ import annotations

from functools import cache
from pathlib import Path
from typing import Any

import numpy as np
import yaml

from .data import GROUP_OF

DESCRIPTIONS = Path(__file__).resolve().parents[1] / "features" / "descriptions.yaml"


@cache
def descriptions() -> dict[str, str]:
    """Feature name -> plain-language template with `{value}`."""
    raw = yaml.safe_load(DESCRIPTIONS.read_text(encoding="utf-8")) or {}
    return {str(k): str(v) for k, v in raw.items()}


def importance(
    contributions: np.ndarray, feature_names: tuple[str, ...], top: int
) -> tuple[list[dict[str, Any]], dict[str, float]]:
    """Top features by mean |SHAP| and each group's share of the total."""
    per_feature = np.abs(contributions[:, : len(feature_names)]).mean(axis=0)
    total = float(per_feature.sum()) or 1.0
    order = np.argsort(-per_feature, kind="stable")
    texts = descriptions()
    factors = [
        {
            "feature": feature_names[i],
            "group": GROUP_OF.get(feature_names[i], "?"),
            "mean_abs_shap": round(float(per_feature[i]), 5),
            "share": round(float(per_feature[i]) / total, 4),
            # Global importance has no single value, so the template shows N instead.
            "description": texts.get(feature_names[i], feature_names[i]).replace("{value}", "N"),
        }
        for i in order[:top]
        if per_feature[i] > 0
    ]
    groups: dict[str, float] = {}
    for i, name in enumerate(feature_names):
        g = GROUP_OF.get(name, "?")
        groups[g] = groups.get(g, 0.0) + float(per_feature[i]) / total
    return factors, {g: round(v, 4) for g, v in sorted(groups.items(), key=lambda kv: -kv[1])}
