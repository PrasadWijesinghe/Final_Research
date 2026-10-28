from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest
from rag import CaseRetriever
from rag.prepare_cases import extract_review_candidates, finalise_cases


def _unchanged(text: str) -> tuple[str, dict[str, int]]:
    return text, {}


def test_extract_review_candidates_groups_incident_and_requires_review() -> None:
    model = pd.DataFrame(
        {
            "Record_ID": [1, 2, 3],
            "Incident_ID": ["same", "same", "negative"],
            "Infra_Failure": [1, 1, 0],
            "Infra_Subtype": ["runner", "runner", "other"],
        }
    )
    review = pd.DataFrame(
        {
            "Record_ID": [1, 2, 3],
            "Error_Evidence": ["runner offline", "runner offline", "test failed"],
            "Reviewer_Evidence": ["job queued", "job queued", "assertion"],
            "Review_Note": ["candidate", "candidate", "negative"],
            "Root_Cause": ["service stopped", "service stopped", "code defect"],
            "Label_Status": ["reviewed", "reviewed", "reviewed"],
        }
    )

    cases = extract_review_candidates(model, review, sanitiser=_unchanged)

    assert len(cases) == 1
    assert cases[0]["source_record_ids"] == [1, 2]
    assert cases[0]["human_verified"] is False
    assert cases[0]["root_cause"] == ""


def test_finalise_writes_and_indexes_only_approved_complete_cases(tmp_path: Path) -> None:
    review_path = tmp_path / "review.jsonl"
    verified_path = tmp_path / "verified.jsonl"
    index_path = tmp_path / "index.joblib"
    case = {
        "case_id": "infra-001",
        "source_record_ids": [1],
        "failure_type": "runner",
        "evidence": "job remained queued",
        "root_cause": "runner service was stopped",
        "remediation": "restart the runner service",
        "verification_evidence": "the rerun completed",
        "human_verified": True,
        "verified_by": "infrastructure reviewer",
        "verified_at": "2026-10-25",
        "redaction_counts": {},
    }
    review_path.write_text(json.dumps(case) + "\n", encoding="utf-8")

    count = finalise_cases(
        review_path,
        verified_path,
        index_path,
        sanitiser=_unchanged,
    )

    stored = json.loads(verified_path.read_text(encoding="utf-8"))
    result = CaseRetriever.load(index_path).retrieve("runner service stopped", limit=1)
    assert count == 1
    assert stored["verified"] is True
    assert stored["sanitised"] is True
    assert result[0].case_id == "infra-001"


def test_finalise_rejects_an_approved_case_without_a_fix(tmp_path: Path) -> None:
    review_path = tmp_path / "review.jsonl"
    case = {
        "case_id": "infra-001",
        "failure_type": "runner",
        "evidence": "job remained queued",
        "root_cause": "runner service was stopped",
        "remediation": "",
        "verification_evidence": "the rerun completed",
        "human_verified": True,
        "verified_by": "infrastructure reviewer",
        "verified_at": "2026-10-25",
    }
    review_path.write_text(json.dumps(case) + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match="remediation"):
        finalise_cases(
            review_path,
            tmp_path / "verified.jsonl",
            tmp_path / "index.joblib",
            sanitiser=_unchanged,
        )
