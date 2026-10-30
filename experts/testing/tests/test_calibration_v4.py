"""P4.7 v4 calibration: recency weights, binary beta calibrators, any-fail combination."""

import numpy as np
from testing_expert.model.metrics import (
    MIN_PROB,
    apply_binary_calibrator,
    combine_any,
    fit_binary_calibrator,
    recency_weights,
)


def test_recency_weights_halve_per_half_life() -> None:
    w = recency_weights(np.array([0.0, 30.0, 60.0, -5.0]), 30)
    assert w is not None and np.allclose(w, [1.0, 0.5, 0.25, 1.0])
    assert recency_weights(np.array([1.0]), None) is None


def test_binary_calibrator_fixes_overconfident_scores_and_never_hits_zero() -> None:
    rng = np.random.default_rng(0)
    p = rng.random(5000)
    y = (rng.random(5000) < p * 0.1).astype(int)  # true rate is a tenth of the raw score
    cal = fit_binary_calibrator(y, p)
    out = apply_binary_calibrator(cal, p)
    assert abs(out.mean() - y.mean()) < 0.01
    assert (out >= MIN_PROB).all()
    assert (apply_binary_calibrator(cal, np.array([0.0, 1.0])) >= MIN_PROB).all()
    # one-outcome slice: constant, never 0
    flat = fit_binary_calibrator(np.zeros(10, dtype=int), p[:10])
    assert (apply_binary_calibrator(flat, p[:10]) >= MIN_PROB).all()


def test_recency_weighting_follows_the_recent_rate() -> None:
    rng = np.random.default_rng(1)
    n = 6000
    p = np.full(n, 0.5)
    age = np.linspace(100, 0, n)  # row 0 oldest
    rate = np.where(age > 50, 0.30, 0.02)  # old period fails a lot, recent period rarely
    y = (rng.random(n) < rate).astype(int)
    plain = apply_binary_calibrator(fit_binary_calibrator(y, p), p[:1])[0]
    weighted = apply_binary_calibrator(
        fit_binary_calibrator(y, p, sample_weight=recency_weights(age, 10)), p[:1]
    )[0]
    assert abs(weighted - 0.02) < abs(plain - 0.02)
    assert weighted < plain


def test_calibrator_extra_column_shifts_a_group() -> None:
    rng = np.random.default_rng(2)
    n = 8000
    p = rng.random(n) * 0.3 + 0.1
    flag = (rng.random(n) < 0.2).astype(float)
    y = (rng.random(n) < p * (1 + 3 * flag) * 0.3).astype(int)
    cal = fit_binary_calibrator(y, p, extra=flag[:, None])
    out = apply_binary_calibrator(cal, p, flag[:, None])
    assert out[flag == 1].mean() > 2 * out[flag == 0].mean()


def test_combine_any() -> None:
    a, b = np.array([0.1, 0.0]), np.array([0.2, 0.5])
    assert np.allclose(combine_any(a, b), [1 - 0.9 * 0.8, 0.5])
