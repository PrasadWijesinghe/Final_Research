from __future__ import annotations

import numpy as np
import pandas as pd
from model.features import (
    HISTORY_FEATURES,
    MODEL_FEATURES,
    RAW_REQUIRED_FEATURES,
    feature_columns,
    prepare_features,
)


def test_prepare_features_uses_expected_columns_and_derives_ratios() -> None:
    row = {column: np.nan for column in RAW_REQUIRED_FEATURES}
    row.update(
        {
            "Trigger_Event": "push",
            "Branch_Category": "Default branch",
            "Runner_Labels": "ubuntu-latest; self-hosted",
            "Is_Default_Branch": "Yes",
            "Docker_Used": "No",
            "Files_Changed_Count": 4,
            "Lines_Added": 10,
            "Lines_Deleted": 2,
            "Infra_Files_Changed_Count": 1,
            "Repo_Prior_Runs_20": 3,
            "Workflow_Prior_Runs_10": 0,
        }
    )
    prepared = prepare_features(pd.DataFrame([row]))

    assert tuple(prepared.columns) == MODEL_FEATURES
    assert prepared.loc[0, "Is_Default_Branch"] == 1.0
    assert prepared.loc[0, "Docker_Used"] == 0.0
    assert prepared.loc[0, "Total_Lines_Changed"] == 12.0
    assert prepared.loc[0, "Infra_File_Change_Ratio"] == 0.25
    assert prepared.loc[0, "Repo_History_Available"] == 1.0
    assert prepared.loc[0, "Workflow_History_Available"] == 0.0
    assert prepared.loc[0, "Runner_Label_Count"] == 2.0


def test_no_history_profile_removes_every_history_signal() -> None:
    columns = feature_columns("no_history")

    assert set(columns) == set(MODEL_FEATURES) - set(HISTORY_FEATURES)


def test_missing_collection_flags_are_derived_from_recorded_events() -> None:
    rows = []
    for event in ["push", "pull_request", "pull_request_target", "schedule", np.nan]:
        row = dict.fromkeys(RAW_REQUIRED_FEATURES, np.nan)
        row["Trigger_Event"] = event
        rows.append(row)
    result = prepare_features(pd.DataFrame(rows))
    assert result.Is_Pull_Request.iloc[:4].tolist() == [0.0, 1.0, 1.0, 0.0]
    assert result.Is_Scheduled.iloc[:4].tolist() == [0.0, 0.0, 0.0, 1.0]
    assert pd.isna(result.Is_Pull_Request.iloc[4])
