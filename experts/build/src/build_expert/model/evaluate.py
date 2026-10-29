"""Run every experiment of section 6 on one dataset and collect the results.

1. Temporal split; baselines and the model for each main target (H1), per-repo test results,
   lead time of correct warnings (H2) and the top factors (section 7).
2. Feature-group ablation on the main target (H3 for the config group).
3. Leave-one-repo-out: train on the other repos, test on the held-out one.
4. Sub-risk models (tsc, lint, bundler), reported only.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from build_expert.features import FEATURE_GROUPS

from .baselines import fit_baselines
from .data import TrainingData
from .explain import importance
from .metrics import binary_metrics
from .settings import TrainSettings
from .splits import Split, temporal_split, train_validation_split
from .train import FittedModel, TooFewPositives, fit_ensemble, matrix

logger = logging.getLogger(__name__)


@dataclass
class TrainingResult:
    """JSON-ready results and the fitted main models (for export)."""

    results: dict[str, Any]
    models: dict[str, FittedModel] = field(default_factory=dict)
    split: Split | None = None


def _part(data: TrainingData, split: Split, name: str, target: str) -> dict[str, Any]:
    idx: np.ndarray = getattr(split, name)
    window = split.window(data.cutoff, name)
    return {
        "rows": int(len(idx)),
        "positives": int(data.labels[target][idx].sum()),
        "from": window[0] if window else None,
        "to": window[1] if window else None,
    }


def _model_metrics(
    model: FittedModel, data: TrainingData, rows: np.ndarray, target: str
) -> dict[str, Any]:
    x = matrix(data, rows, model)
    raw, prob = model.raw_scores(x), model.probabilities(x)
    y = data.labels[target][rows]
    return {
        "at_f1": binary_metrics(y, prob >= model.threshold_f1, raw, prob),
        "at_operating": binary_metrics(y, prob >= model.threshold_operating, raw, prob),
    }


def _lead_time(
    model: FittedModel, data: TrainingData, rows: np.ndarray, target: str
) -> dict[str, Any]:
    """Hours between the warning (cut-off) and the end of CI, for correct warnings (H2)."""
    prob = model.probabilities(matrix(data, rows, model))
    hit = (prob >= model.threshold_operating) & (data.labels[target][rows] == 1)
    done, cut = data.completed[rows][hit], data.cutoff[rows][hit]
    ok = ~np.isnat(done)
    hours = (done[ok] - cut[ok]).astype("timedelta64[s]").astype(float) / 3600
    if not len(hours):
        return {"warnings": 0}
    return {
        "warnings": int(len(hours)),
        "median_hours": round(float(np.median(hours)), 2),
        "p25_hours": round(float(np.percentile(hours, 25)), 2),
        "p75_hours": round(float(np.percentile(hours, 75)), 2),
        "median_minutes": round(float(np.median(hours)) * 60, 1),
        "p25_minutes": round(float(np.percentile(hours, 25)) * 60, 1),
        "p75_minutes": round(float(np.percentile(hours, 75)) * 60, 1),
    }


def _per_repo(
    model: FittedModel, data: TrainingData, rows: np.ndarray, target: str
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for repo in sorted(set(data.repo[rows])):
        mine = rows[data.repo[rows] == repo]
        out[repo] = _model_metrics(model, data, mine, target)["at_f1"]
    return out


def _main_target(
    data: TrainingData, split: Split, target: str, settings: TrainSettings
) -> tuple[dict[str, Any], FittedModel | None]:
    out: dict[str, Any] = {
        "split": {p: _part(data, split, p, target) for p in ("train", "validation", "test")}
    }
    y_test = data.labels[target][split.test]
    out["baselines"] = {}
    for b in fit_baselines(data, target, split.validation):
        out["baselines"][b.name] = {
            "description": b.description,
            "threshold": None if np.isinf(b.threshold) else b.threshold,
            "coverage": b.coverage,
            "test": binary_metrics(y_test, b.predict(data, split.test), b.scores(data, split.test)),
        }
    try:
        model, singles = fit_ensemble(data, target, split.train, split.validation, settings)
    except TooFewPositives as exc:
        out["model"] = {"skipped": str(exc)}
        return out, None
    out["model"] = {
        "models": len(model.boosters),
        "trees": model.trees,
        "rounds_from": model.rounds_from,
        "scale_pos_weight": model.scale_pos_weight,
        "threshold_f1": round(model.threshold_f1, 4),
        "threshold_operating": round(model.threshold_operating, 4),
        "operating_reached_target": model.operating_reached_target,
        "validation": _model_metrics(model, data, split.validation, target),
        "test": _model_metrics(model, data, split.test, target),
    }
    out["model"]["seeds"] = _seed_spread(data, split, target, settings, singles)
    out["per_repo_test"] = _per_repo(model, data, split.test, target)
    out["lead_time"] = _lead_time(model, data, split.test, target)
    return out, model


SPREAD_METRICS = ("precision", "recall", "f1", "pr_auc", "roc_auc")


def _seed_spread(
    data: TrainingData,
    split: Split,
    target: str,
    settings: TrainSettings,
    singles: list[FittedModel],
) -> dict[str, Any]:
    """Each single model's test metrics (best-F1 threshold): mean and standard deviation."""
    runs = [_model_metrics(m, data, split.test, target)["at_f1"] for m in singles]
    out: dict[str, Any] = {"n": len(runs), "seeds": list(settings.seeds)}
    for k in SPREAD_METRICS:
        vals = [r[k] for r in runs if r[k] is not None]
        if vals:
            out[k] = {"mean": round(float(np.mean(vals)), 4), "sd": round(float(np.std(vals)), 4)}
    return out


