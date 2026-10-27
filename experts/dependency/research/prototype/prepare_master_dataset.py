"""Audit reviewed Dependency labels and build a feature-joined training table."""

import argparse
from pathlib import Path

from dependency_expert.features.master_dataset import (
    DEFAULT_DATASET_DIR,
    DEFAULT_FEATURE_CSV,
    audit_and_prepare_dataset,
)


def main() -> None:
    """Run the local label audit and feature join."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset-dir", type=Path, default=DEFAULT_DATASET_DIR)
    parser.add_argument("--features", type=Path, default=DEFAULT_FEATURE_CSV)
    args = parser.parse_args()
    audit_and_prepare_dataset(args.dataset_dir, args.features)


if __name__ == "__main__":
    main()
