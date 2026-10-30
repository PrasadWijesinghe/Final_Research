"""`uv run --env-file .env build-kb --dataset v1`: load fix candidates into `build.kb_cases`.

Reads data/datasets/build/<dataset>/fix_candidates.parquet. Repos marked `commercial_kb: false`
in config/repos/build.yaml are left out. Uses DATABASE_URL and EMBEDDING_PROVIDER (granite, or
`fake` for the offline embedder).
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from collections.abc import Sequence
from pathlib import Path

import pyarrow.parquet as pq
import yaml
from sqlalchemy.exc import SQLAlchemyError

from shared.db.session import get_engine
from shared.embeddings import get_embedder

from .kb import KnowledgeBase, cases_from_fix_candidates

REPO_ROOT = Path(__file__).resolve().parents[5]


def excluded_repos(config_path: Path) -> frozenset[str]:
    """Repos whose licence keeps them out of a knowledge base (`commercial_kb: false`)."""
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    return frozenset(
        r["full_name"] for r in raw.get("repos", []) if r.get("commercial_kb") is False
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point for `build-kb`."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    p = argparse.ArgumentParser(prog="build-kb", description=__doc__)
    p.add_argument("--dataset", required=True, help="dataset version (v1) or a folder path")
    p.add_argument("--data-dir", type=Path, help="data folder (default: DATA_DIR or ./data)")
    p.add_argument(
        "--all", action="store_true", help="also keep fixes that touched none of the error files"
    )
    args = p.parse_args(argv)
    data_dir = args.data_dir or Path(os.environ.get("DATA_DIR") or REPO_ROOT / "data")
    folder = Path(args.dataset)
    if not folder.is_dir():
        folder = data_dir / "datasets" / "build" / args.dataset
    path = folder / "fix_candidates.parquet"
    if not path.exists():
        sys.stderr.write(f"build-kb: {path} not found\n")
        return 2
    cases = cases_from_fix_candidates(
        pq.read_table(path).to_pylist(),
        excluded_repos=excluded_repos(REPO_ROOT / "config" / "repos" / "build.yaml"),
        strong_only=not args.all,
    )
    try:
        kb = KnowledgeBase(get_engine(), get_embedder())
        n = kb.load(cases)
        total = kb.count()
    except (RuntimeError, SQLAlchemyError) as exc:
        sys.stderr.write(f"build-kb: {exc}\n")
        return 1
    sys.stdout.write(
        f"build.kb_cases: {n} cases loaded from {path.parent.name}, {total} in total\n"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