def _ablation(
    data: TrainingData, split: Split, target: str, settings: TrainSettings, full: FittedModel
) -> dict[str, Any]:
    rows = {"all features": _model_metrics(full, data, split.test, target)["at_f1"]}
    out: dict[str, Any] = {}
    for group in settings.ablation_groups:
        keep = tuple(n for n in data.feature_names if n not in FEATURE_GROUPS[group])
        try:
            model, _ = fit_ensemble(
                data, target, split.train, split.validation, settings, features=keep
            )
        except TooFewPositives as exc:
            out[f"without {group}"] = {"skipped": str(exc)}
            continue
        rows[f"without {group}"] = _model_metrics(model, data, split.test, target)["at_f1"]
    for name, m in rows.items():
        out[name] = {k: m[k] for k in ("pr_auc", "roc_auc", "f1", "precision", "recall")}
    return out


def _leave_one_repo_out(data: TrainingData, target: str, settings: TrainSettings) -> dict[str, Any]:
    repos: dict[str, Any] = {}
    y = data.labels[target]
    pooled_y: list[np.ndarray] = []
    pooled_p: list[np.ndarray] = []
    pooled_raw: list[np.ndarray] = []
    pooled_flag: list[np.ndarray] = []
    for repo in sorted(set(data.repo)):
        held = np.flatnonzero(data.repo == repo)
        positives = int(y[held].sum())
        if positives < settings.loro_min_positives:
            repos[repo] = {
                "skipped": f"{positives} build failures (need {settings.loro_min_positives})"
            }
            continue
        others = np.flatnonzero(data.repo != repo)
        inner = train_validation_split(
            data.cutoff,
            data.completed,
            others,
            settings.loro_validation_fraction,
            embargo=settings.embargo,
        )
        try:
            model, _ = fit_ensemble(data, target, inner.train, inner.validation, settings)
        except TooFewPositives as exc:
            repos[repo] = {"skipped": str(exc)}
            continue
        x = matrix(data, held, model)
        raw, prob = model.raw_scores(x), model.probabilities(x)
        flag = prob >= model.threshold_f1
        repos[repo] = binary_metrics(y[held], flag, raw, prob)
        pooled_y.append(y[held])
        pooled_raw.append(raw)
        pooled_p.append(prob)
        pooled_flag.append(flag)
    scored = [m for m in repos.values() if "skipped" not in m]
    summary: dict[str, Any] = {
        "repos_scored": len(scored),
        "repos_skipped": len(repos) - len(scored),
    }
    if scored:
        for k in ("pr_auc", "f1", "precision", "recall"):
            vals = [m[k] for m in scored if m[k] is not None]
            summary[f"mean_{k}"] = round(float(np.mean(vals)), 4) if vals else None
        summary["pooled"] = binary_metrics(
            np.concatenate(pooled_y),
            np.concatenate(pooled_flag),
            np.concatenate(pooled_raw),
            np.concatenate(pooled_p),
        )
    return {"repos": repos, "summary": summary}


