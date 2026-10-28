"""Prepare a human-review queue and finalise approved cases for RAG."""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import sys
from collections import Counter
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

import pandas as pd

from .retriever import CaseRetriever

RESEARCH_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_WORKBOOK = RESEARCH_DIR / "data" / "raw" / "github_actions_research_dataset_3000.xlsx"
DEFAULT_REVIEW_QUEUE = RESEARCH_DIR / "data" / "rag_case_review.jsonl"
DEFAULT_VERIFIED_CASES = RESEARCH_DIR / "data" / "verified_cases.jsonl"
DEFAULT_INDEX = RESEARCH_DIR / "artifacts" / "rag" / "case_retriever.joblib"

MODEL_SHEET = "Infra_Model_Data"
REVIEW_SHEET = "Infra_Label_Review"

Sanitiser = Callable[[str], tuple[str, dict[str, int]]]


def _shared_sanitiser(text: str) -> tuple[str, dict[str, int]]:
    """Use the repository's standard secret sanitiser from this isolated folder."""
    repo_root = str(REPO_ROOT)
    if repo_root not in sys.path:
        sys.path.insert(0, repo_root)
    shared = importlib.import_module("shared.sanitise")
    result = shared.sanitise(text)
    return result.text, result.counts


def _text(value: Any) -> str:
    if value is None or bool(pd.isna(value)):
        return ""
    return str(value).strip()


def _unique_text(values: Iterable[Any]) -> str:
    unique: list[str] = []
    for value in values:
        cleaned = _text(value)
        if cleaned and cleaned not in unique:
            unique.append(cleaned)
    return "\n".join(unique)


def _mode(values: pd.Series) -> str:
    cleaned = values.dropna().astype(str).str.strip()
    cleaned = cleaned.loc[cleaned.ne("")]
    if cleaned.empty:
        return "other"
    return str(cleaned.mode().iloc[0])


def _case_id(incident_id: Any) -> str:
    digest = hashlib.sha256(str(incident_id).encode()).hexdigest()[:12]
    return f"infra-{digest}"


def _sanitise_fields(
    values: dict[str, str], sanitiser: Sanitiser
) -> tuple[dict[str, str], dict[str, int]]:
    result: dict[str, str] = {}
    counts: Counter[str] = Counter()
    for name, value in values.items():
        cleaned, field_counts = sanitiser(value)
        result[name] = cleaned
        counts.update(field_counts)
    return result, dict(counts)


def extract_review_candidates(
    model_rows: pd.DataFrame,
    review_rows: pd.DataFrame,
    *,
    sanitiser: Sanitiser = _shared_sanitiser,
) -> list[dict[str, Any]]:
    """Create one unverified review candidate per positive incident."""
    required_model = {"Record_ID", "Incident_ID", "Infra_Failure", "Infra_Subtype"}
    required_review = {
        "Record_ID",
        "Error_Evidence",
        "Reviewer_Evidence",
        "Review_Note",
        "Root_Cause",
        "Label_Status",
    }
    missing_model = sorted(required_model - set(model_rows.columns))
    missing_review = sorted(required_review - set(review_rows.columns))
    if missing_model or missing_review:
        raise ValueError(
            f"missing workbook columns; model={missing_model}, review={missing_review}"
        )

    positive = model_rows.loc[
        pd.to_numeric(model_rows["Infra_Failure"], errors="coerce").eq(1),
        ["Record_ID", "Incident_ID", "Infra_Subtype"],
    ]
    joined = positive.merge(
        review_rows.loc[:, sorted(required_review)],
        on="Record_ID",
        how="left",
        validate="one_to_one",
    )
    if joined.empty:
        raise ValueError("the workbook contains no positive infrastructure failures")
    if joined["Incident_ID"].isna().any():
        raise ValueError("positive rows must have an Incident_ID")

    candidates = []
    for incident_id, rows in joined.groupby("Incident_ID", sort=True):
        fields, counts = _sanitise_fields(
            {
                "evidence": _unique_text(rows["Error_Evidence"]),
                "reviewer_evidence": _unique_text(rows["Reviewer_Evidence"]),
                "review_note": _unique_text(rows["Review_Note"]),
                "suggested_root_cause": _unique_text(rows["Root_Cause"]),
            },
            sanitiser,
        )
        candidates.append(
            {
                "case_id": _case_id(incident_id),
                "source_record_ids": sorted(int(value) for value in rows["Record_ID"]),
                "failure_type": _mode(rows["Infra_Subtype"]),
                **fields,
                "root_cause": "",
                "remediation": "",
                "verification_evidence": "",
                "human_verified": False,
                "verified_by": "",
                "verified_at": "",
                "sanitised": True,
                "redaction_counts": counts,
                "source_label_statuses": sorted(
                    {_text(value) for value in rows["Label_Status"] if _text(value)}
                ),
            }
        )
    return candidates


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    records = []
    with path.open(encoding="utf-8") as source:
        for line_number, line in enumerate(source, start=1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(f"line {line_number} is not a JSON object")
            records.append(value)
    return records


def _write_jsonl(path: Path, records: Iterable[dict[str, Any]], *, overwrite: bool) -> None:
    if path.exists() and not overwrite:
        raise FileExistsError(f"refusing to overwrite existing review work: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    content = "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records)
    path.write_text(content, encoding="utf-8")


def export_review_queue(workbook: Path, output: Path, *, overwrite: bool = False) -> int:
    """Extract and sanitise positive incidents without claiming human verification."""
    model_rows = pd.read_excel(workbook, sheet_name=MODEL_SHEET)
    review_rows = pd.read_excel(workbook, sheet_name=REVIEW_SHEET)
    candidates = extract_review_candidates(model_rows, review_rows)
    _write_jsonl(output, candidates, overwrite=overwrite)
    return len(candidates)


def _require_review_text(case: dict[str, Any], name: str) -> str:
    value = case.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"verified case {case.get('case_id')!r} requires non-empty {name}")
    return value.strip()


