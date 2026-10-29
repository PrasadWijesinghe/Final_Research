"""Build a local retrieval index from verified JSONL cases."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .retriever import CaseRetriever

RESEARCH_DIR = Path(__file__).resolve().parents[1]


def read_jsonl(path: Path) -> list[dict[str, object]]:
    """Read non-empty JSON objects from a JSONL file."""
    cases = []
    with path.open(encoding="utf-8") as source:
        for line_number, line in enumerate(source, start=1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(f"line {line_number} is not a JSON object")
            cases.append(value)
    return cases


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument(
        "--output",
        type=Path,
        default=RESEARCH_DIR / "artifacts" / "rag" / "case_retriever.joblib",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    cases = read_jsonl(args.input)
    invalid = [
        case.get("case_id", "<missing>")
        for case in cases
        if case.get("verified") is not True or case.get("sanitised") is not True
    ]
    if invalid:
        raise ValueError(
            "every indexed case must have verified=true and sanitised=true; "
            f"invalid cases: {invalid}"
        )
    retriever = CaseRetriever().fit(cases)
    retriever.save(args.output)
    print(f"indexed {len(retriever.cases)} verified cases at {args.output}")


if __name__ == "__main__":
    main()
