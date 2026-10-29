"""Model training and evaluation on a synthetic dataset with a known signal (model_data.py)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pytest
from build_expert.model import (
    LoadedModel,
    TrainSettings,
    export_bundle,
    load_dataset,
    load_settings,
    run_training,
)
from build_expert.model.cli import main as train_main
from build_expert.model.metrics import best_f1_threshold, binary_metrics, precision_threshold
from build_expert.model.splits import temporal_split
from build_expert.model.train import TooFewPositives, fit_model, matrix
from model_data import make_rows, write_dataset

REPO_ROOT = Path(__file__).resolve().parents[3]
FAST = TrainSettings(
    lightgbm={"num_leaves": 15, "seed": 1, "deterministic": True, "force_row_wise": True},
    num_boost_round=300,
    early_stopping_rounds=30,
    loro_min_positives=10,
)


@pytest.fixture(scope="module")
def dataset(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return write_dataset(tmp_path_factory.mktemp("ds") / "v1", make_rows(synthetic=40))


@pytest.fixture(scope="module")
def trained(dataset: Path):  # type: ignore[no-untyped-def]
    data = load_dataset(dataset)
    return data, run_training(data, FAST)


def test_config_file_loads_and_is_valid() -> None:
    s = load_settings(REPO_ROOT / "config" / "build_model.yaml")
    assert s.targets[0] == "y_build"
    assert s.train_fraction + s.validation_fraction < 1
    assert "num_boost_round" not in s.lightgbm  # popped into its own field


def test_bad_settings_are_refused() -> None:
    with pytest.raises(ValueError, match="unknown target"):
        TrainSettings(targets=("y_nope",)).check()
    with pytest.raises(ValueError, match="test part"):
        TrainSettings(train_fraction=0.8, validation_fraction=0.2).check()


def test_load_drops_synthetic_rows_and_encodes_categories(dataset: Path) -> None:
    data = load_dataset(dataset)
    assert len(data) == 2400 and data.dropped_synthetic == 40
    pm = data.column("package_manager")
    assert set(np.unique(pm)) == {0.0, 1.0, 2.0}
    assert data.vocab["package_manager"] == ("npm", "pnpm", "yarn")
    assert np.isnan(data.column("branch_last_build_failed")).any()  # None stays missing


def test_temporal_split_is_ordered_and_embargoed() -> None:
    cutoff = np.array([f"2026-10-{d:02d}T00:00" for d in range(1, 21)], dtype="datetime64[us]")
    completed = cutoff + np.timedelta64(1, "h")
    completed[13] = cutoff[16]  # a train row whose CI finished after validation began
    s = temporal_split(cutoff, completed, 0.7, 0.15, embargo=True)
    assert list(s.validation) == [14, 15, 16]
    assert list(s.test) == [17, 18, 19]
    assert 13 not in s.train and s.dropped_by_embargo == 1
    assert cutoff[s.train].max() < cutoff[s.validation].min()
    assert temporal_split(cutoff, completed, 0.7, 0.15, embargo=False).dropped_by_embargo == 0


def test_metrics_and_thresholds() -> None:
    y = np.array([0, 0, 1, 1, 0, 1])
    m = binary_metrics(y, np.array([0, 1, 1, 1, 0, 0]), np.array([0.1, 0.6, 0.7, 0.9, 0.2, 0.3]))
    assert (m["tp"], m["fp"], m["fn"], m["tn"]) == (2, 1, 1, 2)
    assert m["precision"] == pytest.approx(2 / 3, abs=1e-4)
    assert m["roc_auc"] is not None and m["pr_auc"] is not None
    scores = np.array([0.1, 0.2, 0.3, 0.4, 0.8, 0.9])
    t, f1 = best_f1_threshold(np.array([0, 0, 1, 0, 1, 1]), scores)
    assert t == pytest.approx(0.3) and f1 == pytest.approx(0.8571, abs=1e-3)
    t, reached = precision_threshold(np.array([0, 0, 1, 0, 1, 1]), scores, 1.0)
    assert reached and t == pytest.approx(0.8)
    # Precision 1.0 only on 2 hits: not enough when 3 are required, so fall back to best F1.
    t, reached = precision_threshold(np.array([0, 0, 1, 0, 1, 1]), scores, 1.0, min_hits=3)
    assert not reached and t == pytest.approx(0.3)


def test_model_beats_the_baselines_on_held_out_time(trained) -> None:  # type: ignore[no-untyped-def]
    _, result = trained
    out = result.results["targets"]["y_build"]
    model_test = out["model"]["test"]["at_f1"]
    base = out["baselines"]
    assert model_test["pr_auc"] > base["last_outcome"]["test"]["pr_auc"]
    assert model_test["roc_auc"] > 0.7
    assert out["model"]["threshold_operating"] >= out["model"]["threshold_f1"]
    assert set(out["per_repo_test"]) == {"acme/api", "acme/web", "beta/ui", "gamma/cli"}
    assert out["lead_time"]["warnings"] >= 1


def test_explanations_ablation_loro_and_sub_targets(trained) -> None:  # type: ignore[no-untyped-def]
    _, result = trained
    r = result.results
    top = [f["feature"] for f in r["top_factors"][:5]]
    assert "branch_last_build_failed" in top and "n_build_config_files" in top
    assert "{value}" not in r["top_factors"][0]["description"]
    assert abs(sum(r["group_importance"].values()) - 1) < 1e-3
    assert r["ablation"]["without history"]["pr_auc"] < r["ablation"]["all features"]["pr_auc"]
    assert r["leave_one_repo_out"]["summary"]["repos_scored"] == 4
    assert set(r["sub_targets"]) == {"y_tsc", "y_lint", "y_bundler"}


def test_too_few_failures_is_reported_not_raised(dataset: Path) -> None:
    data = load_dataset(dataset)
    rare = TrainSettings(min_positives=10_000, sub_targets=())
    result = run_training(data, rare, loro=False, ablation=False)
    assert "skipped" in result.results["targets"]["y_build"]["model"]
    assert result.models == {}
    assert result.results["notes"]
    split = temporal_split(data.cutoff, data.completed, 0.7, 0.15)
    with pytest.raises(TooFewPositives):
        fit_model(data, "y_build", split.train, split.validation, rare)


def test_export_round_trip_and_frozen_versions(trained, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    data, result = trained
    clock = lambda: datetime(2026, 10, 28, tzinfo=UTC)  # noqa: E731
    out = export_bundle(tmp_path, "m1", result, data, FAST, clock=clock)
    assert {p.name for p in out.iterdir()} == {
        "metrics.json",
        "report.md",
        "y_build",
        "y_build_confident",
        "y_tsc",
        "y_lint",
        "y_bundler",
    }
    loaded = LoadedModel(out / "y_build")
    fitted = result.models["y_build"]
    assert loaded.features == list(fitted.feature_names)
    rows = result.split.test[:50]
    np.testing.assert_allclose(
        loaded.probabilities(matrix(data, rows, fitted)),
        fitted.probabilities(matrix(data, rows, fitted)),
        atol=1e-9,
    )
    metrics = json.loads((out / "metrics.json").read_text(encoding="utf-8"))
    assert metrics["model"] == "build/m1" and metrics["dataset_id"] == "build/v1"
    report = (out / "report.md").read_text(encoding="utf-8")
    assert "Model against the baselines (H1)" in report and "Leave one repo out" in report
    with pytest.raises(FileExistsError):
        export_bundle(tmp_path, "m1", result, data, FAST, clock=clock)


def test_cli_trains_and_writes_a_bundle(dataset: Path, tmp_path: Path) -> None:
    config = tmp_path / "fast.yaml"
    config.write_text(
        "targets: [y_build]\nsub_targets: []\nablation_groups: [config]\n"
        "lightgbm: {num_leaves: 7, num_boost_round: 100, early_stopping_rounds: 20}\n",
        encoding="utf-8",
    )
    code = train_main(
        [
            "--dataset",
            str(dataset),
            "--version",
            "m1",
            "--out",
            str(tmp_path / "models"),
            "--config",
            str(config),
            "--no-loro",
        ]
    )
    assert code == 0
    assert (tmp_path / "models" / "m1" / "y_build" / "model.txt").exists()
    assert train_main(["--dataset", str(tmp_path / "nope"), "--version", "m2"]) == 2


def test_cv_folds_are_time_ordered_and_embargoed(dataset: Path) -> None:
    from build_expert.model.train import cv_folds

    data = load_dataset(dataset)
    split = temporal_split(data.cutoff, data.completed, 0.7, 0.15)
    folds = cv_folds(data, "y_build", split.train, TrainSettings())
    assert len(folds) == 3
    cut, done = data.cutoff[split.train], data.completed[split.train]
    for fit_pos, val_pos in folds:
        assert cut[fit_pos].max() <= cut[val_pos].min()
        assert done[fit_pos].max() < cut[val_pos].min()  # labels known before validation starts
    assert len(folds[0][0]) < len(folds[1][0]) < len(folds[2][0])  # expanding windows
    assert cv_folds(data, "y_build", split.train, TrainSettings(cv_folds=1)) == []


def test_seed_spread_and_rounds_source(trained) -> None:  # type: ignore[no-untyped-def]
    _, result = trained
    model = result.results["targets"]["y_build"]["model"]
    assert model["rounds_from"] == "cv"
    spread = model["seeds"]
    assert spread["n"] == len(FAST.seeds) == 5
    assert 0 <= spread["f1"]["sd"] < 0.2 and 0 < spread["f1"]["mean"] <= 1