def finalise_cases(
    review_queue: Path,
    verified_output: Path,
    index_output: Path,
    *,
    overwrite: bool = False,
    sanitiser: Sanitiser = _shared_sanitiser,
) -> int:
    """Write and index only complete cases explicitly marked human_verified."""
    approved = [case for case in _read_jsonl(review_queue) if case.get("human_verified") is True]
    if not approved:
        raise ValueError(
            "no human-verified cases found; review the queue and set human_verified to true"
        )

    verified_cases = []
    for case in approved:
        case_id = _require_review_text(case, "case_id")
        fields, counts = _sanitise_fields(
            {
                "failure_type": _require_review_text(case, "failure_type"),
                "evidence": _require_review_text(case, "evidence"),
                "root_cause": _require_review_text(case, "root_cause"),
                "remediation": _require_review_text(case, "remediation"),
                "verification_evidence": _require_review_text(case, "verification_evidence"),
                "verified_by": _require_review_text(case, "verified_by"),
                "verified_at": _require_review_text(case, "verified_at"),
            },
            sanitiser,
        )
        text = (
            f"Failure type: {fields['failure_type']}\n"
            f"Evidence: {fields['evidence']}\n"
            f"Root cause: {fields['root_cause']}\n"
            f"Remediation: {fields['remediation']}\n"
            f"Verification: {fields['verification_evidence']}"
        )
        combined_counts = Counter(case.get("redaction_counts", {}))
        combined_counts.update(counts)
        verified_cases.append(
            {
                "case_id": case_id,
                "text": text,
                "failure_type": fields["failure_type"],
                "source_record_ids": case.get("source_record_ids", []),
                "verified": True,
                "verified_by": fields["verified_by"],
                "verified_at": fields["verified_at"],
                "sanitised": True,
                "redaction_counts": dict(combined_counts),
            }
        )

    if index_output.exists() and not overwrite:
        raise FileExistsError(f"refusing to overwrite existing RAG index: {index_output}")
    retriever = CaseRetriever().fit(verified_cases)
    _write_jsonl(verified_output, verified_cases, overwrite=overwrite)
    retriever.save(index_output)
    return len(verified_cases)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    export = subparsers.add_parser("export", help="create a sanitised human-review queue")
    export.add_argument("--input", type=Path, default=DEFAULT_WORKBOOK)
    export.add_argument("--output", type=Path, default=DEFAULT_REVIEW_QUEUE)
    export.add_argument("--overwrite", action="store_true")

    finalise = subparsers.add_parser(
        "finalise", help="write and index cases explicitly approved by a human"
    )
    finalise.add_argument("--input", type=Path, default=DEFAULT_REVIEW_QUEUE)
    finalise.add_argument("--output", type=Path, default=DEFAULT_VERIFIED_CASES)
    finalise.add_argument("--index-output", type=Path, default=DEFAULT_INDEX)
    finalise.add_argument("--overwrite", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.command == "export":
        count = export_review_queue(args.input, args.output, overwrite=args.overwrite)
        print(f"wrote {count} sanitised incident candidates to {args.output}")
        print("A human must complete and approve cases before finalising them.")
        return
    count = finalise_cases(
        args.input,
        args.output,
        args.index_output,
        overwrite=args.overwrite,
    )
    print(f"wrote and indexed {count} human-verified cases")


if __name__ == "__main__":
    main()
