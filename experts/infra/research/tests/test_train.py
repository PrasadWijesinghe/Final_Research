from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from model.calibration import split_model_fit_and_calibration
from model.train import calibration_summary, choose_threshold, compute_metrics


def test_calibration_summary_reports_perfect_probabilities() -> None:
    labels = np.array([0, 0, 1, 1])
    probabilities = np.array([0.0, 0.0, 1.0, 1.0])

    result = calibration_summary(labels, probabilities, bins=5)

    assert result["expected_calibration_error"] == pytest.approx(0.0)
    assert sum(item["rows"] for item in result["bins"]) == 4


def test_choose_threshold_uses_validation_f1() -> None:
    labels = np.array([0, 0, 1, 1])
    probabilities = np.array([0.1, 0.4, 0.6, 0.9])

    assert choose_threshold(labels, probabilities) == pytest.approx(0.6)


def test_metrics_handle_a_single_class_slice() -> None:
    labels = np.array([0, 0, 0])
    probabilities = np.array([0.1, 0.2, 0.3])

    result = compute_metrics(labels, probabilities, threshold=0.5)

    assert result["roc_auc"] is None
    assert result["pr_auc"] is None
    assert result["mcc"] == 0.0
    assert result["confusion_matrix"] == [[3, 0], [0, 0]]


def test_calibration_split_is_chronological_and_incident_safe() -> None:
    rows = pd.DataFrame(
        {
            "Incident_ID": [f"incident-{index}" for index in range(20)],
            "Run_Created_At_UTC": pd.date_range("2026-01-01", periods=20, freq="D", tz="UTC"),
            "Infra_Failure": [index % 2 for index in range(20)],
        }
    )

    model_fit, calibration, crossing_rows = split_model_fit_and_calibration(rows)

    assert model_fit["Run_Created_At_UTC"].max() < calibration["Run_Created_At_UTC"].min()
    assert not set(model_fit["Incident_ID"]) & set(calibration["Incident_ID"])
    assert crossing_rows == 0
