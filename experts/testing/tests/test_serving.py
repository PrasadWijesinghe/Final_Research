"""Serving the trained model: bundle loading, stub fallback, request-time features, reasons."""

import json
from pathlib import Path
from typing import Any

import joblib
import lightgbm as lgb
import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient
from testing_expert import serving
from testing_expert.api import app
from testing_expert.features import feature_names
from testing_expert.model.data import CLASSES, Encoder
from testing_expert.model.metrics import fit_calibrator

from shared.contracts import CommitContext, RiskOutput

FIXTURES = Path(__file__).parent / "fixtures"

PATCH = (
    "@@ -1,3 +1,12 @@\n"
    " import { fetchUser } from './user';\n"
    "+describe('user', () => {\n"
    "+  it('loads', async () => {\n"
    "+    setTimeout(() => {}, 100);\n"
    "+    const n = Math.random();\n"
    "+    fetch('https://example.com/y');\n"
    "+    const r = await fetch('https://example.com/x');\n"
    "+    expect(r).toBeTruthy();\n"
    "+  });\n"
    "+});\n"
)


def make_bundle(folder: Path, threshold: float = 0.0) -> Path:
    """Train a tiny LightGBM on random rows with the real feature names and write a bundle."""
    rng = np.random.default_rng(0)
    cols = [c for c in feature_names() if c != "rs_runner_os" and c != "rs_event"]
    n = 300
    df = pd.DataFrame(rng.random((n, len(cols))), columns=cols)
    df["rs_runner_os"] = rng.choice(["Linux", "Windows"], n)
    cols.append("rs_runner_os")
    y = (df["tw_set_timeout"] + rng.random(n) * 0.3 > 0.7).astype(int) * rng.integers(1, 3, n)
    df["y"] = y
    enc = Encoder.fit(df, cols)
    est = lgb.LGBMClassifier(
        objective="multiclass", n_estimators=20, min_child_samples=5, verbose=-1, random_state=1
    )
    est.fit(enc.lgbm_frame(df), y.to_numpy())
    folder.mkdir(parents=True)
    est.booster_.save_model(str(folder / "model.txt"))
    proba = est.predict_proba(enc.lgbm_frame(df))
    joblib.dump(fit_calibrator(y.to_numpy(), np.asarray(proba)), folder / "calibrator.joblib")
    (folder / "features.json").write_text(
        json.dumps({"classes": list(CLASSES), "kind": "lightgbm", **enc.to_json()})
    )
    (folder / "threshold.json").write_text(json.dumps({"threshold": threshold}))
    return folder


def context(patch: str | None = PATCH, jobs: bool = True) -> CommitContext:
    data: dict[str, Any] = json.loads((FIXTURES / "commit_context.json").read_text("utf-8"))
    data["changed_files"] = [
        {
            "path": "test/user.test.ts",
            "status": "modified",
            "additions": 9,
            "deletions": 0,
            "patch": patch,
            "tags": ["test"],
        }
    ]
    if not jobs:
        data["workflow_jobs"] = []
    return CommitContext.model_validate(data)


@pytest.fixture()
def bundle_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = make_bundle(tmp_path / "models" / "testing" / "v1")
    monkeypatch.setenv(serving.BUNDLE_ENV, str(path))
    serving.get_bundle.cache_clear()
    return path


def test_bundle_loads_and_returns_valid_risk_output(bundle_dir: Path) -> None:
    outputs = serving.serve(context())
    assert outputs is not None and len(outputs) == 1
    out = RiskOutput.model_validate(outputs[0].model_dump(mode="json"))
    assert out.model_version == "lightgbm-v1"
    assert out.scope == "job" and out.job_key == "test"
    assert out.probability is not None and 0 <= out.probability <= 1
    assert out.threshold == 0.0 and out.alert is True  # threshold 0 in the tiny bundle
    assert set(out.sub_risks) == {"genuine_fail", "flaky_fail"}


def test_factors_present_with_plain_text(bundle_dir: Path) -> None:
    out = serving.serve(context())[0]  # type: ignore[index]
    assert 1 <= len(out.top_factors) <= 5
    for f in out.top_factors:
        assert f.shap_value != 0 and f.reason
        assert (f.direction == "raises") == (f.shap_value > 0)


def test_tw_features_from_context_diff(bundle_dir: Path) -> None:
    ctx = context()
    rows, db_failed = serving.build_features(ctx, [ctx.workflow_jobs[0]])
    row: Any = rows[0]
    assert not db_failed
    assert row["tw_set_timeout"] == 1
    assert row["tw_math_random"] == 1
    assert row["tw_unmocked_network"] >= 1
    assert row["tw_unawaited_promises"] >= 1  # bare fetch() is not awaited
    assert row["cs_test_files"] == 1
    assert row["rs_runner_os"] == "Linux"
    assert row["h_n_prior"] in (0, None)  # no database: cold start


def test_evidence_points_at_added_line() -> None:
    path, line = serving._evidence("tw_set_timeout", context())
    assert path == "test/user.test.ts" and line == 4
    path, line = serving._evidence("tw_math_random", context())
    assert line == 5


def test_no_test_job_scores_one_commit_row(bundle_dir: Path) -> None:
    outputs = serving.serve(context(jobs=False))
    assert outputs is not None and len(outputs) == 1
    assert outputs[0].scope == "commit"


def test_missing_bundle_falls_back_to_stub(caplog: pytest.LogCaptureFixture) -> None:
    assert serving.get_bundle() is None
    client = TestClient(app)
    body = json.loads((FIXTURES / "commit_context.json").read_text("utf-8"))
    out = RiskOutput.model_validate(client.post("/predict", json=body).json()[0])
    assert out.model_version == "stub"
    assert client.get("/health").json()["model_version"] == "stub"


def test_broken_bundle_falls_back_to_stub(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    path = make_bundle(tmp_path / "v1")
    (path / "model.txt").write_text("not a model")
    monkeypatch.setenv(serving.BUNDLE_ENV, str(path))
    serving.get_bundle.cache_clear()
    assert serving.get_bundle() is None
    assert "failed to load" in caplog.text


def test_api_serves_model_and_health_shows_version(bundle_dir: Path) -> None:
    client = TestClient(app)
    body = context().model_dump(mode="json")
    out = RiskOutput.model_validate(client.post("/predict", json=body).json()[0])
    assert out.model_version == "lightgbm-v1"
    assert client.get("/health").json()["model_version"] == "lightgbm-v1"


def test_newest_version_folder_is_picked(tmp_path: Path) -> None:
    make_bundle(tmp_path / "v1")
    make_bundle(tmp_path / "v2")
    make_bundle(tmp_path / "v10")
    assert serving.resolve_bundle_path(str(tmp_path)) == tmp_path / "v10"
