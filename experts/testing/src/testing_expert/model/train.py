"""Train, validate, calibrate and export the v1 pre-run model (P4.6 + P4.7 v1)."""

from __future__ import annotations

import hashlib
import json
import logging
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import pandas as pd

from .baselines import BASELINES, baseline_predict
from .data import (
    CLASSES,
    VARIANTS,
    apply_variant,
    eligible_repos,
    feature_columns,
    load_dataset,
    repo_root,
    temporal_split,
)
from .metrics import (
    apply_calibrator,
    brier,
    brier_fail,
    compute_metrics,
    fit_calibrator,
    p_fail,
    pick_threshold,
    threshold_metrics,
)
from .models import MODELS, FittedModel, fit_model, write_features_json
from .report import write_card, write_report

log = logging.getLogger(__name__)
ALL_NAMES = (*BASELINES, *MODELS)


@dataclass
class VariantResult:
    """Everything measured for one training variant."""

    variant: str
    sizes: dict[str, int] = field(default_factory=dict)
    label_counts: dict[str, int] = field(default_factory=dict)
    temporal: dict[str, dict[str, float]] = field(default_factory=dict)
    calibration: dict[str, dict[str, float]] = field(default_factory=dict)
    threshold: dict[str, dict[str, Any]] = field(default_factory=dict)
    loro: dict[str, dict[str, dict[str, float]]] = field(
        default_factory=dict
    )  # name -> repo -> metrics
    fitted: dict[str, FittedModel] = field(default_factory=dict)
    calibrators: dict[str, dict[str, Any]] = field(default_factory=dict)


def _predict(
    name: str, train: pd.DataFrame, df: pd.DataFrame, fitted: FittedModel | None
) -> tuple[np.ndarray, np.ndarray | None]:
    if fitted is not None:
        return fitted.predict_proba(df), None
    proba, hard = baseline_predict(name, train, df)
    return proba, hard


def run_variant(
    df: pd.DataFrame, variant: str, cols: list[str], loro: bool = True
) -> VariantResult:
    """Temporal split + leave-one-repo-out for baselines and both models on one variant."""
    vdf = apply_variant(df, variant).reset_index(drop=True)
    res = VariantResult(variant)
    res.label_counts = {c: int((vdf["y"] == i).sum()) for i, c in enumerate(CLASSES)}
    train, cal, test = temporal_split(vdf)
    res.sizes = {"train": len(train), "calibration": len(cal), "test": len(test), "total": len(vdf)}
    ytest = test["y"].to_numpy()
    for name in ALL_NAMES:
        fitted = fit_model(name, train, cols) if name in MODELS else None
        if fitted is not None:
            res.fitted[name] = fitted
        proba, hard = _predict(name, train, test, fitted)
        res.temporal[name] = compute_metrics(ytest, proba, hard)
        if fitted is None:
            continue
        # Isotonic calibration on the calibration slice; Brier before/after on the test slice.
        pcal = fitted.predict_proba(cal)
        ycal = cal["y"].to_numpy()
        # DEFAULT: calibration slice split in time order: first half fits the isotonic maps,
        # second half picks the threshold on calibrated scores (no in-sample optimism).
        half = len(cal) // 2
        calibrator = fit_calibrator(ycal[:half], pcal[:half])
        res.calibrators[name] = calibrator
        proba_c = apply_calibrator(calibrator, proba)
        res.calibration[name] = {
            "brier_before": brier(ytest, proba),
            "brier_after": brier(ytest, proba_c),
            "brier_fail_before": brier_fail(ytest, proba),
            "brier_fail_after": brier_fail(ytest, proba_c),
        }
        thr = pick_threshold(ycal[half:], p_fail(apply_calibrator(calibrator, pcal[half:])))
        thr["test"] = threshold_metrics(ytest, p_fail(proba_c), float(thr["threshold"]))
        res.threshold[name] = thr
    if loro:
        res.loro = run_loro(vdf, cols)
    return res


def run_loro(vdf: pd.DataFrame, cols: list[str]) -> dict[str, dict[str, dict[str, float]]]:
    """Leave-one-repo-out: train without the repo, score it (raw probabilities)."""
    out: dict[str, dict[str, dict[str, float]]] = {n: {} for n in ALL_NAMES}
    for repo in eligible_repos(vdf):
        held = vdf[vdf["repo"] == repo]
        rest = vdf[vdf["repo"] != repo]
        y = held["y"].to_numpy()
        for name in ALL_NAMES:
            fitted = fit_model(name, rest, cols) if name in MODELS else None
            proba, hard = _predict(name, rest, held, fitted)
            out[name][repo] = compute_metrics(y, proba, hard)
    return out


