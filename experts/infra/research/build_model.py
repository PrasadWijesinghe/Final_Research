"""Run the complete Infrastructure model workflow with one command."""

from __future__ import annotations

import argparse
from pathlib import Path

from model.benchmark import nested_repository_benchmark
from model.bundle import build_bundle
from model.dataset import (
    DEFAULT_INPUT,
    DEFAULT_OUTPUT_DIR,
    load_prepared_dataset,
    write_prepared_dataset,
)
from model.evaluate import evaluate
from model.explain import explain
from model.features import FEATURE_PROFILES
from model.train import DEFAULT_ARTIFACTS_DIR, train
from model.validate import validate


def build_model(
    input_path: Path,
    artifacts_dir: Path,
    version: str,
    feature_profile: str,
    *,
    explain_model: bool = True,
    reviews_path: Path | None = None,
) -> Path:
    """Build data, train, evaluate, validate, explain and package one model."""
    dataset = load_prepared_dataset(input_path, reviews_path=reviews_path)
    write_prepared_dataset(dataset, input_path, DEFAULT_OUTPUT_DIR)
    artifact_dir = artifacts_dir / version
    train(dataset, artifact_dir, feature_profile=feature_profile)
    evaluate(input_path, artifact_dir, reviews_path)
    validate(input_path, artifact_dir, reviews_path)
    nested_repository_benchmark(
        input_path, artifact_dir, feature_profile=feature_profile, reviews_path=reviews_path
    )
    if explain_model:
        explain(input_path, artifact_dir, sample_size=100, reviews_path=reviews_path)
    bundle_path = artifact_dir / f"infra-model-{version}.zip"
    build_bundle(artifact_dir, bundle_path)
    return bundle_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--artifacts-dir", type=Path, default=DEFAULT_ARTIFACTS_DIR)
    parser.add_argument("--version", default="v1")
    parser.add_argument("--reviews", type=Path, help="Human-verified label review JSONL")
    parser.add_argument("--feature-profile", choices=sorted(FEATURE_PROFILES), default="full")
    parser.add_argument("--skip-explain", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    bundle_path = build_model(
        args.input,
        args.artifacts_dir,
        args.version,
        args.feature_profile,
        explain_model=not args.skip_explain,
        reviews_path=args.reviews,
    )
    print(f"finished model bundle: {bundle_path}")


if __name__ == "__main__":
    main()