def _sub_targets(
    data: TrainingData, split: Split, settings: TrainSettings, models: dict[str, FittedModel]
) -> dict[str, Any]:
    """Train each sub-risk model; fitted ones are added to `models` for export."""
    out: dict[str, Any] = {}
    for target in settings.sub_targets:
        try:
            model, _ = fit_ensemble(data, target, split.train, split.validation, settings)
        except TooFewPositives as exc:
            out[target] = {"skipped": str(exc)}
            continue
        models[target] = model
        out[target] = {
            "trees": model.trees,
            "test": _model_metrics(model, data, split.test, target)["at_f1"],
        }
    return out


def _dataset_summary(data: TrainingData) -> dict[str, Any]:
    repos: dict[str, Any] = {}
    for repo in sorted(set(data.repo)):
        mine = data.repo == repo
        repos[repo] = {
            "commits": int(mine.sum()),
            "build_failures": int(data.labels["y_build"][mine].sum()),
            "other_failures": int(data.labels["y_other_failure"][mine].sum()),
        }
    files = data.manifest.get("files") or {}
    return {
        "id": data.manifest.get("dataset"),
        "built_at": data.manifest.get("built_at"),
        "rows_sha256": (files.get("rows.parquet") or {}).get("sha256"),
        "commits": len(data),
        "dropped_synthetic": data.dropped_synthetic,
        "labels": {k: int(v.sum()) for k, v in data.labels.items()},
        "first_cutoff": str(data.cutoff.min())[:10] if len(data) else None,
        "last_cutoff": str(data.cutoff.max())[:10] if len(data) else None,
        "repos": repos,
        "coverage": {
            name: round(float(np.mean(~np.isnan(data.column(name)))), 4) if len(data) else 0.0
            for name in ("branch_last_build_failed", "last_lint_warnings", "last_type_errors")
        },
    }


def run_training(
    data: TrainingData,
    settings: TrainSettings,
    *,
    loro: bool = True,
    ablation: bool = True,
    sub_targets: bool = True,
) -> TrainingResult:
    """All experiments; the first main target is the one ablation and explanations use."""
    split = temporal_split(
        data.cutoff,
        data.completed,
        settings.train_fraction,
        settings.validation_fraction,
        embargo=settings.embargo,
    )
    results: dict[str, Any] = {
        "dataset": _dataset_summary(data),
        "dropped_by_embargo": split.dropped_by_embargo,
        "targets": {},
        "notes": [],
    }
    result = TrainingResult(results=results, split=split)
    for target in settings.targets:
        logger.info("training", extra={"target": target})
        out, model = _main_target(data, split, target, settings)
        results["targets"][target] = out
        if model is not None:
            result.models[target] = model
    main = settings.targets[0]
    model = result.models.get(main)
    if model is None:
        results["notes"].append(f"No {main} model: too few build failures. Collect more data.")
        return result
    contrib = model.contributions(matrix(data, split.test, model))
    results["top_factors"], results["group_importance"] = importance(
        contrib, model.feature_names, settings.top_factors
    )
    if ablation:
        results["ablation"] = _ablation(data, split, main, settings, model)
    if loro:
        results["leave_one_repo_out"] = _leave_one_repo_out(data, main, settings)
    if sub_targets and settings.sub_targets:
        results["sub_targets"] = _sub_targets(data, split, settings, result.models)
    cov = results["dataset"]["coverage"]
    if cov["last_lint_warnings"] < 0.2:
        results["notes"].append(
            "Few commits have warning counts from an earlier log "
            f"({cov['last_lint_warnings']:.0%}), because passing-job logs are fetched later "
            "(`collector fill-logs`). The warning-rule baseline and the last_* features are "
            "weak until then."
        )
    if not model.operating_reached_target:
        results["notes"].append(
            f"The model did not reach {settings.target_precision:.0%} precision on validation; "
            "the operating threshold falls back to the best-F1 one."
        )
    return result
