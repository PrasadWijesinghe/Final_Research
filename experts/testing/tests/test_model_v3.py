"""P4.7 v3: calibrators never give hard 0.0, monotone constraints hold, two-stage bundle serves."""

import json
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
import pytest
from testing_expert import serving
from testing_expert.features import feature_names
from testing_expert.model.compare_v3 import rank_pick
from testing_expert.model.data import CLASSES, apply_variant
from testing_expert.model.metrics import (
    CALIBRATORS,
    MIN_PROB,
    apply_calibrator,
    ece,
    fit_calibrator,
    p_fail,
    reliability,
)
from testing_expert.model.models import (
    MONOTONE_FEATURES,
    TwoStageBooster,
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


def _raw(n: int = 3000, seed: int = 0) -> tuple[np.ndarray, np.ndarray]:
    """Raw class probabilities where low scores contain no failure at all (isotonic -> 0.0)."""
    rng = np.random.default_rng(seed)
    risk = rng.random(n)
    y = np.where(rng.random(n) < risk**4 * 0.3, rng.integers(1, 3, n), 0)
    p_fail_raw = np.clip(risk * 0.9 + 0.02, 0.001, 0.98)
    split = np.full(n, 0.5)
    proba = np.column_stack([1 - p_fail_raw, p_fail_raw * split, p_fail_raw * (1 - split)])
    return y, proba


@pytest.mark.parametrize("kind", CALIBRATORS)
def test_calibrators_sum_to_one_and_stay_in_range(kind: str) -> None:
    y, proba = _raw()
    out = apply_calibrator(fit_calibrator(y, proba, kind), proba)
    assert np.allclose(out.sum(axis=1), 1)
    assert ((out >= 0) & (out <= 1)).all()


@pytest.mark.parametrize("kind", ["sigmoid", "beta", "isotonic_floor"])
def test_new_calibrators_never_output_zero(kind: str) -> None:
    y, proba = _raw()
    cal = fit_calibrator(y, proba, kind)
    low = np.array([[1 - 1e-9, 5e-10, 5e-10], [1.0, 0.0, 0.0], *proba[:50].tolist()])
    out = apply_calibrator(cal, low)
    assert (p_fail(out) > 0).all()
    assert (out[:, 1:] >= MIN_PROB / 2).all()  # floor before the row renormalisation


def test_isotonic_collapse_is_what_the_new_calibrators_fix() -> None:
    y, proba = _raw()
    iso = p_fail(apply_calibrator(fit_calibrator(y, proba, "isotonic"), proba))
    assert (iso == 0).any()  # the v2 symptom: hard 0.0 for low-risk rows
    for kind in ("sigmoid", "beta"):
        s = p_fail(apply_calibrator(fit_calibrator(y, proba, kind), proba))
        assert (s > 0).all()


def test_old_v2_calibrator_dict_still_applies() -> None:
    y, proba = _raw()
    new = fit_calibrator(y, proba, "isotonic")
    old = {"isotonic": new["models"], "classes": list(CLASSES)}  # the v1 / v2 bundle layout
    assert np.allclose(apply_calibrator(old, proba), apply_calibrator(new, proba))


def test_ece_reliability_and_rank_pick() -> None:
    y = np.array([0] * 90 + [1] * 10)
    perfect = np.array([0.0] * 90 + [1.0] * 10)
    assert ece(y, perfect, bins=2) == pytest.approx(0.0)
    spread = np.array(([0] * 9 + [1]) * 10)
    assert ece(spread, np.full(100, 0.1), bins=5) == pytest.approx(0.0)
    assert ece(spread, np.full(100, 0.5), bins=5) == pytest.approx(0.4)
    rel = reliability(y, np.linspace(0, 1, 100), bins=4)
    assert sum(r["n"] for r in rel) == 100
    rows = {
        "isotonic": {"brier_fail": 0.02, "ece": 0.01},
        "sigmoid": {"brier_fail": 0.01, "ece": 0.005},
        "beta": {"brier_fail": 0.011, "ece": 0.02},
    }
    assert rank_pick(rows) == "sigmoid"


def test_monotone_names_exist_and_have_a_defensible_sign() -> None:
    names = set(feature_names())
    assert set(MONOTONE_FEATURES) <= names
    assert set(MONOTONE_FEATURES.values()) == {1, -1}
    assert MONOTONE_FEATURES["h_days_since_last_failure"] == -1
    assert MONOTONE_FEATURES["h_consecutive_failures"] == 1
    assert all(v == 1 for k, v in MONOTONE_FEATURES.items() if k.startswith(("tw_", "h_os_fail")))
    vec = monotone_vector(["h_consecutive_failures", "cs_lines_added", "tw_math_random"])
    assert vec == [1, 0, 1]


def _train_frame(n: int = 4000, seed: int = 3) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    df = pd.DataFrame(
        {
            "h_consecutive_failures": rng.integers(0, 20, n).astype(float),
            "h_fail_rate_30d": rng.random(n),
            "h_days_since_last_failure": rng.random(n) * 30,
            "tw_math_random": rng.integers(0, 4, n).astype(float),
            "cs_lines_added": rng.integers(0, 500, n).astype(float),
        }
    )
    noise = rng.random(n)
    # a deliberately NON-monotone truth in h_consecutive_failures: risk dips for streaks of 8-12
    bump = np.where(
        (df["h_consecutive_failures"] > 7) & (df["h_consecutive_failures"] < 13), -0.3, 0
    )
    score = 0.05 + 0.04 * df["h_consecutive_failures"] / 20 + 0.2 * df["h_fail_rate_30d"] + bump
    fail = noise < np.clip(score, 0.01, 0.9)
    df["y"] = np.where(fail, rng.integers(1, 3, n), 0)
    return df


def test_monotone_model_never_lowers_risk_when_a_bad_feature_rises() -> None:
    df = _train_frame()
    cols = [c for c in df.columns if c != "y"]
    free = fit_model("lightgbm_2s", df, cols)
    mono = fit_model("lightgbm_mono", df, cols)
    base = df.iloc[[0]].copy()
    sweeps = {
        "h_consecutive_failures": (np.arange(0, 20, dtype=float), 1),
        "h_fail_rate_30d": (np.linspace(0, 1, 30), 1),
        "h_days_since_last_failure": (np.linspace(0, 30, 30), -1),
        "tw_math_random": (np.arange(0, 4, dtype=float), 1),
    }
    free_broken = False
    for feat, (grid, sign) in sweeps.items():
        rows = pd.concat([base] * len(grid), ignore_index=True)
        rows[feat] = grid
        p_m = p_fail(mono.predict_proba(rows))
        p_f = p_fail(free.predict_proba(rows))
        assert (np.diff(p_m) * sign >= -1e-9).all(), feat
        free_broken |= bool((np.diff(p_f) * sign < -1e-4).any())
    assert free_broken  # the test data really has a dip, so the constraint is doing something
    assert mono.predict_proba(df).shape == (len(df), 3)
    assert np.allclose(mono.predict_proba(df).sum(axis=1), 1)


def test_two_stage_bundle_serves(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rng = np.random.default_rng(0)
    cols = [c for c in feature_names() if c not in ("rs_runner_os", "rs_event")]
    n = 300
    df = pd.DataFrame(rng.random((n, len(cols))), columns=cols)
    df["rs_runner_os"] = rng.choice(["Linux", "Windows"], n)
    cols.append("rs_runner_os")
    df["y"] = (df["tw_set_timeout"] + rng.random(n) * 0.3 > 0.7).astype(int) * rng.integers(1, 3, n)
    fitted = fit_model("lightgbm_mono", df, cols)
    folder = tmp_path / "models" / "testing" / "v3"
    fitted.save(folder)
    joblib.dump(fit_calibrator(df["y"].to_numpy(), fitted.predict_proba(df), "sigmoid"),
                folder / "calibrator.joblib")  # fmt: skip
    write_features_json(folder, fitted, {"kind": "lightgbm"})
    (folder / "threshold.json").write_text(json.dumps({"threshold": 0.0}))
    assert isinstance(TwoStageBooster.load(folder), TwoStageBooster)
    monkeypatch.setenv(serving.BUNDLE_ENV, str(folder))
    serving.get_bundle.cache_clear()
    try:
        outputs = serving.serve(context())
        assert outputs is not None and outputs[0].model_version == "lightgbm-v3"
        out = outputs[0]
        assert out.probability is not None and 0 < out.probability < 1  # never a hard 0.0
        assert 1 <= len(out.top_factors) <= 5
        assert set(out.sub_risks) == {"genuine_fail", "flaky_fail"}
    finally:
        serving.get_bundle.cache_clear()


def test_apply_variant_keeps_low_confidence_streak_rows_out_of_high() -> None:
    df = pd.DataFrame(
        {
            "label": ["genuine_fail", "genuine_fail", "pass"],
            "label_confidence": ["high", "low", "high"],
            "genuine_strict": [True, False, False],
        }
    )
    assert len(apply_variant(df, "high")) == 2
