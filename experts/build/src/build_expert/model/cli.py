"""`uv run build-train --dataset v1 --version m1`: baselines, model, report (section 6).

Reads a frozen dataset (data/datasets/build/<dataset>/rows.parquet) and the settings in
config/build_model.yaml. Needs no database and no GitHub. Writes models/build/<version>/ with the
model files, metrics.json and report.md.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from .data import load_dataset
from .evaluate import run_training
from .export import export_bundle
from .settings import load_settings

# experts/build/src/build_expert/model/cli.py -> the checkout root is five folders up.
REPO_ROOT = Path(__file__).resolve().parents[5]
DEFAULT_DATA_DIR = REPO_ROOT / "data"
# DEFAULT: model bundles go to <checkout>/models/build (gitignored, ARCHITECTURE.md).
DEFAULT_MODELS = REPO_ROOT / "models" / "build"


def build_parser() -> argparse.ArgumentParser:
    """Command-line options."""
    p = argparse.ArgumentParser(prog="build-train", description=__doc__)
    p.add_argument("--dataset", required=True, help="dataset version (v1) or a folder path")
    p.add_argument("--version", required=True, help="model version folder, e.g. m1")
    p.add_argument("--data-dir", type=Path, help="data folder (default: DATA_DIR or ./data)")
    p.add_argument("--out", type=Path, help=f"model root (default: {DEFAULT_MODELS})")
    p.add_argument("--config", type=Path, help="settings (default: config/build_model.yaml)")
    p.add_argument("--no-loro", action="store_true", help="skip leave-one-repo-out")
    p.add_argument("--no-ablation", action="store_true", help="skip the feature-group ablation")
    p.add_argument("--no-sub-targets", action="store_true", help="skip the sub-risk models")
    return p


def _dataset_dir(arg: str, data_dir: Path) -> Path:
    as_path = Path(arg)
    if as_path.is_dir():
        return as_path
    return data_dir / "datasets" / "build" / arg


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point for `build-train`."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    args = build_parser().parse_args(argv)
    data_dir = args.data_dir or Path(os.environ.get("DATA_DIR") or DEFAULT_DATA_DIR)
    try:
        settings = load_settings(args.config or REPO_ROOT / "config" / "build_model.yaml")
        data = load_dataset(
            _dataset_dir(args.dataset, data_dir), drop_synthetic=settings.drop_synthetic
        )
    except (OSError, ValueError) as exc:
        sys.stderr.write(f"build-train: {exc}\n")
        return 2
    if len(data) == 0:
        sys.stderr.write("build-train: the dataset has no rows\n")
        return 2
    result = run_training(
        data,
        settings,
        loro=not args.no_loro,
        ablation=not args.no_ablation,
        sub_targets=not args.no_sub_targets,
    )
    try:
        out_dir = export_bundle(
            args.out or DEFAULT_MODELS, args.version, result, data, settings, repo_root=REPO_ROOT
        )
    except FileExistsError as exc:
        sys.stderr.write(f"build-train: {exc}\n")
        return 1
    main_target = settings.targets[0]
    test = result.results["targets"][main_target].get("model", {}).get("test", {}).get("at_f1")
    headline = (
        f"test F1 {test['f1']:.3f}, PR-AUC {test['pr_auc']}" if test else "no model (see notes)"
    )
    sys.stdout.write(
        f"build/{args.version}: {len(data)} commits, {main_target} {headline}\n"
        f"{out_dir / 'report.md'}\n"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
