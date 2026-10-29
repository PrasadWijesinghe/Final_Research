"""P4.8: contributions of the two v4 boosters become ranked, plain-language RiskFactors."""

import math

import lightgbm as lgb
import numpy as np
import pandas as pd
import pytest
from testing_expert import explain
from testing_expert.features._types import ChangedTestFile
from testing_expert.model.data import Encoder
from testing_expert.model.models import SeparateBooster

from shared.contracts.risk_output import RiskFactor

COLS = [
    "h_weighted_flip_rate",
    "h_consecutive_failures",
    "tw_math_random",
    "tw_set_timeout",
    "cs_lines_added",
    "stl_test_to_source_ratio",
]


def _frame(n: int = 1500, seed: int = 3) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    df = pd.DataFrame(rng.random((n, len(COLS))), columns=COLS)
    df["tw_math_random"] = rng.integers(0, 3, n).astype(float)
    df["h_consecutive_failures"] = rng.integers(0, 6, n).astype(float)
    return df


@pytest.fixture()
def booster_and_frame() -> tuple[SeparateBooster, Encoder, pd.DataFrame]:
    df = _frame()
    # genuine failures follow the failure streak, flaky ones follow the flip rate and Math.random
    yg = (df["h_consecutive_failures"] > 3).astype(int)
    yf = ((df["h_weighted_flip_rate"] > 0.7) | (df["tw_math_random"] > 1)).astype(int)
    enc = Encoder.fit(df, COLS)
    frame = enc.lgbm_frame(df)
    boosters = [
        lgb.train(
            {"objective": "binary", "verbose": -1, "min_data_in_leaf": 5, "seed": 1},
            lgb.Dataset(frame, label=y),
            num_boost_round=20,
        )
        for y in (yg, yf)
    ]
    return SeparateBooster(boosters[0], boosters[1]), enc, frame


def test_factors_are_ranked_capped_and_signed(
    booster_and_frame: tuple[SeparateBooster, Encoder, pd.DataFrame],
) -> None:
    booster, enc, frame = booster_and_frame
    row = frame.iloc[0].to_dict()
    factors = explain.top_factors(booster, enc, frame, 0, row, limit=3, pg=0.2, pf=0.2)
    assert 1 <= len(factors) <= 3
    scores = [abs(f.shap_value) for f in factors]
    assert scores == sorted(scores, reverse=True)
    for f in factors:
        assert isinstance(f, RiskFactor)
        assert (f.direction == "raises") == (f.shap_value >= 0)
        assert 0 < len(f.reason) <= 200


def test_each_booster_is_weighted_by_its_share_of_the_combined_risk(
    booster_and_frame: tuple[SeparateBooster, Encoder, pd.DataFrame],
) -> None:
    booster, enc, frame = booster_and_frame
    n = len(COLS)
    flat = np.asarray(booster.predict(frame.iloc[[0]], pred_contrib=True))[0]
    raw = flat.reshape(3, n + 1)[:, :n]
    only_genuine = explain.combine_contributions(flat, n, True, pg=0.3, pf=0.0)
    only_flaky = explain.combine_contributions(flat, n, True, pg=0.0, pf=0.3)
    even = explain.combine_contributions(flat, n, True, pg=0.1, pf=0.1)
    assert np.allclose(only_genuine, raw[1])
    assert np.allclose(only_flaky, raw[2])
    assert np.allclose(even, 0.5 * raw[1] + 0.5 * raw[2])
    # both zero: no division by zero, equal shares
    assert np.allclose(explain.combine_contributions(flat, n, True, 0.0, 0.0), even)
    # a flaky-dominated job gets the flaky booster's top feature first
    top = explain.top_factors(booster, enc, frame, 0, frame.iloc[0].to_dict(), pg=0.0, pf=0.5)
    flaky_rank = np.argsort(-np.abs(raw[2]))[0]
    assert top[0].feature == COLS[flaky_rank]


def test_non_separate_booster_uses_fail_contributions() -> None:
    n = 4
    flat = np.arange(3 * (n + 1), dtype=float)  # blocks: [0..4], [5..9], [10..14]
    out = explain.combine_contributions(flat, n, separate=False)
    assert np.allclose(out, flat[5:9] + flat[10:14])


def test_missing_value_uses_missing_wording() -> None:
    f = explain.make_factor("h_fail_rate_7d", math.nan, 0.4)
    assert f.value == "unknown"
    assert f.reason == "This job has no runs in the last 7 days"
    g = explain.make_factor("h_fail_rate_7d", None, -0.4)
    assert g.direction == "lowers" and g.reason == f.reason


def test_templates_fill_values_and_whole_floats_read_as_ints() -> None:
    assert explain.reason_text("tc_test_config_changed", 1.0) == (
        "The test runner configuration changed (flag 1: 1 = yes)"
    )
    assert explain.reason_text("h_fail_rate_7d", np.float64(0.25)).endswith(
        "25% of its runs in the last 7 days"
    )
    f = explain.make_factor("cs_lines_added", np.int64(12), 0.2)
    assert f.value == 12 and type(f.value) is int


def test_long_reasons_are_cut_to_the_contract_limit() -> None:
    assert len(explain.reason_text("not_a_feature_" + "x" * 400, 1)) <= 200


def test_tw_factor_points_at_the_added_line() -> None:
    patch = "@@ -1,2 +1,4 @@\n import x\n+describe('a', () => {\n+  setTimeout(f, 1);\n+});\n"
    files = [ChangedTestFile("test/a.test.ts", patch)]
    f = explain.make_factor("tw_set_timeout", 1, 0.3, files)
    assert (f.path, f.line) == ("test/a.test.ts", 3)
    unknown = explain.make_factor("tw_set_timeout", None, 0.3, files)
    assert unknown.path is None  # an unknown value points nowhere
    assert explain.make_factor("cs_lines_added", 5, 0.3, files).path is None


def test_zero_limit_and_noise_floor() -> None:
    assert explain.top_factors(object(), Encoder(), pd.DataFrame(), 0, {}, limit=0) == []
