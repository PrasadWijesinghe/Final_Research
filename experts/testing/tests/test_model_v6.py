"""P4.7 v6: the "still failing" rule, operating points, the ht_ decision, serving with a rule."""

import json
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import pandas as pd
import pytest
from testing_expert import serving
from testing_expert.features import feature_names
from testing_expert.model import rule
from testing_expert.model.compare_v6 import (
    decide_ht,
    operating_points,
    paired_bootstrap,
    prf,
    with_rule,
)
from testing_expert.model.metrics import fit_binary_calibrator
from testing_expert.model.models import SEPARATE_KIND, fit_model, write_features_json

from shared.contracts import CommitContext, RiskOutput

FIXTURES = Path(__file__).parent / "fixtures"


def frame(**cols: list[float]) -> pd.DataFrame:
    return pd.DataFrame(cols)


def test_rule_prefers_same_os_and_falls_back_to_job_history() -> None:
    df = frame(
        h_os_n_prior=[5, 5, 0, 0, 3],
        h_os_last_failed=[1, 0, 0, 0, 1],
        h_os_consecutive_failures=[4, 0, 0, 0, 2],
        h_last_failed=[0, 1, 1, 0, 0],
        h_consecutive_failures=[0, 6, 6, 0, 0],
    )
    count, same_os = rule.previous_failures(df)
    # row 0: same OS failed 4 in a row; row 1: same OS passed (job history ignored);
    # row 2: no run on this OS, job history says 6; row 3: all passed; row 4: same OS, 2
    assert list(count) == [4, 0, 6, 0, 2]
    assert list(same_os) == [1, 1, 0, 0, 1]


def test_rule_count_is_at_least_one_when_last_run_failed() -> None:
    df = frame(h_os_n_prior=[1], h_os_last_failed=[1], h_os_consecutive_failures=[0])
    assert list(rule.previous_failures(df)[0]) == [1]


def test_missing_columns_and_nan_mean_no_rule() -> None:
    assert list(rule.previous_failures(pd.DataFrame({"x": [1, 2]}))[0]) == [0, 0]
    df = frame(h_os_n_prior=[float("nan")], h_last_failed=[float("nan")])
    assert list(rule.previous_failures(df)[0]) == [0]


def test_buckets_cover_1_2_3_to_9_and_10_plus() -> None:
    idx = rule.bucket_index(np.array([0, 1, 2, 3, 9, 10, 99]))
    assert list(idx) == [-1, 0, 1, 2, 2, 3, 3]
    assert [rule.bucket_label(lo, hi) for lo, hi in rule.BUCKETS] == ["1", "2", "3-9", "10+"]


def _train(n_per: int = 100) -> pd.DataFrame:
    rows = []
    for count, fails in ((0, 2), (1, 10), (2, 20), (5, 30), (12, 40)):
        for i in range(n_per):
            rows.append(
                {
                    "h_os_n_prior": 3,
                    "h_os_last_failed": int(count > 0),
                    "h_os_consecutive_failures": count,
                    "y": (
                        1 if i < fails * n_per // 100 // 2 else 2 if i < fails * n_per // 100 else 0
                    ),
                }
            )
    return pd.DataFrame(rows)


def test_fit_rule_learns_empirical_rates_and_applies_them() -> None:
    r = rule.fit_rule(_train())
    by = {b["label"]: b for b in r["buckets"]}
    assert by["1"]["any"] == pytest.approx(0.10) and by["2"]["any"] == pytest.approx(0.20)
    assert by["3-9"]["any"] == pytest.approx(0.30) and by["10+"]["any"] == pytest.approx(0.40)
    assert by["10+"]["genuine"] + by["10+"]["flaky"] == pytest.approx(by["10+"]["any"])
    out = rule.apply_rule(
        r, frame(h_os_n_prior=[3, 3], h_os_last_failed=[0, 1], h_os_consecutive_failures=[0, 12])
    )
    assert list(out["active"]) == [False, True]
    assert list(out["any"]) == [0.0, pytest.approx(0.40)]


def test_small_bucket_gets_no_rule() -> None:
    df = _train(n_per=10)  # 10 rows per bucket, under MIN_BUCKET_ROWS
    r = rule.fit_rule(df)
    assert all(b["any"] == 0.0 for b in r["buckets"])
    out = rule.apply_rule(r, df)
    assert not out["active"].any()


