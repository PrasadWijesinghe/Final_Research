"""Pre-run features for the Test expert (docs/experts/test.md section 4).

One module per group; `compute` ties them together; `loader` is the only code that reads `raw`.
"""

from .compute import (
    build_row,
    compute_features,
    compute_features_batch,
    feature_names,
)
from .descriptions import describe, load_descriptions

__all__ = [
    "build_row",
    "compute_features",
    "compute_features_batch",
    "describe",
    "feature_names",
    "load_descriptions",
]
