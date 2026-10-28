"""P4.7 v4: binary calibrators, recency weights, separate genuine/flaky models, v4 bundle serves."""

import json
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
import pytest
from testing_expert import serving
from testing_expert.features import feature_names
from testing_expert.model.compare_v4 import block_metrics, evaluate_event
from testing_expert.model.metrics import fit_binary_calibrator
from testing_expert.model.models import (
    FLAKY_MONOTONE,
    GENUINE_MONOTONE,
    MONOTONE_FEATURES,
    SEPARATE_KIND,
    SeparateBooster,
    fit_model,
    monotone_vector,
    write_features_json,
)

from shared.contracts import CommitContext

FIXTURES = Path(__file__).parent / "fixtures"
PATCH = (
    "@@ -1,3 +1,8 @@\n import x from 'x';\n+describe('u', () => {\n+  it('a', async () => {\n"
    "+    setTimeout(() => {}, 100);\n+    const n = Math.random();\n+  });\n+});\n"
)


def context() -> CommitContext:
    data = json.loads((FIXTURES / "commit_context.json").read_text("utf-8"))
    data["changed_files"] = [
        {
            "path": "test/u.test.ts",
            "status": "modified",
            "additions": 6,
            "deletions": 0,
            "patch": PATCH,
            "tags": ["test"],
        }  # fmt: skip
    ]
    return CommitContext.model_validate(data)


def test_separate_constraints_are_valid_and_differ_by_cause() -> None:
    names = set(feature_names())
    for table in (GENUINE_MONOTONE, FLAKY_MONOTONE):
        assert set(table) <= names
        assert set(table.values()) <= {1, -1}
        assert table["h_days_since_last_failure"] == -1
    # flip rate / re-runs point to flaky, a stuck failure streak to genuine
    assert (
        "h_weighted_flip_rate" in FLAKY_MONOTONE and "h_weighted_flip_rate" not in GENUINE_MONOTONE
    )
    assert "h_reruns_30d" in FLAKY_MONOTONE and "h_reruns_30d" not in GENUINE_MONOTONE
    assert (
        "h_consecutive_failures" in GENUINE_MONOTONE
        and "h_consecutive_failures" not in FLAKY_MONOTONE
    )
    assert "tw_only_added" in GENUINE_MONOTONE and "tw_only_added" not in FLAKY_MONOTONE
    assert set(GENUINE_MONOTONE) | set(FLAKY_MONOTONE) == set(MONOTONE_FEATURES)
    assert monotone_vector(["h_weighted_flip_rate", "cs_lines_added"], GENUINE_MONOTONE) == [0, 0]
    assert monotone_vector(["h_weighted_flip_rate", "cs_lines_added"], FLAKY_MONOTONE) == [1, 0]


def _frame(n: int = 4000, seed: int = 5) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    df = pd.DataFrame(
        {
            "h_consecutive_failures": rng.integers(0, 10, n).astype(float),
            "h_weighted_flip_rate": rng.random(n),
            "h_days_since_last_failure": rng.random(n) * 30,
            "tw_math_random": rng.integers(0, 4, n).astype(float),
            "cs_lines_added": rng.integers(0, 500, n).astype(float),
        }
    )
    u = rng.random(n)
    g = u < 0.02 + 0.03 * df["h_consecutive_failures"] / 10
    f = (~g) & (rng.random(n) < 0.02 + 0.15 * df["h_weighted_flip_rate"])
    df["y"] = np.where(g, 1, np.where(f, 2, 0))
    return df


def test_separate_models_each_follow_their_own_signal() -> None:
    df = _frame()
    cols = [c for c in df.columns if c != "y"]
    m = fit_model(SEPARATE_KIND, df, cols)
    assert isinstance(m.est, SeparateBooster)
    base = df.iloc[[0]].copy()
    streak = pd.concat([base] * 10, ignore_index=True)
    streak["h_consecutive_failures"] = np.arange(10, dtype=float)
    pg, pf = m.est.predict_each(m.encoder.lgbm_frame(streak))
    assert (np.diff(pg) >= -1e-9).all()  # genuine: constrained in the streak
    assert pg[-1] > pg[0]
    flip = pd.concat([base] * 20, ignore_index=True)
    flip["h_weighted_flip_rate"] = np.linspace(0, 1, 20)
    _, pf = m.est.predict_each(m.encoder.lgbm_frame(flip))
    assert (np.diff(pf) >= -1e-9).all()  # flaky: constrained in the flip rate
    proba = m.predict_proba(df)
    assert proba.shape == (len(df), 3) and (proba >= 0).all()


def test_evaluate_event_and_block_metrics() -> None:
    rng = np.random.default_rng(0)
    y = (rng.random(2000) < 0.05).astype(int)
    s = np.clip(y * 0.4 + rng.random(2000) * 0.2, 0.001, 0.99)
    streak = rng.random(2000) < 0.2
    last = rng.random(2000) < 0.1
    r = evaluate_event(y, s, y, s, streak, last)
    assert r["pr_auc"] > 0.9 and 0 <= r["precision"] <= 1 and 0 <= r["recall"] <= 1
    assert r["streak_obs"] == pytest.approx(y[streak].mean())
    b = block_metrics(y, s, streak)
    assert b["brier"] == pytest.approx(((s - y) ** 2).mean())


def test_v4_bundle_serves_with_its_own_calibrators(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rng = np.random.default_rng(0)
    cols = [c for c in feature_names() if c not in ("rs_runner_os", "rs_event")]
    n = 400
    df = pd.DataFrame(rng.random((n, len(cols))), columns=cols)
    df["rs_runner_os"] = rng.choice(["Linux", "Windows"], n)
    cols.append("rs_runner_os")
    df["y"] = (df["tw_set_timeout"] + rng.random(n) * 0.3 > 0.7).astype(int) * rng.integers(1, 3, n)
    fitted = fit_model(SEPARATE_KIND, df, cols)
    folder = tmp_path / "models" / "testing" / "v4"
    fitted.save(folder)
    raw = fitted.predict_proba(df)
    joblib.dump(
        {
            "kind": "separate",
            "gen": fit_binary_calibrator((df["y"].to_numpy() == 1).astype(int), raw[:, 1]),
            "flaky": fit_binary_calibrator((df["y"].to_numpy() == 2).astype(int), raw[:, 2]),
        },
        folder / "calibrator.joblib",
    )
    write_features_json(folder, fitted, {"kind": SEPARATE_KIND})
    (folder / "threshold.json").write_text(json.dumps({"threshold": 0.0}))
    monkeypatch.setenv(serving.BUNDLE_ENV, str(folder))
    serving.get_bundle.cache_clear()
    try:
        outputs = serving.serve(context())
        assert outputs is not None
        out = outputs[0]
        assert out.model_version == "lightgbm-v4"
        assert out.probability is not None and 0 < out.probability < 1
        pg, pf = out.sub_risks["genuine_fail"], out.sub_risks["flaky_fail"]
        assert out.probability == pytest.approx(1 - (1 - pg) * (1 - pf))
        assert 1 <= len(out.top_factors) <= 5
        assert serving.resolve_bundle_path(str(tmp_path / "models" / "testing")) == folder
    finally:
        serving.get_bundle.cache_clear()
