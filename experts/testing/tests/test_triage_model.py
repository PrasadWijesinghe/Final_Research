"""P4.9 classifier: verdict rule, floors, metrics, bundle round trip, baselines."""

from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from testing_expert.triage import model as tm
from testing_expert.triage import train as tt
from testing_expert.triage.features import FEATURE_NAMES


def test_decide_uses_both_floors() -> None:
    assert tm.decide(0.95, 0.9, 0.6) == ("flaky", 0.95)
    assert tm.decide(0.05, 0.9, 0.6) == ("genuine", pytest.approx(0.95))
    verdict, conf = tm.decide(0.5, 0.9, 0.6)
    assert verdict == "unclear" and conf == 0.5
    assert tm.decide(0.89, 0.9, 0.6)[0] == "unclear"  # just under the flaky floor


def toy_scores(n: int = 2000, seed: int = 1) -> tuple[np.ndarray, np.ndarray]:
    rng = np.random.default_rng(seed)
    y = (rng.random(n) < 0.3).astype(int)
    p = np.clip(0.3 + 0.4 * (y - 0.3) + rng.normal(0, 0.2, n), 0.01, 0.99)
    return y, p


def test_floors_respect_the_false_flaky_limit_and_the_minimum() -> None:
    y, p = toy_scores()
    ff, gf = tm.choose_floors(y, p, max_false_flaky=0.05)
    assert ff >= tm.MIN_FLOOR and gf >= tm.MIN_FLOOR
    ffr = ((p >= ff) & (y == 0)).sum() / (y == 0).sum()
    assert ffr <= 0.05
    looser, _ = tm.choose_floors(y, p, max_false_flaky=0.20)
    assert looser <= ff  # a looser limit never raises the floor


def test_no_flaky_verdict_when_the_limit_cannot_be_met() -> None:
    y = np.array([0] * 50 + [1] * 50)
    p = np.full(100, 0.7)  # no separation at all
    ff, _ = tm.choose_floors(y, p, max_false_flaky=0.05)
    assert ff > 1.0
    assert all(v != "flaky" for v, _ in (tm.decide(float(x), ff, 0.5) for x in p))


def test_evaluate_reports_the_headline_numbers() -> None:
    y, p = toy_scores()
    ff, gf = tm.choose_floors(y, p)
    m = tm.evaluate(y, p, ff, gf)
    for key in ("coverage", "accuracy_decided", "balanced_accuracy", "macro_f1", "flaky_f1",
                "pr_auc_flaky", "false_flaky_rate"):  # fmt: skip
        assert key in m and 0.0 <= m[key] <= 1.0
    assert m["false_flaky_rate"] <= 0.05 + 1e-9
    assert m["roc_auc"] > 0.7


def test_verdict_metrics_counts_unclear_as_a_miss_for_recall() -> None:
    y = np.array([1, 1, 0, 0])
    v = np.array(["flaky", "unclear", "genuine", "flaky"], dtype=object)
    m = tm.verdict_metrics(y, v)
    assert m["coverage"] == 0.75
    assert m["flaky_recall"] == 0.5 and m["flaky_precision"] == 0.5
    assert m["false_flaky_rate"] == 0.5 and m["accuracy_all"] == 0.5


def table(n: int = 400, seed: int = 2) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    df = pd.DataFrame(rng.random((n, len(FEATURE_NAMES))), columns=list(FEATURE_NAMES))
    df.loc[rng.random(n) < 0.2, "ss_family_fail"] = np.nan  # unknown values are allowed
    df["y"] = (df["ss_same_job_pass"] + rng.normal(0, 0.2, n) > 0.7).astype(int)
    return df


@pytest.mark.parametrize("kind", tm.KINDS)
def test_fit_predict_and_bundle_round_trip(kind: tm.Kind, tmp_path: Path) -> None:
    df = table()
    m = tm.fit_model(kind, df, df["y"].to_numpy(), list(FEATURE_NAMES))
    m.flaky_floor, m.genuine_floor = 0.8, 0.6
    p = m.predict_flaky(df)
    assert p.shape == (len(df),) and ((p >= 0) & (p <= 1)).all()
    assert np.corrcoef(p, df["y"])[0, 1] > 0.5
    tm.save_bundle(m, tmp_path / "v1", {"note": "t"}, "card")
    again = tm.load_bundle(tmp_path / "v1")
    assert again.kind == kind and again.flaky_floor == 0.8 and again.genuine_floor == 0.6
    assert np.allclose(again.predict_flaky(df), p)


def test_split_is_temporal_and_baselines_run() -> None:
    df = table(300)
    df["failed_at"] = pd.date_range("2026-01-01", periods=len(df), freq="h", tz="UTC")
    df["ov_test_file_changed"] = 0.0
    df["ov_stack_files_changed"] = 0.0
    df["ov_frame_in_hunk"] = 0.0
    df["ov_files_known"] = 1.0
    df["er_leans_flaky"] = 0.0
    df["er_leans_genuine"] = 0.0
    dev, test = tt.split(df)
    assert dev["failed_at"].max() < test["failed_at"].min()
    assert len(dev) == int(len(df) * tt.DEV_FRAC)
    res = tt.baselines(df)
    assert set(res) == set(tt.BASELINES)
    assert res["always genuine"]["false_flaky_rate"] == 0.0
    assert res["always genuine"]["flaky_recall"] == 0.0