def test_final_p_is_max_of_model_and_rule() -> None:
    assert list(rule.combine(np.array([0.02, 0.5]), np.array([0.1, 0.1]))) == [0.1, 0.5]
    s = {"any": np.array([0.02]), "gen": np.array([0.01]), "flaky": np.array([0.01])}
    r = {"any": np.array([0.1]), "gen": np.array([0.07]), "flaky": np.array([0.03])}
    out = with_rule(s, r)
    assert out["any"][0] == 0.1 and out["gen"][0] == 0.07 and out["flaky"][0] == 0.03


def test_operating_points_use_only_the_given_scores() -> None:
    rng = np.random.default_rng(0)
    y = (rng.random(4000) < 0.05).astype(int)
    s = np.clip(0.02 + y * 0.3 * rng.random(4000) + rng.random(4000) * 0.1, 0, 1)
    pts = operating_points(y, s)
    t = pts["max_f1"]["threshold"]
    f1 = prf(y, s, t)["f1"]
    # no other observed threshold has a clearly better F1 (support floor aside)
    best = max(prf(y, s, c)["f1"] for c in np.quantile(s, np.linspace(0.5, 0.995, 80)))
    assert f1 >= best - 1e-9
    assert prf(y, s, pts["top_5pct"]["threshold"])["alert_rate"] == pytest.approx(0.05, abs=0.01)
    if "precision_0.30" in pts:
        assert prf(y, s, pts["precision_0.30"]["threshold"])["precision"] >= 0.30


def test_prf_counts_and_subsets() -> None:
    y = np.array([1, 0, 2, 0, 0])
    s = np.array([0.9, 0.8, 0.2, 0.1, 0.0])
    m = prf(y, s, 0.5)
    assert (m["flagged"], m["fails"]) == (2, 2)
    assert m["precision"] == 0.5 and m["recall"] == 0.5 and m["f1"] == 0.5
    sub = prf(y, s, 0.5, np.array([True, True, False, False, False]))
    assert sub["n"] == 2 and sub["recall"] == 1.0


def test_decide_ht_needs_both_slices_positive() -> None:
    def res(cal: float, oof: float) -> dict[str, Any]:
        return {"ht_decision": {"cal_diff": cal, "oof_diff": oof}}

    assert decide_ht(res(0.01, 0.01))["include"] is True
    assert decide_ht(res(0.01, -0.001))["include"] is False
    assert decide_ht(res(-0.01, 0.02))["include"] is False


def test_paired_bootstrap_interval_contains_point() -> None:
    rng = np.random.default_rng(1)
    x = rng.random(500)
    out = paired_bootstrap(500, {"mean": lambda i: float(x[i].mean())})
    assert out["mean"]["lo"] <= out["mean"]["point"] <= out["mean"]["hi"]


# --- serving a v6 bundle ----------------------------------------------------------------------


def make_v6_bundle(root: Path, threshold: float, with_rule: bool = True) -> Path:
    rng = np.random.default_rng(0)
    cols = [
        c
        for c in feature_names()
        if c not in ("rs_runner_os", "rs_event") and not c.startswith("ht_")
    ]
    n = 400
    df = pd.DataFrame(rng.random((n, len(cols))), columns=cols)
    df["rs_runner_os"] = rng.choice(["Linux", "Windows"], n)
    cols.append("rs_runner_os")
    df["y"] = (df["tw_set_timeout"] + rng.random(n) * 0.3 > 0.7).astype(int) * rng.integers(1, 3, n)
    fitted = fit_model(SEPARATE_KIND, df, cols)
    folder = root / "models" / "testing" / "v6"
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
    (folder / "threshold.json").write_text(json.dumps({"threshold": threshold}))
    if with_rule:
        (folder / "rule.json").write_text(
            json.dumps(
                {
                    "buckets": [
                        {
                            "label": "1",
                            "min": 1,
                            "max": 1,
                            "n": 99,
                            "any": 0.08,
                            "genuine": 0.05,
                            "flaky": 0.03,
                        },
                        {
                            "label": "2",
                            "min": 2,
                            "max": 2,
                            "n": 99,
                            "any": 0.16,
                            "genuine": 0.1,
                            "flaky": 0.06,
                        },
                        {
                            "label": "3-9",
                            "min": 3,
                            "max": 9,
                            "n": 99,
                            "any": 0.62,
                            "genuine": 0.5,
                            "flaky": 0.12,
                        },
                        {
                            "label": "10+",
                            "min": 10,
                            "max": None,
                            "n": 99,
                            "any": 0.11,
                            "genuine": 0.1,
                            "flaky": 0.01,
                        },
                    ]
                }
            )
        )
    return folder


