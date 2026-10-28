from __future__ import annotations

import pandas as pd
import pytest
from model import dataset as dataset_module
from model.dataset import assign_purged_temporal_splits


def test_temporal_split_is_ordered_and_incident_safe() -> None:
    frame = pd.DataFrame(
        {
            "Incident_ID": [f"incident-{index}" for index in range(20)],
            "Run_Created_At_UTC": pd.date_range("2026-01-01", periods=20, freq="D", tz="UTC"),
        }
    )
    split, _, _, crossing_rows = assign_purged_temporal_splits(frame)

    assert crossing_rows == 0
    train = split.loc[split["Split"].eq("train"), "Run_Created_At_UTC"]
    validation = split.loc[split["Split"].eq("validation"), "Run_Created_At_UTC"]
    test = split.loc[split["Split"].eq("test"), "Run_Created_At_UTC"]
    assert train.max() < validation.min()
    assert validation.max() < test.min()
    assert split.groupby("Incident_ID")["Split"].nunique().max() == 1


def test_incident_crossing_a_boundary_is_purged() -> None:
    times = pd.date_range("2026-01-01", periods=12, freq="D", tz="UTC")
    incidents = [f"incident-{index}" for index in range(12)]
    incidents[0] = "crossing"
    incidents[-1] = "crossing"
    frame = pd.DataFrame({"Incident_ID": incidents, "Run_Created_At_UTC": times})

    split, _, _, crossing_rows = assign_purged_temporal_splits(frame)

    assert crossing_rows == 2
    assert set(split.loc[split["Incident_ID"].eq("crossing"), "Split"]) == {
        "excluded_cross_boundary"
    }


@pytest.mark.parametrize("target", [0.9, 1.5, "bad label"])
def test_invalid_target_is_rejected_before_integer_conversion(monkeypatch, target) -> None:
    source = pd.DataFrame({"Infra_Failure": [target]})
    monkeypatch.setattr(dataset_module, "_read_source", lambda path: source)
    with pytest.raises(ValueError, match="other than 0/1/blank"):
        dataset_module.load_prepared_dataset()


def test_old_artifact_is_not_evaluated_with_new_feature_code() -> None:
    with pytest.raises(ValueError, match="feature schema does not match"):
        dataset_module.load_dataset_for_artifact(dataset_module.DEFAULT_INPUT, {})
