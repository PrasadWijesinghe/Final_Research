"""Canonical feature schema and transformations for Infra training and serving."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import numpy as np
import pandas as pd

if TYPE_CHECKING:
    from sklearn.compose import ColumnTransformer

TARGET_COLUMN = "Infra_Failure"
FEATURE_SCHEMA_VERSION = "v5-event-flags-derived"

CATEGORICAL_FEATURES = (
    "Trigger_Event",
    "Branch_Category",
    "Runner_Labels",
)

BINARY_FEATURES = (
    "Is_Default_Branch",
    "Is_Pull_Request",
    "Is_Scheduled",
    "Uses_Self_Hosted_Runner",
    "Permissions_Defined",
    "Docker_Used",
    "Services_Used",
    "Matrix_Used",
    "Cache_Action_Used",
    "Timeout_Defined",
    "Continue_On_Error_Used",
    "Workflow_Changed",
    "Dockerfile_Changed",
    "Deployment_Config_Changed",
)

RAW_NUMERIC_FEATURES = (
    "Run_Attempt",
    "Configured_Job_Count",
    "Secret_Reference_Count",
    "Third_Party_Action_Count",
    "Workflow_Line_Count",
    "Files_Changed_Count",
    "Lines_Added",
    "Lines_Deleted",
    "Infra_Files_Changed_Count",
    "Repo_Prior_Runs_20",
    "Repo_Prior_Failure_Rate_20",
    "Workflow_Prior_Runs_10",
    "Workflow_Prior_Failure_Rate_10",
    "Hours_Since_Previous_Workflow_Run",
)

DERIVED_NUMERIC_FEATURES = (
    "Total_Lines_Changed",
    "Infra_File_Change_Ratio",
    "Repo_History_Available",
    "Workflow_History_Available",
    "Runner_Label_Count",
)

NUMERIC_FEATURES = (*BINARY_FEATURES, *RAW_NUMERIC_FEATURES, *DERIVED_NUMERIC_FEATURES)
MODEL_FEATURES = (*CATEGORICAL_FEATURES, *NUMERIC_FEATURES)
RAW_REQUIRED_FEATURES = (*CATEGORICAL_FEATURES, *BINARY_FEATURES, *RAW_NUMERIC_FEATURES)

HISTORY_FEATURES = (
    "Repo_Prior_Runs_20",
    "Repo_Prior_Failure_Rate_20",
    "Workflow_Prior_Runs_10",
    "Workflow_Prior_Failure_Rate_10",
    "Hours_Since_Previous_Workflow_Run",
    "Repo_History_Available",
    "Workflow_History_Available",
)
FEATURE_PROFILES = {
    "full": MODEL_FEATURES,
    "no_history": tuple(column for column in MODEL_FEATURES if column not in HISTORY_FEATURES),
}

_BINARY_VALUES: dict[Any, float] = {
    "yes": 1.0,
    "no": 0.0,
    "true": 1.0,
    "false": 0.0,
    "1": 1.0,
    "0": 0.0,
    True: 1.0,
    False: 0.0,
}


def _require_columns(frame: pd.DataFrame, columns: tuple[str, ...]) -> None:
    missing = sorted(set(columns) - set(frame.columns))
    if missing:
        raise ValueError(f"dataset is missing required feature columns: {missing}")


def _binary(series: pd.Series) -> pd.Series:
    """Map common yes/no representations to floats while preserving missing values."""
    normalised = series.map(
        lambda value: value.strip().lower() if isinstance(value, str) else value
    )
    return normalised.map(_BINARY_VALUES).astype(float)


def prepare_features(frame: pd.DataFrame) -> pd.DataFrame:
    """Return the model matrix using only documented pre-execution candidate fields."""
    _require_columns(frame, RAW_REQUIRED_FEATURES)
    prepared = pd.DataFrame(index=frame.index)

    for column in CATEGORICAL_FEATURES:
        prepared[column] = frame[column].astype("object")
    for column in BINARY_FEATURES:
        prepared[column] = _binary(frame[column])
    for column in RAW_NUMERIC_FEATURES:
        prepared[column] = pd.to_numeric(frame[column], errors="coerce").astype(float)

    # The expansion collector omitted these flags in 2,000 rows. They are fully
    # determined by the pre-run event; missingness must not encode collection batch.
    known_event = frame["Trigger_Event"].notna()
    prepared.loc[known_event, "Is_Pull_Request"] = (
        frame.loc[known_event, "Trigger_Event"]
        .isin(["pull_request", "pull_request_target"])
        .astype(float)
    )
    prepared.loc[known_event, "Is_Scheduled"] = (
        frame.loc[known_event, "Trigger_Event"].eq("schedule").astype(float)
    )

    prepared["Total_Lines_Changed"] = prepared["Lines_Added"].fillna(0.0) + prepared[
        "Lines_Deleted"
    ].fillna(0.0)
    changed_count = prepared["Files_Changed_Count"]
    prepared["Infra_File_Change_Ratio"] = np.where(
        changed_count.gt(0),
        prepared["Infra_Files_Changed_Count"].fillna(0.0) / changed_count,
        0.0,
    )
    prepared["Repo_History_Available"] = prepared["Repo_Prior_Runs_20"].gt(0).astype(float)
    prepared["Workflow_History_Available"] = prepared["Workflow_Prior_Runs_10"].gt(0).astype(float)
    prepared["Runner_Label_Count"] = frame["Runner_Labels"].map(
        lambda value: float(len(str(value).split(";"))) if pd.notna(value) else np.nan
    )
    return prepared.loc[:, MODEL_FEATURES]


def feature_columns(profile: str) -> tuple[str, ...]:
    """Return the documented feature set for one experiment profile."""
    try:
        return FEATURE_PROFILES[profile]
    except KeyError as exc:
        choices = ", ".join(sorted(FEATURE_PROFILES))
        raise ValueError(f"unknown feature profile {profile!r}; choose one of: {choices}") from exc


def build_preprocessor(columns: tuple[str, ...] = MODEL_FEATURES) -> ColumnTransformer:
    """Build the fitted-later scikit-learn preprocessing graph."""
    from sklearn.compose import ColumnTransformer
    from sklearn.impute import SimpleImputer
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import OneHotEncoder, StandardScaler

    numeric = Pipeline(
        steps=[
            ("impute", SimpleImputer(strategy="median", add_indicator=True)),
            ("scale", StandardScaler()),
        ]
    )
    categorical = Pipeline(
        steps=[
            ("impute", SimpleImputer(strategy="most_frequent")),
            ("encode", OneHotEncoder(handle_unknown="ignore")),
        ]
    )
    unknown = sorted(set(columns) - set(MODEL_FEATURES))
    if unknown:
        raise ValueError(f"unknown model feature columns: {unknown}")
    numeric_columns = [column for column in NUMERIC_FEATURES if column in columns]
    categorical_columns = [column for column in CATEGORICAL_FEATURES if column in columns]
    return ColumnTransformer(
        transformers=[
            ("numeric", numeric, numeric_columns),
            ("categorical", categorical, categorical_columns),
        ],
        verbose_feature_names_out=False,
    )
