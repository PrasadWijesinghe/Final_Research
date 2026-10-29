"""`uv run --env-file .env build-dataset --version v1`: build a frozen Build dataset from raw data.

Reads Postgres (DATABASE_URL) and the stored logs (DATA_DIR, default <checkout>/data). Never calls
GitHub, so no token is needed. Output: data/datasets/build/<version>/ (rows.parquet,
fix_candidates.parquet, manifest.json).
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from collections.abc import Sequence
from datetime import UTC, date, datetime, time
from pathlib import Path

from features.replay import SqlHistory

from shared.db.session import get_engine
from shared.file_tags import FileTagger

from .builder import DatasetSettings, build_dataset
from .labels_sql import SqlLabelSink
from .source import SqlDatasetSource

# experts/build/src/build_expert/dataset/cli.py -> the checkout root is five folders up.
REPO_ROOT = Path(__file__).resolve().parents[5]
# DEFAULT: datasets go to <DATA_DIR>/datasets/build (DATA.md section 7), DATA_DIR = <checkout>/data.
DEFAULT_DATA_DIR = REPO_ROOT / "data"


def _day(text: str) -> datetime:
    try:
        return datetime.combine(date.fromisoformat(text), time(0, 0), tzinfo=UTC)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"not a date (YYYY-MM-DD): {text!r}") from exc


def build_parser() -> argparse.ArgumentParser:
    """Command-line options."""
    p = argparse.ArgumentParser(prog="build-dataset", description=__doc__)
    p.add_argument("--version", required=True, help="dataset version folder, e.g. v1")
    p.add_argument("--from", dest="start", type=_day, help="first day of runs (UTC, inclusive)")
    p.add_argument("--to", dest="end", type=_day, help="day after the last day (UTC, exclusive)")
    p.add_argument("--repo", action="append", help="only this owner/name (repeatable)")
    p.add_argument("--data-dir", type=Path, help="where logs live (default: DATA_DIR or ./data)")
    p.add_argument("--out", type=Path, help="dataset root (default: <data-dir>/datasets/build)")
    p.add_argument(
        "--no-write-labels",
        action="store_true",
        help="do not store FailureLabels in platform.failure_labels",
    )
    return p


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point for `build-dataset`."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    args = build_parser().parse_args(argv)
    data_dir = args.data_dir or Path(os.environ.get("DATA_DIR") or DEFAULT_DATA_DIR)
    out_root = args.out or data_dir / "datasets" / "build"
    try:
        engine = get_engine()
    except RuntimeError as exc:
        sys.stderr.write(f"build-dataset: {exc}\n")
        return 2
    settings = DatasetSettings(
        version=args.version, start=args.start, end=args.end, repos=args.repo or None
    )
    try:
        summary = build_dataset(
            SqlDatasetSource(engine, data_dir),
            SqlHistory(engine),
            out_root,
            settings,
            tagger=FileTagger.load(REPO_ROOT / "config" / "file_tags.yaml"),
            label_sink=None if args.no_write_labels else SqlLabelSink(engine),
            repo_root=REPO_ROOT,
        )
    except FileExistsError as exc:
        sys.stderr.write(f"build-dataset: {exc}\n")
        return 1
    finally:
        engine.dispose()
    skipped = ", ".join(f"{k}={v}" for k, v in sorted(summary.skipped.items())) or "none"
    sys.stdout.write(
        f"build/{args.version}: {summary.rows} commits ({summary.positives} with a build failure), "
        f"{summary.fix_candidates} fix candidates, {summary.labels_written} job labels stored; "
        f"skipped: {skipped}\n{summary.out_dir}\n"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
