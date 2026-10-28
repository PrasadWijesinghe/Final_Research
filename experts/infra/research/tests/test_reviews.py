"""Human review is required before failure labels enter the reviewed dataset."""

import json

import pandas as pd
import pytest
from model.reviews import apply_reviews


def test_only_fully_reviewed_failure_labels_are_used(tmp_path) -> None:
    source = pd.DataFrame(
        {
            "Run_Key": ["run-1", "run-2", "run-3"],
            "Label_Status": [
                "Observed success",
                "Annotation reviewed (agent)",
                "Needs manual review",
            ],
            "Infra_Failure": [0, 1, None],
            "Infra_Subtype": ["None", "Network", "Unknown"],
        }
    )
    record = {
        "run_key": "run-3",
        "source_sha256": "source-digest",
        "human_verified": True,
        "verified_target": 1,
        "verified_subtype": "Container pull",
        "reviewer_id": "reviewer-1",
        "reviewed_at_utc": "2026-10-28T12:00:00Z",
        "evidence_url": "https://github.com/example/project/actions/runs/123",
        "sanitised_evidence": "Container pull failed.",
        "workflow_revision_verified": True,
    }
    path = tmp_path / "reviews.jsonl"
    path.write_text(json.dumps(record), encoding="utf-8")
    result = apply_reviews(source, path, "source-digest")
    assert result.Infra_Failure.iloc[0] == 0
    assert pd.isna(result.Infra_Failure.iloc[1])
    assert result.Infra_Failure.iloc[2] == 1
    assert result.Infra_Subtype.iloc[2] == "Container pull"
    assert source.Infra_Failure.iloc[1] == 1  # The original workbook stays untouched.

    for bad_record in [
        {**record, "source_sha256": "wrong"},
        {**record, "workflow_revision_verified": False},
        {**record, "evidence_url": "https://example.com/incorrect"},
        {**record, "verified_target": 0.9},
        {**record, "reviewer_id": ""},
    ]:
        path.write_text(json.dumps(bad_record), encoding="utf-8")
        with pytest.raises(ValueError):
            apply_reviews(source, path, "source-digest")


def test_empty_or_unapproved_queue_cannot_train(tmp_path) -> None:
    path = tmp_path / "reviews.jsonl"
    path.write_text(json.dumps({"human_verified": False}), encoding="utf-8")
    with pytest.raises(ValueError, match="no independently human-verified labels"):
        apply_reviews(pd.DataFrame(), path, "source-digest")
