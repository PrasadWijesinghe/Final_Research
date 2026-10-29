"""The trained model behind POST /predict (PLAN B9 and B12; docs/experts/build.md sections 6-7).

1. Load a model bundle (`models/build/<version>/`) and the outcomes of the dataset it was
   trained on (`outcomes.parquet`), which give history features exactly as in training.
2. For a CommitContext: compute all 99 features (`features.full_features`), score with the
   calibrated main model and the sub-risk models.
3. Explain: the features with the largest SHAP values become `RiskFactor`s, phrased with the
   templates in `features/descriptions.yaml`, as risk factors, never as causes.
"""

from __future__ import annotations

import json
import logging
import math
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow.parquet as pq

from shared.contracts import CommitContext, RiskFactor, RiskOutput

from .config import EXPERT
from .dataset.builder import outcome_from_row
from .features import CATEGORICAL_FEATURES, HistoryIndex, full_features
from .model.explain import descriptions
from .model.export import LoadedModel

logger = logging.getLogger(__name__)

MAIN_TARGET = "y_build"
# Sub-risk keys in RiskOutput.sub_risks (y_lint covers ESLint and Prettier).
SUB_RISKS = {"y_tsc": "tsc", "y_lint": "lint", "y_bundler": "bundler"}
MAX_FACTORS = 5  # the contract's limit on top_factors
MAX_REASON = 200  # RiskFactor.reason limit


def _value(raw: object) -> str | float | int | bool | None:
    """A feature value as RiskFactor accepts it; None when missing."""
    if raw is None:
        return None
    if isinstance(raw, bool | str):
        return raw
    if isinstance(raw, int | float):
        f = float(raw)
        if math.isnan(f):
            return None
        return int(f) if f.is_integer() else round(f, 4)
    return str(raw)


def _shown(name: str, value: str | float | int | bool) -> str:
    """How a value reads inside a template (rates as percentages)."""
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float) and ("rate" in name or "share" in name) and 0 <= value <= 1:
        return f"{value:.0%}"
    if isinstance(value, float):
        return (
            f"{value:.0f}"
            if abs(value) >= 10
            else f"{value:.1f}"
            if abs(value) >= 1
            else f"{value:.2f}"
        )
    return str(value)


def reason(name: str, value: str | float | int | bool) -> str:
    """The plain-language reason for one factor."""
    template = descriptions().get(name, name.replace("_", " ") + ": {value}")
    return template.replace("{value}", _shown(name, value))[:MAX_REASON]


def risk_factors(
    feats: dict[str, object], names: list[str], contributions: np.ndarray, *, need_one: bool
) -> list[RiskFactor]:
    """Top features that raise the risk (largest SHAP first), skipping missing values.

    When `need_one` (an alert) and nothing qualifies, the largest absolute SHAP value with a
    known value is used, so an alert always carries a reason (contract rule).
    """
    order = np.argsort(-contributions[: len(names)], kind="stable")
    out: list[RiskFactor] = []
    for i in order:
        shap = float(contributions[i])
        if shap <= 0 or len(out) == MAX_FACTORS:
            break
        value = _value(feats.get(names[i]))
        # DEFAULT: a zero, "no" or missing value explains an absence, which a developer cannot act
        # on, so it is not shown as a reason (it still counts in the probability).
        if value is None or value is False or value == 0:
            continue
        out.append(
            RiskFactor(
                feature=names[i],
                value=value,
                shap_value=round(shap, 5),
                direction="raises",
                reason=reason(names[i], value),
            )
        )
    if need_one and not out:
        for i in np.argsort(-np.abs(contributions[: len(names)]), kind="stable"):
            value = _value(feats.get(names[i]))
            if value is not None:
                shap = float(contributions[i])
                out.append(
                    RiskFactor(
                        feature=names[i],
                        value=value,
                        shap_value=round(shap, 5),
                        direction="raises" if shap >= 0 else "lowers",
                        reason=reason(names[i], value),
                    )
                )
                break
    return out


