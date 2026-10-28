"""Guard the split boundaries used for nested repository model selection."""

import pandas as pd
from model.benchmark import inner_partitions


def test_inner_partitions_keep_all_three_stages_disjoint() -> None:
    frame = pd.DataFrame(
        {
            "Incident_ID": [f"incident-{index}" for index in range(100)],
            "Run_Created_At_UTC": pd.date_range("2026-01-01", periods=100, tz="UTC"),
            "Infra_Failure": [index % 2 for index in range(100)],
        }
    )
    fit, calibration, selection = inner_partitions(frame)
    assert fit.Run_Created_At_UTC.max() < calibration.Run_Created_At_UTC.min()
    assert calibration.Run_Created_At_UTC.max() < selection.Run_Created_At_UTC.min()
    assert not set(fit.Incident_ID) & set(calibration.Incident_ID)
    assert not set(selection.Incident_ID) & set(calibration.Incident_ID)
    assert not set(selection.Incident_ID) & set(fit.Incident_ID)
