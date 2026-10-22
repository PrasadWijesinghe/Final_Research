"""Feature conversion for the experimental Dependency baseline dataset."""

import argparse
import csv
import json
from collections.abc import Mapping
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[5]
DEFAULT_INPUT = REPOSITORY_ROOT / "data/datasets/dependency/prototype/dependency_runs_dataset.json"
DEFAULT_OUTPUT = REPOSITORY_ROOT / "data/datasets/dependency/prototype/dependency_features.csv"

type FeatureRow = dict[str, str | int]

CSV_FIELDS = (
    "run_id",
    "repository",
    "commit_sha",
    "total_dependency_files_changed",
    "has_package_json_change",
    "has_lockfile_change",
    "is_failure",
)


def dependency_feature_rows(records: object) -> list[FeatureRow]:
    """Convert collected run records into simple dependency-change feature rows."""
    if not isinstance(records, list):
        raise ValueError("records must be a JSON array")
    rows: list[FeatureRow] = []
    for index, item in enumerate(records):
        if not isinstance(item, Mapping):
            raise ValueError(f"record {index} must be an object")
        dependency_files = item.get("dependency_files_changed", [])
        if not isinstance(dependency_files, list):
            raise ValueError(f"record {index} dependency_files_changed must be a list")

        names: list[str] = []
        for file_index, changed_file in enumerate(dependency_files):
            if not isinstance(changed_file, Mapping):
                raise ValueError(f"record {index} file {file_index} must be an object")
            filename = changed_file.get("filename", "")
            if not isinstance(filename, str):
                raise ValueError(f"record {index} file {file_index} filename must be text")
            names.append(filename)

        status = item.get("status")
        rows.append(
            {
                "run_id": str(item.get("run_id") or ""),
                "repository": str(item.get("repository") or ""),
                "commit_sha": str(item.get("commit_sha") or ""),
                "total_dependency_files_changed": len(names),
                "has_package_json_change": int(
                    any(name.endswith("package.json") for name in names)
                ),
                "has_lockfile_change": int(
                    any(
                        name.endswith(("package-lock.json", "yarn.lock", "pnpm-lock.yaml"))
                        for name in names
                    )
                ),
                "is_failure": int(status == "failure"),
            }
        )
    return rows


def engineer_features(input_json: Path, output_csv: Path) -> int:
    """Write feature rows to CSV and return the number of records processed."""
    with input_json.open(encoding="utf-8") as input_file:
        records = json.load(input_file)
    if not isinstance(records, list):
        raise ValueError(f"{input_json} must contain a JSON array")

    rows = dependency_feature_rows(records)
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    with output_csv.open("w", encoding="utf-8", newline="") as output_file:
        writer = csv.DictWriter(output_file, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    return len(rows)


def main() -> None:
    """Convert the local JSON dataset to the prototype feature CSV."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    count = engineer_features(args.input, args.output)
    print(f"Wrote {count} feature rows to {args.output}")


if __name__ == "__main__":
    main()