@dataclass
class BuildPredictor:
    """A loaded model bundle plus the history it needs."""

    version: str
    main: LoadedModel
    subs: dict[str, LoadedModel]
    index: HistoryIndex
    default_branches: dict[str, str | None]
    repos_with_history: frozenset[str]

    @classmethod
    def load(cls, bundle_dir: Path, data_dir: Path) -> BuildPredictor:
        """Read the bundle and the outcomes of its dataset (data_dir/datasets/<dataset id>)."""
        main = LoadedModel(bundle_dir / MAIN_TARGET)
        dataset_id = str(main.info.get("dataset_id") or "")
        ds_dir = data_dir / "datasets" / dataset_id
        outcomes = [
            outcome_from_row(r) for r in pq.read_table(ds_dir / "outcomes.parquet").to_pylist()
        ]
        manifest = json.loads((ds_dir / "manifest.json").read_text(encoding="utf-8"))
        branches = {
            name: info.get("default_branch") for name, info in (manifest.get("repos") or {}).items()
        }
        subs = {
            key: LoadedModel(bundle_dir / target)
            for target, key in SUB_RISKS.items()
            if (bundle_dir / target / "model.json").exists()
        }
        logger.info(
            "build model loaded",
            extra={"model": main.info.get("model"), "outcomes": len(outcomes), "subs": len(subs)},
        )
        return cls(
            version=str(main.info.get("model") or bundle_dir.name),
            main=main,
            subs=subs,
            index=HistoryIndex(outcomes),
            default_branches=branches,
            repos_with_history=frozenset(o.repo for o in outcomes),
        )

    @property
    def threshold(self) -> float:
        """The operating threshold (D03), on calibrated probabilities."""
        return float(self.main.info["threshold_operating"])

    def _vector(self, feats: dict[str, object], model: LoadedModel) -> np.ndarray:
        vocab: dict[str, list[str]] = model.info.get("categorical") or {}
        row: list[float] = []
        for name in model.features:
            raw = feats.get(name)
            if name in CATEGORICAL_FEATURES:
                values = vocab.get(name, [])
                row.append(float(values.index(raw)) if raw in values else math.nan)
            elif raw is None:
                row.append(math.nan)
            elif isinstance(raw, bool | int | float):
                row.append(float(raw))
            else:
                row.append(math.nan)
        return np.asarray([row], dtype=float)

    def features(self, ctx: CommitContext) -> dict[str, object]:
        """All 99 features for a commit (history before its cut-off only)."""
        return full_features(
            ctx, self.index, default_branch=self.default_branches.get(ctx.repo.full_name)
        )

    def predict(self, ctx: CommitContext) -> RiskOutput:
        """One commit-scoped RiskOutput with sub-risks and top factors."""
        started = time.perf_counter()
        feats = self.features(ctx)
        x = self._vector(feats, self.main)
        probability = round(float(np.clip(self.main.probabilities(x)[0], 0.0, 1.0)), 4)
        threshold = round(self.threshold, 4)
        contributions = np.asarray(self.main.booster.predict(x, pred_contrib=True))[0]
        alert = probability >= threshold
        factors = risk_factors(feats, self.main.features, contributions, need_one=alert)
        sub_risks = {
            key: round(float(np.clip(m.probabilities(self._vector(feats, m))[0], 0.0, 1.0)), 4)
            for key, m in self.subs.items()
        }
        known = ctx.repo.full_name in self.repos_with_history
        return RiskOutput(
            context_id=ctx.context_id,
            expert=EXPERT,
            model_version=self.version,
            scope="commit",
            # DEFAULT: a repo with no stored history gets its history features as missing, so the
            # prediction is marked degraded (it ran with missing inputs).
            status="ok" if known else "degraded",
            probability=probability,
            threshold=threshold,
            alert=alert,
            sub_risks=sub_risks,
            top_factors=factors,
            latency_ms=int((time.perf_counter() - started) * 1000),
        )


def load_from_env(env: dict[str, str]) -> BuildPredictor | None:
    """BUILD_MODEL_DIR names a bundle folder; without it (or when it cannot load) the stub runs."""
    bundle = env.get("BUILD_MODEL_DIR", "").strip()
    if not bundle:
        return None
    data_dir = Path(env.get("DATA_DIR") or Path(__file__).resolve().parents[4] / "data")
    try:
        return BuildPredictor.load(Path(bundle), data_dir)
    except (OSError, KeyError, ValueError) as exc:
        logger.warning("build model not loaded; using the stub", extra={"error": str(exc)})
        return None


def describe(predictor: BuildPredictor) -> dict[str, Any]:
    """Facts for /health and logs."""
    return {"model_version": predictor.version, "threshold": predictor.threshold}
