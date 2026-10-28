"""Audit the actual training workbook and export an unapproved label-review queue."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

import pandas as pd

from .dataset import DEFAULT_INPUT, _read_source, _split_summary, load_prepared_dataset
from .features import MODEL_FEATURES, TARGET_COLUMN


def audit_dataset(input_path: Path, output_dir: Path) -> dict[str, Any]:
    """Record label coverage, missing inputs and feature collisions without changing labels."""
    from shared.sanitise import sanitise

    source = _read_source(input_path)
    dataset = load_prepared_dataset(input_path)
    frame = dataset.frame
    positive = source.loc[source[TARGET_COLUMN].eq(1)]
    signature = pd.util.hash_pandas_object(frame.loc[:, MODEL_FEATURES], index=False)
    collisions = frame.assign(signature=signature).groupby("signature")[TARGET_COLUMN]
    conflicting = collisions.nunique().gt(1)
    report = {
        "source_sha256": hashlib.sha256(input_path.read_bytes()).hexdigest(),
        "source_rows": len(source),
        "raw_event_flag_missing_rows": {
            name: int(source[name].isna().sum()) for name in ("Is_Pull_Request", "Is_Scheduled")
        },
        "labelled_rows": len(frame),
        "positive_rows": len(positive),
        "positive_incidents": int(positive.Incident_ID.nunique()),
        "unresolved_rows": dataset.unresolved_rows,
        "label_status_counts": source.Label_Status.value_counts().to_dict(),
        "positive_label_status_counts": positive.Label_Status.value_counts().to_dict(),
        "positive_subtypes": positive.Infra_Subtype.value_counts().to_dict(),
        "positive_repositories": positive.Repository.value_counts().to_dict(),
        "split_summary": _split_summary(frame),
        "missing_fraction_by_target": {
            str(label): rows.loc[:, MODEL_FEATURES].isna().mean().to_dict()
            for label, rows in frame.groupby(TARGET_COLUMN)
        },
        "indistinguishable_feature_groups_with_opposite_labels": int(conflicting.sum()),
        "rows_in_conflicting_feature_groups": int(
            signature.isin(conflicting.loc[conflicting].index).sum()
        ),
        "deployment_ready": False,
        "limitations": [
            "Assistant annotations are not independent human verification.",
            "Legacy 'Manually verified' text alone does not establish reviewer provenance.",
            "Targeted, failure-enriched sampling cannot establish real push failure prevalence.",
            "Training rows represent workflow runs; live input currently aggregates workflows.",
            "Image identity/availability and current runner/service health are absent inputs.",
            "Historical head-SHA snapshots may differ from the workflow actually executed.",
        ],
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "data_audit.json").write_text(json.dumps(report, indent=2), encoding="utf-8")

    # Export references, not raw logs or actor identities. Humans must supply their own verdict.
    metadata = pd.read_excel(input_path, sheet_name="Source_Metadata")
    urls = metadata.set_index("Record_ID")["Run_URL"].to_dict()
    review = pd.read_excel(input_path, sheet_name="Infra_Label_Review")
    evidence_by_id = review.set_index("Record_ID")["Error_Evidence"].to_dict()
    queue = []
    for row in source.loc[~source.Label_Status.eq("Observed success")].to_dict("records"):
        target = row[TARGET_COLUMN]
        queue.append(
            {
                "record_id": int(row["Record_ID"]),
                "source_sha256": report["source_sha256"],
                "run_key": row["Run_Key"],
                "incident_id": row["Incident_ID"],
                "repository": row["Repository"],
                "run_url": urls.get(row["Record_ID"]),
                "source_label_status": row["Label_Status"],
                "source_target": int(target) if pd.notna(target) else None,
                "source_subtype": row["Infra_Subtype"] if pd.notna(row["Infra_Subtype"]) else None,
                "source_error_evidence": (
                    sanitise(str(evidence_by_id[row["Record_ID"]])).text
                    if pd.notna(evidence_by_id.get(row["Record_ID"]))
                    else ""
                ),
                "human_verified": False,
                "reviewer_id": "",
                "reviewed_at_utc": "",
                "verified_target": None,
                "verified_subtype": "",
                "evidence_url": "",
                "sanitised_evidence": "",
                "workflow_revision_verified": False,
                "review_notes": "",
            }
        )
    queue_path = output_dir / "label_review_queue.jsonl"
    # Do not overwrite somebody's completed reviews on a repeated audit.
    if not queue_path.exists():
        queue_path.write_text(
            "".join(json.dumps(row, allow_nan=False) + "\n" for row in queue), encoding="utf-8"
        )
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    report = audit_dataset(args.input, args.output_dir)
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
