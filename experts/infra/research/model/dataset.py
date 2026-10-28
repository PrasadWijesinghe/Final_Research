"""Build a leakage-controlled training table from the Infra research workbook."""

from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from .features import FEATURE_SCHEMA_VERSION, MODEL_FEATURES, TARGET_COLUMN, prepare_features

RESEARCH_DIR = Path(__file__).resolve().parents[1]
DEFAULT_INPUT = RESEARCH_DIR / "data" / "raw" / "github_actions_research_dataset_3000.xlsx"
DEFAULT_OUTPUT_DIR = RESEARCH_DIR / "data" / "processed"

MODEL_SHEET = "Infra_Model_Data"
METADATA_SHEET = "Source_Metadata"
TRAIN_FRACTION = 0.60
VALIDATION_FRACTION = 0.20

_MODEL_REQUIRED = {
    "Record_ID",
    "Run_Key",
    "Incident_ID",
    "Repository_Group",
    "Infra_Subtype",
    "Label_Status",
    TARGET_COLUMN,
}
_METADATA_REQUIRED = {"Record_ID", "Repository", "Run_Created_At_UTC"}


@dataclass(frozen=True)
class PreparedDataset:
    """Prepared rows plus the audit facts needed by training and reporting."""

    frame: pd.DataFrame
    source_rows: int
    unresolved_rows: int
    crossing_incident_rows: int
    train_cutoff: pd.Timestamp
    validation_cutoff: pd.Timestamp
    source_sha256: str | None = None
    reviews_sha256: str | None = None

    def split(self, name: str) -> pd.DataFrame:
        return self.frame.loc[self.frame["Split"].eq(name)].copy()


def _check_columns(frame: pd.DataFrame, required: set[str], sheet: str) -> None:
    missing = sorted(required - set(frame.columns))
    if missing:
        raise ValueError(f"{sheet} is missing required columns: {missing}")


def _read_source(path: Path) -> pd.DataFrame:
    if not path.is_file():
        raise FileNotFoundError(f"dataset workbook not found: {path}")
    model = pd.read_excel(path, sheet_name=MODEL_SHEET)
    metadata = pd.read_excel(
        path,
        sheet_name=METADATA_SHEET,
        usecols=["Record_ID", "Repository", "Run_Created_At_UTC"],
    )
    _check_columns(model, _MODEL_REQUIRED, MODEL_SHEET)
    _check_columns(metadata, _METADATA_REQUIRED, METADATA_SHEET)
    if model["Record_ID"].duplicated().any():
        raise ValueError("Infra_Model_Data contains duplicate Record_ID values")
    if metadata["Record_ID"].duplicated().any():
        raise ValueError("Source_Metadata contains duplicate Record_ID values")

    joined = model.merge(metadata, on="Record_ID", how="left", validate="one_to_one")
    joined["Run_Created_At_UTC"] = pd.to_datetime(
        joined["Run_Created_At_UTC"], utc=True, errors="coerce"
    )
    if joined[["Repository", "Run_Created_At_UTC"]].isna().any().any():
        raise ValueError("one or more model rows have no repository or run creation timestamp")
    return joined


def assign_purged_temporal_splits(
    frame: pd.DataFrame,
    *,
    train_fraction: float = TRAIN_FRACTION,
    validation_fraction: float = VALIDATION_FRACTION,
) -> tuple[pd.DataFrame, pd.Timestamp, pd.Timestamp, int]:
    """Assign strict time splits and purge incidents that cross either boundary."""
    if not 0 < train_fraction < 1:
        raise ValueError("train_fraction must be between 0 and 1")
    if not 0 < validation_fraction < 1 - train_fraction:
        raise ValueError("validation_fraction must leave a non-empty test fraction")
    if frame.empty:
        raise ValueError("cannot split an empty dataset")

    train_cutoff = frame["Run_Created_At_UTC"].quantile(train_fraction)
    validation_cutoff = frame["Run_Created_At_UTC"].quantile(train_fraction + validation_fraction)
    split = np.where(
        frame["Run_Created_At_UTC"].le(train_cutoff),
        "train",
        np.where(frame["Run_Created_At_UTC"].le(validation_cutoff), "validation", "test"),
    )
    result = frame.copy()
    result["Split"] = split

    incident_split_counts = result.groupby("Incident_ID", dropna=False)["Split"].nunique()
    crossing_incidents = set(incident_split_counts.loc[incident_split_counts.gt(1)].index)
    crossing_mask = result["Incident_ID"].isin(crossing_incidents)
    crossing_rows = int(crossing_mask.sum())
    result.loc[crossing_mask, "Split"] = "excluded_cross_boundary"

    kept = result.loc[result["Split"].isin(["train", "validation", "test"])]
    if set(kept["Split"].unique()) != {"train", "validation", "test"}:
        raise ValueError("the temporal split did not produce three non-empty partitions")
    overlap = kept.groupby("Incident_ID", dropna=False)["Split"].nunique().max()
    if int(overlap) != 1:
        raise AssertionError("incident leakage remains after temporal purging")
    return result, train_cutoff, validation_cutoff, crossing_rows


