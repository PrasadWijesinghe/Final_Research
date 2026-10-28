from __future__ import annotations

import pandas as pd
from model.validate import expanding_temporal_folds


def test_expanding_temporal_folds_are_ordered_and_purge_incidents() -> None:
    frame = pd.DataFrame(
        {
            "Incident_ID": ["crossing", *[f"incident-{index}" for index in range(18)], "crossing"],
            "Run_Created_At_UTC": pd.date_range("2026-01-01", periods=20, freq="D", tz="UTC"),
        }
    )

    folds = expanding_temporal_folds(frame, folds=2, initial_train_fraction=0.5)

    for training, evaluation in folds:
        assert training["Run_Created_At_UTC"].max() < evaluation["Run_Created_At_UTC"].min()
        assert not set(training["Incident_ID"]) & set(evaluation["Incident_ID"])