def macro_mean(per_repo: dict[str, dict[str, float]], key: str) -> float:
    """Mean of one metric over repos, ignoring repos where it is undefined."""
    vals = [m[key] for m in per_repo.values() if not np.isnan(m[key])]
    return float(np.mean(vals)) if vals else float("nan")


def pick_winner(results: dict[str, VariantResult]) -> str:
    """Model with the best any-fail PR-AUC on the temporal test of the `all` variant."""
    t = results["all"].temporal
    return max(MODELS, key=lambda m: np.nan_to_num(t[m]["pr_auc_any_fail"], nan=-1.0))


def _git_commit(root: Path) -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, check=True
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def export_bundle(
    results: dict[str, VariantResult],
    winner: str,
    out_dir: Path,
    manifest: dict[str, Any],
    cols: list[str],
) -> None:
    """models/testing/v1/: model file, calibrator, features, threshold, card."""
    res = results["all"]
    fitted = res.fitted[winner]
    out_dir.mkdir(parents=True, exist_ok=True)
    model_file = fitted.save(out_dir)
    joblib.dump(res.calibrators[winner], out_dir / "calibrator.joblib")
    write_features_json(
        out_dir,
        fitted,
        {
            "features": cols,
            "dataset_version": manifest.get("version", "v1"),
            "dataset_sha256": manifest.get("sha256"),
        },
    )
    thr = res.threshold[winner]
    (out_dir / "threshold.json").write_text(
        json.dumps(
            {
                "score": "P(fail) = P(genuine_fail) + P(flaky_fail), calibrated",
                "threshold": thr["threshold"],
                "rule": thr["rule"],
                "calibration_slice": {"precision": thr["precision"], "recall": thr["recall"]},
                "test_slice": thr["test"],
            },
            indent=2,
        )
    )
    write_card(out_dir / "card.md", results, winner, manifest, model_file, cols)


def _log_mlflow(
    results: dict[str, VariantResult], manifest: dict[str, Any], dataset: str, root: Path
) -> None:
    import mlflow

    # DEFAULT: local sqlite store in mlruns/ (gitignored; MLflow's file store is deprecated),
    # experiment name testing_prerun.
    (root / "mlruns").mkdir(exist_ok=True)
    mlflow.set_tracking_uri(f"sqlite:///{root / 'mlruns' / 'mlflow.db'}")
    mlflow.set_experiment("testing_prerun")
    for variant, res in results.items():
        for name in ALL_NAMES:
            with mlflow.start_run(run_name=f"{name}-{variant}"):
                mlflow.log_params(
                    {
                        "model": name,
                        "variant": variant,
                        "dataset_version": dataset,
                        "dataset_sha256": str(manifest.get("sha256")),
                        **{f"n_{k}": v for k, v in res.sizes.items()},
                    }
                )
                mlflow.set_tag("dataset_version", dataset)
                metrics = {
                    f"temporal_{k}": v for k, v in res.temporal[name].items() if not np.isnan(v)
                }
                if name in res.calibration:
                    metrics.update(res.calibration[name])
                if res.loro:
                    for key in ("pr_auc_any_fail", "macro_f1", "flaky_f1", "false_flaky_rate"):
                        v = macro_mean(res.loro[name], key)
                        if not np.isnan(v):
                            metrics[f"loro_macro_{key}"] = v
                mlflow.log_metrics(metrics)


def run_training(
    dataset: str = "v1",
    root: Path | None = None,
    variants: tuple[str, ...] = VARIANTS,
    loro: bool = True,
    use_mlflow: bool = True,
    out_dir: Path | None = None,
    report_path: Path | None = None,
) -> dict[str, VariantResult]:
    """Run everything and write the bundle, the report and the MLflow runs."""
    root = root or repo_root()
    df, manifest = load_dataset(dataset, root)
    cols = feature_columns(df)
    results: dict[str, VariantResult] = {}
    for v in variants:
        log.info("variant", extra={"variant": v})
        results[v] = run_variant(df, v, cols, loro=loro)
    winner = pick_winner(results)
    rows_hash = hashlib.sha256(
        (root / "data/datasets/testing" / dataset / "rows.parquet").read_bytes()
    ).hexdigest()
    manifest = {**manifest, "version": dataset, "sha256": manifest.get("sha256", rows_hash)}
    export_bundle(results, winner, out_dir or root / "models" / "testing" / dataset, manifest, cols)
    write_report(
        report_path or root / "data" / "reports" / f"testing_model_{dataset}.md",
        results,
        winner,
        manifest,
        _git_commit(root),
    )
    if use_mlflow:
        _log_mlflow(results, manifest, dataset, root)
    return results
