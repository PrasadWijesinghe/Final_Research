"""Serving: features at /predict equal the dataset's, and the loaded model answers valid outputs."""

from __future__ import annotations

import json
import math
import shutil
from pathlib import Path
from typing import Any

import build_scenario as sc
import pyarrow.parquet as pq
import pytest
from build_expert.dataset import DatasetSettings, build_dataset
from build_expert.dataset.builder import outcome_from_row
from build_expert.features import CATEGORICAL_FEATURES, FEATURE_NAMES, HistoryIndex, full_features
from build_expert.model import TrainSettings, export_bundle, load_dataset, run_training
from build_expert.serving import BuildPredictor, load_from_env, reason, risk_factors
from features.replay import build_replay_context
from model_data import make_rows, write_dataset

from shared.contracts import CommitContext
from shared.file_tags import FileTagger

ROOT = Path(__file__).resolve().parents[3]
TAGS = ROOT / "config" / "file_tags.yaml"
FIXTURES = Path(__file__).parent / "fixtures"
FAST = TrainSettings(
    lightgbm={"num_leaves": 15, "seed": 1, "deterministic": True, "force_row_wise": True},
    num_boost_round=200,
    early_stopping_rounds=20,
)


@pytest.fixture(scope="module")
def scenario(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("scenario")
    build_dataset(
        sc.source(), sc.history(), out, DatasetSettings(version="v1"), tagger=FileTagger.load(TAGS)
    )
    return out / "v1"


def _ctx(row: dict[str, Any]) -> CommitContext:
    run_id = int(str(row["run_ids"]).split(",")[0])
    return build_replay_context(
        sc.history(), repo=row["repo"], run_id=run_id, attempt=1, tagger=FileTagger.load(TAGS)
    )


def _same(a: object, b: object) -> bool:
    if a is None or b is None:
        return a is None and b is None
    if isinstance(a, str) or isinstance(b, str):
        return a == b
    return math.isclose(float(a), float(b), abs_tol=1e-9)  # type: ignore[arg-type]


def test_serving_features_equal_the_dataset_rows(scenario: Path) -> None:
    rows = pq.read_table(scenario / "rows.parquet").to_pylist()
    outcomes = [
        outcome_from_row(r) for r in pq.read_table(scenario / "outcomes.parquet").to_pylist()
    ]
    assert len(outcomes) >= len(rows)
    index = HistoryIndex(outcomes)
    for row in rows:
        feats = full_features(_ctx(row), index, default_branch=sc.DEFAULT_BRANCH)
        for name in FEATURE_NAMES:
            served = feats.get(name)
            if name not in CATEGORICAL_FEATURES and isinstance(served, bool):
                served = float(served)
            assert _same(served, row[name]), (row["head_sha"][:6], name, served, row[name])


def test_manifest_records_default_branch(scenario: Path) -> None:
    manifest = json.loads((scenario / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["repos"][sc.REPO]["default_branch"] == sc.DEFAULT_BRANCH
    assert "outcomes.parquet" in manifest["files"]


@pytest.fixture(scope="module")
def bundle(scenario: Path, tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    """A model trained on the synthetic dataset, served with the scenario's history."""
    data_dir = tmp_path_factory.mktemp("data")
    ds = write_dataset(data_dir / "datasets" / "build" / "v1", make_rows())
    shutil.copy(scenario / "outcomes.parquet", ds / "outcomes.parquet")
    manifest = json.loads((ds / "manifest.json").read_text(encoding="utf-8"))
    manifest["repos"] = json.loads((scenario / "manifest.json").read_text(encoding="utf-8"))[
        "repos"
    ]
    (ds / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    data = load_dataset(ds)
    result = run_training(data, FAST, loro=False, ablation=False)
    out = export_bundle(data_dir / "models", "m1", result, data, FAST)
    return out, data_dir


def test_predictor_answers_valid_outputs(scenario: Path, bundle: tuple[Path, Path]) -> None:
    out, data_dir = bundle
    p = BuildPredictor.load(out, data_dir)
    assert p.version == "build/m1"
    assert set(p.subs) == {"tsc", "lint", "bundler"}
    rows = pq.read_table(scenario / "rows.parquet").to_pylist()
    for row in rows:
        r = p.predict(_ctx(row))
        assert r.status == "ok" and r.model_version == "build/m1"
        assert r.alert == (r.probability is not None and r.probability >= r.threshold)
        assert set(r.sub_risks) == {"tsc", "lint", "bundler"}
        assert len(r.top_factors) <= 5
        assert all(f.direction == "raises" for f in r.top_factors if f.shap_value > 0)
    unknown = CommitContext.model_validate_json(
        (FIXTURES / "commit_context.json").read_text("utf-8")
    )
    assert p.predict(unknown).status == "degraded"


def test_alert_always_carries_a_reason() -> None:
    import numpy as np

    feats: dict[str, object] = {"a": None, "b": 3.0}
    factors = risk_factors(feats, ["a", "b"], np.array([0.9, -0.2, 0.0]), need_one=True)
    assert len(factors) == 1 and factors[0].feature == "b" and factors[0].direction == "lowers"
    assert risk_factors(feats, ["a", "b"], np.array([0.9, -0.2, 0.0]), need_one=False) == []


def test_reasons_read_well() -> None:
    assert reason("n_ts_files", 3) == "3 TypeScript files changed"
    assert "25%" in reason("repo_build_fail_rate_30d", 0.25)
    assert reason("branch_last_build_failed", True) == "the previous build on this branch failed"


def test_env_selects_model_or_stub(bundle: tuple[Path, Path]) -> None:
    out, data_dir = bundle
    assert load_from_env({}) is None
    assert load_from_env({"BUILD_MODEL_DIR": str(data_dir / "missing")}) is None
    loaded = load_from_env({"BUILD_MODEL_DIR": str(out), "DATA_DIR": str(data_dir)})
    assert loaded is not None and loaded.version == "build/m1"


def test_api_serves_the_loaded_model(
    bundle: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    from build_expert.api import app
    from fastapi.testclient import TestClient

    from shared.contracts import RiskOutput

    out, data_dir = bundle
    loaded = BuildPredictor.load(out, data_dir)
    monkeypatch.setattr("build_expert.predict.predictor", lambda: loaded)
    monkeypatch.setattr("build_expert.api.default_kb", lambda: None)
    client = TestClient(app)
    body = json.loads((FIXTURES / "commit_context.json").read_text("utf-8"))
    response = client.post("/predict", json=body)
    assert response.status_code == 200
    (output,) = [RiskOutput.model_validate(x) for x in response.json()]
    assert output.model_version == "build/m1" and output.status == "degraded"
    assert set(output.sub_risks) == {"tsc", "lint", "bundler"}
    assert client.get("/health").json()["model_version"] == "build/m1"
