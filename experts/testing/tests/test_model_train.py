"""Model baselines and training on a small synthetic dataset that follows the v1 schema."""

from __future__ import annotations

import json
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
import pytest
from testing_expert.model.baselines import BASELINES, baseline_predict
from testing_expert.model.data import (
    apply_variant,
    eligible_repos,
    feature_columns,
    load_dataset,
    temporal_split,
)
from testing_expert.model.metrics import (
    apply_calibrator,
    compute_metrics,
    fit_calibrator,
    pick_threshold,
)
from testing_expert.model.train import run_training


def make_rows(n: int = 1200, seed: int = 0) -> pd.DataFrame:
    """Synthetic rows: flaky correlates with h_reruns_30d, genuine with h_fail_rate_30d."""
    rng = np.random.default_rng(seed)
    repos = ["a/x", "b/y", "c/z"]
    t0 = pd.Timestamp("2026-01-01", tz="UTC")
    rows = []
    for i in range(n):
        repo = repos[i % 3]
        reruns = int(rng.integers(0, 6))
        fail_rate = float(rng.random())
        r = rng.random()
        if r < 0.5 * reruns / 10 + 0.02:
            label = "flaky_fail"
        elif r > 1 - 0.15 * fail_rate:
            label = "genuine_fail"
        else:
            label = "pass"
        rows.append(
            {
                "run_id": i,
                "attempt": 1,
                "job_id": i,
                "repo": repo,
                "created_at": t0 + pd.Timedelta(hours=i),
                "series_key": f"{repo}|job{i % 6}",
                "label": label,
                "label_confidence": "low" if (label == "flaky_fail" and i % 7 == 0) else "high",
                "rule_name": "r",
                "genuine_strict": label == "genuine_fail" and i % 2 == 0,
                "cs_files_changed": int(rng.integers(0, 50)),
                "h_runs_30d": 10,
                "h_reruns_30d": reruns,
                "h_fail_rate_30d": fail_rate,
                "h_last_failed": float(rng.integers(0, 2)) if i % 5 else np.nan,
                "rs_event": str(rng.choice(["push", "pull_request"])),
                "rp_framework": str(rng.choice(["jest", "mocha"])),
            }
        )
    return pd.DataFrame(rows)


@pytest.fixture
def root(tmp_path: Path) -> Path:
    d = tmp_path / "data" / "datasets" / "testing" / "v1"
    d.mkdir(parents=True)
    make_rows().to_parquet(d / "rows.parquet")
    (d / "manifest.json").write_text(json.dumps({"version": "v1", "sha256": "abc"}))
    return tmp_path


def test_loader_split_and_variants(root: Path) -> None:
    df, _ = load_dataset("v1", root)
    assert feature_columns(df) == [
        "cs_files_changed", "h_runs_30d", "h_reruns_30d", "h_fail_rate_30d",
        "h_last_failed", "rs_event", "rp_framework",
    ]  # fmt: skip
    train, cal, test = temporal_split(df)
    assert (
        train["created_at"].max()
        < cal["created_at"].min()
        <= cal["created_at"].max()
        < test["created_at"].min()
    )
    assert len(train) + len(cal) + len(test) == len(df)
    assert 0.65 < len(train) / len(df) < 0.75
    assert (apply_variant(df, "high")["label_confidence"] == "high").all()
    strict = apply_variant(df, "strict")
    assert strict[strict["label"] == "genuine_fail"]["genuine_strict"].all()
    assert len(strict) < len(df)
    assert eligible_repos(df) == ["a/x", "b/y", "c/z"]


def test_last_outcome_uses_only_earlier_runs(root: Path) -> None:
    df, _ = load_dataset("v1", root)
    first = df.groupby("series_key").head(1)
    assert (first["_last_y"] == 0).all()
    s = df[df["series_key"] == df["series_key"].iloc[7]].reset_index(drop=True)
    assert (s["_last_y"].iloc[1:].to_numpy() == s["y"].iloc[:-1].to_numpy()).all()


def test_baselines_shapes_and_rule(root: Path) -> None:
    df, _ = load_dataset("v1", root)
    train, _, test = temporal_split(df)
    for name in BASELINES:
        proba, hard = baseline_predict(name, train, test)
        assert proba.shape == (len(test), 3)
        assert np.allclose(proba.sum(axis=1), 1)
        assert hard.shape == (len(test),)
    _, hard = baseline_predict("majority", train, test)
    assert set(hard) == {0}
    _, hard = baseline_predict("rerun_heuristic", train, test)
    assert ((test["h_reruns_30d"] / test["h_runs_30d"] >= 0.2).to_numpy() == (hard == 2)).all()


def test_metrics_calibration_threshold() -> None:
    rng = np.random.default_rng(1)
    y = rng.integers(0, 3, 600)
    proba = rng.dirichlet([1, 1, 1], 600)
    proba[np.arange(600), y] += 1.0
    proba /= proba.sum(axis=1, keepdims=True)
    m = compute_metrics(y, proba)
    assert m["pr_auc_any_fail"] > 0.8 and 0 <= m["false_flaky_rate"] <= 1
    cal = fit_calibrator(y, proba)
    out = apply_calibrator(cal, proba)
    assert np.allclose(out.sum(axis=1), 1)
    thr = pick_threshold(y, proba[:, 1] + proba[:, 2])
    assert thr["precision"] >= 0.8 and "precision>=0.8" in thr["rule"]
    # A threshold must flag at least 20 rows even if a tiny top group is perfectly precise.
    sc = np.concatenate([np.ones(3), np.linspace(0.9, 0.0, 297)])
    yy = np.concatenate([np.ones(3, int), (np.random.default_rng(2).random(297) < 0.1).astype(int)])
    assert pick_threshold(yy, sc)["threshold"] < 1.0
    # Unreachable precision falls back to F0.5.
    assert "f0.5" in pick_threshold(y, np.zeros(600))["rule"]


def test_end_to_end_bundle_and_report(root: Path) -> None:
    res = run_training("v1", root=root, use_mlflow=False)
    assert set(res) == {"all", "high", "strict"}
    out = root / "models" / "testing" / "v1"
    for f in ("calibrator.joblib", "features.json", "threshold.json", "card.md"):
        assert (out / f).exists()
    assert (out / "model.txt").exists() or (out / "model.joblib").exists()
    report = (root / "data" / "reports" / "testing_model_v1.md").read_text()
    assert "Leave-one-repo-out" in report and "rerun_heuristic" in report
    r = res["all"]
    assert r.calibration["lightgbm"]["brier_after"] >= 0
    # The synthetic signal is real, so a model must beat the majority baseline on any-fail PR-AUC.
    assert r.temporal["lightgbm"]["pr_auc_any_fail"] > r.temporal["majority"]["pr_auc_any_fail"]
    assert set(res["all"].loro["logreg"]) == {"a/x", "b/y", "c/z"}
    joblib.load(out / "calibrator.joblib")


def test_mlflow_logs_to_local_store(root: Path) -> None:
    pytest.importorskip("mlflow")
    run_training("v1", root=root, variants=("all",), loro=False, use_mlflow=True)
    assert (root / "mlruns" / "mlflow.db").exists()