def load_prepared_dataset(
    path: Path = DEFAULT_INPUT, *, reviews_path: Path | None = None
) -> PreparedDataset:
    """Read, validate, filter and feature-engineer the source workbook."""
    source = _read_source(path)
    source_sha256 = _sha256(path)
    if reviews_path is not None:
        from .reviews import apply_reviews

        source = apply_reviews(source, reviews_path, source_sha256)
    source_rows = len(source)
    target = pd.to_numeric(source[TARGET_COLUMN], errors="coerce")
    unresolved_rows = int(target.isna().sum())
    invalid = source[TARGET_COLUMN].notna() & ~target.isin([0, 1])
    unexpected = source.loc[invalid, TARGET_COLUMN].astype(str).unique().tolist()
    if unexpected:
        raise ValueError(f"Infra_Failure contains values other than 0/1/blank: {unexpected}")
    labelled = source.loc[target.notna()].copy()
    labelled[TARGET_COLUMN] = target.loc[target.notna()].astype(int)
    if labelled[["Run_Key", "Incident_ID", "Label_Status"]].isna().any().any():
        raise ValueError("labelled rows must have run, incident and label provenance")
    if labelled["Run_Key"].duplicated().any():
        raise ValueError("labelled rows contain duplicate Run_Key values")
    if labelled["Label_Status"].eq("Needs manual review").any():
        raise ValueError("unresolved review rows must not have a training target")

    labelled, train_cutoff, validation_cutoff, crossing_rows = assign_purged_temporal_splits(
        labelled
    )
    feature_frame = prepare_features(labelled)
    audit_columns = [
        "Record_ID",
        "Run_Key",
        "Incident_ID",
        "Repository_Group",
        "Repository",
        "Run_Created_At_UTC",
        "Infra_Subtype",
        "Label_Status",
        TARGET_COLUMN,
        "Split",
    ]
    output = pd.concat([labelled[audit_columns], feature_frame], axis=1)
    return PreparedDataset(
        frame=output,
        source_rows=source_rows,
        unresolved_rows=unresolved_rows,
        crossing_incident_rows=crossing_rows,
        train_cutoff=train_cutoff,
        validation_cutoff=validation_cutoff,
        source_sha256=source_sha256,
        reviews_sha256=_sha256(reviews_path) if reviews_path is not None else None,
    )


def _split_summary(frame: pd.DataFrame) -> dict[str, dict[str, Any]]:
    summary: dict[str, dict[str, Any]] = {}
    for split, rows in frame.groupby("Split"):
        positives = int(rows[TARGET_COLUMN].sum())
        summary[str(split)] = {
            "rows": len(rows),
            "positive": positives,
            "negative": len(rows) - positives,
            "positive_rate": positives / len(rows),
            "repositories": int(rows["Repository"].nunique()),
            "first_run_utc": rows["Run_Created_At_UTC"].min().isoformat(),
            "last_run_utc": rows["Run_Created_At_UTC"].max().isoformat(),
        }
    return summary


def _sha256(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def load_dataset_for_artifact(
    path: Path, artifact: dict[str, Any], reviews_path: Path | None = None
) -> PreparedDataset:
    """Prevent evaluation or explanations against different source or review labels."""
    if artifact.get("feature_schema_version") != FEATURE_SCHEMA_VERSION:
        raise ValueError(
            "artifact feature schema does not match current transformations; "
            "use its original code or retrain before evaluation"
        )
    dataset = load_prepared_dataset(path, reviews_path=reviews_path)
    if artifact.get("source_sha256") and artifact["source_sha256"] != dataset.source_sha256:
        raise ValueError("dataset checksum differs from the model's training dataset")
    if artifact.get("reviews_sha256") != dataset.reviews_sha256:
        raise ValueError("review file differs from the model's training reviews")
    return dataset


def write_prepared_dataset(dataset: PreparedDataset, source: Path, output_dir: Path) -> None:
    """Write the analysis-ready CSV and its reproducibility manifest."""
    output_dir.mkdir(parents=True, exist_ok=True)
    csv_path = output_dir / "infra_training_data.csv"
    manifest_path = output_dir / "manifest.json"
    dataset.frame.to_csv(csv_path, index=False)
    manifest = {
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "source_file": source.name,
        "source_sha256": _sha256(source),
        "reviews_sha256": dataset.reviews_sha256,
        "source_rows": dataset.source_rows,
        "labelled_rows": dataset.source_rows - dataset.unresolved_rows,
        "unresolved_rows_excluded": dataset.unresolved_rows,
        "cross_boundary_rows_excluded_from_training": dataset.crossing_incident_rows,
        "train_cutoff_utc": dataset.train_cutoff.isoformat(),
        "validation_cutoff_utc": dataset.validation_cutoff.isoformat(),
        "split_summary": _split_summary(dataset.frame),
        "target": TARGET_COLUMN,
        "feature_columns": list(MODEL_FEATURES),
        "notes": [
            "Only pre-execution candidate fields are model inputs.",
            "Sampling and repository identifiers are split/audit fields, not model inputs.",
            "Rows with unresolved labels are excluded.",
            "Incidents crossing a temporal boundary are excluded from all training partitions.",
        ],
    }
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(json.dumps(manifest, indent=2))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    dataset = load_prepared_dataset(args.input)
    write_prepared_dataset(dataset, args.input, args.output_dir)


if __name__ == "__main__":
    main()
