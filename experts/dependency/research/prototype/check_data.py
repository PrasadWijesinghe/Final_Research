"""Print the number of records in the local prototype dataset."""

import json
from pathlib import Path

DATA_FILE = (
    Path(__file__).resolve().parents[4]
    / "data/datasets/dependency/prototype/dependency_runs_dataset.json"
)


def main() -> None:
    """Read the generated dataset from its repository-local artifact path."""
    with DATA_FILE.open(encoding="utf-8") as input_file:
        data = json.load(input_file)
    if not isinstance(data, list):
        raise ValueError(f"{DATA_FILE} must contain a JSON array")
    print(f"Records: {len(data)}")


if __name__ == "__main__":
    main()