def context() -> CommitContext:
    data = json.loads((FIXTURES / "commit_context.json").read_text("utf-8"))
    data["changed_files"] = []
    return CommitContext.model_validate(data)


@pytest.fixture()
def v6(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    folder = make_v6_bundle(tmp_path, threshold=0.6)
    monkeypatch.setenv(serving.BUNDLE_ENV, str(folder))
    serving.get_bundle.cache_clear()
    return folder


def _history(monkeypatch: pytest.MonkeyPatch, **vals: float) -> None:
    real = serving.build_features

    def patched(ctx: CommitContext, jobs: list[Any], mode: str = "full") -> Any:
        rows, failed = real(ctx, jobs, mode)
        for r in rows:
            r.update(vals)
        return rows, failed

    monkeypatch.setattr(serving, "build_features", patched)


def test_v6_bundle_loads_rule_and_needs_no_test_history(v6: Path) -> None:
    b = serving.get_bundle()
    assert b is not None and b.rule is not None and b.version == "lightgbm-v6"
    assert b.history_mode == "none"  # no ht_ column: the slow test-history query is skipped


def test_still_failing_job_alerts_with_a_plain_reason(
    v6: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _history(monkeypatch, h_os_n_prior=40, h_os_last_failed=1, h_os_consecutive_failures=4)
    out = serving.serve(context())[0]  # type: ignore[index]
    RiskOutput.model_validate(out.model_dump(mode="json"))
    assert out.probability is not None and out.probability >= 0.62
    assert out.alert is True  # 0.62 >= threshold 0.6, and the model alone (random bundle) is lower
    first = out.top_factors[0]
    assert first.feature == "still_failing_streak" and first.value == 4
    assert first.reason == (
        "This job already failed 4 times in a row on Linux; "
        "the failure is likely not caused by this push."
    )
    assert first.direction == "raises" and len(out.top_factors) <= 5
    assert out.sub_risks["still_failing"] == pytest.approx(0.62)
    assert out.sub_risks["genuine_fail"] >= 0.5


def test_job_history_fallback_names_no_os(v6: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _history(monkeypatch, h_os_n_prior=0, h_last_failed=1, h_consecutive_failures=1)
    out = serving.serve(context())[0]  # type: ignore[index]
    assert out.top_factors[0].reason.startswith("This job already failed 1 time in a row;")
    assert out.probability is not None and out.probability >= 0.08


def test_previous_run_passed_means_no_rule(v6: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _history(monkeypatch, h_os_n_prior=40, h_os_last_failed=0, h_os_consecutive_failures=0)
    out = serving.serve(context())[0]  # type: ignore[index]
    assert "still_failing" not in out.sub_risks
    assert all(f.feature != "still_failing_streak" for f in out.top_factors)


def test_bundle_without_rule_json_behaves_like_v4(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = make_v6_bundle(tmp_path, threshold=0.0, with_rule=False)
    monkeypatch.setenv(serving.BUNDLE_ENV, str(folder))
    serving.get_bundle.cache_clear()
    _history(monkeypatch, h_os_n_prior=40, h_os_last_failed=1, h_os_consecutive_failures=4)
    out = serving.serve(context())[0]  # type: ignore[index]
    assert set(out.sub_risks) == {"genuine_fail", "flaky_fail"}


def test_reason_text_fits_the_contract() -> None:
    for n in (1, 2, 70):
        for os_name in ("linux", "windows", "macos", "unknown", None):
            assert len(serving.still_failing_reason(n, os_name)) <= 200


def test_history_mode_follows_the_ht_columns() -> None:
    from testing_expert.model.data import Encoder

    def bundle(cols: list[str]) -> serving.Bundle:
        return serving.Bundle(
            version="x",
            threshold=0.5,
            encoder=Encoder(numeric=cols),
            booster=None,  # type: ignore[arg-type]
            calibrator={},
            path=Path("."),
        )

    assert bundle(["h_runs_7d"]).history_mode == "none"
    assert bundle(["h_runs_7d", "ht_failed_tests_30d"]).history_mode == "job"
    assert bundle(["ht_failed_tests_30d", "ht_touched_fail_rate"]).history_mode == "full"
