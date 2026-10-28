"""Compatibility import for the canonical Infra feature module used by serving."""

from __future__ import annotations

import sys
from pathlib import Path

INFRA_SRC = Path(__file__).resolve().parents[2] / "src"
REPO_ROOT = Path(__file__).resolve().parents[4]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(INFRA_SRC) not in sys.path:
    sys.path.insert(0, str(INFRA_SRC))

from infra_expert.features.model_features import (  # noqa: E402
    BINARY_FEATURES,
    CATEGORICAL_FEATURES,
    DERIVED_NUMERIC_FEATURES,
    FEATURE_PROFILES,
    FEATURE_SCHEMA_VERSION,
    HISTORY_FEATURES,
    MODEL_FEATURES,
    NUMERIC_FEATURES,
    RAW_NUMERIC_FEATURES,
    RAW_REQUIRED_FEATURES,
    TARGET_COLUMN,
    build_preprocessor,
    feature_columns,
    prepare_features,
)

__all__ = [
    "BINARY_FEATURES",
    "CATEGORICAL_FEATURES",
    "DERIVED_NUMERIC_FEATURES",
    "FEATURE_SCHEMA_VERSION",
    "FEATURE_PROFILES",
    "HISTORY_FEATURES",
    "MODEL_FEATURES",
    "NUMERIC_FEATURES",
    "RAW_NUMERIC_FEATURES",
    "RAW_REQUIRED_FEATURES",
    "TARGET_COLUMN",
    "build_preprocessor",
    "feature_columns",
    "prepare_features",
]
