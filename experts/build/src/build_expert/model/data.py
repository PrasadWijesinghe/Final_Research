"""Load a frozen Build dataset (`rows.parquet`) into arrays for training."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow.parquet as pq

from build_expert.features import CATEGORICAL_FEATURES, FEATURE_GROUPS, FEATURE_NAMES

LABELS = (
    "y_build",
    "y_build_confident",
    "y_tsc",
    "y_lint",
    "y_bundler",
    "y_build_other",
    "y_any_failure",
    "y_other_failure",
)
GROUP_OF = {name: group for group, names in FEATURE_GROUPS.items() for name in names}


@dataclass(frozen=True)
class TrainingData:
    """One row per commit: features as floats (NaN = missing), labels, ids and times."""

    x: np.ndarray
    feature_names: tuple[str, ...]
    vocab: dict[str, tuple[str, ...]]  # categorical feature -> values; the code is the index
    labels: dict[str, np.ndarray]
    repo: np.ndarray
    head_sha: np.ndarray
    cutoff: np.ndarray  # datetime64[us], UTC
    completed: np.ndarray  # datetime64[us], UTC; NaT when unknown
    manifest: dict[str, Any]
    dropped_synthetic: int

    def __len__(self) -> int:
        return int(self.x.shape[0])

    def column(self, name: str) -> np.ndarray:
        """One feature column."""
        return self.x[:, self.feature_names.index(name)]

    def feature_index(self, names: tuple[str, ...]) -> list[int]:
        """Column positions of `names`, in that order."""
        return [self.feature_names.index(n) for n in names]


def encode_categorical(values: np.ndarray, vocab: tuple[str, ...]) -> np.ndarray:
    """Strings to codes (index in `vocab`); unknown or missing values become NaN."""
    lookup = {v: float(i) for i, v in enumerate(vocab)}
    return np.array([lookup.get(v, np.nan) if v is not None else np.nan for v in values])


def load_dataset(
    dataset_dir: Path,
    *,
    drop_synthetic: bool = True,
    vocab: dict[str, tuple[str, ...]] | None = None,
) -> TrainingData:
    """Read `rows.parquet` and `manifest.json` from a dataset version folder."""
    rows_path = dataset_dir / "rows.parquet"
    if not rows_path.exists():
        raise FileNotFoundError(f"{rows_path} not found; build it with `build-dataset` first")
    manifest_path = dataset_dir / "manifest.json"
    manifest = (
        json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
    )
    table = pq.read_table(rows_path)
    missing = [n for n in (*FEATURE_NAMES, *LABELS) if n not in table.column_names]
    if missing:
        raise ValueError(f"{rows_path} lacks columns {missing[:5]}; rebuild the dataset")

    synthetic = np.asarray(table.column("is_synthetic").to_numpy(zero_copy_only=False), dtype=bool)
    keep = ~synthetic if drop_synthetic else np.ones(len(synthetic), dtype=bool)

    def col(name: str) -> np.ndarray:
        return np.asarray(table.column(name).to_numpy(zero_copy_only=False))[keep]

    vocab = dict(vocab or {})
    columns: list[np.ndarray] = []
    for name in FEATURE_NAMES:
        values = col(name)
        if name in CATEGORICAL_FEATURES:
            if name not in vocab:
                vocab[name] = tuple(sorted({str(v) for v in values if v is not None}))
            columns.append(encode_categorical(values, vocab[name]))
        else:
            columns.append(np.asarray(values, dtype=np.float64))
    x = np.column_stack(columns) if columns else np.empty((int(keep.sum()), 0))

    return TrainingData(
        x=x,
        feature_names=FEATURE_NAMES,
        vocab=vocab,
        labels={name: np.asarray(col(name), dtype=np.int8) for name in LABELS},
        repo=np.asarray(col("repo"), dtype=object),
        head_sha=np.asarray(col("head_sha"), dtype=object),
        cutoff=np.asarray(col("history_cutoff"), dtype="datetime64[us]"),
        completed=np.asarray(col("completed_at"), dtype="datetime64[us]"),
        manifest=manifest,
        dropped_synthetic=int((~keep).sum()),
    )
